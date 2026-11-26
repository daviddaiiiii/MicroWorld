"""MicroWorld mosaic stitch/refine (ported from Pi scan-mosaic, no LabThings)."""
from __future__ import annotations

import logging
import math
import os
import re
from typing import Dict, Optional, Tuple

import numpy as np
from PIL import Image

LOG = logging.getLogger("openmhs.mosaic_stitch")

EXTENSION_ID = "org.microworld.scan-mosaic"
MICROGRAPHS = "/var/openflexure/data/micrographs"
PREVIEW_DIR = "/var/openflexure/data/scan-mosaic"
PREVIEW_NAME = "preview.jpg"
LIVE_TILE_WIDTH = 512
FINAL_TILE_WIDTH = 640
REFINE_TILE_WIDTH = 960
PREVIEW_MAX_EDGE = 2560  # live mosaic longest side budget (px)
DEFAULT_OVERLAP = 0.30  # target overlap when auto-choosing stride
# Crop this fraction from each tile edge before paste to kill seam artifacts
TILE_EDGE_CROP = 0.02
LIVE_MIN_INTERVAL_S = 2.5
MIN_CELLS_FOR_PREVIEW = 1
# Frontier exploration (Full scan) safety limits
EXPLORE_MAX_CELLS = 200
EXPLORE_MAX_RADIUS = 28
# Empty-field heuristic when background-detect extension is absent
EMPTY_LAP_VAR_MAX = 35.0
EMPTY_STD_MAX = 12.0
# NCC below this → fall back to pure spatial (stage/calib) placement
ALIGN_MIN_SCORE_LIGHT = 0.18
ALIGN_MIN_SCORE_FINE = 0.15
ALIGN_MIN_SCORE_FULL = 0.12
# Residual clamp around stride expect (Ashlar-like stage repeatability window)
ALIGN_MAX_DRIFT_FRAC = 0.12
ALIGN_MAX_DRIFT_PX = 48
ALIGN_TUNE_FRAC = 0.08  # of tile width
ALIGN_TUNE_PX_MIN = 16
ALIGN_TUNE_PX_MAX = 56
# Soft µm budget (converted via FOV≈stride/(1-overlap) heuristics when possible)
ALIGN_MAX_SHIFT_UM = 40
# Overlap strip must have enough texture or we skip NCC
ALIGN_MIN_OVERLAP_STD = 8.0
# Pi RAM safety: mosaic scans always use video-port sized frames
SAFE_RESIZE = (640, 480)
# Fallback if pre-scan calibration fails (updated from live probe @ 640x480)
CAL_REF_WH = (640, 480)
CAL_PX_PER_STEP = (0.1437, 0.1344)
# Pre-scan stride calibration
CALIB_PROBE_STEPS = 600
STRIDE_MIN = 400
STRIDE_MAX = 4500
AUTO_STRIDE_DEFAULT = True
NAME_PAT = re.compile(
    r"^(?P<prefix>.+)_(?P<x>-?\d+)_(?P<y>-?\d+)_(?P<z>-?\d+)\.(?P<ext>jpe?g)$",
    re.I,
)

def sharpness_path(path, sample_w=160):
    """Laplacian variance on a tiny grayscale decode — avoids full-res RAM spike."""
    try:
        im = Image.open(path)
        im = _load_resized(im, sample_w, mode="L")
        g = np.asarray(im, dtype=np.float32)
    except Exception:
        return 0.0
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = (
        g[1:-1, 1:-1] * (-4.0)
        + g[:-2, 1:-1]
        + g[2:, 1:-1]
        + g[1:-1, :-2]
        + g[1:-1, 2:]
    )
    return float(lap.var())


def _load_resized(im, width, mode="RGB"):
    """Decode JPEG at reduced size when possible."""
    w, h = im.size
    if w <= 0:
        return im.convert(mode) if mode else im
    nh = max(1, int(h * (float(width) / float(w))))
    target = (int(width), nh)
    try:
        # JPEG draft hint — much less RAM on Pi
        im.draft(mode if mode != "L" else "L", target)
    except Exception:
        pass
    im = im.convert(mode)
    if im.size != target:
        im = im.resize(target, Image.BILINEAR)
    return im

