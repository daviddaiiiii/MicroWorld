#!/usr/bin/env python3
"""MicroWorld acquisition and live-observation wrapper for OpenFlexure.

The Pi is the hardware boundary, not the high-quality image-processing host.
It wraps ``org.openflexure.scan`` to serialize hardware mutations, capture
source tiles with actual stage metadata, publish bounded progress state, and
retain enough session metadata for Continue and recovery.

In the normal ``mac_preview_only`` path, the Mac worker pulls the source tiles,
builds the incremental stage-coordinate preview, and performs final global
registration/focus-aware fusion. The Pi does not rebuild the accumulated
high-resolution Slide on every tile. A legacy Pi-side finalization path remains
for stock OpenFlexure callers that explicitly do not request the Mac path.

Install on the Pi:
  sudo cp org_openmhs_scan_mosaic.py \\
    /var/openflexure/extensions/microscope_extensions/
  sudo systemctl restart openflexure-microscope-server
"""

from __future__ import annotations

import datetime
import gc
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from flask import Response, abort, current_app, request, send_file
from labthings import current_labthing, fields, find_component, find_extension
from labthings.extensions import BaseExtension
from labthings.views import ActionView, PropertyView, View
from PIL import Image

try:
    from microworld_scan_plan import center_hub_spec, spiral_grid_order
except ImportError:  # OpenFlexure loads each extension by file path.
    import importlib.util

    _plan_path = os.path.join(os.path.dirname(__file__), "microworld_scan_plan.py")
    _plan_spec = importlib.util.spec_from_file_location("microworld_scan_plan", _plan_path)
    if _plan_spec is None or _plan_spec.loader is None:
        raise
    _plan_module = importlib.util.module_from_spec(_plan_spec)
    sys.modules["microworld_scan_plan"] = _plan_module
    _plan_spec.loader.exec_module(_plan_module)
    center_hub_spec = _plan_module.center_hub_spec
    spiral_grid_order = _plan_module.spiral_grid_order

try:
    from microworld_hardware import CONNECTION_HEADER, HardwareBusy, HardwareGate
except ImportError:  # OpenFlexure loads each extension by file path.
    import importlib.util

    _hardware_path = os.path.join(os.path.dirname(__file__), "microworld_hardware.py")
    _hardware_spec = importlib.util.spec_from_file_location(
        "microworld_hardware", _hardware_path
    )
    if _hardware_spec is None or _hardware_spec.loader is None:
        raise
    _hardware_module = importlib.util.module_from_spec(_hardware_spec)
    sys.modules["microworld_hardware"] = _hardware_module
    _hardware_spec.loader.exec_module(_hardware_module)
    CONNECTION_HEADER = _hardware_module.CONNECTION_HEADER
    HardwareBusy = _hardware_module.HardwareBusy
    HardwareGate = _hardware_module.HardwareGate

try:
    import piexif
except ImportError:  # pragma: no cover
    piexif = None

try:
    import marshmallow
except ImportError:  # pragma: no cover
    marshmallow = None

# OpenFlexure can import extension files directly without adding their directory.
try:
    from microworld_api_compat import legacy_extension
except ImportError:
    from importlib.util import module_from_spec, spec_from_file_location

    _compat_spec = spec_from_file_location(
        "microworld_api_compat", Path(__file__).with_name("microworld_api_compat.py")
    )
    if _compat_spec is None or _compat_spec.loader is None:
        raise
    _compat_module = module_from_spec(_compat_spec)
    _compat_spec.loader.exec_module(_compat_module)
    legacy_extension = _compat_module.legacy_extension

EXTENSION_ID = "org.microworld.scan-mosaic"
LEGACY_EXTENSION_ID = "org.openmhs.scan-mosaic"
EXTENSION_VERSION = "1.18.0"
MICROGRAPHS = "/var/openflexure/data/micrographs"
PREVIEW_DIR = "/var/openflexure/data/scan-mosaic"
PREVIEW_NAME = "preview.jpg"
SESSION_STATE_NAME = "session.json"
LIVE_TILE_WIDTH = 512  # live preview per-tile (was 320 — too soft on small grids)
FINAL_TILE_WIDTH = 640
REFINE_TILE_WIDTH = 960
PREVIEW_MAX_EDGE = 2560  # live mosaic longest side budget (px)
DEFAULT_OVERLAP = 0.30  # target overlap when auto-choosing stride
# Crop this fraction from each tile edge before paste to kill seam artifacts
TILE_EDGE_CROP = 0.02
LIVE_MIN_INTERVAL_S = 2.5
MIN_CELLS_FOR_PREVIEW = 1
# Several browser tabs/devices can poll the same read-only observer endpoint
# at once.  A very short cache coalesces those bursts without making scan
# progress feel stale; all hardware mutations still bypass this cache.
LIVE_DETAILS_CACHE_TTL_S = 0.25
_LIVE_DETAILS_CACHE_LOCK = threading.Lock()
_LIVE_DETAILS_CACHE = {"at": 0.0, "payload": None}
# Live scans use stable stage/grid placement most of the time.  A light
# neighbour registration is still useful periodically, but doing it after
# every tile makes the scan increasingly expensive as the grid grows.
LIVE_ALIGN_EVERY = 6
# Automatic-scan focus confirmation.  These values wrap OpenFlexure's stock
# fast autofocus; the adaptive stack below is only engaged when a small probe
# detects genuinely different local focus planes.
# Automatic scan focus policy.  Every field starts with exactly one native
# Fast-AF pass.  A cheap clarity probe below decides whether that result is
# good enough or needs a targeted rescue; the first field has no special
# multi-pass treatment because its content is not representative of the next
# field.
SCAN_FOCUS_TILE_MAX = 1
# Adaptive tilt/focus-stack policy.  The native Fast AF result remains the
# baseline.  A three-plane probe is cheap; only a clear multi-plane signature
# expands to the full seven-plane stack.  Extra frames are fused in memory and
# discarded, so the Pi does not accumulate another source dataset.
ADAPTIVE_FOCUS_DEFAULT = True
FOCUS_PROBE_OFFSETS = (-300, 0, 300)
FOCUS_STACK_OFFSETS = (-300, -200, -100, 0, 100, 200, 300)
FOCUS_BLOCK_ROWS = 4
FOCUS_BLOCK_COLS = 4
FOCUS_ALT_MARGIN = 1.15
FOCUS_ALT_FRACTION = 0.25
FOCUS_MIN_Z_SPAN = 150
# A tile with almost no valid focus blocks is a different failure mode from a
# tilted tile.  In that case the cheap probe is not useful as a stack: first
# give OpenFlexure's native Fast-AF one more chance, then make only a tiny
# correction around the new landing point.
FOCUS_GLOBAL_MIN_VALID_BLOCKS = 2
FOCUS_GLOBAL_MIN_TEXTURE_STD = 8.0
FOCUS_GLOBAL_MIN_BEST_SCORE = 2.5
FOCUS_GLOBAL_AF_RETRIES = 1
FOCUS_MICRO_OFFSETS = (-150, 0, 150)
# Progressive rescue uses the same OpenFlexure Fast-AF engine with a wider
# search window.  It stops as soon as the last pass no longer gives a real
# clarity gain; frames used for the decision remain in memory only.
FOCUS_PROGRESSIVE_MULTIPLIERS = (2, 3)
FOCUS_PROGRESSIVE_MAX_DZ = 8000
FOCUS_PROGRESSIVE_MIN_GAIN = 1.08
# A reserved spatial index for the extra half-step centre exposure.  It is
# deliberately outside the motor-grid namespace so Continue/fill-gaps can
# never mistake the hub for an occupied regular cell.
HUB_TILE_IX = -1000000000
HUB_TILE_IY = 0
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
# MicroWorld Canvas scans follow the stock OpenFlexure geometry by default:
# explicit fixed stride, with no probe movement before acquisition.
AUTO_STRIDE_DEFAULT = False
NAME_PAT = re.compile(
    r"^(?P<prefix>.+)_(?P<x>-?\d+)_(?P<y>-?\d+)_(?P<z>-?\d+)\.(?P<ext>jpe?g)$",
    re.I,
)

XyCoordinate = Tuple[int, int]


def construct_grid(
    initial, step_sizes, n_steps, style="raster",
):
    """Same geometry as org.openflexure.scan.construct_grid (spatial indices)."""
    arr = []
    if style == "spiral":
        coord = initial
        arr.append([initial])
        for i in range(2, n_steps[0] + 1):
            arr.append([])
            side_length = (2 * i) - 1
            last_coordinate = coord
            coord = (
                last_coordinate[0] + [-1, 1][0] * step_sizes[0],
                last_coordinate[1] + [-1, 1][1] * step_sizes[1],
            )
            for direction in ([1, 0], [0, -1], [-1, 0], [0, 1]):
                for _ in range(side_length - 1):
                    last_coordinate = coord
                    coord = (
                        last_coordinate[0] + direction[0] * step_sizes[0],
                        last_coordinate[1] + direction[1] * step_sizes[1],
                    )
                    arr[i - 1].append(coord)
        return arr

    for i in range(n_steps[0]):
        arr.append([])
        for j in range(n_steps[1]):
            coord = (
                initial[0] + i * step_sizes[0],
                initial[1] + j * step_sizes[1],
            )
            arr[i].append(coord)
        if style == "snake" and i % 2 != 0:
            arr[i] = list(reversed(arr[i]))
    return arr


def spatial_index_map(initial, step_sizes, n_steps, style="raster"):
    """Map stage (x,y) -> spatial (ix, iy) for placement (not visit order)."""
    mapping = {}
    if style == "spiral":
        grid = construct_grid(initial, step_sizes, n_steps, style=style)
        xs = sorted({c[0] for row in grid for c in row})
        ys = sorted({c[1] for row in grid for c in row})
        for row in grid:
            for c in row:
                mapping[c] = (xs.index(c[0]), ys.index(c[1]))
        return mapping, len(xs), len(ys)

    nx, ny = int(n_steps[0]), int(n_steps[1])
    for i in range(nx):
        for j in range(ny):
            coord = (
                initial[0] + i * step_sizes[0],
                initial[1] + j * step_sizes[1],
            )
            mapping[coord] = (i, j)
    return mapping, nx, ny


def nearest_index(mapping, x, y):
    if (x, y) in mapping:
        return mapping[(x, y)]
    best = None
    best_d = None
    for (cx, cy), ij in mapping.items():
        d = (cx - x) * (cx - x) + (cy - y) * (cy - y)
        if best_d is None or d < best_d:
            best_d = d
            best = ij
    return best


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
    """Per-tile size for live preview — keep small grids sharp.

    1×1 / 2×2 stay near capture scale; only large mosaics shrink harder.
    """
    import math

    if nx and ny and int(nx) <= 1 and int(ny) <= 1:
        return int(FINAL_TILE_WIDTH)
    if nx and ny:
        side = max(int(nx), int(ny), 1)
    else:
        side = max(1, int(math.ceil(math.sqrt(float(max(1, int(n_cells)))))))
    if side <= 1:
        return int(FINAL_TILE_WIDTH)
    # Soft cap by grid size so 2×2/3×3 are not crushed to 320px
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


def lock_camera_for_scan(microscope):
    """Freeze current exposure/AWB so tiles share one photometric setting."""
    cam = getattr(microscope, "camera", None)
    if cam is None:
        return {"ok": False, "reason": "no-camera"}
    info = {"ok": False}
    try:
        settings = {}
        if hasattr(cam, "read_settings"):
            raw = cam.read_settings() or {}
            pic = raw.get("picamera") if isinstance(raw, dict) else None
            if not isinstance(pic, dict):
                pic = raw if isinstance(raw, dict) else {}
        else:
            pic = {}
        pc = getattr(cam, "picamera", None)
        shutter = None
        awb = None
        analog = None
        if pc is not None:
            try:
                shutter = int(getattr(pc, "shutter_speed", 0) or 0)
            except Exception:
                shutter = 0
            try:
                awb = getattr(pc, "awb_gains", None)
            except Exception:
                awb = None
            try:
                analog = float(getattr(pc, "analog_gain", 0) or 0)
            except Exception:
                analog = None
        if not shutter:
            shutter = int(pic.get("shutter_speed") or 0)
        if awb is None:
            awb = pic.get("awb_gains")
        apply = {"exposure_mode": "off", "awb_mode": "off"}
        if shutter and shutter > 0:
            apply["shutter_speed"] = int(shutter)
        if awb is not None:
            try:
                apply["awb_gains"] = [float(awb[0]), float(awb[1])]
            except Exception:
                pass
        if hasattr(cam, "apply_picamera_settings"):
            cam.apply_picamera_settings(apply, pause_for_effect=True)
        elif pc is not None:
            pc.exposure_mode = "off"
            pc.awb_mode = "off"
            if shutter and shutter > 0:
                pc.shutter_speed = int(shutter)
            if awb is not None:
                pc.awb_gains = awb
        info = {
            "ok": True,
            "shutter_speed": apply.get("shutter_speed"),
            "awb_gains": apply.get("awb_gains"),
            "analog_gain": analog,
        }
        logging.info("Locked camera for scan: %s", info)
    except Exception:
        logging.exception("lock_camera_for_scan failed")
        info = {"ok": False, "reason": "exception"}
    return info


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


def grab_rgb_frame(microscope, size=None):
    """Fast video-port RGB frame (no Gallery write)."""
    cam = microscope.camera
    arr = None
    if hasattr(cam, "array"):
        arr = cam.array(use_video_port=True)
    if arr is None:
        raise RuntimeError("camera.array unavailable")
    im = Image.fromarray(np.asarray(arr)).convert("RGB")
    if size and im.size != tuple(size):
        im = im.resize((int(size[0]), int(size[1])), Image.BILINEAR)
    return im


def grab_gray_frame(microscope, size=SAFE_RESIZE):
    """Fast video-port grayscale frame for calibration (no Gallery write)."""
    im = grab_rgb_frame(microscope, size=size).convert("L")
    return np.asarray(im, dtype=np.float32)


def preview_session_active(st):
    """True while UI may show live preview for this session."""
    if not st.session_id:
        return False
    if st.best:
        return True
    # Start-seed FOV before any tiles land
    if st.phase in ("calibrating", "scanning", "stitching", "refining"):
        return True
    # After tiles-only cleanup: tiles gone but preview.jpg + session stay
    if st.phase == "done" and st.preview_path and os.path.isfile(st.preview_path):
        return True
    return False


def quality_focus_score(gray):
    """Upper-quartile block Laplacian energy; reduce dilution by empty background."""
    g = np.asarray(gray, dtype=np.float32)
    lap = 4*g[1:-1, 1:-1]-g[:-2, 1:-1]-g[2:, 1:-1]-g[1:-1, :-2]-g[1:-1, 2:]
    scores = [float(np.mean(b*b)) for row in np.array_split(lap, 4)
              for b in np.array_split(row, 4, axis=1)]
    return float(np.percentile(scores, 75))


def quality_focus_frame(microscope):
    """Use the SAME ISP resize and JPEG settings as the retained scan tile."""
    buffer = io.BytesIO()
    microscope.camera.capture(buffer, fmt="jpeg", use_video_port=True,
                              resize=SAFE_RESIZE, bayer=False)
    buffer.seek(0)
    with Image.open(buffer) as frame:
        return np.asarray(frame.convert("L"), dtype=np.float32)


def _focus_capture_frame(microscope, size=SAFE_RESIZE):
    """Capture one disposable RGB frame for adaptive focus analysis."""
    buffer = io.BytesIO()
    microscope.camera.capture(
        buffer,
        fmt="jpeg",
        use_video_port=True,
        resize=size,
        bayer=False,
    )
    buffer.seek(0)
    with Image.open(buffer) as frame:
        return frame.convert("RGB").copy()


def _focus_load_frame(path):
    with Image.open(path) as frame:
        return frame.convert("RGB").copy()


def _focus_save_frame(path, frame, quality=90):
    """Atomically replace a tile with the fused frame."""
    temporary = str(path) + ".focus-tmp"
    # CaptureObject reload depends on the original OpenFlexure EXIF payload.
    # Preserve it when replacing a retained scan tile; disposable retake
    # previews simply have no EXIF and are written normally.
    exif = None
    try:
        with Image.open(path) as original:
            exif = original.info.get("exif")
    except Exception:
        pass
    save_kwargs = {"format": "JPEG", "quality": quality, "optimize": False}
    if exif:
        save_kwargs["exif"] = exif
    frame.save(temporary, **save_kwargs)
    os.replace(temporary, path)


def _focus_box_blur(values, radius=3):
    """Small NumPy-only box blur; avoids requiring OpenCV on the Pi."""
    values = np.asarray(values, dtype=np.float32)
    radius = max(0, int(radius))
    if radius == 0:
        return values
    kernel = 2 * radius + 1
    padded = np.pad(values, ((radius, radius), (radius, radius)), mode="edge")
    integral = np.pad(padded, ((1, 0), (1, 0)), mode="constant")
    integral = integral.cumsum(axis=0).cumsum(axis=1)
    return (
        integral[kernel:, kernel:]
        - integral[:-kernel, kernel:]
        - integral[kernel:, :-kernel]
        + integral[:-kernel, :-kernel]
    ) / float(kernel * kernel)


def _focus_map(frame):
    """Absolute local Laplacian energy used to identify the sharp plane."""
    rgb = np.asarray(frame.convert("RGB"), dtype=np.float32)
    gray = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    lap = np.zeros_like(gray, dtype=np.float32)
    lap[1:-1, 1:-1] = np.abs(
        4.0 * gray[1:-1, 1:-1]
        - gray[:-2, 1:-1]
        - gray[2:, 1:-1]
        - gray[1:-1, :-2]
        - gray[1:-1, 2:]
    )
    return _focus_box_blur(lap, radius=3)


def _focus_block_scores(local_map):
    rows = FOCUS_BLOCK_ROWS
    cols = FOCUS_BLOCK_COLS
    height, width = local_map.shape[:2]
    values = np.zeros((rows, cols), dtype=np.float32)
    for row in range(rows):
        y0, y1 = row * height // rows, (row + 1) * height // rows
        for col in range(cols):
            x0, x1 = col * width // cols, (col + 1) * width // cols
            values[row, col] = float(np.mean(local_map[y0:y1, x0:x1]))
    return values


def _focus_frame_score(frame):
    """One bounded focus score for choosing a micro-correction plane."""
    local_map = _focus_map(frame)
    return float(np.percentile(local_map, 75))


def _focus_probe_report(frames, offsets):
    """Decide whether local best focus is distributed across Z planes."""
    maps = [_focus_map(frame) for frame in frames]
    scores = np.stack([_focus_block_scores(local) for local in maps], axis=0)
    center_index = list(offsets).index(0)
    best_index = np.argmax(scores, axis=0)
    best_score = np.max(scores, axis=0)
    center_score = scores[center_index]
    ratio = best_score / np.maximum(center_score, 1e-6)
    valid_floor = max(5.0, float(np.percentile(best_score, 40)) * 0.65)
    valid = best_score >= valid_floor
    alternate = valid & (best_index != center_index) & (ratio >= FOCUS_ALT_MARGIN)
    selected = best_index[valid]
    plane_offsets = [int(offsets[int(index)]) for index in selected]
    unique_planes = sorted(set(plane_offsets))
    z_span = (max(unique_planes) - min(unique_planes)) if unique_planes else 0
    valid_count = max(1, int(valid.sum()))
    alternate_fraction = float(alternate.sum()) / float(valid_count)
    median_gain = float(np.median(ratio[valid])) if valid.any() else 1.0
    baseline_map = maps[center_index]
    baseline_gray = np.asarray(frames[center_index].convert("L"), dtype=np.float32)
    baseline_score = float(np.percentile(baseline_map, 75))
    best_frame_score = max(_focus_frame_score(frame) for frame in frames)
    probe_gain = best_frame_score / max(baseline_score, 1e-6)
    tilt_triggered = bool(
        valid.sum() >= 4
        and len(unique_planes) >= 2
        and z_span >= FOCUS_MIN_Z_SPAN
        and alternate_fraction >= FOCUS_ALT_FRACTION
    )
    # ``valid`` deliberately has a conservative absolute floor.  When it
    # rejects almost every block but the frame still contains real spatial
    # variation, native Fast-AF likely landed on a globally wrong plane rather
    # than on a field that is simply empty.  The relative-gain clause catches
    # a textured field whose blocks all agree on one non-native probe plane.
    global_defocus = bool(
        not tilt_triggered
        and float(baseline_gray.std()) >= FOCUS_GLOBAL_MIN_TEXTURE_STD
        and best_frame_score >= FOCUS_GLOBAL_MIN_BEST_SCORE
        and (
            int(valid.sum()) < FOCUS_GLOBAL_MIN_VALID_BLOCKS
            or probe_gain >= FOCUS_ALT_MARGIN
        )
    )
    # A uniform field can be globally out of focus even though it has no
    # multi-plane/tilt signature. The old policy discarded the useful probe
    # simply because every block agreed on the same non-zero plane. Treat a
    # strong, consistent edge-plane win as a cheap rescue.
    uniform_rescue = bool(
        valid.sum() >= 4
        and len(unique_planes) == 1
        and unique_planes[0] != 0
        and alternate_fraction >= 0.60
        and median_gain >= 1.25
    )
    # A uniform winning probe is still a globally wrong landing, not a third
    # focus strategy.  Route it through progressive Fast-AF when a factory is
    # available; the probe remains a useful fallback for old callers.
    global_defocus = bool(global_defocus or (uniform_rescue and not tilt_triggered))
    triggered = bool(tilt_triggered)
    return {
        "method": "adaptive-focus-classifier-v2",
        "triggered": triggered,
        "tilt_triggered": tilt_triggered,
        "uniform_rescue": uniform_rescue,
        "global_defocus": global_defocus,
        "focus_class": (
            "multi-plane"
            if tilt_triggered
            else "uniform-rescue"
            if uniform_rescue
            else "global-defocus"
            if global_defocus
            else "native"
        ),
        "probe_offsets": [int(value) for value in offsets],
        "best_plane_offsets": unique_planes,
        "best_plane_count": len(unique_planes),
        "valid_block_count": int(valid.sum()),
        "alternate_block_fraction": alternate_fraction,
        "median_probe_gain": median_gain,
        "probe_gain": float(probe_gain),
        "baseline_focus_score": baseline_score,
        "best_probe_focus_score": float(best_frame_score),
        "baseline_texture_std": float(baseline_gray.std()),
        "z_span": int(z_span),
        "probe_block_shape": [FOCUS_BLOCK_ROWS, FOCUS_BLOCK_COLS],
    }


def _focus_fuse(frames, offsets):
    """Fuse a small Z-stack with bounded memory on the Pi.

    The first pass finds the per-pixel focus-map maximum.  The second pass
    accumulates one frame at a time, so a 7-plane stack never becomes a large
    ``(planes, height, width, channels)`` allocation.  This matters on the
    Pi, where the camera process and OpenFlexure server already consume most
    of the available memory.
    """
    maximum = None
    for frame in frames:
        local_map = _focus_map(frame)
        if maximum is None:
            maximum = local_map
        else:
            np.maximum(maximum, local_map, out=maximum)
    if maximum is None:
        raise ValueError("focus stack is empty")

    temperature = max(1.0, 0.03 * float(np.percentile(maximum, 95)))
    height, width = maximum.shape
    weighted_sum = np.zeros((height, width, 3), dtype=np.float32)
    weight_sum = np.zeros((height, width), dtype=np.float32)
    for frame in frames:
        local_map = _focus_map(frame)
        weights = np.exp(
            np.clip((local_map - maximum) / temperature, -20.0, 0.0)
        ).astype(np.float32, copy=False)
        weights = _focus_box_blur(weights, radius=1)
        rgb = np.asarray(frame.convert("RGB"), dtype=np.float32)
        rgb *= weights[:, :, None]
        weighted_sum += rgb
        weight_sum += weights

    fused = weighted_sum / np.maximum(weight_sum[:, :, None], 1e-6)
    output = Image.fromarray(np.clip(fused, 0, 255).astype(np.uint8), mode="RGB")
    del maximum, weighted_sum, weight_sum, fused
    gc.collect()
    return output


def _native_focus_frame(microscope):
    buffer = io.BytesIO()
    microscope.camera.capture(buffer, fmt="jpeg", use_video_port=False, resize=None, bayer=False)
    buffer.seek(0)
    with Image.open(buffer) as frame:
        return frame.convert("RGB")


