"""High-quality Mac-side mosaic stitch (Ashlar-inspired).

Pipeline:
  1. Load tiles at refine resolution
  2. Neighbor pairwise phase-correlation on overlap strips + NCC
  3. Kruskal MST → global positions; fill gaps with stage model
  4. Distance-feather linear blend (no photometric rewrite)

OpenCV is required. Falls back by raising ImportError so caller can use
the lighter Pi-ported stitcher.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

LOG = logging.getLogger("openmhs.mosaic_stitch_hq")

Key = Tuple[int, int]

# Search residual around stage expect (stage calib can be ~20% off)
MAX_SHIFT_FRAC = 0.35
MAX_SHIFT_PX = 220
# Soft edge crop before registration (too aggressive → black gaps)
EDGE_CROP = 0.005
# Multiband levels (1 = distance-feather only — best for brightfield H&E)
PYRAMID_LEVELS = 1
REFINE_DEFAULT_W = 960
PREVIEW_MAX = 4800
MIN_ACCEPT_NCC = 0.18
MIN_ACCEPT_NCC_WEAK = 0.12  # keep weak edges only if no better orientation exists


def _cv2():
    import cv2

    return cv2


def _load_tile(path: str, width: int) -> Image.Image:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    if w <= 0:
        raise ValueError("empty tile")
    nh = max(1, int(round(h * (float(width) / float(w)))))
    if im.size != (width, nh):
        im = im.resize((int(width), nh), Image.BILINEAR)
    if EDGE_CROP > 0:
        tw, th = im.size
        cx = int(tw * EDGE_CROP)
        cy = int(th * EDGE_CROP)
        if cx * 2 < tw and cy * 2 < th:
            im = im.crop((cx, cy, tw - cx, th - cy))
    return im


def _to_gray(arr: np.ndarray) -> np.ndarray:
    if arr.ndim == 2:
        return arr.astype(np.float32)
    return (
        0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    ).astype(np.float32)


def _place_from_stride(stride_xy, tile_w, px_per_step, ref_w=640):
    pps = px_per_step or (0.14, 0.14)
    scale = float(tile_w) / float(ref_w)
    px = max(1, int(round(abs(int(stride_xy[0])) * float(pps[0]) * scale)))
    py = max(1, int(round(abs(int(stride_xy[1])) * float(pps[1]) * scale)))
    return px, py


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float32).ravel()
    b = b.astype(np.float32).ravel()
    a = a - float(a.mean())
    b = b - float(b.mean())
    denom = float(np.sqrt((a * a).sum() * (b * b).sum())) + 1e-6
    return float((a * b).sum()) / denom


def _overlap_patches(
    ref: np.ndarray, mov: np.ndarray, edx: int, edy: int
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Gray (or RGB) overlap when placing mov at (edx,edy) on same-size ref canvas."""
    h, w = ref.shape[:2]
    x0 = max(0, int(edx))
    y0 = max(0, int(edy))
    x1 = min(w, int(edx) + w)
    y1 = min(h, int(edy) + h)
    if x1 - x0 < 32 or y1 - y0 < 32:
        return None
    mx0 = x0 - int(edx)
    my0 = y0 - int(edy)
    ref_c = ref[y0:y1, x0:x1]
    mov_c = mov[my0 : my0 + (y1 - y0), mx0 : mx0 + (x1 - x0)]
    if ref_c.shape != mov_c.shape:
        return None
    return ref_c, mov_c


