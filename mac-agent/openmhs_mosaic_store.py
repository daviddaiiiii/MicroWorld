"""Mac-side mosaic archive: ingest from Pi, refine locally, serve tiles/LOD."""

from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
import hashlib
import logging
import os
import shutil
import socket
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("openmhs.mosaic_store")

from openmhs_proxy import authorize_microscope_request

_refine_lock = threading.Lock()
_refine_threads: Dict[str, threading.Thread] = {}

# Live scan previews are deliberately smaller and less frequent than the
# final Mac refine.  The Pi is an acquisition appliance; the Mac owns these
# temporary renders so the Pi and the browser never carry the accumulated
# canvas in memory on every tile.  This is the overview level only; zoomed
# regions are served from the pyramid below.
MAC_LIVE_PREVIEW_MAX_EDGE = 2048
# Publish the first live overview as soon as one tile arrives.  The preview
# is deliberately low-resolution and stage-positioned, so a short cadence is
# cheaper than making the user wait for a two-tile batch before seeing motion.
MAC_LIVE_PREVIEW_MIN_INTERVAL_S = 1.0
MAC_LIVE_PREVIEW_MIN_PENDING_TILES = 1

# The browser uses the same multi-resolution source during acquisition and
# after refinement.  This is deliberately a small, IIIF/Deep-Zoom-like
# contract rather than a dependency on a particular viewer library.
PYRAMID_TILE_SIZE = 256
PYRAMID_TILE_OVERLAP = 1
PYRAMID_JPEG_QUALITY = 84
_pyramid_lock = threading.Lock()


def _workspace_captures() -> Path:
    from openmhs_views import legacy_mosaics_root

    return legacy_mosaics_root()


def session_dir(session_id: str) -> Path:
    sid = (session_id or "").strip()
    if not sid or "/" in sid or ".." in sid or "\\" in sid:
        raise ValueError("invalid session_id")
    from openmhs_views import (
        record_is_trashed,
        current_view_id,
        ensure_record_for_session,
        legacy_mosaics_root,
        resolve_record_dir,
    )
    if record_is_trashed(sid):raise FileNotFoundError('Canvas is in recovery trash')

    found = resolve_record_dir(sid)
    if found is not None:
        (found / "tiles").mkdir(parents=True, exist_ok=True)
        return found
    vid = current_view_id()
    if vid:
        d = ensure_record_for_session(sid, view_id=vid)
        (d / "tiles").mkdir(parents=True, exist_ok=True)
        return d
    # No Camera view yet (user has not clicked New) — keep legacy until ingest.
    d = legacy_mosaics_root() / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "tiles").mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(session_id: str) -> Path:
    return session_dir(session_id) / "manifest.json"


def read_manifest(session_id: str) -> Dict[str, Any]:
    path = manifest_path(session_id)
    if not path.is_file():
        raise FileNotFoundError(session_id)
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def _publication_lock(folder):
    # The polling worker and the HTTP supervisor are separate processes.
    with (folder / ".publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_manifest(session_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    path = manifest_path(session_id)
    data = dict(data or {})
    data["session_id"] = session_id
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as handle:
        temporary = Path(handle.name)
        try:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return data


def tile_path(session_id: str, ix: int, iy: int) -> Path:
    folder = session_dir(session_id)
    try:
        man = json.loads((folder / "manifest.json").read_text())
        for cell in man.get("cells", []) + man.get("canvas_assets", []):
            if (int(cell["ix"]),int(cell["iy"])) == (int(ix),int(iy)):
                candidate = (folder / cell["relpath"]).resolve()
                if folder.resolve() in candidate.parents: return candidate
    except (OSError, ValueError, KeyError): pass
    return folder / "tiles" / "t_{}_{}.jpg".format(int(ix), int(iy))


def capture_metadata(path):
    """Recover actual XYZ from original EXIF; never invent Z from the grid."""
    import re
    from PIL import Image
    result = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    try:
        with Image.open(path) as im:
            raw = im.getexif().get_ifd(34665).get(37510, b"{}")
        if isinstance(raw, bytes): raw = raw.decode("utf-8")
        meta = json.loads(raw)
        name = meta.get("image", {}).get("name", "")
        match = re.search(r"_(-?\d+)_(-?\d+)_(-?\d+)\.jpe?g$",name)
        if match:
            result.update(stage_position=[int(x) for x in match.groups()],
                          position_source="original-exif-filename", units="motor_steps",
                          source_name=name, captured_at=meta.get("image",{}).get("time"),
                          instrument_id=meta.get("instrument",{}).get("id"))
    except Exception:
        LOG.debug("No recoverable original capture XYZ at %s",path)
    return result


def list_sessions() -> List[Dict[str, Any]]:
    from openmhs_views import list_legacy_mosaics, list_views, resolve_record_dir

    out: List[Dict[str, Any]] = []
    seen = set()
    tree = list_views()
    for view in tree.get("views") or []:
        for rec in view.get("records") or []:
            rid = rec.get("id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            d = resolve_record_dir(rid)
            out.append(
                {
                    "session_id": rid,
                    "view_id": rec.get("view_id") or view.get("id"),
                    "kind": rec.get("kind") or "mosaic",
                    "cells": rec.get("cells") or 0,
                    "refined": bool(rec.get("refined")),
                    "mtime": rec.get("mtime"),
                    "preview": bool(rec.get("preview")),
                    "refined_jpg": bool(d and (d / "refined.jpg").is_file()),
                    "relpath": rec.get("relpath"),
                }
            )
    for rec in list_legacy_mosaics():
        rid = rec.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        d = _workspace_captures() / rid
        out.append(
            {
                "session_id": rid,
                "view_id": None,
                "kind": "mosaic",
                "legacy": True,
                "cells": rec.get("cells") or 0,
                "refined": bool(rec.get("refined")),
                "mtime": rec.get("mtime"),
                "preview": bool(rec.get("preview")),
                "refined_jpg": (d / "refined.jpg").is_file(),
                "relpath": rec.get("relpath"),
            }
        )
    out.sort(key=lambda r: r.get("mtime") or "", reverse=True)
    return out


def _microscope_base() -> str:
    return (os.environ.get("OPENMHS_MICROSCOPE_URL") or "http://127.0.0.1:5000").rstrip(
        "/"
    )


def _lan_bind_ip() -> Optional[str]:
    env = (os.environ.get("OPENMHS_BIND_IP") or "").strip()
    if env:
        return env
    try:
        from urllib.parse import urlparse

        host = urlparse(_microscope_base()).hostname or "127.0.0.1"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 5000))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def _curl_bytes(url: str, timeout: float) -> bytes:
    """LaunchAgent Python often gets Errno 65; /usr/bin/curl still works."""
    import subprocess
    import tempfile

    cmd = ["/usr/bin/curl", "-sS", "-m", str(int(max(3, timeout))), "-L"]
    bind_ip = _lan_bind_ip()
    if bind_ip:
        cmd.extend(["--interface", bind_ip])
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:300]
        raise OSError("curl failed (%s): %s" % (proc.returncode, err or "no output"))
    return proc.stdout or b""