def _fuse_native_focus(microscope, path, frames, offsets, position):
    # Compute weights from small probes. Composite at sensor resolution with
    # one source image and one accumulated image in memory at a time.
    maps = [_focus_map(frames[offset]) for offset in offsets]
    maximum = np.maximum.reduce(maps)
    temperature = max(1.0, 0.03 * float(np.percentile(maximum, 95)))
    total = np.zeros_like(maximum)
    with Image.open(path) as baseline:
        size = baseline.size
    output = None
    x, y, z = position
    try:
        for offset, local_map in zip(offsets, maps):
            if _focus_action_stopped():
                raise RuntimeError("Adaptive focus cancelled")
            if offset == 0:
                with Image.open(path) as baseline:
                    source = baseline.convert("RGB")
            else:
                microscope.stage.move_abs((x, y, z + offset))
                source = _native_focus_frame(microscope)
            if source.size != size:
                source.close()
                raise ValueError("Focus frame size changed")
            weights = _focus_box_blur(np.exp(np.clip((local_map-maximum)/temperature, -20, 0)), 1)
            total += weights
            if output is None:
                output = source
                continue
            mask = Image.fromarray(np.uint8(np.clip(weights / total, 0, 1) * 255))
            mask = mask.resize(size, Image.BILINEAR)
            merged = Image.composite(source, output, mask)
            mask.close()
            source.close()
            output.close()
            output = merged
        _focus_save_frame(path, output, quality=microscope.camera.jpeg_quality)
    finally:
        if output is not None:
            output.close()
        microscope.stage.move_abs(position)


def adaptive_focus_capture(
    microscope,
    baseline_path=None,
    autofocus=None,
    autofocus_factory=None,
    autofocus_dz=2000,
    native_resolution=False,
):
    """Classify one native Fast-AF tile and apply one targeted rescue.

    Every tile has the same entry point: OpenFlexure's native Fast AF has
    already run once, then a three-plane clarity probe classifies the saved
    image.  A multi-plane field gets a small in-memory focus-plane fusion;
    globally wrong focus gets progressive native Fast AF with wider search
    windows.  Probe, rescue, and stack frames never become source tiles.
    """
    initial = list(microscope.stage.position)
    x, y, z0 = [int(value) for value in initial]
    if baseline_path and native_resolution:
        with Image.open(baseline_path) as source:
            baseline = _load_resized(source, SAFE_RESIZE[0]).resize(SAFE_RESIZE, Image.BILINEAR)
    else:
        baseline = _focus_load_frame(baseline_path) if baseline_path else _focus_capture_frame(microscope)

    def save_best(frame, offset):
        if not baseline_path:
            return
        if not native_resolution:
            _focus_save_frame(baseline_path, frame)
        elif offset:
            with _native_focus_frame(microscope) as native:
                _focus_save_frame(baseline_path, native, quality=microscope.camera.jpeg_quality)
    probe = {0: baseline}
    try:
        for offset in FOCUS_PROBE_OFFSETS:
            if offset == 0:
                continue
            if _focus_action_stopped():
                raise RuntimeError("Adaptive focus cancelled")
            microscope.stage.move_abs((x, y, z0 + int(offset)))
            probe[int(offset)] = _focus_capture_frame(microscope, size=baseline.size)
    finally:
        microscope.stage.move_abs((x, y, z0))

    ordered_probe_offsets = list(FOCUS_PROBE_OFFSETS)
    ordered_probe = [probe[offset] for offset in ordered_probe_offsets]
    report = _focus_probe_report(ordered_probe, ordered_probe_offsets)
    report["native_stage_position"] = [x, y, z0]
    if report.get("global_defocus") and (autofocus_factory or autofocus is not None):
        # The probe says the whole field is on the wrong focus basin.  Reuse
        # the native Fast-AF implementation with progressively wider windows;
        # stop on the first non-improving pass and keep the best disposable
        # frame.  This avoids a blind fixed retry and avoids the Pi-side
        # memory cost of a full stack for a field that is uniformly blurred.
        retry_report = {
            "attempted": True,
            "levels": [],
            "scores": [],
            "completed": 0,
            "stop_reason": None,
        }
        best_frame = baseline
        best_score = float(report.get("baseline_focus_score") or _focus_frame_score(baseline))
        best_position = (x, y, z0)

        # A winning probe plane is a useful candidate, but AF still gets the
        # first chance to refine it.  Starting with it also means a factory
        # failure never makes us throw away an otherwise useful probe.
        probe_scores = {
            int(offset): _focus_frame_score(frame)
            for offset, frame in probe.items()
        }
        if probe_scores:
            probe_offset = max(probe_scores, key=probe_scores.get)
            if float(probe_scores[probe_offset]) > best_score:
                best_frame = probe[probe_offset]
                best_score = float(probe_scores[probe_offset])
                best_position = (x, y, z0 + int(probe_offset))

        base_dz = max(200, int(autofocus_dz or 2000))
        levels = [
            min(FOCUS_PROGRESSIVE_MAX_DZ, base_dz * int(multiplier))
            for multiplier in FOCUS_PROGRESSIVE_MULTIPLIERS
        ]
        levels = list(dict.fromkeys(level for level in levels if level > base_dz))
        if not levels and autofocus is not None:
            # Compatibility fallback for old callers that can provide only a
            # prebuilt native callable.
            levels = [base_dz]

        try:
            for level_index, dz in enumerate(levels):
                if _focus_action_stopped():
                    raise RuntimeError("Adaptive focus cancelled")
                candidate_autofocus = (
                    autofocus_factory(int(dz))
                    if autofocus_factory is not None
                    else autofocus
                )
                if candidate_autofocus is None:
                    retry_report["stop_reason"] = "no-autofocus-callable"
                    break
                candidate_autofocus()
                retry_report["completed"] = int(retry_report["completed"]) + 1
                candidate_position = tuple(int(value) for value in microscope.stage.position)
                candidate = _focus_capture_frame(microscope, size=baseline.size)
                candidate_score = float(_focus_frame_score(candidate))
                retry_report["levels"].append(int(dz))
                retry_report["scores"].append(candidate_score)
                if candidate_score >= best_score * FOCUS_PROGRESSIVE_MIN_GAIN:
                    best_frame = candidate
                    best_score = candidate_score
                    best_position = candidate_position
                    continue
                retry_report["stop_reason"] = "clarity-gain-below-threshold"
                break
            if retry_report["stop_reason"] is None:
                retry_report["stop_reason"] = "progressive-levels-complete"
            microscope.stage.move_abs(best_position)
            save_best(best_frame, best_position[2] - z0)
            report.update(
                {
                    "method": "adaptive-progressive-fast-af-v1",
                    "focus_class": "global-defocus",
                    "triggered": True,
                    "fused": False,
                    "global_af_retry": retry_report,
                    "progressive_af": retry_report,
                    "selected_offset": int(best_position[2] - z0),
                    "selected_focus_score": float(best_score),
                    "native_stage_position": [x, y, z0],
                    "selected_stage_position": list(best_position),
                    "stack_plane_count": 1,
                    "probe_frames_persisted": 0,
                }
            )
            del probe_scores, best_frame, probe
            gc.collect()
            return report
        except Exception as exc:
            retry_report["warning"] = str(exc)
            report["global_af_retry"] = retry_report
            try:
                microscope.stage.move_abs(best_position)
            except Exception:
                logging.exception("failed restoring stage after progressive focus failure")
            logging.warning("progressive global focus recovery failed; keeping native tile: %s", exc)

    if report.get("uniform_rescue") and not report.get("global_defocus"):
        # Compatibility path for callers that do not expose an AF factory.
        # Normal scan paths route uniform wins through progressive Fast AF.
        best_offset = int(report["best_plane_offsets"][0])
        if native_resolution and best_offset:
            microscope.stage.move_abs((x, y, z0 + best_offset))
            try:
                save_best(probe[best_offset], best_offset)
            finally:
                microscope.stage.move_abs((x, y, z0))
        else:
            save_best(probe[best_offset], best_offset)
        report.update(
            {
                "method": "adaptive-uniform-rescue-v1",
                "selected_offset": best_offset,
                "stack_plane_count": 1,
                "fused": bool(baseline_path),
                "probe_frames_persisted": 0,
            }
        )
        return report
    if not report["triggered"]:
        del ordered_probe, probe
        gc.collect()
        return report

    frames = dict(probe)
    try:
        for offset in FOCUS_STACK_OFFSETS:
            offset = int(offset)
            if offset in frames:
                continue
            if _focus_action_stopped():
                raise RuntimeError("Adaptive focus cancelled")
            microscope.stage.move_abs((x, y, z0 + offset))
            frames[offset] = _focus_capture_frame(microscope, size=baseline.size)
    finally:
        microscope.stage.move_abs((x, y, z0))

    offsets = [int(value) for value in FOCUS_STACK_OFFSETS]
    if native_resolution and baseline_path:
        _fuse_native_focus(microscope, baseline_path, frames, offsets, (x, y, z0))
    else:
        fused = _focus_fuse([frames[offset] for offset in offsets], offsets)
        if baseline_path:
            _focus_save_frame(baseline_path, fused)
    report.update(
        {
            "method": "adaptive-tilt-stack-v1",
            "focus_class": "multi-plane",
            "stack_offsets": offsets,
            "stack_plane_count": len(offsets),
            "fused": True,
            "probe_frames_persisted": 0,
        }
    )
    del frames, probe, ordered_probe
    gc.collect()
    return report


def _focus_action_stopped():
    try:
        from labthings import current_action

        action = current_action()
        return bool(action and action.stopped)
    except Exception:
        return False


class FocusQualityWarning(RuntimeError):
    """Advisory image-quality outcome, not a hardware/control failure."""


def quality_autofocus(microscope):
    """Bounded +/-300 step search, same-direction landing and fresh-frame acceptance.

    Experimental local search, not a travel calibration or absolute focus guarantee.
    A failed acceptance stops the scan rather than silently publishing a bad tile.
    """
    from labthings import current_action
    home = tuple(microscope.stage.position)
    samples = []

    def check_stop():
        action = current_action()
        if action and action.stopped:
            raise RuntimeError("Quality autofocus cancelled")

    def move(z):
        check_stop()
        # Explicit bounds include the 200-step same-direction approach margin.
        if not home[2]-500 <= z <= home[2]+300:
            raise ValueError("Quality autofocus exceeded local envelope")
        while abs(z-microscope.stage.position[2]) > 250:
            check_stop()
            current = microscope.stage.position[2]
            microscope.stage.move_abs((home[0], home[1], current + (250 if z > current else -250)))
        microscope.stage.move_abs((home[0], home[1], int(z)))

    def stable():
        previous = None
        consecutive = 0
        for _ in range(8):
            check_stop()
            time.sleep(.15)
            frame = quality_focus_frame(microscope)
            if previous is not None:
                error = float(np.mean(np.abs(frame-previous)))
                consecutive = consecutive+1 if error < 2.0 else 0
                if consecutive >= 2:
                    return frame
            previous = frame
        raise FocusQualityWarning("Camera did not settle")

    def sweep(zs):
        zs = sorted(set(int(z) for z in zs))
        move(zs[0]-200)
        for z in zs:
            move(z)
            score = quality_focus_score(stable())
            samples.append({"z": z, "score": score})

    sweep(range(home[2]-150, home[2]+151, 50))
    best = max(samples, key=lambda s: s['score'])
    if best['z'] in (home[2]-150, home[2]+150):
        # One bounded expansion only; never search unboundedly for a peak.
        sweep(range(home[2]-300, home[2]+301, 50))
        best = max(samples, key=lambda s: s['score'])
    if abs(best['z']-home[2]) == 300:
        raise FocusQualityWarning("Focus peak at search boundary; operator review suggested")
    sweep(range(max(home[2]-300, best['z']-40), min(home[2]+300, best['z']+40)+1, 20))
    best = max(samples, key=lambda s: s['score'])
    for attempt in range(2):
        move(best['z']-200)
        move(best['z'])
        final = quality_focus_score(stable())
        logging.info("Focus landing check: %s", json.dumps({"samples": samples, "best": best, "final_score": final, "attempt": attempt}))
        if final >= .8*best['score'] and best['score'] >= 10:
            result = {"method": "bounded-verified-v1", "samples": samples,
                      "position": list(microscope.stage.position), "score": final,
                      "peak_score": best['score'], "accepted": True, "retry": attempt}
            logging.info("Quality autofocus accepted: %s", json.dumps(result))
            return result
    raise FocusQualityWarning("Focus landing verification below target")


def field_is_empty(microscope, size=(320, 240)):
    """Heuristic empty/background detector (frontier edge stop).

    Prefer org.openflexure.background-detect when installed; otherwise use
    low Laplacian variance + low intensity std (featureless field).
    """
    try:
        bg_ext = find_extension("org.openflexure.background-detect")
        if bg_ext and hasattr(bg_ext, "grab_and_classify_image"):
            verdict = bg_ext.grab_and_classify_image()
            if isinstance(verdict, dict):
                return verdict.get("classification") == "background"
    except Exception:
        logging.debug("background-detect unavailable", exc_info=True)

    try:
        g = grab_gray_frame(microscope, size=size)
        if g.shape[0] < 8 or g.shape[1] < 8:
            return False
        # Downsample for speed
        im = Image.fromarray(np.clip(g, 0, 255).astype(np.uint8)).resize(
            (80, 60), Image.BILINEAR
        )
        g2 = np.asarray(im, dtype=np.float32)
        lap = (
            g2[1:-1, 1:-1] * (-4.0)
            + g2[:-2, 1:-1]
            + g2[2:, 1:-1]
            + g2[1:-1, :-2]
            + g2[1:-1, 2:]
        )
        var = float(lap.var())
        std = float(g2.std())
        empty = var < EMPTY_LAP_VAR_MAX and std < EMPTY_STD_MAX
        logging.info(
            "empty-heuristic var=%.1f std=%.1f → %s", var, std, empty
        )
        return empty
    except Exception:
        logging.exception("field_is_empty failed")
        return False