def _crop_edges(im, frac):
    if frac <= 0:
        return im
    w, h = im.size
    cx = int(w * frac)
    cy = int(h * frac)
    if cx * 2 >= w or cy * 2 >= h:
        return im
    return im.crop((cx, cy, w - cx, h - cy))

def preview_tile_width(n_cells, nx=None, ny=None):
    """Per-tile size for live preview — keep small grids sharp."""
    import math

    if nx and ny and int(nx) <= 1 and int(ny) <= 1:
        return int(FINAL_TILE_WIDTH)
    if nx and ny:
        side = max(int(nx), int(ny), 1)
    else:
        side = max(1, int(math.ceil(math.sqrt(float(max(1, int(n_cells)))))))
    if side <= 1:
        return int(FINAL_TILE_WIDTH)
    if side <= 2:
        cap = int(FINAL_TILE_WIDTH)
    elif side <= 4:
        cap = 480
    else:
        cap = int(LIVE_TILE_WIDTH)
    tw = int(float(PREVIEW_MAX_EDGE) / float(side))
    return int(max(160, min(cap, tw)))


def _polish_for_slide(im):
    """Identity — Refine keeps capture color (registration + feather only)."""
    return im

def _overlap_texture_ok(ref, mov, expect_dx, expect_dy):
    """True if expected overlap strips have enough texture for NCC."""
    R = _to_gray(ref)
    M = _to_gray(mov)
    h, w = R.shape
    edx = int(expect_dx) if expect_dx else 0
    edy = int(expect_dy) if expect_dy else 0
    # Approximate overlap rectangles on each tile for the expected paste
    if abs(edx) >= abs(edy):
        ow = max(24, w - abs(edx))
        if edx >= 0:
            r = R[:, w - ow :]
            m = M[:, :ow]
        else:
            r = R[:, :ow]
            m = M[:, w - ow :]
    else:
        oh = max(24, h - abs(edy))
        if edy >= 0:
            r = R[h - oh :, :]
            m = M[:oh, :]
        else:
            r = R[:oh, :]
            m = M[h - oh :, :]
    if r.size < 64 or m.size < 64:
        return False
    return float(r.std()) >= ALIGN_MIN_OVERLAP_STD and float(m.std()) >= ALIGN_MIN_OVERLAP_STD


def _align_min_score(level):
    lv = str(level).lower()
    if lv in ("light", "preview", "1"):
        return ALIGN_MIN_SCORE_LIGHT
    if lv in ("fine", "warm", "incremental"):
        return ALIGN_MIN_SCORE_FINE
    return ALIGN_MIN_SCORE_FULL


def _align_tune_limit(tile_w, level="full", expect_mag=None):
    """Max residual px around stride expect (Ashlar-style, not free search)."""
    tw = max(64, int(tile_w or 640))
    frac = 0.05 if str(level).lower() in ("light", "preview", "1") else ALIGN_TUNE_FRAC
    lim = int(round(tw * frac))
    if expect_mag is not None and abs(float(expect_mag)) > 8:
        lim = max(lim, int(round(abs(float(expect_mag)) * ALIGN_MAX_DRIFT_FRAC)))
    return int(max(ALIGN_TUNE_PX_MIN, min(ALIGN_TUNE_PX_MAX, lim)))


def _paste_too_far(pdx, pdy, expect_dx, expect_dy, tune_px=None):
    """True when paste drifts beyond the stage-repeatability window."""
    lim = float(
        tune_px
        if tune_px is not None
        else _align_tune_limit(
            max(abs(expect_dx), abs(expect_dy), 64),
            expect_mag=max(abs(expect_dx), abs(expect_dy)),
        )
    )
    return (
        abs(float(pdx) - float(expect_dx)) > lim
        or abs(float(pdy) - float(expect_dy)) > lim
    )