def align_pair(
    ref: np.ndarray,
    mov: np.ndarray,
    expect_dx: float,
    expect_dy: float,
) -> Optional[Tuple[float, float, float]]:
    """Robust pairwise paste: strip match → coarse NCC → ECC polish.

    Stage expect is only a soft prior. OpenFlexure stride calib is often 10–25%
    off, which breaks naive phase-correlation-on-expected-overlap.
    Returns (pdx, pdy, ncc).
    """
    cv2 = _cv2()
    edx = int(round(expect_dx))
    edy = int(round(expect_dy))
    h, w = ref.shape[:2]
    tune = min(MAX_SHIFT_PX, max(48, int(MAX_SHIFT_FRAC * min(w, h))))

    Rg = _to_gray(ref)
    Mg = _to_gray(mov)
    candidates: List[Tuple[float, float]] = [(float(edx), float(edy))]

    horizontal = abs(edx) >= abs(edy)
    try:
        if horizontal:
            ow = max(64, min(w // 2, max(abs(edx), w // 3)))
            ow = min(ow, w - 8)
            if edx <= 0:
                for templ, hay, sign in (
                    (Rg[:, :ow], Mg, -1),
                    (Mg[:, Mg.shape[1] - ow :], Rg, 1),
                ):
                    res = cv2.matchTemplate(
                        hay.astype(np.float32),
                        templ.astype(np.float32),
                        cv2.TM_CCOEFF_NORMED,
                    )
                    _mn, _mx, _mnloc, mxloc = cv2.minMaxLoc(res)
                    if sign < 0:
                        candidates.append((float(-mxloc[0]), float(-mxloc[1])))
                    else:
                        candidates.append(
                            (float(mxloc[0] - (w - ow)), float(mxloc[1]))
                        )
            else:
                templ = Rg[:, w - ow :]
                res = cv2.matchTemplate(
                    Mg.astype(np.float32), templ.astype(np.float32), cv2.TM_CCOEFF_NORMED
                )
                _mn, _mx, _mnloc, mxloc = cv2.minMaxLoc(res)
                candidates.append((float(w - ow - mxloc[0]), float(-mxloc[1])))
        else:
            # Vertical: try several strip heights — Y overlap is often weaker
            for frac in (0.25, 0.35, 0.45):
                oh = max(48, min(int(h * frac), h - 8))
                if edy <= 0:
                    templ = Rg[:oh, :]
                    res = cv2.matchTemplate(
                        Mg.astype(np.float32),
                        templ.astype(np.float32),
                        cv2.TM_CCOEFF_NORMED,
                    )
                    _mn, mx, _mnloc, mxloc = cv2.minMaxLoc(res)
                    candidates.append((float(-mxloc[0]), float(-mxloc[1])))
                    templ2 = Mg[:oh, :]
                    res2 = cv2.matchTemplate(
                        Rg.astype(np.float32),
                        templ2.astype(np.float32),
                        cv2.TM_CCOEFF_NORMED,
                    )
                    _mn, mx, _mnloc, mxloc = cv2.minMaxLoc(res2)
                    candidates.append((float(mxloc[0]), float(mxloc[1])))
                else:
                    templ = Rg[h - oh :, :]
                    res = cv2.matchTemplate(
                        Mg.astype(np.float32),
                        templ.astype(np.float32),
                        cv2.TM_CCOEFF_NORMED,
                    )
                    _mn, mx, _mnloc, mxloc = cv2.minMaxLoc(res)
                    candidates.append((float(-mxloc[0]), float(h - oh - mxloc[1])))
    except Exception:
        LOG.debug("strip match failed", exc_info=True)

    # Coarse NCC grid around expect (catches large stride error)
    step = 6
    y_span = tune if not horizontal else max(48, tune // 2)
    x_span = tune if horizontal else max(48, tune // 2)
    for ox in range(edx - x_span, edx + x_span + 1, step):
        for oy in range(edy - y_span, edy + y_span + 1, step):
            candidates.append((float(ox), float(oy)))

    best = (-1e9, float(edx), float(edy))
    for pdx, pdy in candidates:
        if abs(pdx - edx) > tune * 1.6 or abs(pdy - edy) > tune * 1.6:
            continue
        c2 = _overlap_patches(Rg, Mg, int(round(pdx)), int(round(pdy)))
        if c2 is None:
            continue
        if float(c2[0].std()) < 5.0 or float(c2[1].std()) < 5.0:
            continue
        sc = _ncc(c2[0], c2[1])
        if sc > best[0]:
            best = (sc, pdx, pdy)

    if best[0] < -1:
        return float(edx), float(edy), 0.0

    bx, by = int(round(best[1])), int(round(best[2]))
    for ox in range(bx - 6, bx + 7):
        for oy in range(by - 6, by + 7):
            if abs(ox - edx) > tune * 1.6 or abs(oy - edy) > tune * 1.6:
                continue
            c2 = _overlap_patches(Rg, Mg, ox, oy)
            if c2 is None:
                continue
            sc = _ncc(c2[0], c2[1])
            if sc > best[0]:
                best = (sc, float(ox), float(oy))

    # ECC subpixel polish on overlap
    try:
        c2 = _overlap_patches(Rg, Mg, int(round(best[1])), int(round(best[2])))
        if c2 is not None and min(c2[0].shape) >= 48:
            warp = np.eye(2, 3, dtype=np.float32)
            criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)
            ok, warp = cv2.findTransformECC(
                c2[0].astype(np.float32),
                c2[1].astype(np.float32),
                warp,
                cv2.MOTION_TRANSLATION,
                criteria,
                None,
                1,
            )
            if ok:
                pdx = best[1] + float(warp[0, 2])
                pdy = best[2] + float(warp[1, 2])
                c3 = _overlap_patches(Rg, Mg, int(round(pdx)), int(round(pdy)))
                if c3 is not None:
                    sc = _ncc(c3[0], c3[1])
                    if sc >= best[0] - 0.02:
                        best = (sc, pdx, pdy)
    except Exception:
        pass

    return best[1], best[2], float(best[0])


def permutation_ncc_threshold(
    tiles: Dict[Key, np.ndarray], rng: np.random.Generator, samples: int = 64
) -> float:
    """Ashlar-style: 99th percentile NCC of non-adjacent random pairs."""
    keys = list(tiles.keys())
    if len(keys) < 3:
        return MIN_ACCEPT_NCC
    vals: List[float] = []
    for _ in range(samples):
        a, b = rng.choice(len(keys), size=2, replace=False)
        ka, kb = keys[a], keys[b]
        if abs(ka[0] - kb[0]) + abs(ka[1] - kb[1]) <= 1:
            continue
        ga = _to_gray(tiles[ka])
        gb = _to_gray(tiles[kb])
        h = min(ga.shape[0], gb.shape[0]) // 3
        w = min(ga.shape[1], gb.shape[1]) // 3
        if h < 32 or w < 32:
            continue
        ya = int(rng.integers(0, ga.shape[0] - h))
        xa = int(rng.integers(0, ga.shape[1] - w))
        yb = int(rng.integers(0, gb.shape[0] - h))
        xb = int(rng.integers(0, gb.shape[1] - w))
        vals.append(_ncc(ga[ya : ya + h, xa : xa + w], gb[yb : yb + h, xb : xb + w]))
    if len(vals) < 8:
        return MIN_ACCEPT_NCC
    # Cap so we don't reject all real overlaps on textured histology
    return float(min(0.35, max(MIN_ACCEPT_NCC, np.percentile(vals, 99))))


def _mst_positions(
    keys: List[Key],
    edges: List[Tuple[float, Key, Key, float, float, float]],
    grid_xy: Callable[[Key], Tuple[float, float]],
) -> Dict[Key, Tuple[float, float]]:
    parent = {k: k for k in keys}
    rank = {k: 0 for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1
        return True

    tree = []
    for cost, a, b, pdx, pdy, sc in sorted(edges, key=lambda t: (t[0], -t[5])):
        if union(a, b):
            tree.append((a, b, pdx, pdy))

    adj: Dict[Key, List[Tuple[Key, float, float]]] = {}
    for a, b, pdx, pdy in tree:
        adj.setdefault(a, []).append((b, pdx, pdy))
        adj.setdefault(b, []).append((a, -pdx, -pdy))

    seed = min(keys, key=lambda t: (t[1], t[0]))
    pos = {seed: (0.0, 0.0)}
    queue = [seed]
    seen = {seed}
    while queue:
        cur = queue.pop(0)
        for nb, pdx, pdy in adj.get(cur, []):
            if nb in seen:
                continue
            pos[nb] = (pos[cur][0] + pdx, pos[cur][1] + pdy)
            seen.add(nb)
            queue.append(nb)

    # Disconnected pieces: place with stage model relative to nearest placed
    for ij in keys:
        if ij in pos:
            continue
        # Find any placed neighbor preference via grid distance
        best = None
        for other, (ox, oy) in pos.items():
            d = abs(ij[0] - other[0]) + abs(ij[1] - other[1])
            if best is None or d < best[0]:
                gx0, gy0 = grid_xy(other)
                gx1, gy1 = grid_xy(ij)
                best = (d, ox + (gx1 - gx0), oy + (gy1 - gy0))
        pos[ij] = (best[1], best[2]) if best else grid_xy(ij)
    return pos


def _distance_weight(h: int, w: int) -> np.ndarray:
    """Distance to nearest edge — wider effective feather than linear ramp."""
    ys = np.minimum(np.arange(h), np.arange(h - 1, -1, -1)).astype(np.float32)
    xs = np.minimum(np.arange(w), np.arange(w - 1, -1, -1)).astype(np.float32)
    yy, xx = np.meshgrid(ys, xs, indexing="ij")
    d = np.minimum(yy, xx)
    # Soften: raise to power so center dominates more in overlaps
    return np.maximum(d ** 1.35, 1e-3)


def _pyr_down(img: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    return cv2.pyrDown(img)


def _pyr_up(img: np.ndarray, size: Tuple[int, int]) -> np.ndarray:
    cv2 = _cv2()
    out = cv2.pyrUp(img)
    if out.shape[0] != size[0] or out.shape[1] != size[1]:
        out = cv2.resize(out, (size[1], size[0]), interpolation=cv2.INTER_LINEAR)
    return out


def _gaussian_pyramid(img: np.ndarray, levels: int) -> List[np.ndarray]:
    pyr = [img.astype(np.float32)]
    for _ in range(levels - 1):
        pyr.append(_pyr_down(pyr[-1]))
    return pyr


def _laplacian_pyramid(img: np.ndarray, levels: int) -> List[np.ndarray]:
    gp = _gaussian_pyramid(img, levels)
    lp = []
    for i in range(levels - 1):
        up = _pyr_up(gp[i + 1], (gp[i].shape[0], gp[i].shape[1]))
        lp.append(gp[i] - up)
    lp.append(gp[-1])
    return lp


def _collapse_laplacian(lp: List[np.ndarray]) -> np.ndarray:
    img = lp[-1]
    for i in range(len(lp) - 2, -1, -1):
        img = _pyr_up(img, (lp[i].shape[0], lp[i].shape[1])) + lp[i]
    return img


def _least_squares_positions(
    keys: List[Key],
    edges: List[Tuple[float, Key, Key, float, float, float]],
    seed_pos: Dict[Key, Tuple[float, float]],
    min_ncc: float = 0.2,
) -> Dict[Key, Tuple[float, float]]:
    """Refine MST positions with weighted least-squares over strong edges."""
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)
    if n < 2:
        return dict(seed_pos)
    strong = [e for e in edges if e[5] >= min_ncc]
    if len(strong) < 1:
        return dict(seed_pos)
    # Separate X and Y systems: for each edge a->b: pos[b]-pos[a] = (pdx,pdy)
    Ax = []
    bx = []
    wx = []
    Ay = []
    by_ = []
    wy = []
    for _cost, a, b, pdx, pdy, sc in strong:
        w = float(max(sc, 0.05))
        row = np.zeros(n, dtype=np.float64)
        row[idx[b]] = 1.0
        row[idx[a]] = -1.0
        Ax.append(row)
        bx.append(pdx)
        wx.append(w)
        Ay.append(row.copy())
        by_.append(pdy)
        wy.append(w)
    # Pin seed
    seed = min(keys, key=lambda t: (t[1], t[0]))
    pin = np.zeros(n, dtype=np.float64)
    pin[idx[seed]] = 1.0
    Ax.append(pin)
    bx.append(seed_pos[seed][0])
    wx.append(1e3)
    Ay.append(pin.copy())
    by_.append(seed_pos[seed][1])
    wy.append(1e3)

    Ax = np.asarray(Ax)
    Ay = np.asarray(Ay)
    WX = np.sqrt(np.asarray(wx))
    WY = np.sqrt(np.asarray(wy))
    try:
        x, *_ = np.linalg.lstsq(Ax * WX[:, None], np.asarray(bx) * WX, rcond=None)
        y, *_ = np.linalg.lstsq(Ay * WY[:, None], np.asarray(by_) * WY, rcond=None)
    except Exception:
        return dict(seed_pos)
    return {k: (float(x[idx[k]]), float(y[idx[k]])) for k in keys}


def composite_multiband(
    tiles: Dict[Key, np.ndarray],
    positions: Dict[Key, Tuple[float, float]],
    tw: int,
    th: int,
    levels: int = PYRAMID_LEVELS,
) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    min_x, min_y = min(xs), min(ys)
    positions = {k: (v[0] - min_x, v[1] - min_y) for k, v in positions.items()}
    cw = int(math.ceil(max(p[0] for p in positions.values()) + tw))
    ch = int(math.ceil(max(p[1] for p in positions.values()) + th))
    cw += cw % 2
    ch += ch % 2

    # Background ≈ brightfield slide (avoid green/cyan percentile fill)
    bg = np.array([246.0, 242.0, 238.0], dtype=np.float32)

    base_w = _distance_weight(th, tw)
    acc = np.zeros((ch, cw, 3), dtype=np.float32)
    wsum = np.zeros((ch, cw), dtype=np.float32)

    for ij in sorted(positions.keys(), key=lambda t: (t[1], t[0])):
        tile = tiles[ij].astype(np.float32)
        tile = np.clip(tile, 0, 255)
        x, y = positions[ij]
        xi, yi = int(round(x)), int(round(y))
        x1, y1 = min(cw, xi + tw), min(ch, yi + th)
        x0, y0 = max(0, xi), max(0, yi)
        if x1 <= x0 or y1 <= y0:
            continue
        tx0, ty0 = x0 - xi, y0 - yi
        patch = tile[ty0 : ty0 + (y1 - y0), tx0 : tx0 + (x1 - x0)]
        ww = base_w[ty0 : ty0 + (y1 - y0), tx0 : tx0 + (x1 - x0)]
        acc[y0:y1, x0:x1] += patch * ww[:, :, None]
        wsum[y0:y1, x0:x1] += ww

    covered = wsum > 1e-4
    out = np.zeros((ch, cw, 3), dtype=np.float32)
    out[:] = bg
    out[covered] = acc[covered] / wsum[covered][:, None]
    crop = (0, 0, cw, ch)
    rows = np.any(covered, axis=1)
    cols = np.any(covered, axis=0)
    if rows.any() and cols.any():
        r0, r1 = int(np.argmax(rows)), int(len(rows) - np.argmax(rows[::-1]))
        c0, c1 = int(np.argmax(cols)), int(len(cols) - np.argmax(cols[::-1]))
        if r1 > r0 + 16 and c1 > c0 + 16:
            out = out[r0:r1, c0:c1]
            crop = (c0, r0, c1, r1)
    return np.clip(out, 0, 255), crop


def stitch_cells_hq(
    cells: Dict[Key, str],
    nx: int,
    ny: int,
    tile_w: int = REFINE_DEFAULT_W,
    overlap: float = 0.30,
    stride_size=None,
    px_per_step=None,
    progress_cb: Optional[Callable[[int], None]] = None,
    prior_positions=None,
    prior_tile_w=None,
    cache_out: Optional[Dict[str, Any]] = None,
    axis_sign=None,
    **_kwargs,
) -> Image.Image:
    """Ashlar-inspired HQ stitch for Mac refine."""
    del nx, ny, prior_positions, prior_tile_w  # HQ recomputes from stage+PCM
    _cv2()  # ensure available

    def report(p):
        if progress_cb:
            try:
                progress_cb(int(p))
            except Exception:
                pass

    report(2)
    tiles_img: Dict[Key, Image.Image] = {}
    tw = th = None
    for ij, path in cells.items():
        try:
            im = _load_tile(path, int(tile_w))
            if tw is None:
                tw, th = im.size
            elif im.size != (tw, th):
                im = im.resize((tw, th), Image.BILINEAR)
            tiles_img[ij] = im
        except Exception:
            LOG.exception("HQ skip tile %s", path)
    if not tiles_img or tw is None or th is None:
        return Image.new("RGB", (tile_w, max(1, int(tile_w * 0.75))), (0, 0, 0))

    tiles = {k: np.asarray(v, dtype=np.float32) for k, v in tiles_img.items()}
    report(8)

    report(15)

    ax, ay = (-1, -1)
    if axis_sign and len(axis_sign) >= 2:
        ax, ay = (1 if axis_sign[0] >= 0 else -1, 1 if axis_sign[1] >= 0 else -1)

    if stride_size and len(stride_size) >= 2:
        gx, gy = _place_from_stride(stride_size, tw, px_per_step)
    else:
        gx = max(1, int(tw * (1.0 - overlap)))
        gy = max(1, int(th * (1.0 - overlap)))

    keys = sorted(tiles.keys(), key=lambda t: (t[1], t[0]))
    origin = (min(k[0] for k in keys), min(k[1] for k in keys))

    def grid_xy(ij: Key) -> Tuple[float, float]:
        return (
            float((ij[0] - origin[0]) * ax * gx),
            float((ij[1] - origin[1]) * ay * gy),
        )

    def expect_paste(a: Key, b: Key) -> Tuple[float, float]:
        return (
            float((b[0] - a[0]) * ax * gx),
            float((b[1] - a[1]) * ay * gy),
        )

    rng = np.random.default_rng(0)
    ncc_thr = permutation_ncc_threshold(tiles, rng)
    LOG.info("HQ stitch NCC threshold=%.3f (%s tiles)", ncc_thr, len(tiles))
    report(22)

    keys = sorted(tiles.keys(), key=lambda t: (t[1], t[0]))
    edges: List[Tuple[float, Key, Key, float, float, float]] = []
    neigh = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    done = 0
    total_pairs = sum(
        1
        for a in keys
        for dx, dy in neigh
        if (a[0] + dx, a[1] + dy) in tiles and (a[0] + dx, a[1] + dy) > a
    )
    for a in keys:
        for dx, dy in neigh:
            b = (a[0] + dx, a[1] + dy)
            if b not in tiles or b <= a:
                continue
            edx, edy = expect_paste(a, b)
            res = align_pair(tiles[a], tiles[b], edx, edy)
            done += 1
            report(22 + int(50 * done / max(1, total_pairs)))
            if res is None:
                continue
            pdx, pdy, sc = res
            # Strong edges always kept; weak only if above absolute floor
            if sc < MIN_ACCEPT_NCC_WEAK:
                LOG.debug("reject edge %s-%s ncc=%.3f", a, b, sc)
                continue
            if sc < MIN_ACCEPT_NCC and sc < ncc_thr:
                LOG.debug("weak-reject edge %s-%s ncc=%.3f thr=%.3f", a, b, sc, ncc_thr)
                continue
            cost = -math.log(max(sc, 1e-6))
            edges.append((cost, a, b, pdx, pdy, sc))

    report(75)
    # Refine stage stride from accepted image-space edges (fixes black gaps)
    hx = [abs(pdx) for _c, a, b, pdx, pdy, sc in edges if a[1] == b[1] and sc >= MIN_ACCEPT_NCC]
    hy = [abs(pdy) for _c, a, b, pdx, pdy, sc in edges if a[0] == b[0] and sc >= MIN_ACCEPT_NCC_WEAK]
    if hx:
        gx = int(round(float(np.median(hx))))
    if hy:
        gy = int(round(float(np.median(hy))))
    LOG.info(
        "HQ edges=%s med_stride=(%s,%s) axis=%s",
        len(edges),
        gx,
        gy,
        (ax, ay),
    )
    positions = _mst_positions(keys, edges, grid_xy)
    positions = _least_squares_positions(keys, edges, positions, min_ncc=MIN_ACCEPT_NCC_WEAK)
    report(82)

    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    min_x, min_y = min(xs), min(ys)
    pos_n = {k: (v[0] - min_x, v[1] - min_y) for k, v in positions.items()}
    cw0 = int(max(p[0] for p in pos_n.values()) + tw)
    ch0 = int(max(p[1] for p in pos_n.values()) + th)
    if cw0 > PREVIEW_MAX or ch0 > PREVIEW_MAX:
        scale = min(PREVIEW_MAX / float(cw0), PREVIEW_MAX / float(ch0))
        new_w = max(64, int(tw * scale))
        return stitch_cells_hq(
            cells,
            1,
            1,
            tile_w=new_w,
            overlap=overlap,
            stride_size=stride_size,
            px_per_step=px_per_step,
            progress_cb=progress_cb,
            cache_out=cache_out,
            axis_sign=axis_sign,
        )

    mosaic, crop = composite_multiband(tiles, positions, tw, th, levels=PYRAMID_LEVELS)
    report(95)
    polished = Image.fromarray(np.clip(mosaic, 0, 255).astype(np.uint8))
    c0, r0, c1, r1 = crop
    pos_n = {k: (v[0] - c0, v[1] - r0) for k, v in pos_n.items()}
    cw, ch = max(1, c1 - c0), max(1, r1 - r0)

    if cache_out is not None:
        cache_out["positions"] = dict(pos_n)
        cache_out["tile_w"] = tw
        cache_out["tile_h"] = th
        cache_out["canvas"] = (cw, ch)
        cache_out["engine"] = "hq_ashlar"
        cache_out["ncc_threshold"] = ncc_thr
        cache_out["edges"] = len(edges)

    report(100)
    return polished