def measure_frame_shift(a, b):
    """Return (dx, dy, response) via OpenCV phaseCorrelate (high-pass)."""
    cv2 = _get_cv2()
    if cv2 is None:
        return 0.0, 0.0, 0.0
    h, w = a.shape
    k = max(5, (min(h, w) // 16) | 1)
    ah = a - cv2.GaussianBlur(a, (k, k), 0)
    bh = b - cv2.GaussianBlur(b, (k, k), 0)
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (dx, dy), resp = cv2.phaseCorrelate(ah, bh, win)
    return float(dx), float(dy), float(resp)


def calibrate_stride(
    microscope,
    target_overlap=DEFAULT_OVERLAP,
    probe_steps=CALIB_PROBE_STEPS,
    frame_size=SAFE_RESIZE,
):
    """Move ±probe, measure px/step, choose stride for target overlap.

    Returns dict with px_per_step, stride_xy, scores; restores stage home.
    """
    home = tuple(int(v) for v in microscope.stage.position)
    probe = int(probe_steps)
    fov_w, fov_h = int(frame_size[0]), int(frame_size[1])
    result = {
        "ok": False,
        "px_per_step": list(CAL_PX_PER_STEP),
        "stride_xy": None,
        "scores": (0.0, 0.0),
        "shifts": None,
        "target_overlap": float(target_overlap),
        "probe_steps": probe,
        "frame_size": [fov_w, fov_h],
    }

    def _restore():
        try:
            microscope.stage.move_abs(home)
        except Exception:
            logging.exception("Failed restoring stage after calibration")

    try:
        time.sleep(0.1)
        a0 = grab_gray_frame(microscope, frame_size)

        microscope.stage.move_rel((probe, 0, 0))
        time.sleep(0.2)
        ax = grab_gray_frame(microscope, frame_size)
        dx, dy, rx = measure_frame_shift(a0, ax)

        microscope.stage.move_abs(home)
        time.sleep(0.2)
        a0y = grab_gray_frame(microscope, frame_size)
        microscope.stage.move_rel((0, probe, 0))
        time.sleep(0.2)
        ay = grab_gray_frame(microscope, frame_size)
        dx2, dy2, ry = measure_frame_shift(a0y, ay)

        _restore()
        time.sleep(0.1)

        # Primary-axis magnitudes (px per stage step)
        pps_x = abs(dx) / float(probe) if abs(dx) > 2 else float(CAL_PX_PER_STEP[0])
        pps_y = abs(dy2) / float(probe) if abs(dy2) > 2 else float(CAL_PX_PER_STEP[1])
        # Reject nonsense (too small / huge)
        if not (0.02 <= pps_x <= 2.0):
            pps_x = float(CAL_PX_PER_STEP[0])
            rx = 0.0
        if not (0.02 <= pps_y <= 2.0):
            pps_y = float(CAL_PX_PER_STEP[1])
            ry = 0.0

        ov = min(0.55, max(0.10, float(target_overlap)))
        sx = int(round((fov_w * (1.0 - ov)) / max(pps_x, 1e-6)))
        sy = int(round((fov_h * (1.0 - ov)) / max(pps_y, 1e-6)))
        sx = int(max(STRIDE_MIN, min(STRIDE_MAX, sx)))
        sy = int(max(STRIDE_MIN, min(STRIDE_MAX, sy)))

        ok = (rx > 0.08 and ry > 0.08) or (abs(dx) > 2 and abs(dy2) > 2)
        # Opposite of image shift: +stage that moves features right/down means
        # the newly captured neighbor belongs left/above on the mosaic.
        # (Validated on this OFM; do not flip casually — breaks all grids.)
        axis_x = -1 if (abs(dx) <= 2 or dx >= 0) else 1
        axis_y = -1 if (abs(dy2) <= 2 or dy2 >= 0) else 1
        result.update(
            {
                "ok": bool(ok),
                "px_per_step": [pps_x, pps_y],
                "stride_xy": [sx, sy],
                "scores": [rx, ry],
                "shifts": [[dx, dy], [dx2, dy2]],
                "axis_sign": [axis_x, axis_y],
            }
        )
        logging.info(
            "stride calib: probe=%s overlap=%.2f pps=(%.4f,%.4f) "
            "stride=(%s,%s) scores=(%.3f,%.3f) shifts=%s axis_sign=%s",
            probe,
            ov,
            pps_x,
            pps_y,
            sx,
            sy,
            rx,
            ry,
            result["shifts"],
            result["axis_sign"],
        )
        return result
    except Exception:
        logging.exception("stride calibration failed; using fallback")
        _restore()
        pps = list(CAL_PX_PER_STEP)
        ov = float(target_overlap)
        sx = int(round((fov_w * (1.0 - ov)) / max(pps[0], 1e-6)))
        sy = int(round((fov_h * (1.0 - ov)) / max(pps[1], 1e-6)))
        result.update(
            {
                "ok": False,
                "px_per_step": pps,
                "stride_xy": [
                    int(max(STRIDE_MIN, min(STRIDE_MAX, sx))),
                    int(max(STRIDE_MIN, min(STRIDE_MAX, sy))),
                ],
                "axis_sign": [-1, -1],
            }
        )
        return result


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
    quality_out=None,
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
    rejected_edges = []
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
        min_sc = max(0.35, _align_min_score(pair_level))
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
        if use_spatial:
            rejected_edges.append({"a": list(a), "b": list(b), "score": float(sc), "reason": reason})
        report(5 + int(80.0 * (ei + 1) / float(n_e)))

    seed = origin if origin in tiles else keys[0]
    pos, tree_n = _mst_positions(keys, edge_list, seed, grid_xy)
    residuals = [float(np.hypot(pos[b][0]-pos[a][0]-dx, pos[b][1]-pos[a][1]-dy))
                 for cost, a, b, dx, dy, sc in edge_list if cost < 0]
    max_residual = max(residuals or [0.0])
    if quality_out is not None:
        quality_out.update({"accepted": not rejected_edges and max_residual <= 4.0,
                            "rejected_edges": rejected_edges, "max_residual_px": max_residual,
                            "min_ncc": 0.35, "residual_limit_px": 4.0})

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
):
    """Stitch with overlap phase-correlation align + sharpness-weighted blend.

    align: True/'full', 'fine' (warm), 'light' (preview), or False/'none'.
    prior_positions: optional draft placements to warm-start refine.
    cache_out: if dict, filled with positions/tile_w/med for later refine.
    axis_sign: (sx, sy) mosaic direction for +stage-grid index.
    """
    import gc
    if cells and _normalize_align(align) != "none":
        try:
            import importlib.util
            module_name = "microworld_registration"
            if module_name not in sys.modules:
                spec = importlib.util.spec_from_file_location(module_name, os.path.join(os.path.dirname(__file__), module_name + ".py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                sys.modules[module_name] = module
            return sys.modules[module_name].stitch_cells_hq(
                cells, nx, ny, tile_w, stride_size=stride_size, px_per_step=px_per_step,
                axis_sign=axis_sign, cache_out=cache_out, progress_cb=progress_cb)
        except Exception:
            logging.exception("Hybrid registration unavailable; rendering stage/legacy Slide")

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
    alignment_quality = {}
    positions, med_x, med_y = _estimate_positions(
        tiles,
        guess_x,
        guess_y,
        refine=align_level,
        progress_cb=progress_cb,
        prior_positions=prior_positions,
        prior_tile_w=prior_tile_w,
        tile_w=tw,
        axis_sign=axis_sign,
        quality_out=alignment_quality,
    )

    alignment_quality["policy"] = "advisory-only-always-slide"

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
        positions, med_x, med_y = _estimate_positions(
            tiles,
            guess_x,
            guess_y,
            refine=align_level,
            progress_cb=progress_cb,
            prior_positions=prior_positions,
            prior_tile_w=prior_tile_w,
            tile_w=tw,
            axis_sign=axis_sign,
            quality_out=alignment_quality,
        )
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
            cache_out["alignment_quality"] = alignment_quality
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


def build_exif_bytes(capture_id, name, tags, annotations, dataset=None, thumb_im=None):
    if piexif is None:
        return b""
    meta = {
        "dataset": dataset or {},
        "image": {
            "id": str(capture_id),
            "name": name,
            "time": datetime.datetime.now().isoformat(),
            "format": "jpeg",
            "tags": list(tags or []),
            "annotations": dict(annotations or {}),
        },
    }
    zeroth = {
        piexif.ImageIFD.Software: "MicroWorld scan-mosaic".encode("utf-8"),
        piexif.ImageIFD.ImageDescription: b"live mosaic",
    }
    exif = {
        piexif.ExifIFD.UserComment: json.dumps(meta).encode("utf-8"),
        piexif.ExifIFD.DateTimeOriginal: datetime.datetime.now()
        .strftime("%Y:%m:%d %H:%M:%S")
        .encode("utf-8"),
    }
    thumb_bytes = None
    if thumb_im is not None:
        buf = io.BytesIO()
        t = thumb_im.copy()
        t.thumbnail((200, 150))
        t.save(buf, format="JPEG", quality=80)
        thumb_bytes = buf.getvalue()
    dump = {"0th": zeroth, "Exif": exif, "1st": {}, "thumbnail": thumb_bytes}
    return piexif.dump(dump)


def save_jpeg_with_meta(path, image, capture_id, name, tags, annotations, dataset=None):
    path = str(path)
    exif_bytes = build_exif_bytes(
        capture_id, name, tags, annotations, dataset=dataset, thumb_im=image
    )
    if exif_bytes:
        image.save(path, format="JPEG", quality=88, optimize=True, exif=exif_bytes)
    else:
        image.save(path, format="JPEG", quality=88, optimize=True)


def register_capture(microscope, path):
    """Load CaptureObject from path and attach to CaptureManager.images."""
    try:
        from openflexure_microscope.captures.capture import capture_from_path

        cap = capture_from_path(str(path))
    except Exception:
        logging.exception("capture_from_path failed for %s", path)
        cap = None
    if cap is None:
        return None
    try:
        images = microscope.captures.images
        images[str(cap.id)] = cap
        if hasattr(cap, "on_delete") and getattr(cap, "on_delete", None) is None:
            cap.on_delete = microscope.captures.remove_image
    except Exception:
        logging.exception("Failed to attach capture to captures.images")
        return None
    return cap


def list_captures(microscope):
    try:
        images = microscope.captures.images
    except Exception:
        images = None
    if images is None:
        return []
    if isinstance(images, dict):
        return list(images.values())
    try:
        return list(images)
    except TypeError:
        return []


def capture_manager(microscope):
    return getattr(microscope, "captures", None)


class MosaicScanState(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.basename = None
        self.dataset_id = None
        self.folder = None
        self.mapping = {}
        self.nx = 1
        self.ny = 1
        self.stride_size = (2000, 1500, 100)
        self.px_per_step = list(CAL_PX_PER_STEP)
        self.axis_sign = [-1, -1]  # mosaic dir for +grid; opposite of camera feature shift
        self.calib = None
        self.alignment_quality = {}
        self.tile_metadata = {}
        self.best = {}  # (ix,iy) -> (score, path)
        self.mosaic_path = None
        self.mosaic_id = None
        self.mosaic_name = None
        self.mosaic_cap = None
        self.rev = 0
        self.last_write = 0.0
        self.tile_count = 0
        self.phase = "idle"  # idle|calibrating|scanning|stitching|done|error
        self.preview_path = os.path.join(PREVIEW_DIR, PREVIEW_NAME)
        self.keep_tiles = False
        # When the Mac worker is available the Pi publishes only a bounded
        # current-tile preview.  The source JPEG remains on disk for Mac-side
        # preview/refine, but the Pi never builds an accumulated mosaic.
        self.mac_preview_only = False
        self.auto_refine = False
        self.initial_xy = None
        self.origin_xy = None  # stable origin for append sessions
        self.session_id = None
        self.view_id = None
        self.refined = False
        self.min_ix = 0
        self.min_iy = 0
        self.refine_progress = 0
        self.last_grid = [3, 3, 1]  # last Start/Continue chunk shape
        self.plan_bounds = None  # (i0, j0, nx, ny) live UI grid while scanning
        self.draft_positions = None  # {(ix,iy):(x,y)} from light stitch
        self.draft_tile_w = None
        self.draft_tile_h = None
        self.draft_canvas = None  # (cw, ch)
        self.draft_med = None
        self.empty_cells = set()  # (ix,iy) known empty/background (explore)
        self.slide_id = None
        self.slide_path = None
        self.slide_name = None
        self.slide_saved = False
        self.live_alignment_count = 0
        self.focus_phase = "idle"  # idle|initializing|scanning
        self.focus_attempt = 0
        self.focus_attempts = 0
        self.center_tile_path = None
        self.center_tile_metadata = None
        self.center_tile_spec = None


class ScanMosaicExtension(BaseExtension):
    def __init__(self):
        BaseExtension.__init__(self, EXTENSION_ID, version=EXTENSION_VERSION)
        self.state = MosaicScanState()
        self.hardware = HardwareGate(os.path.join(PREVIEW_DIR, "hardware.json"))
        self._hardware_hooks_ready = False
        self._stage_hardware_hook_ready = False
        self._autofocus_hardware_hook_ready = False
        self._stock_tile = None
        self._stock_patched = False
        self._focus_reports = {}
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        self.register_views()
        try:
            self.load_session_state()
        except Exception:
            logging.debug("session state restore skipped", exc_info=True)

        def _late_patch():
            for _ in range(60):
                time.sleep(1.0)
                try:
                    if self.patch_stock_scan() and self.ensure_hardware_hooks():
                        return
                except Exception:
                    logging.debug("late mosaic patch retry", exc_info=True)

        try:
            import threading

            threading.Thread(
                target=_late_patch, name="microworld-mosaic-patch", daemon=True
            ).start()
        except Exception:
            logging.exception("Could not start mosaic patch thread")

    def register_views(self):
        self.add_view(StatusView, "/status")
        self.add_view(HardwareStatusView, "/hardware")
        self.add_view(ConnectionView, "/connection")
        self.add_view(CaptureListView, "/captures")
        self.add_view(LiveDetailsView, "/live_details")
        self.add_view(PreviewStitchView, "/latest_preview_stitch.jpg")
        self.add_view(PreviewUiView, "/preview_ui")
        self.add_view(TileScanAPI, "/tile", endpoint="tile")
        self.add_view(AdaptiveAutofocusView, "/adaptive_autofocus", endpoint="adaptive_autofocus")
        self.add_view(RecaptureTileView, "/recapture_tile", endpoint="recapture_tile")
        self.add_view(RecaptureImageView, "/recapture_image")
        self.add_view(TileAtView, "/tile_at")
        self.add_view(CenterTileView, "/center_tile")
        self.add_view(TileLayoutView, "/tile_layout")
        self.add_view(CleanupTilesView, "/cleanup_tiles")
        self.add_view(HydrateFromMacView, "/hydrate")
        self.add_view(CalibrateScaleView, "/calibrate_scale")
        self.add_view(RefineView, "/refine", endpoint="refine")
        self.add_view(MacPreviewView, "/mac/preview.jpg")
        self.add_view(MacPyramidInfoView, "/mac/pyramid/info.json")
        self.add_view(MacPyramidTileView, "/mac/pyramid/tile")
        self.add_view(MacTileView, "/mac/tile")
        self.add_view(MacLayoutView, "/mac/layout")
        self.add_view(MacRefineStatusView, "/mac/refine_status")
        self.add_view(MacRefineStartView, "/mac/refine")
        self.add_view(MacSessionView, "/mac/session")
        self.add_view(MacListView, "/mac/list")
        self.add_view(MacWorkspaceView, "/mac/workspace")
        self.add_view(MacViewsView, "/mac/views")
        self.add_view(MacCurrentViewView, "/mac/views/current")
        self.add_view(MacRecaptureView, "/mac/recapture")
        self.add_view(MacUiEventsView, "/mac/ui/events")
        self.add_view(MacAgentSnapshotView, "/mac/agent")

    def ensure_hardware_hooks(self):
        """Install the gate on stock stage and autofocus entry points.

        Extensions are discovered lazily by OpenFlexure, so this is safe to
        call from every status/action request until both components exist.
        The wrappers are idempotent.
        """
        try:
            microscope = find_component("org.openflexure.microscope")
            stage_ready = self.hardware.install_stage_hooks(microscope)
        except Exception:
            stage_ready = False
            logging.debug("stage hardware hook not ready", exc_info=True)
        try:
            autofocus = find_extension("org.openflexure.autofocus")
            autofocus_ready = self.hardware.install_autofocus_hooks(autofocus)
        except Exception:
            autofocus_ready = False
            logging.debug("autofocus hardware hook not ready", exc_info=True)
        self._stage_hardware_hook_ready = bool(stage_ready)
        self._autofocus_hardware_hook_ready = bool(autofocus_ready)
        self._hardware_hooks_ready = bool(stage_ready and autofocus_ready)
        return bool(stage_ready)

    def session_state_path(self):
        return os.path.join(PREVIEW_DIR, SESSION_STATE_NAME)

    def save_session_state(self):
        """Persist inspectable session after tiles-only cleanup / scan done."""
        st = self.state
        if not st.session_id:
            return
        payload = {
            "session_id": st.session_id,
            "view_id": getattr(st, "view_id", None),
            "basename": st.basename,
            "mosaic_id": st.mosaic_id,
            "mosaic_name": st.mosaic_name,
            "phase": st.phase or "done",
            "alignment_quality": getattr(st, "alignment_quality", {}),
            "tile_metadata": getattr(st, "tile_metadata", {}),
            "nx": st.nx,
            "ny": st.ny,
            "min_ix": st.min_ix,
            "min_iy": st.min_iy,
            "origin_xy": list(st.origin_xy) if st.origin_xy else None,
            "initial_xy": list(st.initial_xy) if st.initial_xy else None,
            "stride_size": list(st.stride_size) if st.stride_size else None,
            "px_per_step": list(st.px_per_step) if st.px_per_step else None,
            "axis_sign": list(st.axis_sign) if st.axis_sign else [-1, -1],
            "last_grid": list(st.last_grid) if st.last_grid else None,
            "refined": bool(st.refined),
            "preview_path": st.preview_path,
            "mac_preview_only": bool(getattr(st, "mac_preview_only", False)),
            "mac_tiles_only": len(st.best) == 0 and st.phase == "done",
            "center_tile": getattr(st, "center_tile_metadata", None),
        }
        path = self.session_state_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
        except Exception:
            logging.exception("Failed saving session state")

    def load_session_state(self):
        path = self.session_state_path()
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            logging.exception("Failed reading session state")
            return False
        sid = payload.get("session_id")
        if not sid:
            return False
        st = self.state
        if st.session_id and st.best:
            return False  # don't clobber an active scan
        st.session_id = sid
        st.view_id = payload.get("view_id") or getattr(st, "view_id", None)
        st.basename = payload.get("basename") or st.basename
        st.mosaic_id = payload.get("mosaic_id") or st.mosaic_id
        st.mosaic_name = payload.get("mosaic_name") or st.mosaic_name
        st.phase = payload.get("phase") or "done"
        st.alignment_quality = payload.get("alignment_quality") or {}
        st.tile_metadata = payload.get("tile_metadata") or {}
        if payload.get("nx"):
            st.nx = int(payload["nx"])
        if payload.get("ny"):
            st.ny = int(payload["ny"])
        st.min_ix = int(payload.get("min_ix") or 0)
        st.min_iy = int(payload.get("min_iy") or 0)
        if payload.get("origin_xy"):
            st.origin_xy = list(payload["origin_xy"])
        if payload.get("initial_xy"):
            st.initial_xy = list(payload["initial_xy"])
        if payload.get("stride_size"):
            st.stride_size = tuple(payload["stride_size"])
        if payload.get("px_per_step"):
            st.px_per_step = list(payload["px_per_step"])
        if payload.get("axis_sign"):
            st.axis_sign = list(payload["axis_sign"])
        if payload.get("last_grid"):
            st.last_grid = list(payload["last_grid"])
        st.refined = bool(payload.get("refined"))
        st.mac_preview_only = bool(payload.get("mac_preview_only", False))
        st.center_tile_metadata = payload.get("center_tile") or None
        if st.center_tile_metadata and st.center_tile_metadata.get("source_path"):
            candidate = str(st.center_tile_metadata["source_path"])
            if os.path.isfile(candidate):
                st.center_tile_path = candidate
        logging.info("Restored scan-mosaic session %s from disk", sid)
        return True

    def clear_session_state_file(self):
        path = self.session_state_path()
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass

    def hydrate_from_mac(self, session_id=None):
        """Restore Continue path from PC manifest — occupancy + stage math only.

        Does **not** pull tile JPEGs back to Pi. Fill-gaps only needs which
        (ix,iy) are already taken, plus origin/stride. New captures stay local;
        old cells remain on PC (preview/LOD via /mac/*).
        """
        st = self.state
        sid = (session_id or st.session_id or "").strip()
        if not sid:
            raise ValueError("session_id required to hydrate from PC")

        raw, _ = _proxy_mac("/mosaic/{}".format(sid), timeout=30)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("Invalid PC Worker manifest: {}".format(exc))
        man = payload.get("manifest") if isinstance(payload, dict) else None
        if not isinstance(man, dict):
            man = payload if isinstance(payload, dict) else {}
        cells = man.get("cells") or []
        if not cells:
            # Fall back to layout tile indices if cells list empty
            layout = man.get("layout") or {}
            cells = [
                {"ix": t.get("ix"), "iy": t.get("iy")}
                for t in (layout.get("tiles") or [])
                if t.get("ix") is not None and t.get("iy") is not None
            ]
        if not cells:
            raise ValueError("PC session {} has no tiles".format(sid))

        origin = man.get("origin_xy")
        stride = man.get("stride_size")
        if not origin or not stride or len(stride) < 2:
            raise ValueError("PC session missing origin/stride")

        best = {}
        mapping = {}
        sx = int(stride[0])
        sy = int(stride[1])
        ox = float(origin[0])
        oy = float(origin[1])
        ok = 0
        for c in cells:
            try:
                ix = int(c.get("ix"))
                iy = int(c.get("iy"))
            except (TypeError, ValueError):
                continue
            # path=None → occupied on PC only; stitch skips missing files
            best[(ix, iy)] = (0.0, None)
            stage_xy = (
                int(round(ox + ix * sx)),
                int(round(oy + iy * sy)),
            )
            mapping[stage_xy] = (ix, iy)
            ok += 1

        if ok < 1:
            raise ValueError("could not hydrate any PC cell indices")

        st.session_id = sid
        st.best = best
        st.mapping = mapping
        st.tile_count = len(best)
        st.origin_xy = [int(origin[0]), int(origin[1])]
        if man.get("initial_xy"):
            st.initial_xy = [int(man["initial_xy"][0]), int(man["initial_xy"][1])]
        else:
            st.initial_xy = list(st.origin_xy)
        st.stride_size = (
            int(stride[0]),
            int(stride[1]),
            int(stride[2]) if len(stride) > 2 else 100,
        )
        if man.get("px_per_step"):
            st.px_per_step = list(man["px_per_step"])
        if man.get("axis_sign"):
            st.axis_sign = [int(man["axis_sign"][0]), int(man["axis_sign"][1])]
        if man.get("last_grid"):
            st.last_grid = list(man["last_grid"])
        if man.get("basename"):
            st.basename = man["basename"]
        if man.get("dataset_id"):
            st.dataset_id = man["dataset_id"]
        center = man.get("center_tile") or next(
            (a for a in (man.get("canvas_assets") or []) if a.get("center_tile")),
            None,
        )
        st.center_tile_metadata = center or None
        st.keep_tiles = True
        st.phase = "done"
        st.refined = bool(man.get("refined"))
        self.refresh_grid_bounds()
        # One lightweight preview for UI (not per-tile sync)
        try:
            prev, _ = _proxy_mac(
                "/mosaic/{}/preview.jpg".format(sid), timeout=60
            )
            if prev and len(prev) > 32:
                path = st.preview_path or os.path.join(PREVIEW_DIR, PREVIEW_NAME)
                os.makedirs(os.path.dirname(path) or PREVIEW_DIR, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(prev)
                st.preview_path = path
        except Exception:
            logging.debug("hydrate preview pull failed", exc_info=True)
        try:
            self.save_session_state()
        except Exception:
            pass
        logging.info(
            "Hydrated Continue path from PC %s (%s occupied cells, no tile pull)",
            sid,
            len(best),
        )
        return {
            "ok": True,
            "session_id": sid,
            "cells": len(best),
            "tiles_pulled": False,
            "origin_xy": st.origin_xy,
            "stride_size": list(st.stride_size),
            "nx": st.nx,
            "ny": st.ny,
            "min_ix": st.min_ix,
            "min_iy": st.min_iy,
            "axis_sign": list(st.axis_sign) if st.axis_sign else None,
            "last_grid": list(st.last_grid) if st.last_grid else None,
        }

    def ensure_mosaic(self, microscope, basename):
        name_stem = "LIVE_MOSAIC_{}".format(basename)
        tags = ["mosaic", "live-scan"]
        annotations = {
            "source": EXTENSION_ID,
            "scan": basename,
            "rev": "0",
        }
        os.makedirs(MICROGRAPHS, exist_ok=True)

        cap = None
        try:
            cap = microscope.captures.new_image(
                temporary=False, filename=name_stem, folder="", fmt="jpeg"
            )
        except Exception:
            logging.exception("captures.new_image failed; falling back to file register")

        if cap is not None:
            path = cap.file
            capture_id = str(cap.id)
            name = cap.name
            placeholder = Image.new("RGB", (640, 480), (20, 20, 20))
            save_jpeg_with_meta(path, placeholder, capture_id, name, tags, annotations)
            try:
                # Re-sync tags/annotations through CaptureObject helpers
                if hasattr(cap, "put_tags"):
                    cap.put_tags(tags)
                if hasattr(cap, "put_annotations"):
                    cap.put_annotations(annotations)
            except Exception:
                logging.exception("Failed putting mosaic tags")
        else:
            name = "{}.jpeg".format(name_stem)
            path = os.path.join(MICROGRAPHS, name)
            capture_id = str(uuid.uuid4())
            placeholder = Image.new("RGB", (640, 480), (20, 20, 20))
            save_jpeg_with_meta(path, placeholder, capture_id, name, tags, annotations)
            cap = register_capture(microscope, path)
            if cap is not None:
                capture_id = str(cap.id)
                name = cap.name

        self.state.mosaic_path = path
        self.state.mosaic_id = str(capture_id)
        self.state.mosaic_name = name
        self.state.mosaic_cap = cap
        return path

    def write_preview_file(self, image):
        """v3-style lightweight preview.jpg for UI polling (no gallery EXIF)."""
        st = self.state
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        path = st.preview_path
        try:
            image.save(path, format="JPEG", quality=90, optimize=True)
        except Exception:
            logging.exception("Failed writing preview.jpg")

    def update_live_tile_preview(self, path):
        """Publish one bounded tile while the Mac owns live mosaic work.

        This is intentionally not a Gallery capture and never loads the
        accumulated tile set.  It gives a Pi-only fallback something useful
        to show, while the Mac worker assembles the real low-resolution live
        preview from the retained source files.
        """
        st = self.state
        now = time.time()
        if st.tile_count > 1 and (now - st.last_write) < LIVE_MIN_INTERVAL_S:
            return
        try:
            with Image.open(path) as source:
                image = _load_resized(source, 480, mode="RGB")
            temporary = st.preview_path + ".tile-tmp"
            image.save(temporary, format="JPEG", quality=76, optimize=False)
            os.replace(temporary, st.preview_path)
            image.close()
            st.rev += 1
            st.last_write = now
        except Exception:
            logging.exception("Failed writing current-tile preview")

    def release_scan_capture_reference(self, microscope, capture_obj):
        """Drop a capture object from the Pi manager without deleting its file.

        OpenFlexure's Gallery manager keeps Python objects for every capture.
        For Mac-owned scans those objects are not needed after the path and
        metadata have been recorded; the JPEGs remain available through
        ``tile_at`` and are later removed by the normal scan cleanup.
        """
        if not getattr(self.state, "mac_preview_only", False):
            return
        manager = capture_manager(microscope)
        images = getattr(manager, "images", None) if manager is not None else None
        if not isinstance(images, dict):
            return
        capture_id = str(getattr(capture_obj, "id", "") or "")
        for key, value in list(images.items()):
            if value is capture_obj or (capture_id and str(key) == capture_id):
                try:
                    del images[key]
                except Exception:
                    pass
                break

    def seed_start_preview(self, microscope):
        """Snapshot the Start-moment FOV so Scan view can open immediately."""
        try:
            im = grab_rgb_frame(microscope, size=SAFE_RESIZE)
            st = self.state
            w, h = im.size
            # 1×1 keeps capture resolution; multi-tile matches live stitch scale
            single = int(getattr(st, "nx", 1) or 1) <= 1 and int(
                getattr(st, "ny", 1) or 1
            ) <= 1
            # Multi-tile seed uses preview budget (not the old 320px crush)
            tw = preview_tile_width(1, st.nx, st.ny)
            if (not single) and w > tw:
                nh = max(1, int(round(h * (float(tw) / float(w)))))
                im = im.resize((tw, nh), Image.BILINEAR)
            self.write_preview_file(im)
            logging.info(
                "Seeded start FOV preview for session %s (single=%s size=%sx%s)",
                st.session_id,
                single,
                im.size[0],
                im.size[1],
            )
        except Exception:
            logging.exception("seed_start_preview failed")

    def update_mosaic_image(self, image, tags=None, annotations=None, force=False):
        st = self.state
        if not st.mosaic_path or not st.mosaic_id:
            return
        now = time.time()
        if not force and (now - st.last_write) < LIVE_MIN_INTERVAL_S and st.tile_count > 1:
            return
        st.rev += 1
        tags = tags or ["mosaic", "live-scan"]
        annotations = annotations or {}
        annotations = dict(annotations)
        annotations.update(
            {
                "source": EXTENSION_ID,
                "scan": st.basename or "",
                "rev": str(st.rev),
                "tiles": str(st.tile_count),
                "phase": st.phase,
            }
        )
        save_jpeg_with_meta(
            st.mosaic_path,
            image,
            st.mosaic_id,
            st.mosaic_name,
            tags,
            annotations,
        )
        self.write_preview_file(image)
        st.last_write = now
        # Refresh in-memory tags if possible
        cap = st.mosaic_cap
        if cap is not None:
            try:
                if hasattr(cap, "tags"):
                    cap.tags = list(tags)
                if hasattr(cap, "annotations"):
                    cap.annotations = dict(annotations)
            except Exception:
                pass

    def on_tile(self, microscope, capture_obj, dataset):
        st = self.state
        st.phase = "scanning"
        if dataset and dataset.get("id"):
            st.dataset_id = str(dataset.get("id"))
        if capture_obj is None:
            return
        path = getattr(capture_obj, "file", None) or getattr(capture_obj, "path", None)
        if not path or not os.path.isfile(path):
            return
        name = os.path.basename(path)
        m = NAME_PAT.match(name)
        if not m:
            # Sequential names contain no coordinates. The capture callback
            # runs before the scan advances the stage.
            x, y, z = (int(value) for value in microscope.stage.position)
            position_source = "stage"
        else:
            x, y, z = int(m.group("x")), int(m.group("y")), int(m.group("z"))
            position_source = "capture-filename"
        ij = self.index_from_stage(x, y)
        if ij is None:
            ij = nearest_index(st.mapping, x, y)
        if ij is None:
            return
        try:
            score = sharpness_path(path)
        except Exception:
            score = 0.0
        prev = st.best.get(ij)
        # Consume the one-shot focus report even when this tile is not sharper
        # than the retained tile.  Otherwise a rejected capture can leak into
        # a later capture with the same path during a Continue/retake flow.
        focus_report = self._focus_report_for_path(path)
        if prev is None or score >= prev[0]:
            st.best[ij] = (score, path)
            metadata = {
                "stage_position": [x,y,z], "position_source": position_source,
                "capture_id": str(getattr(capture_obj, "id", "")), "source_name": name,
                "captured_at": datetime.datetime.now().isoformat(), "units": "motor_steps"}
            if focus_report:
                metadata["adaptive_focus"] = focus_report
            st.tile_metadata["{},{}".format(*ij)] = metadata
        st.tile_count += 1
        st.refined = False
        self.refresh_grid_bounds()

        # With a Mac worker, this is the only Pi-side preview work during a
        # scan.  The worker downloads the retained JPEG and builds a bounded
        # stage preview on the Mac; the Pi must not repeatedly allocate an
        # accumulated canvas here.
        if getattr(st, "mac_preview_only", False) and st.keep_tiles:
            self.update_live_tile_preview(path)
            return

        # v3 starts live preview after a few images
        if len(st.best) < MIN_CELLS_FOR_PREVIEW:
            return

        cells = {k: v[1] for k, v in st.best.items() if v and v[1]}
        if not cells:
            return
        try:
            cache = {}
            tw = preview_tile_width(len(cells), st.nx, st.ny)
            st.live_alignment_count += 1
            # Fast path: render against the calibrated stage/grid coordinates.
            # This keeps the origin fixed and avoids re-registering the entire
            # accumulated mosaic for every incoming tile.
            # During acquisition, keep the preview responsive and deterministic:
            # place tiles from stage coordinates only.  Registration is deferred
            # to the single Mac-side global refine after the scan completes.
            live_align = False
            mosaic = stitch_cells(
                cells,
                st.nx,
                st.ny,
                tw,
                align=live_align,
                stride_size=st.stride_size,
                px_per_step=st.px_per_step,
                cache_out=cache,
                axis_sign=getattr(st, "axis_sign", None),
            )
            if cache.get("positions"):
                self._store_draft_cache(cache)
            logging.info(
                "live mosaic update: tiles=%s mode=stage alignment_pass=%s",
                len(cells),
                st.live_alignment_count,
            )
            self.update_mosaic_image(mosaic)
            del mosaic
        except Exception:
            logging.exception("live mosaic stitch failed")

    def apply_adaptive_focus(
        self,
        microscope,
        capture_obj,
        enabled=True,
        autofocus=None,
        autofocus_factory=None,
        autofocus_dz=2000,
        native_resolution=False,
    ):
        """Run the conditional Z-stack after native AF and before on_tile."""
        path = getattr(capture_obj, "file", None) or getattr(capture_obj, "path", None)
        if not enabled or not path or not os.path.isfile(path):
            return None
        path = str(path)
        try:
            report = adaptive_focus_capture(
                microscope,
                baseline_path=path,
                autofocus=autofocus,
                autofocus_factory=autofocus_factory,
                autofocus_dz=autofocus_dz,
                native_resolution=native_resolution,
            )
            self._focus_reports[path] = report
            if report.get("progressive_af"):
                logging.info(
                    "adaptive focus progressively recovered tile %s: "
                    "levels=%s selected_offset=%s score=%.2f",
                    os.path.basename(path),
                    report.get("progressive_af", {}).get("levels", []),
                    report.get("selected_offset", 0),
                    report.get("selected_focus_score", 0.0),
                )
            elif report.get("triggered"):
                logging.info(
                    "adaptive focus fused tile %s: z_span=%s alternate=%.2f gain=%.2f",
                    os.path.basename(path),
                    report.get("z_span"),
                    report.get("alternate_block_fraction", 0.0),
                    report.get("median_probe_gain", 1.0),
                )
            else:
                logging.info(
                    "adaptive focus kept native tile %s: alternate=%.2f gain=%.2f",
                    os.path.basename(path),
                    report.get("alternate_block_fraction", 0.0),
                    report.get("median_probe_gain", 1.0),
                )
            return report
        except Exception as exc:
            # A focus enhancement must never discard a valid native Fast-AF
            # capture. Keep the baseline tile and make the advisory visible in
            # metadata/logs for diagnosis.
            logging.exception("adaptive focus failed; keeping native tile %s", path)
            report = {
                "method": "adaptive-tilt-stack-v1",
                "triggered": False,
                "fused": False,
                "warning": str(exc),
            }
            self._focus_reports[path] = report
            return report

    def _focus_report_for_path(self, path):
        return self._focus_reports.pop(str(path), None)

    def _store_draft_cache(self, cache):
        st = self.state
        st.alignment_quality = cache.get("registration", cache.get("alignment_quality", {}))
        if cache and cache.get("positions"):
            st.draft_positions = dict(cache["positions"])
            st.draft_tile_w = cache.get("tile_w") or LIVE_TILE_WIDTH
            th = cache.get("tile_h")
            st.draft_tile_h = (
                int(th)
                if th
                else int(round(float(st.draft_tile_w) * 0.75))
            )
            canvas = cache.get("canvas")
            st.draft_canvas = (
                (int(canvas[0]), int(canvas[1])) if canvas else None
            )
            st.draft_med = cache.get("med")

    def _rule_grid_layout_payload(self):
        """Stable overlapping layout (~30%) for Continue / occupancy-only sessions."""
        st = self.state
        nx = max(1, int(st.nx or 1))
        ny = max(1, int(st.ny or 1))
        min_ix = int(getattr(st, "min_ix", 0) or 0)
        min_iy = int(getattr(st, "min_iy", 0) or 0)
        signs = getattr(st, "axis_sign", None) or [-1, -1]
        ax = 1 if int(signs[0]) >= 0 else -1
        ay = 1 if int(signs[1]) >= 0 else -1
        keys = list(st.best.keys()) if st.best else []
        if not keys:
            for iy in range(min_iy, min_iy + ny):
                for ix in range(min_ix, min_ix + nx):
                    keys.append((ix, iy))
        overlap = 0.30
        stride = 1.0 - overlap
        canvas_w = 1.0 + (nx - 1) * stride
        canvas_h = 1.0 + (ny - 1) * stride
        tw = 1.0 / canvas_w
        th = 1.0 / canvas_h
        tiles = []
        for ix, iy in keys:
            col = (min_ix + nx - 1 - ix) if ax < 0 else (ix - min_ix)
            row = (min_iy + ny - 1 - iy) if ay < 0 else (iy - min_iy)
            tiles.append(
                {
                    "ix": int(ix),
                    "iy": int(iy),
                    "x": float(col) * stride / canvas_w,
                    "y": float(row) * stride / canvas_h,
                    "w": tw,
                    "h": th,
                }
            )
        return {
            "ok": True,
            "canvas": [nx, ny],
            "tile_w": 1,
            "tile_h": 1,
            "tiles": tiles,
            "rule_grid": True,
            "overlap": overlap,
        }

    def tile_layout_payload(self):
        """Normalized tile poses for frontend LOD (fractions of canvas)."""
        st = self.state
        # Occupancy-only / incomplete local stitch → full rule grid (Continue)
        occ_only = any(
            (v is not None and not (v[1] and os.path.isfile(v[1])))
            for v in (st.best or {}).values()
        )
        positions = st.draft_positions or {}
        if occ_only or (
            st.best and positions and len(positions) < max(1, int(len(st.best) * 0.85))
        ):
            return self._rule_grid_layout_payload()
        if getattr(st, "plan_bounds", None) and st.phase in (
            "calibrating",
            "scanning",
        ):
            return self._rule_grid_layout_payload()

        tw = int(st.draft_tile_w or FINAL_TILE_WIDTH)
        th = int(
            st.draft_tile_h
            or max(1, int(round(float(tw) * 0.75)))
        )
        canvas = st.draft_canvas
        if canvas and len(canvas) >= 2:
            cw, ch = int(canvas[0]), int(canvas[1])
        elif positions:
            cw = int(max(p[0] for p in positions.values()) + tw)
            ch = int(max(p[1] for p in positions.values()) + th)
        else:
            return self._rule_grid_layout_payload()
        cw = max(1, cw)
        ch = max(1, ch)
        tiles = []
        for (ix, iy), (x, y) in positions.items():
            tiles.append(
                {
                    "ix": int(ix),
                    "iy": int(iy),
                    "x": float(x) / float(cw),
                    "y": float(y) / float(ch),
                    "w": float(tw) / float(cw),
                    "h": float(th) / float(ch),
                }
            )
        return {
            "ok": True,
            "canvas": [cw, ch],
            "tile_w": tw,
            "tile_h": th,
            "tiles": tiles,
            "rule_grid": False,
        }

    def finalize(self, microscope, delete_tiles=True):
        st = self.state
        st.plan_bounds = None
        # The Pi is an acquisition appliance, not the final registration
        # worker. Keep source tiles for the Mac and publish only a cheap
        # stage-coordinate preview here.
        st.phase = "done"
        cells = {k: v[1] for k, v in st.best.items() if v and v[1]}
        stitch_ok = True
        if not cells:
            # Occupancy-only Continue (old tiles on PC): skip Pi stitch
            st.phase = "done"
            st.refined = False
            try:
                self.save_session_state()
            except Exception:
                pass
            return {
                "ok": True,
                "cells": len(st.best),
                "local_cells": 0,
                "mac_occupancy": True,
                "session_id": st.session_id,
                "center_tile": getattr(st, "center_tile_metadata", None),
                "refined": False,
                "auto_refined": False,
            }
        # In the normal path the live preview already exists. Only create a
        # stage-only fallback if a restart/edge case left no preview behind.
        if not os.path.isfile(st.preview_path):
            try:
                mosaic = stitch_cells(
                    cells,
                    st.nx,
                    st.ny,
                    preview_tile_width(len(cells), st.nx, st.ny),
                    align=False,
                    stride_size=st.stride_size,
                    px_per_step=st.px_per_step,
                    axis_sign=getattr(st, "axis_sign", None),
                )
                self.update_mosaic_image(
                    mosaic,
                    tags=["mosaic", "live-scan"],
                    annotations={"phase": "stage-preview"},
                    force=True,
                )
                del mosaic
            except Exception:
                logging.exception("stage-only final preview failed")
                stitch_ok = False

        out = {
            "mosaic": st.mosaic_name,
            "alignment_quality": getattr(st, "alignment_quality", {}),
            "mosaic_id": st.mosaic_id,
            "dataset_id": st.dataset_id,
            "session_id": st.session_id,
            "cells": len(cells),
            "tiles_seen": st.tile_count,
            "preview": st.preview_path if os.path.isfile(st.preview_path) else None,
            "stride_size": list(st.stride_size),
            "px_per_step": list(st.px_per_step) if st.px_per_step else None,
            "axis_sign": list(getattr(st, "axis_sign", [-1, -1]) or [-1, -1]),
            "nx": st.nx,
            "ny": st.ny,
            "initial_xy": list(st.initial_xy) if st.initial_xy else None,
            "origin_xy": list(st.origin_xy) if st.origin_xy else None,
            "min_ix": getattr(st, "min_ix", 0),
            "min_iy": getattr(st, "min_iy", 0),
            "kept_tiles": False,
            "deleted": 0,
            "refined": False,
            "pc_refine_pending": True,
            "auto_refined": False,
            "last_grid": list(st.last_grid) if getattr(st, "last_grid", None) else None,
            "center_tile": getattr(st, "center_tile_metadata", None),
        }
        if st.calib:
            out["calibration"] = st.calib

        if not delete_tiles:
            out["kept_tiles"] = True
            if not stitch_ok:
                logging.warning("Skipping tile cleanup because stage preview failed")
                return out
            logging.info(
                "Scan complete: retained %s tiles for PC Worker registration",
                len(cells),
            )
            st.phase = "done"
            st.refined = False
            out["auto_refined"] = False
            out["mac_refine"] = True
            try:
                self.save_session_state()
            except Exception:
                pass
            return out

        # Cleanup is deferred until the Mac has ingested and refined the tiles.
        logging.info("Deferring tile cleanup until PC Worker refinement succeeds")
        deleted = {"deleted": 0, "deferred": True}
        out["deleted"] = (
            deleted.get("deleted", 0) if isinstance(deleted, dict) else deleted
        )
        st.phase = "done" if stitch_ok else "error"
        try:
            self.save_session_state()
        except Exception:
            pass
        return out

    def refine_mosaic(self, microscope=None, raise_http=True):
        """Heavy pairwise align + write final mosaic (user or auto after scan)."""
        st = self.state
        cells = {k: v[1] for k, v in st.best.items() if v and v[1]}
        if not cells:
            if raise_http:
                abort(400, "No local tiles to refine (use PC Refine)")
            raise ValueError("No local tiles to refine")
        t0 = time.time()
        st.phase = "refining"
        st.refine_progress = 0
        st.refined = False
        slide_meta = None

        def on_progress(pct):
            st.refine_progress = int(pct)
            try:
                from labthings import update_action_progress

                update_action_progress(int(pct))
            except Exception:
                pass

        try:
            on_progress(1)
            cache = {}
            engine = "legacy"
            logging.info("Pi refine using light stitch (%s cells)", len(cells))
            mosaic = stitch_cells(
                cells,
                st.nx,
                st.ny,
                REFINE_TILE_WIDTH,
                align="fine" if st.draft_positions else "full",
                stride_size=st.stride_size,
                px_per_step=st.px_per_step,
                progress_cb=on_progress,
                prior_positions=st.draft_positions if getattr(st, "alignment_quality", {}).get("accepted") is not False else None,
                prior_tile_w=st.draft_tile_w,
                cache_out=cache,
                axis_sign=getattr(st, "axis_sign", None),
            )
            self._store_draft_cache(cache)
            polished = _polish_for_slide(mosaic)
            # Update live mosaic (preview) with refined geometry
            self.update_mosaic_image(
                polished,
                tags=["mosaic", "scan-refine", engine],
                annotations={"phase": "refined", "engine": engine},
                force=True,
            )
            slide_meta = self.save_refined_slide(microscope, polished)
            del mosaic, polished
            st.refined = True
            st.phase = "done"
            st.refine_progress = 100
            on_progress(100)
        except Exception:
            logging.exception("refine mosaic failed")
            st.phase = "error"
            st.refine_progress = 0
            if raise_http:
                abort(500, "Refine failed")
            raise
        elapsed = time.time() - t0
        return {
            "ok": True,
            "mosaic": st.mosaic_name,
            "mosaic_id": st.mosaic_id,
            "session_id": st.session_id,
            "cells": len(cells),
            "refined": True,
            "slide_saved": bool(getattr(st, "slide_saved", False)),
            "slide_id": getattr(st, "slide_id", None),
            "slide_name": getattr(st, "slide_name", None),
            "elapsed_s": round(elapsed, 2),
            "preview": st.preview_path if os.path.isfile(st.preview_path) else None,
            "nx": st.nx,
            "ny": st.ny,
            "stride_size": list(st.stride_size),
            "px_per_step": list(st.px_per_step) if st.px_per_step else None,
            "axis_sign": list(getattr(st, "axis_sign", [-1, -1]) or [-1, -1]),
            "origin_xy": list(st.origin_xy) if st.origin_xy else None,
            "min_ix": getattr(st, "min_ix", 0),
            "min_iy": getattr(st, "min_iy", 0),
            "last_grid": list(st.last_grid) if st.last_grid else None,
            "refine_progress": 100,
            "slide": slide_meta,
        }

    def save_refined_slide(self, microscope, image):
        """Persist polished high-res slide to Gallery (kept after Scan close)."""
        st = self.state
        basename = st.basename or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = "OPENMHS_SLIDE_{}.jpeg".format(basename)
        path = os.path.join(MICROGRAPHS, name)
        tags = ["mosaic", "saved-slide", "openmhs", "refined"]
        annotations = {
            "phase": "refined-slide",
            "source": EXTENSION_ID,
            "session_id": st.session_id or "",
            "nx": st.nx,
            "ny": st.ny,
        }
        capture_id = None
        cap = None
        try:
            if hasattr(microscope, "captures") and hasattr(microscope.captures, "new_image"):
                try:
                    cap = microscope.captures.new_image(
                        False, temporary=False, filename=name.replace(".jpeg", "")
                    )
                except Exception:
                    logging.exception("captures.new_image failed for refined slide")
                    cap = None
            if cap is not None:
                capture_id = str(cap.id)
                path = getattr(cap, "file", None) or getattr(cap, "path", None) or path
                save_jpeg_with_meta(
                    path, image, capture_id, name, tags, annotations, dataset=None
                )
                try:
                    if hasattr(cap, "put_tags"):
                        cap.put_tags(tags)
                    if hasattr(cap, "put_annotations"):
                        cap.put_annotations(annotations)
                except Exception:
                    logging.debug("tag refined slide failed", exc_info=True)
            else:
                capture_id = str(uuid.uuid4())
                save_jpeg_with_meta(
                    path, image, capture_id, name, tags, annotations, dataset=None
                )
                cap = register_capture(microscope, path)
                if cap is not None:
                    capture_id = str(getattr(cap, "id", capture_id))
            st.slide_id = capture_id
            st.slide_path = path
            st.slide_name = name
            st.slide_saved = True
            logging.info("Saved refined slide %s (%s)", name, capture_id)
            return {"id": capture_id, "name": name, "path": path, "ok": True}
        except Exception:
            logging.exception("save_refined_slide failed")
            st.slide_saved = False
            return {"ok": False}

    def index_from_stage(self, x, y):
        """Map stage XY to grid index using stable origin + stride."""
        st = self.state
        origin = st.origin_xy or st.initial_xy
        sx = int(st.stride_size[0]) if st.stride_size else 2000
        sy = int(st.stride_size[1]) if st.stride_size else 1500
        if not origin or sx == 0 or sy == 0:
            return nearest_index(st.mapping, x, y)
        ix = int(round((float(x) - float(origin[0])) / float(sx)))
        iy = int(round((float(y) - float(origin[1])) / float(sy)))
        return (ix, iy)

    def refresh_grid_bounds(self):
        st = self.state
        if not st.best:
            return
        ixs = [k[0] for k in st.best.keys()]
        iys = [k[1] for k in st.best.keys()]
        min_ix, max_ix = min(ixs), max(ixs)
        min_iy, max_iy = min(iys), max(iys)
        st.min_ix = int(min_ix)
        st.min_iy = int(min_iy)
        st.nx = max(1, max_ix - min_ix + 1)
        st.ny = max(1, max_iy - min_iy + 1)
        # While scanning a planned window (Start/Continue), keep the full
        # target rectangle so the UI can show empty slots filling in.
        pb = getattr(st, "plan_bounds", None)
        if st.phase in ("calibrating", "scanning") and pb and len(pb) >= 4:
            i0, j0, pnx, pny = [int(pb[0]), int(pb[1]), int(pb[2]), int(pb[3])]
            st.min_ix = int(i0)
            st.min_iy = int(j0)
            st.nx = max(1, int(pnx))
            st.ny = max(1, int(pny))

    def clear_live_preview(self):
        """Remove UI preview.jpg so closed sessions cannot be revived."""
        st = self.state
        path = st.preview_path or os.path.join(PREVIEW_DIR, PREVIEW_NAME)
        try:
            if path and os.path.isfile(path):
                os.remove(path)
                logging.info("Cleared live preview %s", path)
        except Exception:
            logging.exception("Failed clearing preview.jpg")

    def delete_scan_tiles(
        self, microscope, dataset_id=None, save_slide=False, tiles_only=False, keep_preview=False
    ):
        """Delete SCAN_* tiles for the given (or current) dataset.

        If save_slide is True, keep LIVE mosaic / OPENMHS_SLIDE in Gallery.
        If tiles_only/keep_preview: wipe tile files + st.best but keep
        preview.jpg and session metadata so UI can still show the mosaic
        while LOD comes from Mac.
        """
        st = self.state
        dataset_id = dataset_id or st.dataset_id
        basename = st.basename
        folder = st.folder
        mosaic_id = st.mosaic_id
        mosaic_path = st.mosaic_path
        deleted = 0
        keep_preview = bool(keep_preview or tiles_only)
        tiles_only = bool(tiles_only or keep_preview)

        if dataset_id:
            images = {}
            try:
                images = microscope.captures.images
            except Exception:
                logging.exception("No captures.images for delete")
            for cap in list(list_captures(microscope)):
                try:
                    ds = getattr(cap, "dataset", None) or {}
                    if str(ds.get("id")) != str(dataset_id):
                        continue
                    cap_id = str(getattr(cap, "id", ""))
                    if mosaic_id and cap_id and cap_id == str(mosaic_id):
                        continue
                    name = getattr(cap, "name", "") or ""
                    if name.startswith("LIVE_MOSAIC_"):
                        continue
                    if name.startswith("OPENMHS_SLIDE_"):
                        continue
                    slide_id = str(getattr(st, "slide_id", "") or "")
                    if slide_id and cap_id == slide_id:
                        continue
                    if hasattr(cap, "delete"):
                        try:
                            cap.delete()
                        except Exception:
                            logging.exception("cap.delete failed for %s", name)
                    if cap_id and cap_id in images:
                        try:
                            del images[cap_id]
                        except Exception:
                            pass
                    deleted += 1
                except Exception:
                    logging.exception("Failed deleting tile capture")
        else:
            logging.warning("No dataset_id recorded; clearing preview/session anyway")

        scan_folders = [folder or "SCAN_{}".format(basename)] if folder or basename else []
        if basename:
            scan_folders.append("SCAN_{}_retake".format(basename))
        for scan_folder in scan_folders:
            folder_path = os.path.join(MICROGRAPHS, scan_folder)
            try:
                if os.path.isdir(folder_path):
                    shutil.rmtree(folder_path, ignore_errors=True)
            except Exception:
                logging.exception("Failed removing scan folder %s", folder_path)

        try:
            images = microscope.captures.images
            for cap_id, cap in list(images.items()):
                path = getattr(cap, "file", None) or getattr(cap, "path", None)
                if path and not os.path.isfile(path):
                    name = getattr(cap, "name", "") or ""
                    if basename and basename in name and not name.startswith(
                        "LIVE_MOSAIC_"
                    ):
                        del images[cap_id]
        except Exception:
            logging.exception("Failed pruning missing capture files from memory")

        # Live mosaic: drop unless tiles-only / save_slide (keep Gallery + preview)
        mosaic_deleted = False
        keep_slide_id = str(getattr(st, "slide_id", "") or "")
        drop_live_mosaic = not (save_slide or tiles_only or keep_preview)
        if drop_live_mosaic:
            try:
                images = microscope.captures.images
                mid = str(mosaic_id) if mosaic_id else None
                if mid and mid in images and mid != keep_slide_id:
                    cap = images[mid]
                    if hasattr(cap, "delete"):
                        try:
                            cap.delete()
                        except Exception:
                            pass
                    try:
                        del images[mid]
                    except Exception:
                        pass
                    mosaic_deleted = True
                elif mosaic_path and os.path.isfile(mosaic_path):
                    try:
                        os.remove(mosaic_path)
                        mosaic_deleted = True
                    except Exception:
                        logging.exception("Failed removing mosaic file")
                if basename:
                    for cap in list(list_captures(microscope)):
                        name = getattr(cap, "name", "") or ""
                        cap_id = str(getattr(cap, "id", ""))
                        if keep_slide_id and cap_id == keep_slide_id:
                            continue
                        if name.startswith("OPENMHS_SLIDE_"):
                            continue
                        if basename in name and name.startswith("LIVE_MOSAIC_"):
                            try:
                                if hasattr(cap, "delete"):
                                    cap.delete()
                                if cap_id and cap_id in images:
                                    del images[cap_id]
                                mosaic_deleted = True
                            except Exception:
                                logging.exception("Failed sweeping mosaic %s", name)
            except Exception:
                logging.exception("Failed deleting live mosaic capture")
        if save_slide and getattr(st, "slide_saved", False):
            logging.info(
                "Keeping refined slide %s after cleanup", getattr(st, "slide_name", "")
            )

        if keep_preview:
            # Tiles gone; keep preview.jpg + session so UI still shows mosaic
            st.best = {}
            st.folder = None
            st.tile_count = 0
            st.phase = "done" if st.session_id else "idle"
            st.refine_progress = 0
            logging.info(
                "Tiles-only cleanup for session %s (preview kept)", st.session_id
            )
            try:
                self.save_session_state()
            except Exception:
                logging.debug("session state save after tiles-only failed", exc_info=True)
            saved = bool(getattr(st, "slide_saved", False) or save_slide)
            return {
                "deleted": deleted,
                "mosaic_deleted": mosaic_deleted,
                "saved": saved,
                "tiles_only": True,
                "keep_preview": True,
                "session_id": st.session_id,
                "slide_id": getattr(st, "slide_id", None),
                "slide_name": getattr(st, "slide_name", None),
            }

        # Full close: clear live preview + session
        self.clear_live_preview()
        self.clear_session_state_file()

        st.best = {}
        st.refined = False
        st.session_id = None
        st.origin_xy = None
        st.initial_xy = None
        st.dataset_id = None
        st.folder = None
        st.basename = None
        st.mapping = {}
        st.tile_count = 0
        st.phase = "idle"
        st.refine_progress = 0
        st.draft_positions = None
        st.draft_tile_w = None
        st.draft_tile_h = None
        st.draft_canvas = None
        st.draft_med = None
        st.empty_cells = set()
        st.nx, st.ny = 1, 1
        st.min_ix = st.min_iy = 0
        st.mosaic_path = None
        st.mosaic_id = None
        st.mosaic_name = None
        st.mosaic_cap = None
        saved = bool(getattr(st, "slide_saved", False) or save_slide)
        return {
            "deleted": deleted,
            "mosaic_deleted": mosaic_deleted,
            "saved": saved,
            "slide_id": getattr(st, "slide_id", None),
            "slide_name": getattr(st, "slide_name", None),
        }

    def tile_path_at_norm(self, nx, ny):
        """Map normalized preview coords to a retained tile file path."""
        st = self.state
        if not st.best:
            return None, None
        try:
            nx = float(nx)
            ny = float(ny)
        except (TypeError, ValueError):
            return None, None
        nx = min(1.0, max(0.0, nx))
        ny = min(1.0, max(0.0, ny))

        keys = list(st.best.keys())
        ixs = [k[0] for k in keys]
        iys = [k[1] for k in keys]
        min_ix, max_ix = min(ixs), max(ixs)
        min_iy, max_iy = min(iys), max(iys)
        span_x = max(1, max_ix - min_ix + 1)
        span_y = max(1, max_iy - min_iy + 1)
        ix_from_left = int(min(span_x - 1, max(0, int(nx * span_x))))
        iy_from_top = int(min(span_y - 1, max(0, int(ny * span_y))))
        # Match mosaic image orientation (axis_sign may flip stage index vs image)
        ax = ay = -1
        try:
            signs = getattr(st, "axis_sign", None) or [-1, -1]
            ax = int(signs[0])
            ay = int(signs[1])
        except Exception:
            ax = ay = -1
        if ax < 0:
            ix = int(min_ix + (span_x - 1 - ix_from_left))
        else:
            ix = int(min_ix + ix_from_left)
        if ay < 0:
            iy = int(min_iy + (span_y - 1 - iy_from_top))
        else:
            iy = int(min_iy + iy_from_top)
        hit = st.best.get((ix, iy))
        if hit is None:
            # nearest occupied cell
            best_d = None
            best_ij = None
            for (cx, cy) in keys:
                d = (cx - ix) ** 2 + (cy - iy) ** 2
                if best_d is None or d < best_d:
                    best_d = d
                    best_ij = (cx, cy)
            if best_ij is None:
                return None, None
            ix, iy = best_ij
            hit = st.best.get(best_ij)
        path = hit[1] if hit else None
        if not path or not os.path.isfile(path):
            return None, None
        return path, {"ix": ix, "iy": iy}

    def plan_missing_cells(self, grid, center_xy=None):
        """Return missing cells for an nx×ny block expanding the existing mosaic.

        Continue should feel like growing the current tile rectangle, not
        starting a new patch from wherever the stage happens to sit (often far
        from a loaded PC mosaic). Anchor at the occupied min corner
        ``(min_ix, min_iy)`` and fill ``[i0, i0+nx) × [j0, j0+ny)``, skipping
        cells already in ``st.best``.

        If there are no occupied cells yet, fall back to the current stage cell
        (same +X/+Y growth as Start).
        """
        st = self.state
        nx = max(1, int(grid[0]))
        ny = max(1, int(grid[1]))
        sx = int(st.stride_size[0]) if st.stride_size else 2000
        sy = int(st.stride_size[1]) if st.stride_size else 1500
        origin = st.origin_xy
        if not origin:
            return [], None
        keys = list(st.best.keys()) if st.best else []
        if keys:
            i0 = min(i for i, _j in keys)
            j0 = min(j for _i, j in keys)
            anchor = [i0, j0]
        else:
            if center_xy is None:
                center_xy = st.initial_xy or origin
            cx = int(round((float(center_xy[0]) - float(origin[0])) / float(sx)))
            cy = int(round((float(center_xy[1]) - float(origin[1])) / float(sy)))
            i0, j0 = cx, cy
            anchor = [cx, cy]
        missing = []
        for i in range(i0, i0 + nx):
            for j in range(j0, j0 + ny):
                if (i, j) in st.best:
                    continue
                stage_x = int(round(float(origin[0]) + i * sx))
                stage_y = int(round(float(origin[1]) + j * sy))
                missing.append(((i, j), (stage_x, stage_y)))
        meta = {"i0": i0, "j0": j0, "nx": nx, "ny": ny, "anchor_ij": anchor}
        return missing, meta

    def scan_missing_cells(
        self,
        microscope,
        scan_ext,
        missing,
        args,
        stride_size,
        basename,
        resize,
    ):
        """Capture only missing cells (Continue fill-gaps)."""
        from labthings import current_action, update_action_progress

        st = self.state
        if not missing:
            logging.info("Continue: no missing cells to capture")
            return self.finalize(microscope, delete_tiles=False)

        autofocus_dz = int(args.get("autofocus_dz") or 0)
        # OpenFlexure's stock fast autofocus is the default. The slower
        # MicroWorld quality survey remains an explicit opt-in only.
        fast_autofocus = bool(args.get("fast_autofocus", True))
        use_video_port = bool(args.get("use_video_port", True))
        namemode = args.get("namemode") or "coordinates"
        temporary = bool(args.get("temporary", False))
        annotations = args.get("annotations") or {}
        tags = args.get("tags") or []

        initial_position = microscope.stage.position
        dataset_d = {
            "id": st.dataset_id or str(uuid.uuid4()),
            "type": "xyzScan",
            "name": basename,
            "acquisitionDate": datetime.datetime.now().isoformat(),
            "strideSize": list(stride_size),
            "grid": list(st.last_grid or [1, 1, 1]),
            "style": "fill_gaps",
            "autofocusDz": autofocus_dz,
        }
        if not st.dataset_id:
            st.dataset_id = str(dataset_d["id"])

        autofocus = None
        autofocus_factory = None
        if autofocus_dz and hasattr(scan_ext, "get_autofocus_function"):
            try:
                autofocus = scan_ext.get_autofocus_function(
                    dz=autofocus_dz, use_fast_autofocus=fast_autofocus
                )

                def autofocus_factory(dz):
                    return scan_ext.get_autofocus_function(
                        dz=int(dz), use_fast_autofocus=fast_autofocus
                    )
            except Exception:
                logging.exception("get_autofocus_function failed")

        FocusManager = None
        try:
            from openflexure_microscope.api.default_extensions.scan import FocusManager as FM

            FocusManager = FM
        except Exception:
            logging.debug("FocusManager import failed", exc_info=True)

        focus_manager = None
        if FocusManager is not None:
            try:
                focus_manager = FocusManager(
                    microscope,
                    initial_position,
                    autofocus_function=autofocus,
                    current_image_is_background_function=None,
                    axial_jump_threshold=None,
                )
            except Exception:
                logging.exception("FocusManager init failed")
                focus_manager = None

        total = len(missing)
        captured = 0
        st.phase = "scanning"
        logging.info("Continue fill-gaps: %s missing cells", total)

        original_capture = scan_ext.capture

        def hooked_capture(*cargs, **ckwargs):
            result = original_capture(*cargs, **ckwargs)
            self.apply_adaptive_focus(
                microscope,
                result,
                enabled=bool(args.get("adaptive_focus_stack", ADAPTIVE_FOCUS_DEFAULT)),
                autofocus=autofocus,
                autofocus_factory=autofocus_factory,
                autofocus_dz=autofocus_dz,
                native_resolution=bool(args.get("native_resolution")),
            )
            try:
                dataset = ckwargs.get("dataset")
                self.on_tile(microscope, result, dataset)
            except Exception:
                logging.exception("on_tile hook failed")
            self.release_scan_capture_reference(microscope, result)
            return result

        scan_ext.capture = hooked_capture
        try:
            for (ij, xy) in missing:
                next_z = initial_position[2]
                if focus_manager is not None:
                    try:
                        next_z = focus_manager.estimate_z(xy)
                    except Exception:
                        next_z = microscope.stage.position[2]
                microscope.stage.move_abs((xy[0], xy[1], next_z))
                if focus_manager is not None:
                    try:
                        focus_manager.autofocus()
                    except Exception:
                        logging.exception("autofocus failed at %s", ij)
                elif autofocus:
                    try:
                        autofocus()
                    except Exception:
                        logging.exception("autofocus callable failed at %s", ij)

                scan_ext.capture(
                    microscope,
                    basename,
                    namemode=namemode,
                    temporary=temporary,
                    use_video_port=use_video_port,
                    resize=resize,
                    bayer=bool(args.get("bayer", False)),
                    dataset=dataset_d,
                    annotations=annotations,
                    tags=tags,
                )
                captured += 1
                try:
                    update_action_progress(int(100.0 * captured / float(total)))
                except Exception:
                    pass
                if current_action() and current_action().stopped:
                    break
        finally:
            scan_ext.capture = original_capture
            try:
                microscope.stage.move_abs(initial_position)
            except Exception:
                logging.exception("Failed returning to start after fill-gaps")

        return self.finalize(microscope, delete_tiles=False)

    def stage_xy_for_index(self, ij):
        st = self.state
        origin = st.origin_xy or st.initial_xy or [0, 0]
        sx = int(st.stride_size[0]) if st.stride_size else 2000
        sy = int(st.stride_size[1]) if st.stride_size else 1500
        return (
            int(round(float(origin[0]) + int(ij[0]) * sx)),
            int(round(float(origin[1]) + int(ij[1]) * sy)),
        )

    def run_video_scout_scan(self, microscope, scan_ext, args, basename, *,
                             bounds=None, dataset=None, inspect_cell=None,
                             max_cells=None, progress_start=0, progress_end=98):
        """Acquire a bounded grid from Pi-local JPEGs and revisit weak or missing fields."""
        import tempfile
        from labthings import current_action, update_action_progress

        st = self.state
        start = tuple(int(p) for p in microscope.stage.position)
        legacy_video = args.get("explore_strategy") == "video"
        if bounds is None:
            if legacy_video:
                radius = int(args.get("explore_max_radius", 2))
                if not 1 <= radius <= 5:
                    raise ValueError("Video Scout radius must be 1–5 tiles.")
                bounds = (-radius, radius, -radius, radius)
                st.last_grid = ["explore", "explore", 1]
            else:
                nx, ny = (int(v) for v in args["grid"][:2])
                bounds = (0, nx-1, 0, ny-1)
                st.last_grid = [nx, ny, 1]
        min_x, max_x, min_y, max_y = bounds
        nx, ny = max_x-min_x+1, max_y-min_y+1
        rows = []
        for index, iy in enumerate(range(min_y, max_y+1)):
            columns = list(range(min_x, max_x+1))
            if index % 2:
                columns.reverse()
            # Bound temporary storage and tracking work even on large Full scans.
            rows.extend([(ix, iy) for ix in columns[offset:offset+11]]
                        for offset in range(0, nx, 11))
        st.plan_bounds = (min_x, min_y, nx, ny)
        st.phase = "scanning"
        st.focus_phase = "video_scout"
        resolution = (1280, 960)
        if dataset is None:
            dataset = {"id": str(uuid.uuid4()), "type": "xyzScan", "name": basename,
                       "acquisitionDate": datetime.datetime.now().isoformat(),
                       "strideSize": list(st.stride_size), "grid": [nx, ny, 1], "style": "snake"}
        st.dataset_id = dataset["id"]
        report = {"grid": [nx, ny], "resolution": list(resolution), "rows": [],
                  "retakes": [], "quality_flags": [], "timings": {}}

        def needs_tile(cell):
            return cell not in st.best and cell not in st.empty_cells

        def has_capacity():
            return max_cells is None or len(st.best) < max_cells

        def progress(fraction):
            update_action_progress(int(progress_start + (progress_end-progress_start)*fraction))

        camera = microscope.camera
        stream = camera.stream
        native_write = stream.write
        settings = {"stream_resolution": camera.stream_resolution, "mjpeg_quality": camera.mjpeg_quality}
        partial = bytearray()
        frame_ready = threading.Condition()
        latest = [0, None, 0.]
        counts = {"fragment_writes": 0, "abandoned_jpegs": 0}

        def assemble(chunk):
            if not (chunk.startswith(b"\xff\xd8") and chunk.endswith(b"\xff\xd9")):
                counts["fragment_writes"] += 1
            if chunk.startswith(b"\xff\xd8"):
                counts["abandoned_jpegs"] += bool(partial)
                partial.clear()
                partial.extend(chunk)
            elif partial:
                partial.extend(chunk)
            if len(partial) > 3 * 1024 * 1024:
                counts["abandoned_jpegs"] += 1
                partial.clear()
            elif partial.endswith(b"\xff\xd9"):
                complete = bytes(partial)
                partial.clear()
                native_write(complete)
                with frame_ready:
                    latest[:] = [latest[0] + 1, complete, time.monotonic()]
                    frame_ready.notify_all()
            return len(chunk)

        def next_frame(timeout=1.0):
            with frame_ready:
                sequence = latest[0]
                if frame_ready.wait_for(lambda: latest[0] != sequence, timeout):
                    return latest[1], latest[2]
            return None, 0.

        def check():
            action = current_action()
            if action and action.stopped:
                raise RuntimeError("Scan cancelled")

        def move(cell):
            check()
            microscope.stage.move_abs((*self.stage_xy_for_index(cell), start[2]))

        def describe(path):
            with Image.open(path) as source:
                if source.size != resolution:
                    raise ValueError("Unexpected JPEG size")
                source.draft("L", (160, 120))
                source.load()
                gray = np.asarray(source.convert("L").resize((160, 120), Image.BILINEAR),
                                  dtype=np.float32)
            lap = (gray[1:-1, :-2] + gray[1:-1, 2:] + gray[:-2, 1:-1]
                   + gray[2:, 1:-1] - 4 * gray[1:-1, 1:-1])
            # Normalize for the amount of texture and exclude field edges.
            sharpness = float(lap[15:-15, 15:-15].var() / max(1., gray.var()))
            gradients = gray - np.roll(gray, 1, axis=1)
            gradients -= gradients.mean()
            small = gray.reshape(60, 2, 80, 2).mean(axis=(1, 3))
            lap_small = (small[1:-1, :-2] + small[1:-1, 2:] + small[:-2, 1:-1]
                         + small[2:, 1:-1] - 4 * small[1:-1, 1:-1])
            # Only accept clear tissue here. The stationary detector decides
            # whether to skip a weak or ambiguous field in Full scans.
            tissue = (small.std() >= EMPTY_STD_MAX and lap_small.var() >= EMPTY_LAP_VAR_MAX)
            return np.fft.rfft2(gradients).astype(np.complex64), sharpness, tissue

        def displacement(a, b):
            cross = a * np.conj(b)
            cross /= np.maximum(1e-9, np.abs(cross))
            corr = np.fft.irfft2(cross, s=(120, 160))
            dy, dx = np.unravel_index(np.argmax(corr), corr.shape)
            return (int(dx if dx <= 80 else dx-160), int(dy if dy <= 60 else dy-120),
                    float(corr[dy, dx]))

        def save_raw(cell, path, fraction):
            xa, ya = self.stage_xy_for_index(cell)
            position = [int(round(xa + fraction * st.stride_size[0])), ya, start[2]]
            name = "{}_video_{}_{}_{}.jpeg".format(basename, *position)
            target = os.path.join(MICROGRAPHS, st.folder, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            score = sharpness_path(path)
            os.replace(path, target)
            st.tile_metadata["{},{}".format(*cell)] = {
                "stage_position": position, "position_source": "video-registered",
                "capture_id": str(uuid.uuid4()), "source_name": name,
                "captured_at": datetime.datetime.now().isoformat(), "units": "motor_steps"}
            st.best[cell] = (score, target)
            st.tile_count += 1
            self.refresh_grid_bounds()

        def sweep(row, directory):
            if len(row) < 2 or not any(needs_tile(cell) for cell in row):
                return
            move(row[0])
            samples = []
            stopped = threading.Event()

            def save_sample(frame):
                data, timestamp = frame
                if data is None:
                    return
                path = os.path.join(directory, "{}.jpeg".format(len(samples)))
                with open(path, "wb") as handle:
                    handle.write(data)
                samples.append((path, timestamp))

            def collect():
                while not stopped.is_set():
                    save_sample(next_frame(.25))
                    stopped.wait(.18)

            save_sample(next_frame(2))
            first_count = len(samples)
            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            try:
                move(row[-1])
            finally:
                stopped.set()
                reader.join()
            last_count = len(samples)
            save_sample(next_frame(2))
            row_result = {"row": row[0][1], "sampled": len(samples), "bad_jpegs": 0, "kept": 0}
            report["rows"].append(row_result)
            if first_count != 1 or len(samples) == last_count:
                return

            descriptors = []
            for index, (path, _) in enumerate(samples):
                check()
                try:
                    spectrum, sharpness, tissue = describe(path)
                except (OSError, ValueError):
                    row_result["bad_jpegs"] += 1
                    continue
                descriptors.append((index, path, spectrum, sharpness, tissue))
            if len(descriptors) < len(row) or descriptors[0][0] != 0 or descriptors[-1][0] != len(samples)-1:
                return
            tracked = [(0, 0, descriptors[0])]
            for candidate in descriptors[1:]:
                _, _, spectrum, _, _ = candidate
                links = []
                for x, y, previous in tracked[-4:]:
                    dx, dy, confidence = displacement(previous[2], spectrum)
                    if confidence >= .16 and abs(dx) <= 42 and abs(dy) <= 4:
                        links.append((confidence, x+dx, y+dy))
                if links:
                    _, x, y = max(links)
                    tracked.append((x, y, candidate))
            span, drift, last = tracked[-1]
            expected = abs(st.stride_size[0]) * (len(row)-1) * st.px_per_step[0] / 4
            direction = (row[-1][0] - row[0][0]) * st.axis_sign[0]
            if (last[0] != len(samples)-1 or span*direction <= 0 or
                    not .40 * expected <= abs(span) <= 1.5 * expected or abs(drift) > 6):
                return

            reference = max(descriptors[0][3], descriptors[-1][3])
            floor = .90 * reference
            row_result.update(span_px_160=span, sharpness_floor=floor)
            for index, cell in enumerate(row):
                if not has_capacity():
                    break
                if not needs_tile(cell):
                    continue
                target = index / (len(row)-1)
                # A field needs a frame within 15% of one motor-grid step.
                nearby = [(x, d) for x, y, d in tracked
                          if abs(x/span-target) * (len(row)-1) <= .15 and d[3] >= floor
                          and (inspect_cell is None or d[4])]
                if not nearby:
                    continue
                x, descriptor = max(nearby, key=lambda item: item[1][3])
                fraction = (x/span-target) * (row[-1][0]-row[0][0])
                save_raw(cell, descriptor[1], fraction)
                row_result["kept"] += 1
            if row_result["kept"]:
                last_tile = next(st.best[cell][1] for cell in reversed(row) if cell in st.best)
                self.update_live_tile_preview(last_tile)

        previous_count = len(st.best)
        try:
            began = time.monotonic()
            eligible_rows = [row for row in rows if len(row) > 1 and any(needs_tile(c) for c in row)]
            try:
                if eligible_rows:
                    stream.write = assemble
                    camera.update_settings({"stream_resolution": resolution, "mjpeg_quality": 55})
                for index, row in enumerate(eligible_rows):
                    if not has_capacity():
                        break
                    # Keep a single row on disk and publish only selected frames.
                    with tempfile.TemporaryDirectory(dir=PREVIEW_DIR, prefix="video-row-") as directory:
                        sweep(row, directory)
                    progress(.70 * (index+1) / len(eligible_rows))
            finally:
                if eligible_rows:
                    stream.write = native_write
                    camera.update_settings(settings)
            report["timings"]["continuous_s"] = round(time.monotonic()-began, 2)
            report["jpeg"] = counts
            report["continuous_tiles"] = len(st.best) - previous_count
            pending = [cell for row in reversed(rows) for cell in reversed(row) if needs_tile(cell)]
            st.focus_phase = "scout_retake"
            began = time.monotonic()
            autofocus = (scan_ext.get_autofocus_function(
                dz=int(args.get("autofocus_dz", 2000)), use_fast_autofocus=True)
                if pending and inspect_cell is None else None)
            z = start[2]
            for index, cell in enumerate(pending):
                check()
                if not has_capacity():
                    break
                if inspect_cell is not None:
                    if inspect_cell(cell):
                        report["retakes"].append(list(cell))
                    progress(.70 + .30 * (index+1) / len(pending))
                    continue
                xy = self.stage_xy_for_index(cell)
                microscope.stage.move_abs((*xy, z))
                autofocus()
                z = int(microscope.stage.position[2])
                st.mapping[xy] = cell
                capture = scan_ext.capture(
                    microscope, basename, namemode="coordinates", temporary=False,
                    use_video_port=True, resize=resolution, bayer=False, dataset=dataset,
                    annotations=args.get("annotations") or {}, tags=args.get("tags") or [])
                try:
                    self.on_tile(microscope, capture, dataset)
                finally:
                    self.release_scan_capture_reference(microscope, capture)
                report["retakes"].append(list(cell))
                progress(.70 + .30 * (index+1) / len(pending))
            report["timings"]["retake_s"] = round(time.monotonic()-began, 2)
            floor = .70 * float(np.median([tile[0] for tile in st.best.values()])) if st.best else 0
            report["quality_flags"] = [list(cell) for cell, tile in st.best.items() if tile[0] < floor]
            report["missing_cells"] = [list(cell) for row in rows for cell in row if needs_tile(cell)]
        except Exception:
            st.phase = "error"
            self.save_session_state()
            raise
        finally:
            st.focus_phase = "idle"
            if inspect_cell is None:
                microscope.stage.move_abs(start)
        st.plan_bounds = None
        self.refresh_grid_bounds()
        if inspect_cell is not None:
            return report
        update_action_progress(99)
        output = self.finalize(microscope, delete_tiles=False)
        output["video_scout"] = report
        output["acquisition"] = "continuous"
        return output


    def run_explore_scan(
        self,
        microscope,
        scan_ext,
        args,
        stride_size,
        basename,
        resize,
        seed_capture=True,
    ):
        """Explore a sample until empty edges are reached.

        Full scans use a bounded cardinal-ray search followed by a snake fill
        and a one-shell frontier safety pass.  ``frontier`` remains available
        as the conservative fallback.  Both use OF background-detect when
        available, else the Laplacian/std heuristic.
        """
        from labthings import current_action, update_action_progress

        st = self.state
        autofocus_dz = int(args.get("autofocus_dz") or 0)
        # Keep Explore on the stock fast autofocus path by default.
        fast_autofocus = bool(args.get("fast_autofocus", True))
        use_video_port = bool(args.get("use_video_port", True))
        namemode = args.get("namemode") or "coordinates"
        temporary = bool(args.get("temporary", False))
        annotations = dict(args.get("annotations") or {})
        tags = list(args.get("tags") or [])
        max_cells = int(args.get("explore_max_cells") or EXPLORE_MAX_CELLS)
        max_radius = int(args.get("explore_max_radius") or EXPLORE_MAX_RADIUS)
        explore_strategy = str(args.get("explore_strategy") or "boundary").lower()
        if explore_strategy not in ("boundary", "frontier"):
            explore_strategy = "boundary"

        if not isinstance(st.empty_cells, set):
            st.empty_cells = set(st.empty_cells or [])

        initial_position = microscope.stage.position
        dataset_d = {
            "id": st.dataset_id or str(uuid.uuid4()),
            "type": "exploreScan",
            "name": basename,
            "acquisitionDate": datetime.datetime.now().isoformat(),
            "strideSize": list(stride_size),
            "grid": ["explore"],
            "style": "explore",
            "autofocusDz": autofocus_dz,
            "exploreStrategy": explore_strategy,
        }
        if not st.dataset_id:
            st.dataset_id = str(dataset_d["id"])

        autofocus = None
        autofocus_factory = None
        if autofocus_dz and hasattr(scan_ext, "get_autofocus_function"):
            try:
                autofocus = scan_ext.get_autofocus_function(
                    dz=autofocus_dz, use_fast_autofocus=fast_autofocus
                )

                def autofocus_factory(dz):
                    return scan_ext.get_autofocus_function(
                        dz=int(dz), use_fast_autofocus=fast_autofocus
                    )
            except Exception:
                logging.exception("get_autofocus_function failed")

        FocusManager = None
        try:
            from openflexure_microscope.api.default_extensions.scan import (
                FocusManager as FM,
            )

            FocusManager = FM
        except Exception:
            logging.debug("FocusManager import failed", exc_info=True)

        focus_manager = None
        if FocusManager is not None:
            try:
                focus_manager = FocusManager(
                    microscope,
                    initial_position,
                    autofocus_function=autofocus,
                    current_image_is_background_function=None,
                    axial_jump_threshold=None,
                )
            except Exception:
                logging.exception("FocusManager init failed")

        def neighbors(ij):
            x, y = ij
            return ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))

        def within_radius(ij):
            return max(abs(ij[0]), abs(ij[1])) <= max_radius

        # Frontier = unvisited 4-neighbors of known tissue
        frontier = set()
        for ij in list(st.best.keys()):
            for nb in neighbors(ij):
                if (
                    nb not in st.best
                    and nb not in st.empty_cells
                    and within_radius(nb)
                ):
                    frontier.add(nb)

        original_capture = scan_ext.capture

        def hooked_capture(*cargs, **ckwargs):
            result = original_capture(*cargs, **ckwargs)
            self.apply_adaptive_focus(
                microscope,
                result,
                enabled=bool(args.get("adaptive_focus_stack", ADAPTIVE_FOCUS_DEFAULT)),
                autofocus=autofocus,
                autofocus_factory=autofocus_factory,
                autofocus_dz=autofocus_dz,
                native_resolution=bool(args.get("native_resolution")),
            )
            try:
                self.on_tile(microscope, result, ckwargs.get("dataset"))
            except Exception:
                logging.exception("on_tile hook failed")
            self.release_scan_capture_reference(microscope, result)
            return result

        def capture_here(ij):
            xy = self.stage_xy_for_index(ij)
            next_z = microscope.stage.position[2]
            if focus_manager is not None:
                try:
                    next_z = focus_manager.estimate_z(xy)
                except Exception:
                    pass
            microscope.stage.move_abs((xy[0], xy[1], next_z))
            if focus_manager is not None:
                try:
                    focus_manager.autofocus()
                except Exception:
                    logging.exception("autofocus failed at %s", ij)
            elif autofocus:
                try:
                    autofocus()
                except Exception:
                    logging.exception("autofocus callable failed at %s", ij)
            st.mapping[xy] = ij
            scan_ext.capture(
                microscope,
                basename,
                namemode=namemode,
                temporary=temporary,
                use_video_port=use_video_port,
                resize=resize,
                bayer=bool(args.get("bayer", False)),
                dataset=dataset_d,
                annotations=annotations,
                tags=tags,
            )

        steps = 0

        def inspect_cell(ij):
            """Classify one candidate, capturing it only when it has tissue."""
            nonlocal steps
            if ij in st.best or ij in st.empty_cells or not within_radius(ij):
                return ij in st.best
            xy = self.stage_xy_for_index(ij)
            z = microscope.stage.position[2]
            microscope.stage.move_abs((xy[0], xy[1], z))
            time.sleep(0.08)
            steps += 1
            if field_is_empty(microscope):
                st.empty_cells.add(ij)
                return False
            capture_here(ij)
            return True

        scan_ext.capture = hooked_capture
        st.phase = "scanning"
        st.last_grid = ["explore", "explore", 1]
        captured_tissue = len(st.best)
        empty_hits = 0
        logging.info(
            "Explore start: tissue=%s frontier=%s empty=%s max_cells=%s radius=%s",
            captured_tissue,
            len(frontier),
            len(st.empty_cells),
            max_cells,
            max_radius,
        )

        try:
            # Seed capture at current index if starting fresh
            if seed_capture and not st.best:
                seed = (0, 0)
                if st.origin_xy is None:
                    pos = microscope.stage.position
                    st.origin_xy = [int(pos[0]), int(pos[1])]
                    st.initial_xy = list(st.origin_xy)
                capture_here(seed)
                captured_tissue = len(st.best)
                for nb in neighbors(seed):
                    if within_radius(nb):
                        frontier.add(nb)

            boundary_bounds = None
            video_report = None
            if explore_strategy == "boundary" and seed_capture and st.best:
                # Probe four cardinal rays with exponential expansion, then
                # binary-search the last occupied position.  The resulting
                # rectangle limits the expensive fill to the sample's likely
                # extent; the ordinary frontier pass below still checks one
                # shell outside it, so diagonal/irregular tissue is not lost.
                def ray_extent(dx, dy):
                    last_inside = 0
                    probe = 1
                    first_empty = None
                    while probe <= max_radius and len(st.best) < max_cells:
                        if inspect_cell((dx * probe, dy * probe)):
                            last_inside = probe
                            probe *= 2
                            continue
                        # Do not mistake a one-tile hole for the outer edge.
                        if probe + 1 <= max_radius and inspect_cell(
                            (dx * (probe + 1), dy * (probe + 1))
                        ):
                            last_inside = probe + 1
                            probe *= 2
                            continue
                        first_empty = probe
                        break
                    if first_empty is None:
                        return last_inside
                    low, high = last_inside + 1, first_empty - 1
                    while low <= high and len(st.best) < max_cells:
                        mid = (low + high) // 2
                        if inspect_cell((dx * mid, dy * mid)):
                            last_inside = mid
                            low = mid + 1
                        else:
                            high = mid - 1
                    return last_inside

                left = ray_extent(-1, 0)
                right = ray_extent(1, 0)
                top = ray_extent(0, -1)
                bottom = ray_extent(0, 1)
                margin = 1
                min_x = max(-max_radius, -left - margin)
                max_x = min(max_radius, right + margin)
                min_y = max(-max_radius, -top - margin)
                max_y = min(max_radius, bottom + margin)
                boundary_bounds = [min_x, max_x, min_y, max_y]
                logging.info(
                    "Explore boundary probes: bounds=%s probes=%s tissue=%s",
                    boundary_bounds,
                    steps,
                    len(st.best),
                )
                if args.get("continuous_scan") and len(st.best) < max_cells:
                    video_report = self.run_video_scout_scan(
                        microscope, scan_ext, args, basename,
                        bounds=(min_x, max_x, min_y, max_y), dataset=dataset_d,
                        inspect_cell=inspect_cell, max_cells=max_cells, progress_end=85,
                    )
                else:
                    for row, iy in enumerate(range(min_y, max_y + 1)):
                        columns = list(range(min_x, max_x + 1))
                        if row % 2:
                            columns.reverse()
                        for ix in columns:
                            if len(st.best) >= max_cells:
                                break
                            inspect_cell((ix, iy))
                        if len(st.best) >= max_cells:
                            break

                # Rebuild the shell from all occupied cells so an irregular
                # or diagonal sample can extend past the cardinal rectangle.
                frontier = set()
                for ij in list(st.best.keys()):
                    for nb in neighbors(ij):
                        if (
                            nb not in st.best
                            and nb not in st.empty_cells
                            and within_radius(nb)
                        ):
                            frontier.add(nb)

            while frontier and len(st.best) < max_cells:
                if current_action() and current_action().stopped:
                    break
                # Greedy nearest-frontier (cheap TSP-like travel)
                here = microscope.stage.position
                def travel_key(ij):
                    xy = self.stage_xy_for_index(ij)
                    return (xy[0] - here[0]) ** 2 + (xy[1] - here[1]) ** 2

                nxt = min(frontier, key=travel_key)
                frontier.discard(nxt)
                if nxt in st.best or nxt in st.empty_cells:
                    continue
                if not within_radius(nxt):
                    continue

                # Classify before expensive AF+capture.
                if not inspect_cell(nxt):
                    empty_hits += 1
                    logging.info("Explore edge empty at %s", nxt)
                    # Do not expand frontier through empty cells
                else:
                    for nb in neighbors(nxt):
                        if (
                            nb not in st.best
                            and nb not in st.empty_cells
                            and nb not in frontier
                            and within_radius(nb)
                        ):
                            frontier.add(nb)

                # Soft progress: rises with tissue + emptied frontier pressure
                done_est = len(st.best) + len(st.empty_cells)
                prog = min(95, int(100.0 * done_est / float(max(max_cells, done_est + len(frontier) + 1))))
                try:
                    update_action_progress(prog)
                except Exception:
                    pass

            logging.info(
                "Explore done: tissue=%s empty=%s frontier_left=%s steps=%s",
                len(st.best),
                len(st.empty_cells),
                len(frontier),
                steps,
            )
            try:
                update_action_progress(99)
            except Exception:
                pass
        finally:
            scan_ext.capture = original_capture
            try:
                microscope.stage.move_abs(initial_position)
            except Exception:
                logging.exception("Failed returning after explore")

        out = self.finalize(microscope, delete_tiles=False)
        out["explore"] = True
        out["explore_strategy"] = explore_strategy
        out["boundary_bounds"] = boundary_bounds
        out["empty_cells"] = len(st.empty_cells)
        out["frontier_remaining"] = len(frontier)
        if video_report is not None:
            out["acquisition"] = "continuous"
            out["video_scout"] = video_report
        return out

    def run_spiral_center_scan(
        self, microscope, scan_ext, args, stride_size, grid, basename, resize,
        include_center=True,
    ):
        """Capture a centre hub followed by a deterministic outward spiral.

        This is intentionally an acquisition-only path.  The Pi places the
        live preview from stage coordinates and keeps the source JPEGs; the
        Mac later registers the regular lattice plus the optional fractional
        centre hub globally.  No registration or image scoring is performed
        here, so scan time remains dominated by native OpenFlexure capture
        and fast autofocus.
        """
        from labthings import current_action, update_action_progress

        st = self.state
        nx, ny = max(1, int(grid[0])), max(1, int(grid[1]))
        sx, sy = int(stride_size[0]), int(stride_size[1])
        nz, sz = max(1, int(grid[2])), int(stride_size[2])
        origin = tuple(int(v) for v in (st.origin_xy or st.initial_xy or microscope.stage.position[:2]))
        spec = center_hub_spec(nx, ny, origin, (sx, sy))
        want_hub = bool(include_center and spec.get("extra"))
        order = spiral_grid_order(nx, ny)
        center_ij = (int(round(float(spec["grid_position"][0]))), int(round(float(spec["grid_position"][1]))))

        autofocus_dz = int(args.get("autofocus_dz", 2000))
        fast_autofocus = bool(args.get("fast_autofocus", True))
        use_video_port = bool(args.get("use_video_port", True))
        namemode = args.get("namemode") or "coordinates"
        temporary = bool(args.get("temporary", False))
        annotations = dict(args.get("annotations") or {})
        tags = list(args.get("tags") or [])
        dataset_d = {
            "id": st.dataset_id or str(uuid.uuid4()),
            "type": "xyzScan",
            "name": basename,
            "acquisitionDate": datetime.datetime.now().isoformat(),
            "strideSize": list(stride_size),
            "grid": [nx, ny, nz],
            "style": "spiral",
            "centerTile": bool(want_hub),
            "autofocusDz": autofocus_dz,
        }
        if not st.dataset_id:
            st.dataset_id = str(dataset_d["id"])

        rescue_autofocus = None
        rescue_autofocus_factory = None
        if autofocus_dz and hasattr(scan_ext, "get_autofocus_function"):
            try:
                rescue_autofocus = scan_ext.get_autofocus_function(
                    dz=autofocus_dz,
                    use_fast_autofocus=fast_autofocus,
                )

                def rescue_autofocus_factory(dz):
                    return scan_ext.get_autofocus_function(
                        dz=int(dz), use_fast_autofocus=fast_autofocus
                    )
            except Exception:
                logging.debug("spiral rescue autofocus unavailable", exc_info=True)

        original_capture = scan_ext.capture
        original_position = list(microscope.stage.position)
        st.nx, st.ny = nx, ny
        st.last_grid = [nx, ny, nz]
        st.mapping = {}
        for ix, iy in order:
            st.mapping[(origin[0] + ix * sx, origin[1] + iy * sy)] = (ix, iy)
        st.center_tile_spec = spec
        st.center_tile_path = None
        st.center_tile_metadata = None
        st.phase = "scanning"

        def move_to_xy(xy, z=None):
            target = [int(xy[0]), int(xy[1]), int(microscope.stage.position[2] if z is None else z)]
            microscope.stage.move_abs(target)

        is_background = None
        if autofocus_dz and args.get("detect_empty_fields_and_skip_autofocus"):
            is_background = scan_ext.get_background_detect_function()

        def autofocus_passes(count):
            if (
                not autofocus_dz
                or not hasattr(scan_ext, "get_autofocus_function")
                or (is_background is not None and is_background())
            ):
                st.focus_phase = "scanning"
                st.focus_attempt = 0
                st.focus_attempts = 0
                return
            st.focus_phase = "initializing" if not st.best and count > 1 else "scanning"
            st.focus_attempts = int(count)
            for attempt in range(int(count)):
                if current_action() and current_action().stopped:
                    raise RuntimeError("Scan cancelled")
                st.focus_attempt = attempt + 1
                autofocus = scan_ext.get_autofocus_function(
                    dz=autofocus_dz, use_fast_autofocus=fast_autofocus
                )
                autofocus()
                logging.info(
                    "spiral scan fast AF: pass=%s/%s z=%s",
                    attempt + 1,
                    count,
                    int(microscope.stage.position[2]),
                )
            st.focus_phase = "scanning"

        def capture_path(result):
            path = getattr(result, "file", None) or getattr(result, "path", None)
            if isinstance(result, dict):
                path = path or result.get("file") or result.get("path")
            return str(path) if path else None

        def record_center(result, stage_position, extra):
            path = capture_path(result)
            if not path or not os.path.isfile(path):
                logging.warning("spiral centre capture returned no local file")
                return
            st.center_tile_path = path
            st.center_tile_metadata = {
                "ix": HUB_TILE_IX if extra else center_ij[0],
                "iy": HUB_TILE_IY if extra else center_ij[1],
                "grid_position": [float(spec["grid_position"][0]), float(spec["grid_position"][1])],
                "stage_position": [int(v) for v in stage_position],
                "position_source": "spiral-center-hub" if extra else "spiral-grid-center",
                "center_tile": True,
                "extra": bool(extra),
                "source_name": os.path.basename(path),
                "source_path": path,
                "captured_at": datetime.datetime.now().isoformat(),
                "units": "motor_steps",
            }
            focus_report = self._focus_report_for_path(path)
            if focus_report:
                st.center_tile_metadata["adaptive_focus"] = focus_report
            st.tile_count += 1

        def hooked_capture(*cargs, **ckwargs):
            result = original_capture(*cargs, **ckwargs)
            self.apply_adaptive_focus(
                microscope,
                result,
                enabled=bool(args.get("adaptive_focus_stack", ADAPTIVE_FOCUS_DEFAULT)),
                autofocus=rescue_autofocus,
                autofocus_factory=rescue_autofocus_factory,
                autofocus_dz=autofocus_dz,
                native_resolution=bool(args.get("native_resolution")),
            )
            try:
                self.on_tile(microscope, result, ckwargs.get("dataset"))
            except Exception:
                logging.exception("on_tile hook failed")
            self.release_scan_capture_reference(microscope, result)
            return result

        total = len(order) * nz + (1 if want_hub else 0)
        original_counts = scan_ext._images_captured_so_far, scan_ext._images_to_be_captured
        scan_ext._images_to_be_captured = total
        scan_ext.capture = hooked_capture
        completed = 0
        try:
            if want_hub:
                hub_xy = spec["stage_xy"]
                move_to_xy(hub_xy)
                autofocus_passes(SCAN_FOCUS_TILE_MAX)
                hub_annotations = dict(annotations)
                hub_annotations.update({"scan_role": "center-hub", "center_tile": True})
                scan_ext._images_captured_so_far = completed
                hub_result = original_capture(
                    microscope,
                    basename,
                    namemode=namemode,
                    temporary=temporary,
                    use_video_port=use_video_port,
                    resize=resize,
                    bayer=bool(args.get("bayer", False)),
                    dataset=dataset_d,
                    annotations=hub_annotations,
                    tags=tags + ["center-tile"],
                )
                self.apply_adaptive_focus(
                    microscope,
                    hub_result,
                    enabled=bool(args.get("adaptive_focus_stack", ADAPTIVE_FOCUS_DEFAULT)),
                    autofocus=rescue_autofocus,
                    autofocus_factory=rescue_autofocus_factory,
                    autofocus_dz=autofocus_dz,
                    native_resolution=bool(args.get("native_resolution")),
                )
                record_center(hub_result, list(microscope.stage.position), True)
                completed += 1
                try:
                    update_action_progress(int(100.0 * completed / total))
                except Exception:
                    pass
                # The hub's focused Z is the seed for the first regular tile.
                seed_z = int(microscope.stage.position[2])
            else:
                seed_z = int(original_position[2])

            for index, ij in enumerate(order):
                if current_action() and current_action().stopped:
                    break
                xy = (origin[0] + ij[0] * sx, origin[1] + ij[1] * sy)
                move_to_xy(xy, seed_z if index == 0 and not st.best else None)
                attempts = SCAN_FOCUS_TILE_MAX
                autofocus_passes(attempts)
                focus_z = int(microscope.stage.position[2])
                for plane in range(nz):
                    if current_action() and current_action().stopped:
                        break
                    if nz > 1:
                        offset = plane * sz - ((nz - 1) * sz // 2)
                        move_to_xy(xy, focus_z + offset)
                    scan_ext._images_captured_so_far = completed
                    result = scan_ext.capture(
                        microscope,
                        basename,
                        namemode=namemode,
                        temporary=temporary,
                        use_video_port=use_video_port,
                        resize=resize,
                        bayer=bool(args.get("bayer", False)),
                        dataset=dataset_d,
                        annotations=annotations,
                        tags=tags,
                    )
                    if nz == 1 and not want_hub and ij == center_ij:
                        record_center(result, list(microscope.stage.position), False)
                    completed += 1
                    try:
                        update_action_progress(int(100.0 * completed / total))
                    except Exception:
                        pass
                if nz > 1:
                    move_to_xy(xy, focus_z)
            try:
                update_action_progress(99)
            except Exception:
                pass
            return self.finalize(microscope, delete_tiles=not bool(st.keep_tiles))
        except Exception:
            st.phase = "error"
            self.save_session_state()
            raise
        finally:
            scan_ext.capture = original_capture
            scan_ext._images_captured_so_far, scan_ext._images_to_be_captured = original_counts
            try:
                microscope.stage.move_abs(original_position)
            except Exception:
                logging.exception("Failed returning to start after spiral scan")
            st.focus_phase = "idle"
            st.focus_attempt = 0
            st.focus_attempts = 0

    def run_tile_scan(self, microscope, args, stock_tile=None, acquire_lock=True):
        scan_ext = find_extension("org.openflexure.scan")
        if not scan_ext:
            abort(503, "org.openflexure.scan is not available")

        tile_fn = stock_tile or getattr(self, "_stock_tile", None) or scan_ext.tile

        basename = args.get("filename")
        if not basename:
            try:
                from openflexure_microscope.captures.capture_manager import (
                    generate_basename,
                )

                basename = generate_basename()
            except Exception:
                basename = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        requested_stride = args.get("stride_size") or [2000, 1500, 100]
        grid = args.get("grid") or [3, 3, 1]
        # Clamp absurd grids
        try:
            grid = [
                max(1, min(32, int(grid[0]))),
                max(1, min(32, int(grid[1]))),
                max(1, int(grid[2]) if len(grid) > 2 else 1),
            ]
        except Exception:
            grid = [3, 3, 1]
        style = args.get("style") or "snake"
        explore = bool(args.get("explore", False)) or str(style).lower() == "explore"
        center_tile = args.get("center_tile")
        if center_tile is None:
            center_tile = str(style).lower() == "spiral"
        center_tile = bool(center_tile)
        auto_stride = args.get("auto_stride")
        if auto_stride is None:
            auto_stride = AUTO_STRIDE_DEFAULT
        auto_stride = bool(auto_stride)
        target_overlap = args.get("target_overlap")
        if target_overlap is None:
            target_overlap = DEFAULT_OVERLAP
        target_overlap = float(target_overlap)

        # High-quality PC-backed scans use the camera's native still mode.
        # The Video Scout remains an explicit acquisition mode. An explicit
        # resize in Advanced Acquisition retains its requested video size.
        native_resolution = bool(
            args.get("mac_preview_only") and args.get("keep_tiles")
            and not args.get("resize") and not args.get("bayer")
            and not args.get("continuous_scan")
            and not (explore and args.get("explore_strategy") == "video")
        )
        use_video_port = not (args.get("bayer") or native_resolution)
        args = dict(args, use_video_port=use_video_port, native_resolution=native_resolution)
        resize = args.get("resize")
        if not resize:
            resize = SAFE_RESIZE
            logging.info("Using safe resize %s for mosaic scan", resize)
        if isinstance(resize, dict):
            resize = (int(resize["width"]), int(resize["height"]))
        if args.get("continuous_scan"):
            resize = (1280, 960)
        if args.get("quality_autofocus"):
            resize = SAFE_RESIZE
        if args.get("bayer") or native_resolution:
            # Native stills are encoded by the camera without software scaling.
            # PiCamera also requires this path for the optional Bayer trailer.
            resize = None

        def _run():
            st = self.state
            append = bool(args.get("append_session", False))
            keep_tiles = bool(args.get("keep_tiles", False))
            fill_gaps = bool(args.get("fill_gaps", True))
            do_explore = bool(explore)
            want_sid = (args.get("session_id") or st.session_id or "").strip()

            # The Pi may still hold a different, more recent scan in memory
            # while the user opens an older Canvas from the Mac and chooses
            # Continue. A populated state is not sufficient: it must be the
            # requested Canvas. Hydrate that exact session before planning.
            if append and want_sid and str(st.session_id or "") != want_sid:
                try:
                    self.hydrate_from_mac(want_sid)
                except Exception:
                    logging.exception(
                        "hydrate requested Canvas from PC failed for Continue (%s)",
                        want_sid,
                    )

            if append and (not st.best or not st.origin_xy or not st.stride_size):
                if want_sid:
                    try:
                        self.hydrate_from_mac(want_sid)
                    except Exception:
                        logging.exception(
                            "hydrate from PC failed for Continue (%s)", want_sid
                        )

            if append and not (st.best and st.origin_xy and st.stride_size):
                raise ValueError(
                    "Continue failed: no session path on Pi/PC (hydrate first)"
                )
            if append and want_sid and str(st.session_id or "") != want_sid:
                raise ValueError(
                    "Continue refused session mismatch: requested {} but loaded {}".format(
                        want_sid, st.session_id
                    )
                )

            if append and st.best and st.origin_xy and st.stride_size:
                # Continue: keep mosaic + tiles; reuse stride/pps/origin/basename
                logging.info(
                    "Appending to mosaic session %s (%s cells) fill_gaps=%s explore=%s",
                    st.session_id,
                    len(st.best),
                    fill_gaps,
                    do_explore,
                )
                if args.get("view_id"):
                    st.view_id = str(args.get("view_id") or "").strip() or st.view_id
                # Keep original basename/folder so cleanup covers all tiles
                if not st.basename:
                    st.basename = basename
                if not st.folder:
                    st.folder = "SCAN_{}".format(st.basename)
                st.keep_tiles = True
                st.mac_preview_only = bool(args.get("mac_preview_only", False))
                st.auto_refine = bool(args.get("auto_refine", False))
                st.refined = False
                st.phase = "calibrating"
                stride_size = [
                    int(st.stride_size[0]),
                    int(st.stride_size[1]),
                    int(st.stride_size[2]) if len(st.stride_size) > 2 else 100,
                ]
                if auto_stride and bool(args.get("recalibrate", False)):
                    calib = calibrate_stride(
                        microscope,
                        target_overlap=target_overlap,
                        probe_steps=CALIB_PROBE_STEPS,
                        frame_size=resize if resize else SAFE_RESIZE,
                    )
                    st.calib = calib
                    if calib.get("px_per_step"):
                        st.px_per_step = list(calib["px_per_step"])
                    if calib.get("axis_sign"):
                        st.axis_sign = [int(calib["axis_sign"][0]), int(calib["axis_sign"][1])]
                    if calib.get("stride_xy"):
                        stride_size[0] = int(calib["stride_xy"][0])
                        stride_size[1] = int(calib["stride_xy"][1])
                initial = tuple(microscope.stage.position[:2])
                st.initial_xy = [int(initial[0]), int(initial[1])]
                st.last_grid = [
                    int(grid[0]),
                    int(grid[1]),
                    int(grid[2]) if len(grid) > 2 else 1,
                ]
                st.stride_size = (
                    int(stride_size[0]),
                    int(stride_size[1]),
                    int(stride_size[2]),
                )
                if not st.mosaic_path:
                    self.ensure_mosaic(microscope, st.basename)

                if do_explore:
                    st.phase = "scanning"
                    return self.run_explore_scan(
                        microscope,
                        scan_ext,
                        args,
                        stride_size,
                        st.basename,
                        resize,
                        seed_capture=False,
                    )

                if fill_gaps:
                    missing, meta = self.plan_missing_cells(grid, center_xy=initial)
                    logging.info(
                        "Continue plan %s → %s missing",
                        meta,
                        len(missing),
                    )
                    # Refresh mapping for nearest fallback
                    for (ij, xy) in missing:
                        st.mapping[xy] = ij
                    if meta:
                        st.plan_bounds = (
                            int(meta["i0"]),
                            int(meta["j0"]),
                            int(meta["nx"]),
                            int(meta["ny"]),
                        )
                        st.min_ix = int(meta["i0"])
                        st.min_iy = int(meta["j0"])
                        st.nx = max(1, int(meta["nx"]))
                        st.ny = max(1, int(meta["ny"]))
                    st.phase = "scanning"
                    return self.scan_missing_cells(
                        microscope,
                        scan_ext,
                        missing,
                        args,
                        stride_size,
                        st.basename,
                        resize,
                    )

                # Legacy: full re-scan of a new block (no fill_gaps)
                mapping, nx, ny = spatial_index_map(
                    initial,
                    (int(stride_size[0]), int(stride_size[1])),
                    (int(grid[0]), int(grid[1])),
                    style=style,
                )
                st.mapping.update(mapping)
                st.phase = "scanning"
            else:
                st.reset()
                st.basename = basename
                st.folder = "SCAN_{}".format(basename)
                st.keep_tiles = keep_tiles
                st.mac_preview_only = bool(args.get("mac_preview_only", False))
                st.auto_refine = bool(args.get("auto_refine", False))
                st.session_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
                st.view_id = (args.get("view_id") or "").strip() or None
                st.refined = False
                st.phase = "calibrating"
                st.empty_cells = set()
                # Know grid shape before seed so 1×1 keeps full preview resolution
                if do_explore:
                    st.nx, st.ny = 1, 1
                else:
                    st.nx = max(1, int(grid[0]))
                    st.ny = max(1, int(grid[1]))
                # Wipe previous live preview so UI cannot flash an old mosaic
                self.clear_live_preview()
                # Lock exposure/AWB once before capturing tiles
                lock_camera_for_scan(microscope)
                # Immediate Start FOV for Scan main view (before long calibration)
                self.seed_start_preview(microscope)

                stride_size = [
                    int(requested_stride[0]),
                    int(requested_stride[1]),
                    int(requested_stride[2]) if len(requested_stride) > 2 else 100,
                ]
                calib = None
                if auto_stride:
                    logging.info(
                        "Pre-scan stride calibration (target_overlap=%.2f)...",
                        target_overlap,
                    )
                    calib = calibrate_stride(
                        microscope,
                        target_overlap=target_overlap,
                        probe_steps=CALIB_PROBE_STEPS,
                        frame_size=resize if resize else SAFE_RESIZE,
                    )
                    st.calib = calib
                    if calib.get("px_per_step"):
                        st.px_per_step = list(calib["px_per_step"])
                    if calib.get("axis_sign"):
                        st.axis_sign = [int(calib["axis_sign"][0]), int(calib["axis_sign"][1])]
                    if calib.get("stride_xy"):
                        stride_size[0] = int(calib["stride_xy"][0])
                        stride_size[1] = int(calib["stride_xy"][1])
                        logging.info(
                            "Using calibrated stride_size=%s (requested was %s)",
                            stride_size,
                            requested_stride,
                        )
                else:
                    st.px_per_step = list(CAL_PX_PER_STEP)
                    st.axis_sign = [-1, -1]
                    logging.info("auto_stride=false; using requested stride %s", stride_size)

                initial = tuple(microscope.stage.position[:2])
                st.initial_xy = [int(initial[0]), int(initial[1])]
                st.origin_xy = list(st.initial_xy)
                st.stride_size = (
                    int(stride_size[0]),
                    int(stride_size[1]),
                    int(stride_size[2]),
                )
                self.ensure_mosaic(microscope, basename)

                if do_explore and args.get("explore_strategy") == "video":
                    return self.run_video_scout_scan(microscope, scan_ext, args, basename)
                if do_explore:
                    st.last_grid = ["explore", "explore", 1]
                    st.nx, st.ny = 1, 1
                    st.phase = "scanning"
                    return self.run_explore_scan(
                        microscope,
                        scan_ext,
                        args,
                        stride_size,
                        basename,
                        resize,
                        seed_capture=True,
                    )

                if args.get("continuous_scan"):
                    return self.run_video_scout_scan(microscope, scan_ext, args, basename)

                mapping, nx, ny = spatial_index_map(
                    initial,
                    (int(stride_size[0]), int(stride_size[1])),
                    (int(grid[0]), int(grid[1])),
                    style=style,
                )
                st.mapping = mapping
                st.nx, st.ny = nx, ny
                st.min_ix, st.min_iy = 0, 0
                st.last_grid = [int(grid[0]), int(grid[1]), int(grid[2]) if len(grid) > 2 else 1]
                st.plan_bounds = (0, 0, int(nx), int(ny))
                st.phase = "scanning"

                if str(style).lower() == "spiral":
                    return self.run_spiral_center_scan(
                        microscope,
                        scan_ext,
                        args,
                        stride_size,
                        grid,
                        basename,
                        resize,
                        include_center=center_tile,
                    )

            rescue_autofocus = None
            rescue_autofocus_factory = None
            original_capture = scan_ext.capture

            def hooked_capture(*cargs, **ckwargs):
                if args.get("quality_autofocus"):
                    try:
                        report = quality_autofocus(microscope)
                    except FocusQualityWarning as exc:
                        report = {"accepted": False, "warning": str(exc), "position": list(microscope.stage.position)}
                        logging.warning("Focus advisory; continuing capture: %s", exc)
                    ckwargs["annotations"] = dict(ckwargs.get("annotations") or {})
                    ckwargs["annotations"]["quality_autofocus"] = report
                result = original_capture(*cargs, **ckwargs)
                self.apply_adaptive_focus(
                    microscope,
                    result,
                    enabled=bool(args.get("adaptive_focus_stack", ADAPTIVE_FOCUS_DEFAULT)),
                    autofocus=rescue_autofocus,
                    autofocus_factory=rescue_autofocus_factory,
                    autofocus_dz=int(args.get("autofocus_dz") or 2000),
                    native_resolution=bool(args.get("native_resolution")),
                )
                try:
                    dataset = ckwargs.get("dataset")
                    self.on_tile(microscope, result, dataset)
                except Exception:
                    logging.exception("on_tile hook failed")
                self.release_scan_capture_reference(microscope, result)
                return result

            scan_ext.capture = hooked_capture
            # Keep OpenFlexure's Fast-AF as the actual autofocus engine.  Every
            # tile gets one native pass; clarity classification and any rescue
            # happen after the source capture and never add source frames.
            original_get_autofocus = getattr(scan_ext, "get_autofocus_function", None)
            # Use the stock OpenFlexure Fast-AF engine.  Repeated rescue passes
            # are created separately with wider dz values and never reach the
            # capture manager.
            focus_guard_enabled = bool(args.get("scan_focus_guard", True))

            def _guarded_get_autofocus_function(*gargs, **gkwargs):
                if not focus_guard_enabled or original_get_autofocus is None:
                    return original_get_autofocus(*gargs, **gkwargs)

                requested_dz = int(gkwargs.get("dz") or args.get("autofocus_dz") or 2000)
                use_fast = bool(gkwargs.get("use_fast_autofocus", True))

                def guarded_autofocus(*aargs, **akwargs):
                    max_attempts = SCAN_FOCUS_TILE_MAX
                    last_result = None

                    for attempt in range(max_attempts):
                        st.focus_phase = "scanning"
                        st.focus_attempt = attempt + 1
                        st.focus_attempts = max_attempts
                        factory_kwargs = dict(gkwargs)
                        # Never narrow or replace the requested OpenFlexure
                        # fast-AF search range.
                        factory_kwargs["dz"] = requested_dz
                        factory_kwargs["use_fast_autofocus"] = use_fast
                        autofocus = original_get_autofocus(*gargs, **factory_kwargs)
                        last_result = autofocus(*aargs, **akwargs)
                        logging.info(
                            "scan fast AF pass: attempt=%s/%s z=%s",
                            attempt + 1,
                            max_attempts,
                            int(microscope.stage.position[2]),
                        )

                    st.focus_phase = "scanning"
                    st.focus_attempt = max_attempts
                    st.focus_attempts = max_attempts
                    logging.info(
                        "scan fast AF complete: z=%s passes=%s saved_frames=0",
                        int(microscope.stage.position[2]),
                        max_attempts,
                    )
                    return last_result

                return guarded_autofocus

            # Keep unwrapped native Fast-AF callables for progressive global
            # defocus rescue.  The regular scan wrapper above controls the one
            # native pass used to acquire each tile.
            if original_get_autofocus is not None:
                def _native_rescue_autofocus():
                    requested_dz = int(
                        args.get("autofocus_dz") or 2000
                    )
                    autofocus = original_get_autofocus(
                        dz=requested_dz,
                        use_fast_autofocus=True,
                    )
                    return autofocus()

                rescue_autofocus = _native_rescue_autofocus

                def _native_rescue_autofocus_factory(dz):
                    return original_get_autofocus(
                        dz=int(dz), use_fast_autofocus=True
                    )

                rescue_autofocus_factory = _native_rescue_autofocus_factory

            if original_get_autofocus is not None and focus_guard_enabled:
                scan_ext.get_autofocus_function = _guarded_get_autofocus_function
            try:
                tile_fn(
                    microscope,
                    basename=basename,
                    namemode=args.get("namemode") or "coordinates",
                    temporary=bool(args.get("temporary", False)),
                    stride_size=tuple(stride_size),
                    grid=tuple(grid),
                    style=style,
                    autofocus_dz=0 if args.get("quality_autofocus") else int(args.get("autofocus_dz", 2000)),
                    use_video_port=use_video_port,
                    resize=resize,
                    bayer=bool(args.get("bayer", False)),
                    fast_autofocus=bool(args.get("fast_autofocus", True)),
                    annotations=args.get("annotations") or {},
                    tags=args.get("tags") or [],
                    detect_empty_fields_and_skip_autofocus=bool(
                        args.get("detect_empty_fields_and_skip_autofocus", False)
                    ),
                )
                return self.finalize(
                    microscope, delete_tiles=not bool(st.keep_tiles)
                )
            except Exception:
                st.phase = "error"
                self.save_session_state()
                raise
            finally:
                scan_ext.capture = original_capture
                if original_get_autofocus is not None and focus_guard_enabled:
                    scan_ext.get_autofocus_function = original_get_autofocus
                st.focus_phase = "idle"
                st.focus_attempt = 0
                st.focus_attempts = 0

        # The OpenFlexure microscope/stage locks are still useful for
        # component safety, but this lease is the MicroWorld scheduling
        # authority.  It spans the complete scan, including all moves,
        # autofocus passes, capture, and finalisation.
        self.ensure_hardware_hooks()
        with self.hardware.hold(
            "scan",
            session_id=args.get("session_id"),
            phase="starting",
        ):
            if acquire_lock:
                with microscope.lock(timeout=1):
                    result = _run()
            else:
                result = _run()
            if self.state.mac_preview_only and self.state.keep_tiles and self.state.tile_count:
                self.hardware.mark_processing(self.state.session_id)
            return result

    def patch_stock_scan(self):
        """Make stock org.openflexure.scan/tile also run live mosaic + cleanup."""
        self.ensure_hardware_hooks()
        if getattr(self, "_stock_patched", False):
            return True
        scan_ext = find_extension("org.openflexure.scan")
        if not scan_ext:
            return False
        if getattr(scan_ext, "_openmhs_mosaic_patched", False):
            self._stock_tile = getattr(scan_ext, "_openmhs_stock_tile", scan_ext.tile)
            self._stock_patched = True
            return True

        stock_tile = scan_ext.tile
        self._stock_tile = stock_tile
        mosaic_ext = self

        def wrapped_tile(microscope, **kwargs):
            args = {
                "filename": kwargs.get("basename"),
                "namemode": kwargs.get("namemode", "coordinates"),
                "temporary": kwargs.get("temporary", False),
                "stride_size": kwargs.get("stride_size", (2000, 1500, 100)),
                "grid": kwargs.get("grid", (3, 3, 1)),
                "style": kwargs.get("style", "snake"),
                "autofocus_dz": kwargs.get("autofocus_dz", 2000),
                "use_video_port": kwargs.get("use_video_port", True),
                "resize": kwargs.get("resize") or SAFE_RESIZE,
                "bayer": kwargs.get("bayer", False),
                "fast_autofocus": kwargs.get("fast_autofocus", True),
                "annotations": kwargs.get("annotations") or {},
                "tags": kwargs.get("tags") or [],
                "detect_empty_fields_and_skip_autofocus": kwargs.get(
                    "detect_empty_fields_and_skip_autofocus", False
                ),
                "auto_stride": kwargs.get("auto_stride", AUTO_STRIDE_DEFAULT),
                "target_overlap": kwargs.get("target_overlap", DEFAULT_OVERLAP),
                "keep_tiles": kwargs.get("keep_tiles", False),
                "mac_preview_only": kwargs.get("mac_preview_only", False),
                "explore_strategy": kwargs.get("explore_strategy", "boundary"),
                "explore_max_cells": kwargs.get("explore_max_cells", EXPLORE_MAX_CELLS),
                "explore_max_radius": kwargs.get("explore_max_radius", EXPLORE_MAX_RADIUS),
                "append_session": kwargs.get("append_session", False),
            }
            # OF TileScanAPI already holds microscope.lock
            return mosaic_ext.run_tile_scan(
                microscope, args, stock_tile=stock_tile, acquire_lock=False
            )

        scan_ext.tile = wrapped_tile
        scan_ext._openmhs_stock_tile = stock_tile
        scan_ext._openmhs_mosaic_patched = True
        self._stock_patched = True
        logging.info("Patched org.openflexure.scan.tile for live mosaic")
        return True


class CaptureListView(View):
    def get(self):
        from openflexure_microscope.api.v2.views.captures import CaptureList

        # A dictionary works on Flask releases that cannot return a list
        # directly from the stock capture view.
        captures = CaptureList().get()
        rows = CaptureList.schema.dump(list(captures))
        return {row["id"]: row for row in rows}


class ConnectionView(View):
    """Claim, renew or release the desktop connection at the Pi."""

    def _gate(self):
        gate = self.extension.hardware
        gate.install_connection_guard(current_app, current_labthing().url_prefix)
        return gate

    def get(self):
        return self._gate().connection_status()

    def post(self):
        import ipaddress
        from werkzeug.exceptions import Conflict

        if request.headers.get("Origin"):
            return {"error": "Use the MicroWorld desktop app to connect."}, 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return {"error": "Expected a JSON connection request."}, 400
        operation = payload.get("action")

        def actions_busy():
            return any(task.is_alive() for task in current_labthing().actions.tasks())

        try:
            if operation == "claim":
                self.extension.ensure_hardware_hooks()
            address = ipaddress.ip_address(request.remote_addr)
            host = "[{}]".format(address) if address.version == 6 else str(address)
            return self._gate().connection_command(
                operation, request.headers.get(CONNECTION_HEADER),
                computer=str(payload.get("computer") or ""), session_id=payload.get("session_id"),
                takeover=payload.get("takeover") is True, host=host, actions_busy=actions_busy,
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except Conflict as exc:
            return {"error": exc.description}, 409


class HardwareStatusView(View):
    """Cross-device view of the Pi's single hardware scheduling lease."""

    def get(self):
        try:
            self.extension.ensure_hardware_hooks()
        except Exception:
            logging.debug("hardware hook status refresh failed", exc_info=True)
        self.extension.hardware.install_connection_guard(current_app, current_labthing().url_prefix)
        state = self.extension.hardware.status()
        scan = self.extension.state
        if state.get("busy") and state.get("operation") == "scan":
            state["phase"] = getattr(scan, "phase", state.get("phase"))
            state["session_id"] = getattr(scan, "session_id", state.get("session_id"))
        state["scan_phase"] = getattr(scan, "phase", "idle")
        state["scan_session_id"] = getattr(scan, "session_id", None)
        state["scan_tiles_seen"] = int(getattr(scan, "tile_count", 0) or 0)
        state["advanced_capture"] = True
        state["video_scout"] = True
        state["continuous_scan"] = True
        state["native_resolution_scan"] = True
        state["focus_phase"] = getattr(scan, "focus_phase", "idle")
        state["bayer_scan"] = True
        state["stage_hook"] = bool(getattr(self.extension, "_stage_hardware_hook_ready", False))
        state["autofocus_hook"] = bool(
            getattr(self.extension, "_autofocus_hardware_hook_ready", False)
        )
        return state


class StatusView(View):
    def get(self):
        patched = False
        try:
            patched = bool(self.extension.patch_stock_scan())
        except Exception:
            logging.exception("patch_stock_scan failed")
        st = self.extension.state
        preview_mtime = None
        if st.preview_path and os.path.isfile(st.preview_path):
            preview_mtime = os.path.getmtime(st.preview_path)
        return {
            "ok": True,
            "extension": EXTENSION_ID,
            "stock_scan_patched": patched,
            "mosaic": st.mosaic_name,
            "mosaic_id": st.mosaic_id,
            "tiles_seen": st.tile_count,
            "cells": len(st.best),
            "dataset_id": st.dataset_id,
            "phase": st.phase,
            "stride_size": list(st.stride_size) if st.stride_size else None,
            "px_per_step": list(st.px_per_step) if st.px_per_step else None,
            "calibration": st.calib,
            "latest_preview_stitch_time": preview_mtime,
            "session_id": st.session_id,
            "view_id": getattr(st, "view_id", None),
            "refined": bool(st.refined),
            "origin_xy": list(st.origin_xy) if st.origin_xy else None,
            "initial_xy": list(st.initial_xy) if st.initial_xy else None,
            "nx": st.nx,
            "ny": st.ny,
            "min_ix": st.min_ix,
            "min_iy": st.min_iy,
            "refine_progress": int(getattr(st, "refine_progress", 0) or 0),
            "last_grid": list(st.last_grid) if getattr(st, "last_grid", None) else None,
            "focus_phase": getattr(st, "focus_phase", "idle"),
            "focus_attempt": int(getattr(st, "focus_attempt", 0) or 0),
            "focus_attempts": int(getattr(st, "focus_attempts", 0) or 0),
            "center_tile": getattr(st, "center_tile_metadata", None),
            "mac_preview_only": bool(getattr(st, "mac_preview_only", False)),
            "hardware": self.extension.hardware.status(),
        }


class LiveDetailsView(PropertyView):
    """v3-like live scan details for UI polling."""

    def get(self):
        now = time.monotonic()
        with _LIVE_DETAILS_CACHE_LOCK:
            cached = _LIVE_DETAILS_CACHE.get("payload")
            if cached is not None and now - float(_LIVE_DETAILS_CACHE.get("at") or 0.0) <= LIVE_DETAILS_CACHE_TTL_S:
                return cached
        try:
            self.extension.patch_stock_scan()
        except Exception:
            pass
        st = self.extension.state
        mtime = None
        if st.preview_path and os.path.isfile(st.preview_path):
            mtime = os.path.getmtime(st.preview_path)
        # Advertise preview for active sessions (incl. Start-seed before tiles)
        active = preview_session_active(st)
        payload = {
            "name": st.basename,
            "image_count": st.tile_count,
            "cell_count": len(st.best),
            "cells_expected": len(st.best) if st.last_grid and st.last_grid[0] == "explore"
                              else int(st.nx) * int(st.ny),
            "grid": [st.nx, st.ny],
            "scan_phase": st.phase,
            "alignment_quality": getattr(st, "alignment_quality", {}),
            "tile_metadata": getattr(st, "tile_metadata", {}),
            "stitch_timestamp": mtime if active else None,
            "mosaic_id": st.mosaic_id,
            "mosaic_name": st.mosaic_name,
            "preview_ready": bool(active and mtime is not None),
            "partial": len(st.best) < int(st.nx) * int(st.ny),
            "session_id": st.session_id,
            "view_id": getattr(st, "view_id", None),
            "refined": bool(st.refined),
            "origin_xy": list(st.origin_xy) if st.origin_xy else None,
            "initial_xy": list(st.initial_xy) if st.initial_xy else None,
            "stride_size": list(st.stride_size) if st.stride_size else None,
            "px_per_step": list(st.px_per_step) if st.px_per_step else None,
            "axis_sign": list(getattr(st, "axis_sign", [-1, -1]) or [-1, -1]),
            "min_ix": getattr(st, "min_ix", 0),
            "min_iy": getattr(st, "min_iy", 0),
            "refine_progress": int(getattr(st, "refine_progress", 0) or 0),
            "last_grid": list(st.last_grid) if getattr(st, "last_grid", None) else None,
            "focus_phase": getattr(st, "focus_phase", "idle"),
            "focus_attempt": int(getattr(st, "focus_attempt", 0) or 0),
            "focus_attempts": int(getattr(st, "focus_attempts", 0) or 0),
            "center_tile": getattr(st, "center_tile_metadata", None),
            "mac_preview_only": bool(getattr(st, "mac_preview_only", False)),
            "hardware": self.extension.hardware.status(),
            "layout": self.extension.tile_layout_payload(),
        }
        with _LIVE_DETAILS_CACHE_LOCK:
            _LIVE_DETAILS_CACHE["at"] = time.monotonic()
            _LIVE_DETAILS_CACHE["payload"] = payload
        return payload


class PreviewStitchView(View):
    """GET latest preview stitch JPEG (same role as v3 latest_preview_stitch.jpg)."""

    def get(self):
        st = self.extension.state
        active = preview_session_active(st)
        path = st.preview_path
        if not active or not path or not os.path.isfile(path):
            return {"error": "File not found"}, 404
        return send_file(path, mimetype="image/jpeg")


class PreviewUiView(View):
    """Minimal live-preview page (works without rebuilding the OF webapp)."""

    def get(self):
        html = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MicroWorld Live Stitch Preview</title>
<style>
 body{margin:0;font-family:system-ui,sans-serif;background:#0f1419;color:#e8eef4}
 header{padding:12px 16px;border-bottom:1px solid #243040}
 .meta{color:#8b9aab;font-size:14px}
 main{padding:12px;display:flex;justify-content:center}
 img{max-width:100%;max-height:80vh;background:#000}
 .ph{padding:40px;color:#8b9aab}
</style></head><body>
<header>
 <div><b>Live stitching preview</b> (v3-style on v2)</div>
 <div class="meta" id="meta">loading…</div>
</header>
<main><div id="box" class="ph">Waiting for tiles…</div></main>
<script>
const meta=document.getElementById('meta');
const box=document.getElementById('box');
const base=location.pathname.replace(/\\/preview_ui\\/?$/,'');
async function tick(){
  try{
    const r=await fetch(base+'/live_details',{cache:'no-store'});
    const d=await r.json();
    meta.textContent=`phase=${d.scan_phase||'-'} · tiles=${d.image_count||0} · cells=${d.cell_count||0} · ${d.name||''}`;
    if(d.preview_ready && d.stitch_timestamp){
      box.innerHTML=`<img src="${base}/latest_preview_stitch.jpg?t=${d.stitch_timestamp}" alt="preview"/>`;
    }
  }catch(e){ meta.textContent='offline / restarting'; }
}
tick(); setInterval(tick,1000);
</script></body></html>"""
        return Response(html, mimetype="text/html; charset=utf-8")


def _tile_scan_args_class():
    """Prefer OF TileScanArgs extended with mosaic options; fall back if needed."""
    try:
        from openflexure_microscope.api.default_extensions.scan import TileScanArgs

        class MosaicTileScanArgs(TileScanArgs):
            style = fields.String(load_default="snake")
            quality_autofocus = fields.Boolean(load_default=False)
            auto_stride = fields.Boolean(load_default=AUTO_STRIDE_DEFAULT)
            target_overlap = fields.Float(load_default=DEFAULT_OVERLAP)
            keep_tiles = fields.Boolean(load_default=False)
            append_session = fields.Boolean(load_default=False)
            auto_refine = fields.Boolean(load_default=False)
            fill_gaps = fields.Boolean(load_default=True)
            explore = fields.Boolean(load_default=False)
            recalibrate = fields.Boolean(load_default=False)
            scan_focus_guard = fields.Boolean(load_default=True)
            adaptive_focus_stack = fields.Boolean(load_default=ADAPTIVE_FOCUS_DEFAULT)
            mac_preview_only = fields.Boolean(load_default=False)
            explore_strategy = fields.String(load_default="boundary")
            continuous_scan = fields.Boolean(load_default=False)
            explore_max_cells = fields.Integer(load_default=EXPLORE_MAX_CELLS)
            explore_max_radius = fields.Integer(load_default=EXPLORE_MAX_RADIUS)
            center_tile = fields.Boolean(load_default=False)
            # Continue-from-PC: which Mac archive to hydrate if Pi has no tiles
            session_id = fields.String(load_default=None, allow_none=True)
            view_id = fields.String(load_default=None, allow_none=True)

        return MosaicTileScanArgs
    except Exception:
        logging.debug("Could not extend OF TileScanArgs", exc_info=True)

    try:
        from openflexure_microscope.api.v2.views.actions.camera import FullCaptureArgs

        class TileScanArgs(FullCaptureArgs):
            quality_autofocus = fields.Boolean(load_default=False)
            namemode = fields.String(load_default="coordinates")
            grid = fields.List(fields.Integer(), load_default=[3, 3, 1])
            style = fields.String(load_default="snake")
            autofocus_dz = fields.Integer(load_default=2000)
            fast_autofocus = fields.Boolean(load_default=True)
            stride_size = fields.List(fields.Integer(), load_default=[2000, 1500, 100])
            detect_empty_fields_and_skip_autofocus = fields.Boolean(load_default=False)
            auto_stride = fields.Boolean(load_default=AUTO_STRIDE_DEFAULT)
            target_overlap = fields.Float(load_default=DEFAULT_OVERLAP)
            keep_tiles = fields.Boolean(load_default=False)
            append_session = fields.Boolean(load_default=False)
            auto_refine = fields.Boolean(load_default=False)
            fill_gaps = fields.Boolean(load_default=True)
            explore = fields.Boolean(load_default=False)
            recalibrate = fields.Boolean(load_default=False)
            scan_focus_guard = fields.Boolean(load_default=True)
            adaptive_focus_stack = fields.Boolean(load_default=ADAPTIVE_FOCUS_DEFAULT)
            mac_preview_only = fields.Boolean(load_default=False)
            explore_strategy = fields.String(load_default="boundary")
            continuous_scan = fields.Boolean(load_default=False)
            explore_max_cells = fields.Integer(load_default=EXPLORE_MAX_CELLS)
            explore_max_radius = fields.Integer(load_default=EXPLORE_MAX_RADIUS)
            center_tile = fields.Boolean(load_default=True)
            session_id = fields.String(load_default=None, allow_none=True)
            view_id = fields.String(load_default=None, allow_none=True)

        return TileScanArgs
    except Exception:
        logging.exception("Could not build TileScanArgs")
        return None


TileScanArgsSchema = _tile_scan_args_class()




class CalibrateScaleView(View):
    """POST: jog stage, measure pixel shift → px/step for the current optics setup."""

    def post(self):
        from flask import request

        st = self.extension.state
        if st.phase in ("calibrating", "scanning", "stitching", "refining"):
            abort(409, "busy: {}".format(st.phase))
        microscope = find_component("org.openflexure.microscope")
        if microscope is None:
            abort(503, "microscope unavailable")
        body = request.get_json(silent=True) or {}
        probe = int(body.get("probe_steps") or CALIB_PROBE_STEPS)
        probe = max(100, min(2500, probe))
        overlap = float(body.get("target_overlap") or DEFAULT_OVERLAP)
        prev_phase = st.phase
        self.extension.ensure_hardware_hooks()
        with self.extension.hardware.hold("calibrate", phase="calibrating"):
            st.phase = "calibrating"
            try:
                calib = calibrate_stride(
                    microscope,
                    target_overlap=overlap,
                    probe_steps=probe,
                    frame_size=SAFE_RESIZE,
                )
                if calib.get("px_per_step"):
                    st.px_per_step = list(calib["px_per_step"])
                st.calib = calib
                return calib
            finally:
                if st.phase == "calibrating":
                    st.phase = prev_phase if prev_phase != "calibrating" else "idle"


class RefineView(ActionView):
    """POST: delegate retained-tile refine to the Mac worker."""

    default_stop_timeout = 600

    def post(self):
        sid = self.extension.state.session_id
        if not sid:
            abort(400, "No scan session")
        data, _ = _proxy_mac(
            "/mosaic/{}/refine".format(sid),
            method="POST",
            body={"align": "full"},
            timeout=30,
        )
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker refine response")


class TileAtView(View):
    """GET retained tile JPEG by normalized mosaic preview coordinates."""

    def get(self):
        from flask import make_response, request

        st = self.extension.state
        req_sid = request.args.get("session_id") or request.args.get("sid")
        if req_sid and st.session_id and str(req_sid) != str(st.session_id):
            abort(409, "session mismatch")

        def _send(path):
            resp = make_response(send_file(path, mimetype="image/jpeg"))
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            if st.session_id:
                resp.headers["X-MicroWorld-Session"] = resp.headers["X-OpenMHS-Session"] = str(st.session_id)
            return resp

        nx = request.args.get("nx", request.args.get("x"))
        ny = request.args.get("ny", request.args.get("y"))
        if nx is None or ny is None:
            # also allow integer indices
            ix = request.args.get("ix")
            iy = request.args.get("iy")
            if ix is None or iy is None:
                abort(400, "Provide nx&ny or ix&iy")
            try:
                key = (int(ix), int(iy))
            except (TypeError, ValueError):
                abort(400, "Invalid ix/iy")
            hit = st.best.get(key)
            path = hit[1] if hit else None
            if path and os.path.isfile(path):
                return _send(path)
            # Occupancy-only / Continue: fall back to PC archive tile
            sid = st.session_id or req_sid
            if sid:
                try:
                    data, ctype = _proxy_mac(
                        "/mosaic/{}/tile".format(sid),
                        query={"ix": int(key[0]), "iy": int(key[1])},
                        timeout=60,
                    )
                    resp = make_response(data)
                    resp.headers["Content-Type"] = ctype or "image/jpeg"
                    resp.headers["Cache-Control"] = "no-store"
                    if st.session_id:
                        resp.headers["X-MicroWorld-Session"] = resp.headers["X-OpenMHS-Session"] = str(st.session_id)
                    return resp
                except Exception:
                    logging.debug(
                        "tile_at PC Worker fallback failed for %s", key, exc_info=True
                    )
            abort(404, "No tile at that index")

        path, meta = self.extension.tile_path_at_norm(nx, ny)
        if not path:
            abort(404, "No tile at that location")
        resp = _send(path)
        try:
            if meta:
                resp.headers["X-MicroWorld-Tile-IX"] = resp.headers["X-OpenMHS-Tile-IX"] = str(meta.get("ix"))
                resp.headers["X-MicroWorld-Tile-IY"] = resp.headers["X-OpenMHS-Tile-IY"] = str(meta.get("iy"))
        except Exception:
            pass
        return resp


class CenterTileView(View):
    """GET the extra half-step centre exposure for Mac-side refinement."""

    def get(self):
        from flask import make_response, request

        st = self.extension.state
        req_sid = request.args.get("session_id") or request.args.get("sid")
        if req_sid and st.session_id and str(req_sid) != str(st.session_id):
            abort(409, "session mismatch")
        path = getattr(st, "center_tile_path", None)
        if not path and getattr(st, "center_tile_metadata", None):
            path = st.center_tile_metadata.get("source_path")
        if not path or not os.path.isfile(path):
            # A restart can restore the metadata while the capture remains in
            # the scan folder; recover it without scanning the whole disk.
            folder = getattr(st, "folder", None)
            if folder and os.path.isdir(folder):
                for root, _dirs, files in os.walk(folder):
                    for name in files:
                        if name.lower().endswith((".jpg", ".jpeg")) and "center" in name.lower():
                            path = os.path.join(root, name)
                            break
                    if path and os.path.isfile(path):
                        break
        if not path or not os.path.isfile(path):
            abort(404, "No centre tile for this session")
        resp = make_response(send_file(path, mimetype="image/jpeg"))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        if st.session_id:
            resp.headers["X-MicroWorld-Session"] = resp.headers["X-OpenMHS-Session"] = str(st.session_id)
        return resp


class TileLayoutView(View):
    """GET stitch layout: normalized tile poses for zoom LOD overlays."""

    def get(self):
        return self.extension.tile_layout_payload()


class CleanupTilesView(View):
    """POST: delete retained SCAN tiles after exiting Scan Mode.

    Body: { dataset_id?, save_slide?: bool }
    save_slide=true keeps the LIVE mosaic as a finished slide; tiles still deleted.
    """

    def post(self):
        from flask import request

        microscope = find_component("org.openflexure.microscope")
        if not microscope:
            abort(503, "No microscope connected.")
        body = request.get_json(silent=True) or {}
        dataset_id = body.get("dataset_id")
        save_slide = bool(body.get("save_slide", False))
        tiles_only = bool(body.get("tiles_only", False))
        keep_preview = bool(body.get("keep_preview", False))
        result = self.extension.delete_scan_tiles(
            microscope,
            dataset_id=dataset_id,
            save_slide=save_slide,
            tiles_only=tiles_only,
            keep_preview=keep_preview,
        )
        if isinstance(result, dict):
            out = {"ok": True}
            out.update(result)
            return out
        return {"ok": True, "deleted": result}


class HydrateFromMacView(View):
    """POST: rebuild Pi occupancy from a PC mosaic session for Continue."""

    def post(self):
        from flask import request

        body = request.get_json(silent=True) or {}
        sid = (body.get("session_id") or "").strip()
        try:
            return self.extension.hydrate_from_mac(sid or None)
        except ValueError as exc:
            abort(400, str(exc))
        except Exception as exc:
            logging.exception("Restore from PC Worker failed")
            abort(502, "hydrate failed: {}".format(exc))


class AdaptiveAutofocusView(ActionView):
    """Native Fast AF followed by a conditional tilt-aware focus stack.

    The stock OpenFlexure autofocus remains the first and final mechanical
    focus reference.  The adaptive pass only probes a few Z planes and, when
    different image blocks clearly prefer different planes, replaces the
    temporary preview with a locally fused stack result.
    """

    default_stop_timeout = 180

    def post(self):
        from flask import request

        microscope = find_component("org.openflexure.microscope")
        if not microscope:
            abort(503, "No microscope connected.")
        phase = getattr(self.extension.state, "phase", "idle")
        if phase in ("calibrating", "scanning", "stitching", "refining"):
            abort(409, "Microscope is busy with an active scan.")
        body = request.get_json(silent=True) or {}
        dz = int(body.get("dz") or 2000)
        dz = max(200, min(8000, dz))

        scan_ext = find_extension("org.openflexure.scan")
        if not scan_ext or not hasattr(scan_ext, "get_autofocus_function"):
            abort(503, "OpenFlexure fast autofocus is unavailable.")

        self.extension.ensure_hardware_hooks()
        with self.extension.hardware.hold("autofocus", phase="autofocus"):
            with microscope.lock(timeout=1):
                autofocus = scan_ext.get_autofocus_function(
                    dz=dz,
                    use_fast_autofocus=True,
                )
                autofocus()
                token = str(uuid.uuid4())
                folder = os.path.join(PREVIEW_DIR, "retakes")
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, token + ".jpg")
                baseline = _focus_capture_frame(microscope)
                _focus_save_frame(path, baseline)
                report = dict(
                    adaptive_focus_capture(
                        microscope,
                        baseline_path=path,
                        autofocus=autofocus,
                    )
                )
                report.update(
                    {
                        "native_engine": "openflexure-fast",
                        "autofocus_dz": dz,
                    }
                )
                return {
                    "token": token,
                    "stage_position": list(microscope.stage.position),
                    "focus": report,
                    "preview_url": (
                        "/api/v2/extensions/{}/recapture_image?token={}"
                    ).format(EXTENSION_ID, token),
                    "captured_at": datetime.datetime.now().isoformat(),
                }


class RecaptureTileView(ActionView):
    """Explicit one-tile retake. No automatic invocation by registration."""
    def post(self):
        from flask import request
        from labthings import current_action
        body = request.get_json(silent=True) or {}
        target = body.get("stage_position")
        if not isinstance(target, list) or len(target) != 3 or any(type(v) is not int for v in target):
            abort(400, "stage_position must contain three integer motor coordinates")
        if body.get("confirm_same_sample") is not True:
            abort(400, "Confirm unchanged specimen and stage coordinate reference")
        microscope = find_component("org.openflexure.microscope")
        self.extension.ensure_hardware_hooks()
        with self.extension.hardware.hold("retake", phase="retake"):
            with microscope.lock(timeout=1):
                initial = list(microscope.stage.position)
                capture_only = body.get('capture_only') is True
                move_only = body.get('move_only') is True
                if capture_only and move_only:abort(400, 'Choose move or capture')
                if capture_only and any(abs(target[i]-initial[i])>2 for i in range(3)):
                    abort(409, 'Stage moved since FOV was displayed; refresh before capture')
                if max(abs(target[i]-initial[i]) for i in (0,1)) > 50000 or abs(target[2]-initial[2]) > 10000:
                    abort(400, "Retake exceeds travel envelope; position manually first")
                while not capture_only and list(microscope.stage.position) != target:
                    if current_action() and current_action().stopped:
                        raise RuntimeError("Retake cancelled")
                    here = list(microscope.stage.position)
                    step = [here[i]+int(np.clip(target[i]-here[i],-250 if i==2 else -1000,250 if i==2 else 1000)) for i in range(3)]
                    microscope.stage.move_abs(step)
                report = {}
                if not capture_only and body.get("autofocus", True):
                    # FOV dragging is interactive: use OpenFlexure's stock fast AF,
                    # not MicroWorld quality_autofocus (which performs a slower Z survey).
                    try:
                        scan_ext = find_extension("org.openflexure.scan")
                        autofocus = scan_ext.get_autofocus_function(
                            dz=int(body.get("autofocus_dz") or 1000),
                            use_fast_autofocus=True,
                        )
                        autofocus()
                        report = {"engine": "openflexure-fast", "dz": int(body.get("autofocus_dz") or 1000)}
                    except Exception as exc:
                        logging.exception("OpenFlexure fast autofocus failed")
                        report = {"engine": "openflexure-fast", "warning": str(exc)}
                if current_action() and current_action().stopped:
                    raise RuntimeError("Retake cancelled")
                if move_only:
                    return {'stage_position':list(microscope.stage.position),'initial_position':initial,'focus':report,'move_only':True}
                time.sleep(.3)
                token = str(uuid.uuid4())
                folder = os.path.join(PREVIEW_DIR,"retakes")
                os.makedirs(folder,exist_ok=True)
                path = os.path.join(folder,token+".jpg")
                baseline = _focus_capture_frame(microscope)
                _focus_save_frame(path, baseline)
                if bool(body.get("adaptive_focus_stack", ADAPTIVE_FOCUS_DEFAULT)):
                    adaptive = adaptive_focus_capture(microscope, baseline_path=path)
                    report = dict(report)
                    report["adaptive_focus"] = adaptive
                return {"token": token, "stage_position": list(microscope.stage.position),
                        "initial_position": initial, "focus": report,
                        "captured_at": datetime.datetime.now().isoformat()}


class RecaptureImageView(View):
    def get(self):
        from flask import request
        try: token = str(uuid.UUID(request.args.get("token", "")))
        except (ValueError, TypeError): abort(400, "Invalid retake token")
        return send_file(os.path.join(PREVIEW_DIR,"retakes",token+".jpg"), mimetype="image/jpeg")


class TileScanAPI(ActionView):
    args = TileScanArgsSchema() if TileScanArgsSchema is not None else None
    default_stop_timeout = 3600  # Full explore can take a long time

    def post(self, args):
        microscope = find_component("org.openflexure.microscope")
        if not microscope:
            abort(503, "No microscope connected.")

        args = dict(args or {})
        if args.get("quality_autofocus") and (
            args.get("append_session") or args.get("explore") or
            args.get("style") == "explore" or len(args.get("grid") or []) < 3 or
            int(args["grid"][2]) != 1
        ):
            abort(400, "quality_autofocus currently supports new XY grids with one Z plane only")
        if args.get("continuous_scan"):
            grid = args.get("grid") or []
            if (args.get("append_session") or args.get("bayer") or args.get("temporary")
                    or args.get("quality_autofocus") or args.get("style") == "spiral"
                    or len(grid) < 3 or int(grid[2]) != 1 or min(grid[:2]) < 1
                    or max(grid[:2]) > 20):
                abort(400, "Continuous capture requires a new single-plane XY scan.")
            if not args.get("mac_preview_only") or not args.get("keep_tiles"):
                abort(400, "Continuous capture requires PC sync and retained tiles.")
        if args.get("explore_strategy") == "video":
            radius = int(args.get("explore_max_radius", 2))
            if not 1 <= radius <= 5 or args.get("append_session"):
                abort(400, "Video Scout needs a new scan and a radius of 1–5 tiles.")
            if not args.get("mac_preview_only") or not args.get("keep_tiles"):
                abort(400, "Video Scout requires PC sync and retained source tiles.")
        # OF TileScanArgs may strip unknown keys; recover from raw JSON
        try:
            from flask import request

            raw = request.get_json(silent=True) or {}
            if "auto_stride" in raw:
                args["auto_stride"] = bool(raw["auto_stride"])
            if "target_overlap" in raw:
                args["target_overlap"] = float(raw["target_overlap"])
            if "keep_tiles" in raw:
                args["keep_tiles"] = bool(raw["keep_tiles"])
            if "append_session" in raw:
                args["append_session"] = bool(raw["append_session"])
            if "session_id" in raw and raw.get("session_id"):
                args["session_id"] = str(raw["session_id"])
            if "view_id" in raw and raw.get("view_id"):
                args["view_id"] = str(raw["view_id"])
            if "auto_refine" in raw:
                args["auto_refine"] = bool(raw["auto_refine"])
            if "fill_gaps" in raw:
                args["fill_gaps"] = bool(raw["fill_gaps"])
            if "explore" in raw:
                args["explore"] = bool(raw["explore"])
            if "recalibrate" in raw:
                args["recalibrate"] = bool(raw["recalibrate"])
            if "style" in raw and raw.get("style"):
                args["style"] = str(raw["style"])
            if "center_tile" in raw:
                args["center_tile"] = bool(raw["center_tile"])
            if "adaptive_focus_stack" in raw:
                args["adaptive_focus_stack"] = bool(raw["adaptive_focus_stack"])
        except Exception:
            logging.debug("Could not merge auto_stride from request", exc_info=True)

        resize = args.get("resize", None)
        if resize:
            if ("width" in resize) and ("height" in resize):
                args["resize"] = (int(resize["width"]), int(resize["height"]))
            else:
                abort(404)

        logging.info("Running MicroWorld mosaic tile scan...")
        try:
            self.extension.patch_stock_scan()
        except Exception:
            logging.exception("patch_stock_scan failed before tile")
        return self.extension.run_tile_scan(microscope, args)


def _mac_supervisor_base():
    """LAN URL of PC supervisor from visual-prompts heartbeat (or last known)."""
    try:
        extension = find_extension("org.microworld.scan-mosaic")
        if extension is not None:
            connection = extension.hardware.connection_status()
            if connection["managed"]:
                return connection["supervisor_url"]
        vp = find_extension("org.microworld.visual-prompts")
        store = getattr(vp, "store", None) or vp
        info = getattr(store, "_agent_worker", None) or {}
        url = (info.get("supervisor_url") or "").strip().rstrip("/")
        if url:
            return url
        # Persisted across Pi restarts / heartbeat gaps
        settings = getattr(store, "read_settings", None)
        if callable(settings):
            url = str((settings() or {}).get("pc_supervisor_url") or "").strip().rstrip("/")
            if url:
                return url
    except Exception:
        logging.debug("pc supervisor lookup failed", exc_info=True)
    # Env fallback (optional)
    env = (os.environ.get("OPENMHS_PC_SUPERVISOR_URL") or "").strip().rstrip("/")
    return env or None


def _proxy_mac(path, query=None, timeout=60, method="GET", body=None):
    import urllib.error
    import urllib.parse
    import urllib.request

    base = _mac_supervisor_base()
    if not base:
        abort(503, "PC supervisor offline (no heartbeat URL)")
    url = base + path
    if query:
        url = url + "?" + urllib.parse.urlencode(query)
    try:
        data = None
        headers = {}
        if body is not None:
            data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            return payload, ctype
    except urllib.error.HTTPError as exc:
        abort(exc.code, "PC proxy HTTP {}".format(exc.code))
    except Exception as exc:
        logging.warning("PC proxy failed %s: %s", url, exc)
        abort(502, "PC proxy failed: {}".format(exc))


class MacSessionView(View):
    """GET: whether Mac has archived this session. DELETE: drop Mac copy."""

    def get(self):
        from flask import request

        sid = request.args.get("session_id") or ""
        if not sid:
            abort(400, "session_id required")
        data, ctype = _proxy_mac("/mosaic/{}".format(sid))
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker response")

    def delete(self):
        from flask import request

        sid = request.args.get("session_id") or ""
        if not sid:
            abort(400, "session_id required")
        data, _ = _proxy_mac("/mosaic/{}".format(sid), method="DELETE", timeout=30)
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {"ok": True, "deleted": sid}


class MacListView(View):
    """GET: list mosaic sessions on Mac (for UI restore after Pi restart)."""

    def get(self):
        data, _ = _proxy_mac("/mosaic/list")
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker list")


class MacWorkspaceView(View):
    """GET: list a folder inside the PC workspace."""

    def get(self):
        from flask import request

        path = request.args.get("path") or "."
        data, _ = _proxy_mac("/workspace/ls", query={"path": path}, timeout=20)
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker workspace listing")


class MacRecaptureView(View):
    def post(self):
        from flask import request
        body = request.get_json(silent=True) or {}
        sid = str(body.get("session_id") or "")
        if not re.match(r"^[a-zA-Z0-9_-]+$", sid): abort(400, "Invalid session")
        operation = body.get("operation")
        route = {"plan":"recapture-plan", "start":"recapture", "status":"recapture-status", "canvas":"canvas"}.get(operation)
        if not route: abort(400, "Invalid retake operation")
        # During acquisition a browser can still have an old Canvas tab open
        # and ask for action=load. Never forward that expensive full render to
        # the Mac while the tile set is changing; the Mac's status response is
        # image-free and the Canvas UI can keep showing live tiles instead.
        if operation == "canvas" and str(body.get("action") or "load") == "load":
            try:
                phase = str(getattr(self.extension.state, "phase", "") or "")
            except Exception:
                phase = ""
            if phase in ("calibrating", "scanning", "stitching", "refining"):
                body = dict(body)
                body["action"] = "status"
                body["deferred_load"] = True
        data, _ = _proxy_mac("/mosaic/{}/{}".format(sid,route),method="POST",body=body,timeout=30)
        result = json.loads(data.decode("utf-8"))
        if body.get("deferred_load") and isinstance(result, dict):
            result["deferred_load"] = True
        return result


class MacViewsView(View):
    """GET/POST Cameras + Canvas records on the PC workspace."""

    def get(self):
        data, _ = _proxy_mac("/views", timeout=10)
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker views")

    def post(self):
        from flask import request

        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "create").strip().lower()
        path = "/views"
        if action == "current":
            path = "/views/current"
        elif action in ("import-legacy", "import_legacy"):
            path = "/views/import-legacy"
        elif action == "ingest":
            path = "/views/ingest"
        elif action in ("bind-session", "bind_session"):
            path = "/views/bind-session"
        elif action in ("active-record", "active_record"):
            path = "/views"
        elif action in ("import-record", "import_record"):
            path = "/views/import-record"
        elif action in ("record", "video"):
            path = "/views/records"
            if action == "video":
                body = dict(body)
                body["kind"] = "video"
        elif action in ("save_analysis", "analysis"):
            path = "/views/analysis"
        data, _ = _proxy_mac(path, method="POST", body=body, timeout=30)
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {"ok": True}


class MacCurrentViewView(View):
    """GET the shared current Camera/Canvas pointer without loading history."""

    def get(self):
        data, _ = _proxy_mac("/views/current", timeout=5)
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker current view")


class MacUiEventsView(View):
    """GET agent→UI events so the browser can follow Observation/Comprehension/ROI tools."""

    def get(self):
        from flask import request

        after = request.args.get("after") or "0"
        limit = request.args.get("limit") or "100"
        data, _ = _proxy_mac(
            "/ui/events",
            query={"after": after, "limit": limit},
            timeout=2,
        )
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker ui events")


class MacAgentSnapshotView(View):
    """GET/POST agent chat snapshot stored next to a PC mosaic session."""

    def get(self):
        from flask import request

        sid = request.args.get("session_id") or ""
        if not sid:
            abort(400, "session_id required")
        data, _ = _proxy_mac("/mosaic/{}/agent".format(sid), timeout=20)
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker agent snapshot")

    def post(self):
        from flask import request

        body = request.get_json(silent=True) or {}
        sid = request.args.get("session_id") or body.get("session_id") or ""
        if not sid:
            abort(400, "session_id required")
        data, _ = _proxy_mac(
            "/mosaic/{}/agent".format(sid),
            method="POST",
            body=body,
            timeout=20,
        )
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {"ok": True, "session_id": sid}


class MacLayoutView(View):
    def get(self):
        from flask import request

        sid = request.args.get("session_id") or ""
        if not sid:
            abort(400, "session_id required")
        data, _ = _proxy_mac("/mosaic/{}/layout".format(sid))
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker layout")


class MacRefineStatusView(View):
    def get(self):
        from flask import request

        sid = request.args.get("session_id") or ""
        if not sid:
            abort(400, "session_id required")
        data, _ = _proxy_mac("/mosaic/{}/refine_status".format(sid))
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            abort(502, "Invalid PC Worker refine status")


class MacRefineStartView(View):
    """POST: start HQ refine on Mac (browser → Pi → Mac)."""

    def post(self):
        from flask import request

        sid = request.args.get("session_id") or ""
        body = request.get_json(silent=True) or {}
        if not sid:
            sid = str(body.get("session_id") or "")
        if not sid:
            abort(400, "session_id required")
        align = body.get("align") or "full"
        data, _ = _proxy_mac(
            "/mosaic/{}/refine".format(sid),
            method="POST",
            body={"align": align},
            timeout=30,
        )
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {"ok": True, "session_id": sid}


class MacPreviewView(View):
    def get(self):
        from flask import make_response, request

        sid = request.args.get("session_id") or ""
        if not sid:
            abort(400, "session_id required")
        data, ctype = _proxy_mac("/mosaic/{}/preview.jpg".format(sid), timeout=120)
        resp = make_response(data)
        resp.headers["Content-Type"] = ctype or "image/jpeg"
        # The browser URL carries the Mac manifest preview revision.  Allow a
        # short cache window for repeated polls of the same revision instead
        # of retransmitting the same JPEG through the Pi proxy.
        resp.headers["Cache-Control"] = "private, max-age=3"
        return resp


class MacPyramidInfoView(View):
    """GET: multi-resolution canvas metadata from the Mac worker."""

    def get(self):
        from flask import make_response, request

        sid = request.args.get("session_id") or ""
        if not sid:
            abort(400, "session_id required")
        data, ctype = _proxy_mac(
            "/mosaic/{}/pyramid/info.json".format(sid), timeout=180
        )
        resp = make_response(data)
        resp.headers["Content-Type"] = ctype or "application/json"
        resp.headers["Cache-Control"] = "no-store"
        return resp


class MacPyramidTileView(View):
    """GET: one cached JPEG tile from the Mac canvas pyramid."""

    def get(self):
        from flask import make_response, request

        sid = request.args.get("session_id") or ""
        level = request.args.get("level")
        x = request.args.get("x")
        y = request.args.get("y")
        if not sid or level is None or x is None or y is None:
            abort(400, "session_id, level, x, y required")
        data, ctype = _proxy_mac(
            "/mosaic/{}/pyramid/tile".format(sid),
            query={"level": level, "x": x, "y": y},
            timeout=120,
        )
        resp = make_response(data)
        resp.headers["Content-Type"] = ctype or "image/jpeg"
        # Revision is part of the browser URL; an immutable tile can be
        # cached safely until a new source revision creates a new URL.
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


class MacTileView(View):
    def get(self):
        from flask import make_response, request

        sid = request.args.get("session_id") or ""
        ix = request.args.get("ix")
        iy = request.args.get("iy")
        if not sid or ix is None or iy is None:
            abort(400, "session_id, ix, iy required")
        thumb = str(request.args.get("thumb") or "").lower() in ("1", "true", "yes")
        width = request.args.get("width") or "320"
        data, ctype = _proxy_mac(
            "/mosaic/{}/tile".format(sid),
            query={"ix": ix, "iy": iy, "thumb": "1", "width": width} if thumb else {"ix": ix, "iy": iy},
            timeout=60,
        )
        resp = make_response(data)
        resp.headers["Content-Type"] = ctype or "image/jpeg"
        resp.headers["Cache-Control"] = "public, max-age=86400" if thumb else "public, max-age=300"
        return resp


LABTHINGS_EXTENSIONS = (
    ScanMosaicExtension,
    legacy_extension(ScanMosaicExtension, EXTENSION_ID, LEGACY_EXTENSION_ID, EXTENSION_VERSION),
)