def _clamp_paste_fine_tune(pdx, pdy, expect_dx, expect_dy, tune_px=None):
    """Keep stride placement; allow only a bounded PCM/NCC residual."""
    lim = float(
        tune_px
        if tune_px is not None
        else _align_tune_limit(
            max(abs(expect_dx), abs(expect_dy), 64),
            expect_mag=max(abs(expect_dx), abs(expect_dy)),
        )
    )
    return (
        float(expect_dx) + max(-lim, min(lim, float(pdx) - float(expect_dx))),
        float(expect_dy) + max(-lim, min(lim, float(pdy) - float(expect_dy))),
    )


def resize_rgb(path, width):
    im = Image.open(path)
    im = _load_resized(im, width, mode="RGB")
    im = _crop_edges(im, TILE_EDGE_CROP)
    return im  # geometry-only: no flat-field / exposure rewrite


def _to_gray(im):
    return np.asarray(im.convert("L"), dtype=np.float32)


def _place_from_stride(stride_xy, resize_w, px_per_step=None):
    """Pixel stride from stage stride using live or fallback calibration."""
    pps = px_per_step or CAL_PX_PER_STEP
    scale = float(resize_w) / float(CAL_REF_WH[0])
    px = max(1, int(round(abs(int(stride_xy[0])) * float(pps[0]) * scale)))
    py = max(1, int(round(abs(int(stride_xy[1])) * float(pps[1]) * scale)))
    return px, py

def _get_cv2():
    try:
        import cv2

        return cv2
    except Exception:
        return None



def _ncc(a, b):
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    a = a - float(a.mean())
    b = b - float(b.mean())
    denom = float(np.sqrt((a * a).sum() * (b * b).sum())) + 1e-6
    return float((a * b).sum()) / denom


def _overlap_crops_gray(R, M, edx, edy):
    """Gray overlap patches when placing M at paste (edx, edy) on R."""
    h, w = R.shape
    x0 = max(0, int(edx))
    y0 = max(0, int(edy))
    x1 = min(w, int(edx) + w)
    y1 = min(h, int(edy) + h)
    if x1 - x0 < 24 or y1 - y0 < 24:
        return None
    mx0 = x0 - int(edx)
    my0 = y0 - int(edy)
    ref_c = R[y0:y1, x0:x1]
    mov_c = M[my0 : my0 + (y1 - y0), mx0 : mx0 + (x1 - x0)]
    if ref_c.shape != mov_c.shape or ref_c.size < 64:
        return None
    return ref_c, mov_c


def _score_paste(R, M, pdx, pdy):
    crops = _overlap_crops_gray(R, M, pdx, pdy)
    if crops is None:
        return -1.0
    return _ncc(crops[0], crops[1])