def _http_json(url: str, timeout: float = 30.0) -> Any:
    req = authorize_microscope_request(urllib.request.Request(url, headers={"Accept": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        msg = str(exc)
        if "Errno 65" not in msg and "No route to host" not in msg and "Network is unreachable" not in msg:
            raise
        LOG.warning("mosaic urllib blocked (%s); retry via curl", msg[:120])
        raw = _curl_bytes(url, timeout)
        return json.loads(raw.decode("utf-8")) if raw else {}


def _http_bytes(url: str, timeout: float = 60.0) -> bytes:
    req = authorize_microscope_request(urllib.request.Request(url))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        msg = str(exc)
        if "Errno 65" not in msg and "No route to host" not in msg and "Network is unreachable" not in msg:
            raise
        LOG.warning("mosaic urllib blocked (%s); retry via curl", msg[:120])
        return _curl_bytes(url, timeout)


def _scan_ext(path: str) -> str:
    namespace = os.environ.get("MICROWORLD_API_NAMESPACE", "org.microworld")
    return f"{_microscope_base()}/api/v2/extensions/{namespace}.scan-mosaic{path}"


def layout_from_positions(
    positions: Dict[Tuple[int, int], Tuple[float, float]],
    tile_w: int,
    tile_h: int,
    canvas: Tuple[int, int],
    asset_keys=None,
) -> Dict[str, Any]:
    cw, ch = int(canvas[0]), int(canvas[1])
    tw, th = int(tile_w), int(tile_h)
    tiles = []
    asset_keys = set(asset_keys or [])
    for (ix, iy), (x, y) in positions.items():
        tile = {
                "ix": int(ix),
                "iy": int(iy),
                "x": float(x) / float(max(1, cw)),
                "y": float(y) / float(max(1, ch)),
                "w": float(tw) / float(max(1, cw)),
                "h": float(th) / float(max(1, ch)),
            }
        if (ix, iy) in asset_keys:
            tile["asset"] = True
        tiles.append(tile)
    return {
        "ok": True,
        "canvas": [cw, ch],
        "tile_w": tw,
        "tile_h": th,
        "tiles": tiles,
    }


def _build_mac_live_preview(
    session_id: str,
    details: Dict[str, Any],
    cells: List[Dict[str, Any]],
    prior_manifest: Dict[str, Any],
) -> Tuple[bool, Optional[Dict[str, Any]], int]:
    """Build a bounded preview using a cached stage-to-image calibration."""
    if not cells:
        return False, None, int(prior_manifest.get("live_preview_revision") or 0)
    try:
        from PIL import Image

        from openmhs_mosaic_stitch import preview_tile_width, stitch_cells

        root = session_dir(session_id)
        render_cells = {}
        for cell in cells:
            try:
                ix, iy = int(cell["ix"]), int(cell["iy"])
                path = tile_path(session_id, ix, iy)
                if path.is_file() and path.stat().st_size > 32:
                    render_cells[(ix, iy)] = str(path)
            except (KeyError, TypeError, ValueError):
                continue
        if not render_cells:
            return False, None, int(prior_manifest.get("live_preview_revision") or 0)

        raw_grid = details.get("grid") or [1, 1]
        try:
            nx, ny = max(1, int(raw_grid[0])), max(1, int(raw_grid[1]))
        except (TypeError, ValueError, IndexError):
            xs = [key[0] for key in render_cells]
            ys = [key[1] for key in render_cells]
            nx = max(xs) - min(xs) + 1
            ny = max(ys) - min(ys) + 1
        side = max(nx, ny, 1)
        # Bound the working canvas before stitch_cells allocates it.  The
        # regular preview helper keeps small grids sharp with a 160 px floor,
        # but that floor would create a very large intermediate image for a
        # wide full-scan boundary.  Live monitoring is intentionally coarse;
        # the final Mac refine owns all full-resolution work.
        tile_w = max(
            16,
            min(
                int(preview_tile_width(len(render_cells), nx, ny)),
                int(MAC_LIVE_PREVIEW_MAX_EDGE / float(side)),
            ),
        )
        live_geometry = None
        stride = details.get("stride_size")
        px = details.get("px_per_step")
        if stride and px and len(stride) >= 2 and len(px) >= 2:
            try:
                from openmhs_registration import update_live_basis

                live_geometry = update_live_basis(
                    render_cells, cells, stride, px, details.get("axis_sign") or [-1, -1],
                    prior_manifest.get("live_geometry"),
                )
                prior_manifest["live_geometry"] = live_geometry
            except Exception:
                LOG.warning("Live calibration unavailable; using the stage prior", exc_info=True)
        cache: Dict[str, Any] = {}
        image = stitch_cells(
            render_cells,
            nx,
            ny,
            tile_w,
            align="none",
            stride_size=details.get("stride_size"),
            px_per_step=details.get("px_per_step"),
            cache_out=cache,
            axis_sign=details.get("axis_sign"),
            live_basis=live_geometry["basis"] if live_geometry else None,
        )
        if image.width > MAC_LIVE_PREVIEW_MAX_EDGE or image.height > MAC_LIVE_PREVIEW_MAX_EDGE:
            resampling = getattr(Image, "Resampling", Image)
            image.thumbnail(
                (MAC_LIVE_PREVIEW_MAX_EDGE, MAC_LIVE_PREVIEW_MAX_EDGE),
                resampling.BILINEAR,
            )
        target = root / "preview.jpg"
        temporary = root / "preview.live.tmp.jpg"
        image.save(str(temporary), format="JPEG", quality=78, optimize=False)
        with _publication_lock(root):
            latest = read_manifest(session_id) if (root / "manifest.json").is_file() else {}
            if latest.get("refined") and (details.get("scan_phase") == "done" or
                                          not prior_manifest.get("refined")):
                temporary.unlink(missing_ok=True)
                image.close()
                return False, None, int(prior_manifest.get("live_preview_revision") or 0)
            temporary.replace(target)
        image.close()

        layout = None
        if cache.get("positions") and cache.get("canvas"):
            layout = layout_from_positions(
                cache["positions"],
                int(cache.get("tile_w") or tile_w),
                int(cache.get("tile_h") or max(1, int(tile_w * 0.75))),
                tuple(cache["canvas"]),
            )
        revision = int(prior_manifest.get("live_preview_revision") or 0) + 1
        return True, layout, revision
    except Exception:
        LOG.exception("PC Worker live preview failed for %s", session_id)
        return False, None, int(prior_manifest.get("live_preview_revision") or 0)


def _pyramid_source_state(session_id: str) -> Tuple[Path, str, int, int]:
    """Return the best local image and a stable state key for its pyramid."""
    root = session_dir(session_id)
    manifest = read_manifest(session_id) if (root / "manifest.json").is_file() else {}
    refined = root / "refined.jpg"
    preview = root / "preview.jpg"
    if manifest.get("refined") and refined.is_file() and refined.stat().st_size > 32:
        source = refined
    elif preview.is_file() and preview.stat().st_size > 32:
        source = preview
    elif refined.is_file() and refined.stat().st_size > 32:
        # A refine can publish the image just before the manifest flag.  Do
        # not leave the viewer without a source during that short window.
        source = refined
    else:
        raise FileNotFoundError("no preview source")
    stat = source.stat()
    return source, source.name, int(stat.st_mtime_ns), int(stat.st_size)


def _pyramid_root(session_id: str) -> Path:
    return session_dir(session_id) / "pyramid"


def _read_pyramid_info(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def ensure_pyramid(session_id: str, force: bool = False) -> Dict[str, Any]:
    """Build or reuse a tiled multi-resolution representation on the Mac.

    Levels are stored from coarse to full resolution.  Every tile is an
    ordinary JPEG with one-pixel overlap, so the browser can request only the
    tiles intersecting its current viewport.  The source is either the live
    bounded preview or the final refined Slide; the directory is invalidated
    automatically when the source file changes.
    """
    source, source_name, source_mtime, source_size = _pyramid_source_state(session_id)
    root = _pyramid_root(session_id)
    info_path = root / "info.json"
    current_key = (source_name, source_mtime, source_size)
    with _pyramid_lock:
        existing = _read_pyramid_info(info_path) if not force else None
        if existing:
            existing_key = (
                existing.get("source"),
                int(existing.get("source_mtime_ns") or 0),
                int(existing.get("source_size") or 0),
            )
            if (
                existing_key == current_key
                and existing.get("tile_size") == PYRAMID_TILE_SIZE
                and existing.get("levels")
            ):
                return existing

        from PIL import Image

        parent = root.parent
        temporary = parent / (".pyramid.building.{}-{}".format(os.getpid(), threading.get_ident()))
        if temporary.exists():
            shutil.rmtree(str(temporary), ignore_errors=True)
        temporary.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(str(source)) as opened:
                image = opened.convert("RGB")
            width, height = image.size
            max_dim = max(width, height)
            low_downsample = 1
            while max_dim / float(low_downsample) > PYRAMID_TILE_SIZE * 2:
                low_downsample *= 2
            downsamples = []
            ds = low_downsample
            while ds >= 1:
                downsamples.append(ds)
                ds //= 2

            levels = []
            resampling = getattr(Image, "Resampling", Image)
            for level_index, downsample in enumerate(downsamples):
                level_width = max(1, (width + downsample - 1) // downsample)
                level_height = max(1, (height + downsample - 1) // downsample)
                level_dir = temporary / str(level_index)
                level_dir.mkdir(parents=True, exist_ok=True)
                if downsample == 1:
                    scaled = image
                else:
                    scaled = image.resize(
                        (level_width, level_height), resample=resampling.LANCZOS
                    )
                tiles_x = (level_width + PYRAMID_TILE_SIZE - 1) // PYRAMID_TILE_SIZE
                tiles_y = (level_height + PYRAMID_TILE_SIZE - 1) // PYRAMID_TILE_SIZE
                for tile_y in range(tiles_y):
                    for tile_x in range(tiles_x):
                        x0 = max(0, tile_x * PYRAMID_TILE_SIZE - PYRAMID_TILE_OVERLAP)
                        y0 = max(0, tile_y * PYRAMID_TILE_SIZE - PYRAMID_TILE_OVERLAP)
                        x1 = min(
                            level_width,
                            (tile_x + 1) * PYRAMID_TILE_SIZE + PYRAMID_TILE_OVERLAP,
                        )
                        y1 = min(
                            level_height,
                            (tile_y + 1) * PYRAMID_TILE_SIZE + PYRAMID_TILE_OVERLAP,
                        )
                        tile = scaled.crop((x0, y0, x1, y1))
                        tile.save(
                            str(level_dir / ("{}_{}.jpg".format(tile_x, tile_y))),
                            format="JPEG",
                            quality=PYRAMID_JPEG_QUALITY,
                            optimize=False,
                        )
                        tile.close()
                if scaled is not image:
                    scaled.close()
                levels.append(
                    {
                        "level": level_index,
                        "width": level_width,
                        "height": level_height,
                        "downsample": downsample,
                        "tiles_x": tiles_x,
                        "tiles_y": tiles_y,
                    }
                )
            image.close()

            info = {
                "ok": True,
                "session_id": session_id,
                "source": source_name,
                "source_mtime_ns": source_mtime,
                "source_size": source_size,
                "revision": "{}-{}-{}".format(source_name, source_mtime, source_size),
                "width": width,
                "height": height,
                "tile_size": PYRAMID_TILE_SIZE,
                "tile_overlap": PYRAMID_TILE_OVERLAP,
                "levels": levels,
            }
            (temporary / "info.json").write_text(
                json.dumps(info, indent=2), encoding="utf-8"
            )
            if root.exists():
                shutil.rmtree(str(root), ignore_errors=True)
            temporary.rename(root)
            return info
        except Exception:
            shutil.rmtree(str(temporary), ignore_errors=True)
            raise


def get_pyramid_info(session_id: str) -> Dict[str, Any]:
    return ensure_pyramid(session_id)


def get_pyramid_tile(session_id: str, level: int, x: int, y: int) -> bytes:
    info = ensure_pyramid(session_id)
    try:
        level_index = int(level)
        tile_x = int(x)
        tile_y = int(y)
    except (TypeError, ValueError):
        raise ValueError("level, x and y must be integers")
    levels = info.get("levels") or []
    if (
        level_index < 0
        or level_index >= len(levels)
        or tile_x < 0
        or tile_y < 0
        or tile_x >= int(levels[level_index].get("tiles_x") or 0)
        or tile_y >= int(levels[level_index].get("tiles_y") or 0)
    ):
        raise FileNotFoundError("pyramid tile not found")
    path = _pyramid_root(session_id) / str(level_index) / "{}_{}.jpg".format(tile_x, tile_y)
    if not path.is_file():
        raise FileNotFoundError("pyramid tile not found")
    return path.read_bytes()


def sync_pull_incremental(
    session_id: Optional[str] = None, *, details: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Incrementally pull new Pi tiles into the PC workspace."""
    if details is None:
        details = _http_json(_scan_ext("/live_details"), timeout=20.0)
    sid = session_id or details.get("session_id")
    if not sid:
        raise ValueError("no active session on Pi")
    if details.get("session_id") and str(details["session_id"]) != str(sid):
        raise ValueError(
            "session mismatch: want {} got {}".format(sid, details.get("session_id"))
        )

    layout = details.get("layout") or {}
    if not layout.get("tiles"):
        try:
            layout = _http_json(_scan_ext("/tile_layout"), timeout=20.0)
        except Exception:
            layout = {"tiles": []}

    indices: List[Tuple[int, int]] = []
    for t in layout.get("tiles") or []:
        try:
            indices.append((int(t["ix"]), int(t["iy"])))
        except Exception:
            continue

    d = session_dir(sid)
    existing: Dict[str, Dict[str, Any]] = {}
    man: Dict[str, Any] = {}
    if manifest_path(sid).is_file():
        try:
            man = read_manifest(sid)
            for c in man.get("cells") or []:
                existing["{},{}".format(c["ix"], c["iy"])] = c
                try:
                    indices.append((int(c["ix"]), int(c["iy"])))
                except Exception:
                    pass
        except Exception:
            man = {}

    # The optional centre hub is a real source image, but it is not a regular
    # motor-grid cell.  Pull it into canvas-assets so the Mac can include it
    # in the final global registration without affecting Continue occupancy.
    center_meta = details.get("center_tile") or {}
    center_required = bool(center_meta.get("extra"))
    center_assets = [
        a for a in (man.get("canvas_assets") or [])
        if a.get("center_tile") or (int(a.get("ix", 0)) == -1000000000)
    ]
    center_entry = center_assets[0] if center_assets else None
    center_downloaded = bool(
        center_entry
        and (d / center_entry.get("relpath", "")).is_file()
    ) if center_entry else False
    if center_required:
        center_relpath = (center_entry or {}).get("relpath") or "canvas-assets/center-tile.jpg"
        center_dest = (d / center_relpath).resolve()
        if d.resolve() not in center_dest.parents:
            raise ValueError("invalid centre tile path")
        center_dest.parent.mkdir(parents=True, exist_ok=True)
        if not center_dest.is_file() or center_dest.stat().st_size <= 32:
            try:
                q = urllib.parse.urlencode({"session_id": sid, "_": int(time.time() * 1000)})
                data = _http_bytes(_scan_ext("/center_tile?" + q), timeout=60.0)
                if len(data) > 32:
                    center_dest.write_bytes(data)
            except Exception as exc:
                failed = 1
                LOG.debug("centre tile pull failed: %s", exc)
        center_downloaded = center_dest.is_file() and center_dest.stat().st_size > 32
        if center_downloaded:
            center_entry = dict(center_meta)
            center_entry.pop("source_path", None)
            center_entry.update(
                ix=-1000000000,
                iy=0,
                relpath=str(center_dest.relative_to(d)),
                center_tile=True,
                extra=True,
            )

    unseen = any("{},{}".format(ix,iy) not in existing for ix,iy in indices)
    center_ready = (not center_required) or center_downloaded
    if (man.get("tile_revision_history") or man.get("canvas_edited")) and not unseen and center_ready and details.get("scan_phase") not in ("scanning", "calibrating") and int(details.get("cell_count") or 0) <= len(existing):
        if man.get('sync',{}).get('phase') != details.get('scan_phase'):
            man.setdefault('sync',{})['phase']=details.get('scan_phase')
            write_manifest(sid,man)
        return {"ok": True, "session_id": sid, "cells": len(man.get("cells", [])), "retake_revision_preserved": True}

    if not indices or len(set(indices)) < max(1, int(details.get("cell_count") or 0)):
        # Fall back / augment with known grid; missing cells will 404 and be skipped
        nx = int((details.get("grid") or [1, 1])[0] or 1)
        ny = int((details.get("grid") or [1, 1])[1] or 1)
        min_ix = int(details.get("min_ix") or 0)
        min_iy = int(details.get("min_iy") or 0)
        for iy in range(min_iy, min_iy + max(1, ny)):
            for ix in range(min_ix, min_ix + max(1, nx)):
                indices.append((ix, iy))

    # de-dupe
    indices = list({(ix, iy) for ix, iy in indices})

    new_count = 0
    failed = 0
    missing_revisions = False
    metadata = details.get("tile_metadata") or {}
    for ix, iy in indices:
        if [ix,iy] in man.get('canvas_trashed_coordinates',[]):continue
        key = "{},{}".format(ix, iy)
        dest = tile_path(sid, ix, iy)
        incoming = metadata.get(key, {})
        prior_capture = existing.get(key, {})
        capture_id = incoming.get("capture_id")
        replacement = bool(capture_id and prior_capture.get("capture_id")
                           and capture_id != prior_capture["capture_id"])
        if dest.is_file() and dest.stat().st_size > 32 and not replacement:
            if key not in existing:
                existing[key] = {
                    "ix": ix,
                    "iy": iy,
                    "relpath": "tiles/" + dest.name,
                }
            continue
        q = urllib.parse.urlencode(
            {"ix": ix, "iy": iy, "session_id": sid, "_": int(time.time() * 1000)}
        )
        url = _scan_ext("/tile_at?" + q)
        try:
            data = _http_bytes(url, timeout=60.0)
            if len(data) < 32 or not data.startswith(b"\xff\xd8"):
                failed += 1
                missing_revisions |= replacement
                continue
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as download:
                temp_path = Path(download.name)
                try:
                    download.write(data)
                    download.flush()
                    temp_path.replace(dest)
                finally:
                    temp_path.unlink(missing_ok=True)
            existing[key] = {
                "ix": ix,
                "iy": iy,
                "relpath": "tiles/" + dest.name,
            }
            new_count += 1
        except Exception as exc:
            failed += 1
            missing_revisions |= replacement
            LOG.debug("tile pull %s,%s: %s", ix, iy, exc)

    cells = list(existing.values())
    for cell in cells:
        if [cell['ix'],cell['iy']] in man.get('canvas_trashed_coordinates',[]):continue
        incoming = metadata.get("{},{}".format(cell["ix"],cell["iy"]), {})
        if cell.get("capture_id") and incoming.get("capture_id") not in (None, cell["capture_id"]):
            continue  # The download failed; do not label the old bytes as the replacement.
        cell.update(incoming)
        if not cell.get("sha256") or not cell.get("stage_position"):
            cell.update(capture_metadata(tile_path(sid,cell["ix"],cell["iy"])))
    pi_cells = int(details.get("cell_count") or 0)
    expected = int(details.get("cells_expected") or 0) or pi_cells
    phase = details.get("scan_phase") or ""
    old_layout = man.get("layout") if isinstance(man.get("layout"), dict) else {}
    old_n = len((old_layout or {}).get("tiles") or [])
    new_n = len((layout or {}).get("tiles") or [])
    # Never replace a fuller Mac layout with a sparse Pi Continue stitch
    if layout.get('rule_grid') and old_n >= new_n and not old_layout.get('rule_grid') and old_layout.get('tile_w',0)>2:
        keep_layout = old_layout
    elif new_n > 0 and new_n >= old_n:
        keep_layout = layout
    elif old_n > 0:
        keep_layout = old_layout
    else:
        keep_layout = layout or old_layout

    # Mac-owned live preview: render only a small stage-coordinate monitor and
    # rate-limit it by both time and accumulated new tiles.  Registration and
    # fusion remain a separate final operation after the scan.
    preview_ok = False
    preview_revision = int(man.get("live_preview_revision") or 0)
    live_preview_pending = int(man.get("live_preview_pending") or 0) + int(new_count)
    preview_layout = None
    mac_preview_only = bool(details.get("mac_preview_only"))
    now = time.time()
    last_preview = float(man.get("live_preview_at") or 0.0)
    preview_due = bool(
        mac_preview_only
        and (new_count > 0 or not (d / "preview.jpg").is_file())
        and (
            not (d / "preview.jpg").is_file()
            or now - last_preview >= MAC_LIVE_PREVIEW_MIN_INTERVAL_S
            or live_preview_pending >= MAC_LIVE_PREVIEW_MIN_PENDING_TILES
            or phase == "done"
        )
    )
    if preview_due:
        preview_ok, preview_layout, preview_revision = _build_mac_live_preview(
            sid,
            details,
            cells,
            man,
        )
        if preview_ok:
            live_preview_pending = 0
            last_preview = now
            if preview_layout:
                # Once the Mac has produced a monitor layout, prefer it over
                # a sparse Pi rule grid for the next Tiles refresh.
                keep_layout = preview_layout
    elif mac_preview_only and man.get("live_preview_revision") and old_layout.get("tile_w", 0) > 2:
        # Preserve the last Mac monitor layout during the interval between
        # two coalesced preview renders.
        keep_layout = old_layout

    # Preserve a Pi-only installation as a fallback, but do not fetch its
    # image when Mac preview ownership is active.
    if not mac_preview_only and new_count > 0 and not preview_ok:
        try:
            prev = _http_bytes(
                _scan_ext("/latest_preview_stitch.jpg?t={}".format(int(time.time()))),
                timeout=60.0,
            )
            if len(prev) > 32:
                (d / "preview.jpg").write_bytes(prev)
                preview_ok = True
        except Exception as exc:
            LOG.debug("preview pull: %s", exc)

    canvas_assets = [
        a for a in (man.get("canvas_assets") or [])
        if not (a.get("center_tile") or int(a.get("ix", 0)) == -1000000000)
    ]
    if center_entry and center_downloaded:
        canvas_assets.append(center_entry)
    safe_center_meta = dict(center_meta) if center_meta else (dict(center_entry) if center_entry else None)
    if safe_center_meta:
        safe_center_meta.pop("source_path", None)
    man.update(
        {
            "session_id": sid,
            "basename": details.get("name") or man.get("basename"),
            "dataset_id": details.get("dataset_id") or man.get("dataset_id"),
            "mosaic_id": details.get("mosaic_id") or man.get("mosaic_id"),
            "stride_size": details.get("stride_size") or man.get("stride_size"),
            "px_per_step": details.get("px_per_step") or man.get("px_per_step"),
            "axis_sign": details.get("axis_sign") or man.get("axis_sign") or [-1, -1],
            "origin_xy": details.get("origin_xy") or man.get("origin_xy"),
            "initial_xy": details.get("initial_xy") or man.get("initial_xy"),
            "min_ix": details.get("min_ix") if details.get("min_ix") is not None else man.get("min_ix"),
            "min_iy": details.get("min_iy") if details.get("min_iy") is not None else man.get("min_iy"),
            "nx": (details.get("grid") or [None, None])[0] or man.get("nx"),
            "ny": (details.get("grid") or [None, None])[1] or man.get("ny"),
            "last_grid": details.get("last_grid") or man.get("last_grid"),
            "cells": cells,
            "canvas_assets": canvas_assets,
            "center_tile": safe_center_meta,
            "layout": keep_layout,
            "registration": details.get("alignment_quality") or man.get("registration") or {},
            "live_preview_revision": preview_revision,
            "live_preview_pending": live_preview_pending,
            "live_preview_at": last_preview if preview_ok else man.get("live_preview_at"),
            "mac_preview_only": mac_preview_only,
            "source": "pi-scan-mosaic",
            "host": "mac",
            "sync": {
                "pi_cells": pi_cells,
                "mac_cells": len(cells),
                "expected": expected,
                "new": new_count,
                "failed": failed,
                "phase": phase,
                "preview": preview_ok,
                "center_tile": bool(center_downloaded),
                "complete": bool(
                    phase == "done"
                    and len(cells) >= max(1, pi_cells)
                    and center_ready
                    and not missing_revisions
                    and (expected <= 0 or len(cells) >= expected or pi_cells >= expected)
                ),
            },
        }
    )
    if not man.get("archived_at"):
        man["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with _publication_lock(d):
        latest = read_manifest(sid) if (d / "manifest.json").is_file() else {}
        refined_layout = latest.get("layout") or {}
        if latest.get("refined") and refined_layout.get("tile_w", 0) > 2:
            man["layout"] = refined_layout
            keep_layout = refined_layout
            for key in ("refined", "refine_engine", "registration", "refined_at",
                        "preview_revision", "fusion", "canvas_reference_locked"):
                if key in latest:
                    man[key] = latest[key]
        write_manifest(sid, man)
        if keep_layout and new_n >= old_n and new_n > 0:
            (d / "layout.json").write_text(json.dumps(keep_layout, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "session_id": sid,
        "cells": len(cells),
        "new": new_count,
        "failed": failed,
        "preview": preview_ok,
        "phase": phase,
        "center_tile": bool(center_downloaded),
        "sync": man["sync"],
        "manifest": man,
    }


def pull_slide_image(session_id: str, mosaic_id: Optional[str] = None) -> Dict[str, Any]:
    """Fetch final slide/mosaic JPEG from Pi Gallery into session dir as slide.jpg."""
    d = session_dir(session_id)
    out = d / "slide.jpg"
    # Prefer refined local copy
    refined = d / "refined.jpg"
    if refined.is_file() and refined.stat().st_size > 32:
        out.write_bytes(refined.read_bytes())
        return {"ok": True, "path": str(out), "source": "refined"}

    mid = mosaic_id
    if not mid:
        try:
            man = read_manifest(session_id)
            mid = man.get("mosaic_id") or man.get("slide_id")
        except Exception:
            mid = None
    if mid:
        for path in (
            "/api/v2/captures/{}/download".format(mid),
            "/api/v2/instrument/captures/{}/download".format(mid),
            "/api/v2/captures/{}".format(mid),
        ):
            try:
                data = _http_bytes(
                    "{}{}".format(_microscope_base(), path), timeout=120.0
                )
                if len(data) > 1000 and data[:2] == b"\xff\xd8":
                    out.write_bytes(data)
                    return {"ok": True, "path": str(out), "source": "capture:" + str(mid)}
            except Exception:
                continue
    # Fallback: latest preview
    prev = d / "preview.jpg"
    if prev.is_file():
        out.write_bytes(prev.read_bytes())
        return {"ok": True, "path": str(out), "source": "preview"}
    return {"ok": False, "error": "no slide source"}


def ingest_pull(session_id: Optional[str] = None) -> Dict[str, Any]:
    """Full pull (compatibility): incremental sync + slide attempt."""
    result = sync_pull_incremental(session_id)
    sid = result["session_id"]
    try:
        slide = pull_slide_image(sid, (result.get("manifest") or {}).get("mosaic_id"))
        result["slide"] = slide
    except Exception as exc:
        result["slide"] = {"ok": False, "error": str(exc)}
    return result


def get_layout(session_id: str) -> Dict[str, Any]:
    d = session_dir(session_id)
    layout_file = d / "layout.json"
    if layout_file.is_file():
        return json.loads(layout_file.read_text(encoding="utf-8"))
    man = read_manifest(session_id)
    return man.get("layout") or {"ok": True, "tiles": []}


def refine_status(session_id: str) -> Dict[str, Any]:
    d = session_dir(session_id)
    path = d / "refine_status.json"
    status = None
    if path.is_file():
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    man = read_manifest(session_id) if manifest_path(session_id).is_file() else {}
    fallback = {
        "session_id": session_id,
        "phase": "done" if man.get("refined") else "idle",
        "refine_progress": 100 if man.get("refined") else 0,
        "refined": bool(man.get("refined")),
    }
    if status:
        # A prior implementation could leave canvas-layout in the status file
        # after a real Mac-side refinement. The completed manifest is the
        # authoritative engine record.
        if status.get("phase") == "done" and man.get("refine_engine"):
            status["engine"] = man.get("refine_engine")
            status["registration"] = man.get("registration") or {}
        return {**fallback, **status}
    return fallback


def _write_refine_status(session_id: str, **kwargs: Any) -> None:
    d = session_dir(session_id)
    cur = refine_status(session_id)
    cur.update(kwargs)
    cur["session_id"] = session_id
    (d / "refine_status.json").write_text(
        json.dumps(cur, indent=2), encoding="utf-8"
    )


def _run_refine(session_id: str, align: str = "fine") -> None:
    from openmhs_mosaic_stitch import REFINE_TILE_WIDTH, stitch_cells

    t0 = time.time()
    try:
        _write_refine_status(
            session_id,
            phase="refining",
            refine_progress=1,
            refined=False,
            error=None,
            engine=None,
            registration={},
        )
        man = read_manifest(session_id)
        # Canvas edits must never suppress registration. Visibility, opacity,
        # layer order, and manual tile placement are all presentation state;
        # the Scan/Retake pipeline still needs to compute image-space poses.
        cells: Dict[Tuple[int, int], str] = {}
        for c in man.get("cells") or []:
            ix, iy = int(c["ix"]), int(c["iy"])
            p = tile_path(session_id, ix, iy)
            if p.is_file():
                cells[(ix, iy)] = str(p)
        if not cells:
            raise ValueError("no tiles to refine")

        # Stage-positioned canvas assets (currently the spiral centre hub) are
        # extra registration evidence, not regular motor-grid occupancy.
        # Imported images without a physical coordinate stay presentation-only.
        render_cells = dict(cells)
        asset_keys = set()
        grid_coordinates = {}
        origin = man.get("origin_xy") or [0, 0]
        stride = man.get("stride_size") or [1, 1]
        try:
            ox, oy = float(origin[0]), float(origin[1])
            sx, sy = float(stride[0]), float(stride[1])
        except (TypeError, ValueError, IndexError):
            ox = oy = 0.0
            sx = sy = 1.0
        for cell in man.get("cells") or []:
            stage = cell.get("stage_position")
            if cell.get("position_source") == "video-registered" and stage and len(stage) >= 2:
                key = (int(cell["ix"]), int(cell["iy"]))
                grid_coordinates[key] = ((float(stage[0])-ox)/(sx or 1.0),
                                         (float(stage[1])-oy)/(sy or 1.0))

        for asset in man.get("canvas_assets") or []:
            stage = asset.get("stage_position")
            if not stage or len(stage) < 2:
                continue
            try:
                key = (int(asset["ix"]), int(asset["iy"]))
                p = tile_path(session_id, key[0], key[1])
                if not p.is_file() or key in render_cells:
                    continue
                render_cells[key] = str(p)
                grid_coordinates[key] = (
                    (float(stage[0]) - ox) / (sx or 1.0),
                    (float(stage[1]) - oy) / (sy or 1.0),
                )
                asset_keys.add(key)
            except (KeyError, TypeError, ValueError, IndexError):
                LOG.debug("Skipping unpositioned canvas asset %s", asset)

        prior = None
        prior_tw = None
        layout = man.get("layout") or {}
        if layout.get("tiles") and layout.get("canvas") and layout.get("tile_w"):
            cw, ch = layout["canvas"]
            tw = int(layout["tile_w"])
            prior = {}
            for t in layout["tiles"]:
                prior[(int(t["ix"]), int(t["iy"]))] = (
                    float(t["x"]) * float(cw),
                    float(t["y"]) * float(ch),
                )
            prior_tw = tw

        def on_progress(pct: int) -> None:
            _write_refine_status(
                session_id,
                phase="refining",
                refine_progress=int(pct),
                refined=False,
            )

        cache: Dict[str, Any] = {}
        engine = "legacy"
        mosaic = None
        # Register predicted overlaps on the Mac (OpenCV required).
        try:
            import importlib

            import openmhs_registration as _hq

            _hq = importlib.reload(_hq)
            mosaic = _hq.stitch_cells_hq(
                render_cells,
                int(man.get("nx") or 1),
                int(man.get("ny") or 1),
                REFINE_TILE_WIDTH,
                stride_size=man.get("stride_size"),
                px_per_step=man.get("px_per_step"),
                progress_cb=on_progress,
                cache_out=cache,
                axis_sign=man.get("axis_sign"),
                grid_coordinates=grid_coordinates,
                asset_keys=asset_keys,
            )
            engine = str(cache.get("engine") or "hq-registration")
            polished = mosaic
        except Exception:
            LOG.exception("HQ stitch failed — falling back to legacy stitch")
            mosaic = stitch_cells(
                cells,
                int(man.get("nx") or 1),
                int(man.get("ny") or 1),
                REFINE_TILE_WIDTH,
                align="full",
                stride_size=man.get("stride_size"),
                px_per_step=man.get("px_per_step"),
                progress_cb=on_progress,
                prior_positions=prior,
                prior_tile_w=prior_tw,
                cache_out=cache,
                axis_sign=man.get("axis_sign"),
            )
            polished = mosaic
            engine = "legacy"

        d = session_dir(session_id)
        refined_path = d / "refined.jpg"
        # Keep the full-resolution result separate from the bounded overview.
        # The browser must never download the full Slide just to establish its
        # aspect ratio; zoomed regions come from the pyramid below.
        for target, quality in ((refined_path, 93), (d / "slide.jpg", 93)):
            tmp = target.with_suffix(".tmp")
            polished.save(str(tmp), format="JPEG", quality=quality, optimize=True)
            tmp.replace(target)
        overview = polished.copy()
        if overview.width > MAC_LIVE_PREVIEW_MAX_EDGE or overview.height > MAC_LIVE_PREVIEW_MAX_EDGE:
            resampling = getattr(type(overview), "Resampling", None)
            if resampling is None:
                from PIL import Image

                resampling = getattr(Image, "Resampling", Image)
            overview.thumbnail(
                (MAC_LIVE_PREVIEW_MAX_EDGE, MAC_LIVE_PREVIEW_MAX_EDGE),
                resampling.BILINEAR,
            )
        preview_tmp = (d / "preview.jpg").with_suffix(".tmp")
        overview.save(str(preview_tmp), format="JPEG", quality=82, optimize=True)
        with _publication_lock(d):
            preview_tmp.replace(d / "preview.jpg")
            overview.close()

            if cache.get("positions") and cache.get("canvas"):
                layout = layout_from_positions(
                    cache["positions"],
                    int(cache.get("tile_w") or REFINE_TILE_WIDTH),
                    int(cache.get("tile_h") or REFINE_TILE_WIDTH),
                    tuple(cache["canvas"]),
                    asset_keys=asset_keys,
                )
                (d / "layout.json").write_text(
                    json.dumps(layout, indent=2), encoding="utf-8"
                )
                man["layout"] = layout

            man["refined"] = True
            man["refine_engine"] = engine
            man["registration"] = cache.get("registration", {})
            # Keep render diagnostics with the session so a Slide can be
            # reproduced/audited without changing the immutable source tiles.
            man["fusion"] = cache.get("fusion", {})
            man["refined_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # A completed source refinement is the explicit event that is
            # allowed to refresh the stage-to-Canvas scale.  The next Canvas load
            # will keep the established physical origin while adopting the new
            # refined relative poses.  Ordinary Canvas edits set this lock back
            # to True and never move the Camera PiP reference.
            man["canvas_reference_locked"] = False
            # Reuse the same revision channel as the live monitor so the browser
            # can keep a stable URL while scanning and fetch the final Slide once.
            man["preview_revision"] = int(time.time() * 1000)
            latest = read_manifest(session_id)
            for key in ("layout", "refined", "refine_engine", "registration", "fusion",
                        "refined_at", "canvas_reference_locked", "preview_revision"):
                if key in man:
                    latest[key] = man[key]
            write_manifest(session_id, latest)
        # Build the full-resolution pyramid before declaring refine complete,
        # so the first zoom after a scan is served immediately.  A pyramid
        # failure must not discard an otherwise valid refined Slide; the
        # supervisor will lazily retry it on the first zoom request.
        try:
            ensure_pyramid(session_id, force=True)
        except Exception:
            LOG.exception("pyramid build failed after refine; leaving lazy retry")
        elapsed = round(time.time() - t0, 2)
        _write_refine_status(
            session_id,
            phase="done",
            refine_progress=100,
            refined=True,
            elapsed_s=elapsed,
            engine=engine,
            error=None,
            fusion=cache.get("fusion", {}),
        )
        LOG.info(
            "refine %s done in %ss (%s cells, engine=%s)",
            session_id,
            elapsed,
            len(render_cells),
            engine,
        )
    except Exception as exc:
        LOG.exception("refine %s failed", session_id)
        _write_refine_status(
            session_id,
            phase="error",
            refine_progress=0,
            refined=False,
            error=str(exc)[:500],
        )
    finally:
        with _refine_lock:
            _refine_threads.pop(session_id, None)


def start_refine(session_id: str, align: str = "fine") -> Dict[str, Any]:
    from openmhs_recapture import active_sessions
    if session_id in active_sessions:
        raise ValueError("An explicit ROI retake owns this session; wait for completion")
    read_manifest(session_id)  # ensure exists
    with _refine_lock:
        existing = _refine_threads.get(session_id)
        if existing and existing.is_alive():
            return {
                "ok": True,
                "started": False,
                "already": True,
                "session_id": session_id,
                **refine_status(session_id),
            }
        th = threading.Thread(
            target=_run_refine,
            args=(session_id, align),
            name="mosaic-refine-" + session_id,
            daemon=True,
        )
        _refine_threads[session_id] = th
        th.start()
    return {
        "ok": True,
        "started": True,
        "session_id": session_id,
        "phase": "refining",
        "refine_progress": 1,
    }


def session_id_from_relpath(path: str) -> Optional[str]:
    """views/<view>/records/<id>/... or captures/mosaics/<id>/... → id."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p and p != "."]
    if "records" in parts:
        idx = parts.index("records")
        if idx + 1 < len(parts):
            sid = parts[idx + 1]
            if sid and "/" not in sid and ".." not in sid:
                return sid
    try:
        idx = parts.index("mosaics")
    except ValueError:
        return None
    if idx + 1 >= len(parts):
        return None
    sid = parts[idx + 1]
    if "/" in sid or ".." in sid:
        return None
    return sid


def read_agent_snapshot(session_id: str) -> Dict[str, Any]:
    path = session_dir(session_id) / "agent.json"
    if not path.is_file():
        return {"ok": True, "session_id": session_id, "agent": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    return {"ok": True, "session_id": session_id, "agent": data}


def write_agent_snapshot(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    path = session_dir(session_id) / "agent.json"
    body = dict(payload or {})
    body["session_id"] = session_id
    body["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "session_id": session_id, "path": "agent.json"}


def delete_session(session_id: str) -> Dict[str, Any]:
    import shutil

    from openmhs_views import read_index, resolve_record_dir, write_index

    sid = (session_id or "").strip()
    d = resolve_record_dir(sid)
    if d is not None and d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    idx = read_index()
    recs = dict(idx.get("records") or {})
    recs.pop(sid, None)
    idx["records"] = recs
    write_index(idx)
    return {"ok": True, "deleted": sid}