def _pair_paste_pcm_ncc(R, M, edx, edy, level="full"):
    """Ashlar/MIST-style: phase correlation on overlap + NCC peak pick."""
    cv2 = _get_cv2()
    if cv2 is None:
        return None
    h, w = R.shape
    tune = _align_tune_limit(w, level, expect_mag=max(abs(edx), abs(edy)))
    crops = _overlap_crops_gray(R, M, edx, edy)
    if crops is None:
        return None
    ref_c, mov_c = crops
    ch, cw = ref_c.shape
    if ch < 32 or cw < 32:
        return None
    Rf = ref_c.astype(np.float32)
    Mf = mov_c.astype(np.float32)
    k = max(5, (min(ch, cw) // 16) | 1)
    Rh = Rf - cv2.GaussianBlur(Rf, (k, k), 0)
    Mh = Mf - cv2.GaussianBlur(Mf, (k, k), 0)
    win = cv2.createHanningWindow((cw, ch), cv2.CV_32F)
    (sx, sy), resp = cv2.phaseCorrelate(Rh, Mh, win)
    # phaseCorrelate(ref, mov): mov shift relative to ref → correct paste by +(sx,sy)
    candidates = [
        (float(edx) + float(sx), float(edy) + float(sy)),
        (float(edx) + float(sx) - cw, float(edy) + float(sy)),
        (float(edx) + float(sx) + cw, float(edy) + float(sy)),
        (float(edx) + float(sx), float(edy) + float(sy) - ch),
        (float(edx) + float(sx), float(edy) + float(sy) + ch),
        (float(edx), float(edy)),
    ]
    # Light: also try small neighborhood around best residual for subpixel polish later
    best = (-1e9, float(edx), float(edy))
    for pdx, pdy in candidates:
        if abs(pdx - edx) > tune * 1.25 or abs(pdy - edy) > tune * 1.25:
            continue
        sc = _score_paste(R, M, pdx, pdy)
        if sc > best[0]:
            best = (sc, pdx, pdy)
    # Local NCC polish around winner (fine/full denser)
    if best[0] > -1e8:
        bx, by = int(round(best[1])), int(round(best[2]))
        rad = 2 if str(level).lower() in ("light", "preview", "1") else 4
        for ox in range(bx - rad, bx + rad + 1):
            for oy in range(by - rad, by + rad + 1):
                if abs(ox - edx) > tune or abs(oy - edy) > tune:
                    continue
                sc = _score_paste(R, M, ox, oy)
                if sc > best[0]:
                    best = (sc, float(ox), float(oy))
    if best[0] < -1:
        return None
    pdx, pdy = _clamp_paste_fine_tune(best[1], best[2], edx, edy, tune_px=tune)
    return pdx, pdy, float(best[0]), float(resp or 0.0)


def _pair_paste_ncc_local(R, M, edx, edy, level="full"):
    """Fallback local NCC around expected paste (no OpenCV)."""
    h, w = R.shape
    tune = _align_tune_limit(w, level, expect_mag=max(abs(edx), abs(edy)))
    light = str(level).lower() in ("light", "preview", "1")
    step = 4 if light else 2
    best = (_score_paste(R, M, edx, edy), float(edx), float(edy))
    for ox in range(int(edx - tune), int(edx + tune) + 1, step):
        for oy in range(int(edy - tune), int(edy + tune) + 1, step):
            sc = _score_paste(R, M, ox, oy)
            if sc > best[0]:
                best = (sc, float(ox), float(oy))
    if best[0] > -1e8 and not light:
        bx, by = int(best[1]), int(best[2])
        for ox in range(bx - 2, bx + 3):
            for oy in range(by - 2, by + 3):
                sc = _score_paste(R, M, ox, oy)
                if sc > best[0]:
                    best = (sc, float(ox), float(oy))
    pdx, pdy = _clamp_paste_fine_tune(best[1], best[2], edx, edy, tune_px=tune)
    return pdx, pdy, float(best[0])


def _pair_paste_offset(ref, mov, expect_dx, expect_dy, level="full"):
    """Find mosaic paste offset (pdx, pdy) putting mov relative to ref.

    Prefer PCM+NCC on the expected overlap (Ashlar/MIST style); residual is
    clamped to a stage-repeatability window around the fixed stride expect.
    Returns (pdx, pdy, score).
    """
    R = _to_gray(ref)
    M = _to_gray(mov)
    edx = int(round(expect_dx)) if expect_dx else 0
    edy = int(round(expect_dy)) if expect_dy else 0
    if abs(edx) < 4 and abs(edy) < 4:
        return float(edx), float(edy), 0.0

    pcm = _pair_paste_pcm_ncc(R, M, edx, edy, level=level)
    if pcm is not None:
        pdx, pdy, sc, _resp = pcm
        return pdx, pdy, sc
    pdx, pdy, sc = _pair_paste_ncc_local(R, M, edx, edy, level=level)
    if sc < 0.12:
        return float(edx), float(edy), float(sc)
    return pdx, pdy, sc


def _content_shift_cv(ref, mov, expect_dx, expect_dy, search_frac=0.5):
    """Legacy name: return OpenCV-style content shift + score via strip NCC paste.

    paste = -content_shift, so content_shift = -paste.
    """
    del search_frac
    pdx, pdy, sc = _pair_paste_offset(ref, mov, expect_dx, expect_dy)
    return -pdx, -pdy, sc


def _normalize_align(align):
    """Map align flag to 'none' | 'light' | 'fine' | 'full'."""
    s = str(align).lower() if not isinstance(align, bool) else ("full" if align else "none")
    if align is True or align == 2 or s in ("full", "true", "1"):
        return "full"
    if s in ("fine", "warm", "incremental"):
        return "fine"
    if align is False or align == 0 or s in ("none", "false", "0", "off"):
        return "none"
    return "light"


def _mst_positions(keys, edge_list, origin, grid_xy):
    """Kruskal MST over pairwise pastes, then walk tree from origin.

    edge_list items: (cost, a, b, pdx, pdy, score) with paste of b relative to a.
    Lower cost is preferred (use -score for good matches).
    """
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
    for item in sorted(edge_list, key=lambda t: (t[0], -t[5])):
        _cost, a, b, pdx, pdy, sc = item
        if a not in parent or b not in parent:
            continue
        if union(a, b):
            tree.append((a, b, float(pdx), float(pdy), float(sc)))

    adj = {}
    for a, b, pdx, pdy, sc in tree:
        adj.setdefault(a, []).append((b, pdx, pdy))
        adj.setdefault(b, []).append((a, -pdx, -pdy))

    seed = origin if origin in keys else keys[0]
    pos = {seed: grid_xy(seed)}
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
    for ij in keys:
        if ij not in pos:
            pos[ij] = grid_xy(ij)
    return pos, len(tree)


def _estimate_positions(
    tiles,
    guess_x,
    guess_y,
    refine=True,
    progress_cb=None,
    prior_positions=None,
    prior_tile_w=None,
    tile_w=None,
    axis_sign=None,
):
    """Pairwise PCM+NCC edges + MST placement (Ashlar/MIST-style).

    refine: none|light|fine|full. fine/full can warm-start expected deltas from
    prior_positions (scaled by tile_w/prior_tile_w).
    axis_sign: (sx, sy) mosaic direction for +grid index.
    """
    keys = sorted(tiles.keys(), key=lambda t: (t[1], t[0]))
    min_ix = min(k[0] for k in keys)
    min_iy = min(k[1] for k in keys)
    origin = (min_ix, min_iy)
    gx, gy = max(1, int(abs(guess_x))), max(1, int(abs(guess_y)))
    ax = 1
    ay = 1
    if axis_sign and len(axis_sign) >= 2:
        ax = 1 if int(axis_sign[0]) >= 0 else -1
        ay = 1 if int(axis_sign[1]) >= 0 else -1
    level = _normalize_align(refine)
    tw0 = int(tile_w or next(iter(tiles.values())).size[0])

    def grid_xy(ij):
        return (
            float((ij[0] - origin[0]) * gx * ax),
            float((ij[1] - origin[1]) * gy * ay),
        )

    def report(pct):
        if progress_cb:
            try:
                progress_cb(int(max(0, min(100, pct))))
            except Exception:
                pass

    if level == "none" or len(tiles) < 2:
        pos = {ij: grid_xy(ij) for ij in keys}
        logging.info(
            "mosaic: cal steps=(%s,%s) axis=%s no refine origin=%s",
            gx,
            gy,
            (ax, ay),
            origin,
        )
        report(100)
        return {k: (int(v[0]), int(v[1])) for k, v in pos.items()}, gx, gy

    scale = 1.0
    if (
        prior_positions
        and prior_tile_w
        and tile_w
        and int(prior_tile_w) > 0
        and level in ("fine", "full")
    ):
        scale = float(tile_w) / float(prior_tile_w)

    if level == "light":
        pair_level = "light"
    elif level == "fine" or (level == "full" and prior_positions):
        pair_level = "fine"
    else:
        pair_level = "full"

    # Undirected unique neighbor edges
    undirected = []
    seen_e = set()
    for cx, cy in keys:
        for nx_, ny_ in ((cx + 1, cy), (cx, cy + 1)):
            nb = (nx_, ny_)
            if nb not in tiles:
                continue
            ek = ((cx, cy), nb)
            if ek in seen_e:
                continue
            seen_e.add(ek)
            undirected.append(((cx, cy), nb))

    report(2)
    edge_list = []
    signed_x = []
    signed_y = []
    n_e = max(1, len(undirected))
    for ei, (a, b) in enumerate(undirected):
        cx, cy = a
        nx_, ny_ = b
        if prior_positions and a in prior_positions and b in prior_positions:
            expect_dx = (prior_positions[b][0] - prior_positions[a][0]) * scale
            expect_dy = (prior_positions[b][1] - prior_positions[a][1]) * scale
        else:
            expect_dx = (nx_ - cx) * gx * ax
            expect_dy = (ny_ - cy) * gy * ay
        tune = _align_tune_limit(
            tw0, pair_level, expect_mag=max(abs(expect_dx), abs(expect_dy))
        )
        min_sc = _align_min_score(pair_level)
        use_spatial = not _overlap_texture_ok(
            tiles[a], tiles[b], expect_dx, expect_dy
        )
        if use_spatial:
            pdx, pdy, sc = float(expect_dx), float(expect_dy), 0.0
            reason = "low-texture"
            cost = 1.0
        else:
            pdx, pdy, sc = _pair_paste_offset(
                tiles[a], tiles[b], expect_dx, expect_dy, level=pair_level
            )
            if sc < min_sc or _paste_too_far(
                pdx, pdy, expect_dx, expect_dy, tune_px=tune
            ):
                reason = "low-score" if sc < min_sc else "drift"
                pdx, pdy = float(expect_dx), float(expect_dy)
                use_spatial = True
                cost = 1.0
            else:
                pdx, pdy = _clamp_paste_fine_tune(
                    pdx, pdy, expect_dx, expect_dy, tune_px=tune
                )
                reason = "pcm-ncc"
                cost = -float(sc)
        logging.info(
            "edge %s->%s paste=(%.1f,%.1f) expect=(%.1f,%.1f) sc=%.3f "
            "level=%s via=%s tune=%s",
            a,
            b,
            pdx,
            pdy,
            expect_dx,
            expect_dy,
            sc,
            pair_level,
            reason,
            tune,
        )
        if (not use_spatial) and nx_ != cx and sc >= min_sc:
            signed_x.append(pdx / float(nx_ - cx))
        if (not use_spatial) and ny_ != cy and sc >= min_sc:
            signed_y.append(pdy / float(ny_ - cy))
        edge_list.append((cost, a, b, pdx, pdy, float(sc)))
        report(5 + int(80.0 * (ei + 1) / float(n_e)))

    seed = origin if origin in tiles else keys[0]
    pos, tree_n = _mst_positions(keys, edge_list, seed, grid_xy)

    # Optional warm prior for isolated tiles already handled in MST fallback
    if prior_positions and level in ("fine", "full"):
        for ij, xy in prior_positions.items():
            if ij in tiles and ij not in pos:
                pos[ij] = (float(xy[0]) * scale, float(xy[1]) * scale)

    pos_i = {k: (int(round(v[0])), int(round(v[1]))) for k, v in pos.items()}
    med_x = int(round(float(np.median(signed_x)))) if signed_x else gx
    med_y = int(round(float(np.median(signed_y)))) if signed_y else gy
    med_x = abs(med_x) if med_x else gx
    med_y = abs(med_y) if med_y else gy
    logging.info(
        "mosaic match-align(%s/%s): cal=(%s,%s) med=(%s,%s) axis=%s "
        "edges=%s tree=%s prior=%s",
        level,
        pair_level,
        gx,
        gy,
        med_x,
        med_y,
        (ax, ay),
        len(edge_list),
        tree_n,
        bool(prior_positions),
    )
    report(95)
    return pos_i, med_x, med_y


def _sharpness_map(gray):
    """Local |Laplacian| energy for preferential blending."""
    # 3x3 Laplacian via simple differences (no scipy)
    g = gray.astype(np.float32)
    lap = np.zeros_like(g)
    lap[1:-1, 1:-1] = (
        4.0 * g[1:-1, 1:-1]
        - g[:-2, 1:-1]
        - g[2:, 1:-1]
        - g[1:-1, :-2]
        - g[1:-1, 2:]
    )
    e = np.abs(lap)
    # blur energy slightly via box downsample
    tiny = Image.fromarray(np.clip(e, 0, 255).astype(np.uint8)).resize(
        (max(8, e.shape[1] // 8), max(8, e.shape[0] // 8)), Image.BILINEAR
    )
    e2 = np.asarray(tiny.resize((e.shape[1], e.shape[0]), Image.BILINEAR), dtype=np.float32)
    return e2


def _blend_weight(tw, th, feather_x, feather_y, gray):
    """Weight overlap by edge feather and normalized sharpness."""
    wx = np.ones((th, tw), dtype=np.float32)
    wy = np.ones((th, tw), dtype=np.float32)
    fx = max(1, int(feather_x))
    fy = max(1, int(feather_y))
    for i in range(fx):
        v = float(i + 1) / float(fx)
        wx[:, i] = np.minimum(wx[:, i], v)
        wx[:, tw - 1 - i] = np.minimum(wx[:, tw - 1 - i], v)
    for i in range(fy):
        v = float(i + 1) / float(fy)
        wy[i, :] = np.minimum(wy[i, :], v)
        wy[th - 1 - i, :] = np.minimum(wy[th - 1 - i, :], v)
    edge = wx * wy
    sharp = _sharpness_map(gray)
    smax = float(sharp.max()) + 1e-6
    pref = 0.35 + 0.65 * (sharp / smax)
    return np.maximum(edge * pref, 1e-3)


def stitch_cells(
    cells,
    nx,
    ny,
    tile_w,
    overlap=DEFAULT_OVERLAP,
    crop_occupied=True,
    align=True,
    stride_size=None,
    px_per_step=None,
    progress_cb=None,
    prior_positions=None,
    prior_tile_w=None,
    cache_out=None,
    axis_sign=None,
    live_basis=None,
):
    """Stitch with overlap phase-correlation align + sharpness-weighted blend.

    align: True/'full', 'fine' (warm), 'light' (preview), or False/'none'.
    prior_positions: optional draft placements to warm-start refine.
    cache_out: if dict, filled with positions/tile_w/med for later refine.
    axis_sign: (sx, sy) mosaic direction for +stage-grid index.
    """
    import gc

    del nx, ny, crop_occupied

    if not cells:
        return Image.new("RGB", (tile_w, max(1, int(tile_w * 0.75))), (0, 0, 0))

    tiles = {}
    tw = th = None
    for ij, path in cells.items():
        try:
            tile = resize_rgb(path, tile_w)
            if tw is None:
                tw, th = tile.size
            elif tile.size != (tw, th):
                tile = tile.resize((tw, th), Image.BILINEAR)
            tiles[ij] = tile
        except Exception:
            logging.exception("skip tile load %s", path)

    if not tiles:
        return Image.new("RGB", (tile_w, max(1, int(tile_w * 0.75))), (0, 0, 0))


    if stride_size and len(stride_size) >= 2:
        guess_x, guess_y = _place_from_stride(stride_size, tile_w, px_per_step)
    else:
        guess_x = max(1, int(tw * (1.0 - overlap)))
        guess_y = max(1, int(th * (1.0 - overlap)))

    align_level = _normalize_align(align)
    if progress_cb:
        try:
            progress_cb(1)
        except Exception:
            pass
    def place():
        if live_basis is not None:
            basis = np.asarray(live_basis) * tw
            poses = {k: tuple(np.round(np.asarray(k) @ basis)) for k in tiles}
            return poses, max(1, int(abs(basis[0, 0]))), max(1, int(abs(basis[1, 1])))
        return _estimate_positions(
            tiles, guess_x, guess_y, refine=align_level, progress_cb=progress_cb,
            prior_positions=prior_positions, prior_tile_w=prior_tile_w,
            tile_w=tw, axis_sign=axis_sign,
        )

    positions, med_x, med_y = place()

    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    min_x, min_y = min(xs), min(ys)
    positions = {k: (v[0] - min_x, v[1] - min_y) for k, v in positions.items()}
    cw = int(max(p[0] for p in positions.values()) + tw)
    ch = int(max(p[1] for p in positions.values()) + th)

    max_dim = int(PREVIEW_MAX_EDGE) if align_level in ("light", "preview", "1", "none") else 4000
    if cw > max_dim or ch > max_dim:
        scale = min(float(max_dim) / float(cw), float(max_dim) / float(ch))
        tile_w = max(64, int(tile_w * scale))
        tiles = {}
        tw = th = None
        for ij, path in cells.items():
            try:
                tile = resize_rgb(path, tile_w)
                if tw is None:
                    tw, th = tile.size
                elif tile.size != (tw, th):
                    tile = tile.resize((tw, th), Image.BILINEAR)
                tiles[ij] = tile
            except Exception:
                logging.exception("skip tile reload %s", path)
        if stride_size and len(stride_size) >= 2:
            guess_x, guess_y = _place_from_stride(stride_size, tile_w, px_per_step)
        else:
            guess_x = max(1, int(tw * (1.0 - overlap)))
            guess_y = max(1, int(th * (1.0 - overlap)))
        positions, med_x, med_y = place()

        xs = [p[0] for p in positions.values()]
        ys = [p[1] for p in positions.values()]
        min_x, min_y = min(xs), min(ys)
        positions = {k: (v[0] - min_x, v[1] - min_y) for k, v in positions.items()}
        cw = int(max(p[0] for p in positions.values()) + tw)
        ch = int(max(p[1] for p in positions.values()) + th)

    feather_x = max(1, min(max(12, tw // 4), max(1, tw - med_x)))
    feather_y = max(1, min(max(12, th // 4), max(1, th - med_y)))

    acc = np.zeros((ch, cw, 3), dtype=np.float32)
    wsum = np.zeros((ch, cw), dtype=np.float32)

    for ij in sorted(positions.keys(), key=lambda t: (t[1], t[0])):
        tile = tiles[ij]
        arr = np.asarray(tile, dtype=np.float32)
        gray = arr.mean(axis=2)
        weight = _blend_weight(tw, th, feather_x, feather_y, gray)
        x0, y0 = int(positions[ij][0]), int(positions[ij][1])
        x1, y1 = x0 + tw, y0 + th
        # clip to canvas
        xs0, ys0 = max(0, x0), max(0, y0)
        xs1, ys1 = min(cw, x1), min(ch, y1)
        if xs1 <= xs0 or ys1 <= ys0:
            continue
        tx0, ty0 = xs0 - x0, ys0 - y0
        tx1, ty1 = tx0 + (xs1 - xs0), ty0 + (ys1 - ys0)
        w = weight[ty0:ty1, tx0:tx1]
        acc[ys0:ys1, xs0:xs1] += arr[ty0:ty1, tx0:tx1] * w[:, :, None]
        wsum[ys0:ys1, xs0:xs1] += w

    wsafe = np.maximum(wsum, 1e-6)
    out = acc / wsafe[:, :, None]
    out[wsum < 1e-5] = 0
    canvas = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
    if cache_out is not None:
        try:
            cache_out.clear()
            cache_out["positions"] = dict(positions)
            cache_out["tile_w"] = int(tw)
            cache_out["tile_h"] = int(th)
            cache_out["med"] = (int(med_x), int(med_y))
            cache_out["canvas"] = (int(cw), int(ch))
        except Exception:
            logging.debug("stitch cache_out failed", exc_info=True)
    del tiles, acc, wsum, out
    gc.collect()
    if progress_cb:
        try:
            progress_cb(100)
        except Exception:
            pass
    return canvas
