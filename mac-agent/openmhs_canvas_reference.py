"""Stage-to-canvas mapping shared by Camera PiP and Canvas.

The mapping is deliberately independent of the cropped Slide bounds. A live
scan starts with the calibrated stage scale; once the Mac publishes a refined
layout, the same physical origin is retained while the scale is refreshed
from the refined tile poses. This prevents the PiP FOV from drifting away
from the Slide after global registration.
"""
import hashlib
import json
import uuid

import numpy as np

import openmhs_mosaic_store as store


def _layout_signature(layout):
    """Stable token for a Mac-published layout revision."""
    payload = {
        "canvas": layout.get("canvas"),
        "tiles": [
            {
                k: t.get(k)
                for k in ("ix", "iy", "x", "y", "w", "h", "asset")
            }
            for t in layout.get("tiles", [])
            if not t.get("asset")
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def _write_atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def _manifest_refs(man, tiles):
    cells = {
        (int(c["ix"]), int(c["iy"])): c
        for c in man.get("cells", [])
        if c.get("ix") is not None and c.get("iy") is not None
    }
    doc_by_key = {
        (int(t["ix"]), int(t["iy"])): t
        for t in tiles
        if t.get("ix") is not None and t.get("iy") is not None
    }
    refs = []
    for key, tile in doc_by_key.items():
        # A manually dragged tile is user-owned and must not influence the
        # physical transform used by the other tiles.
        if tile.get("asset") or tile.get("manual"):
            continue
        cell = cells.get(key, {})
        xyz = cell.get("stage_position") or tile.get("stage_position")
        if xyz is not None and len(xyz) == 3:
            refs.append((tile, cell, [float(v) for v in xyz]))
    return refs


def _refined_model(man, tiles, existing):
    """Fit stage XY to the refined Mac layout, preserving the origin."""
    layout = man.get("layout") or {}
    canvas = layout.get("canvas") or []
    if not man.get("refined") or len(canvas) < 2 or not layout.get("tiles"):
        return None
    try:
        canvas_w, canvas_h = float(canvas[0]), float(canvas[1])
    except (TypeError, ValueError):
        return None
    if canvas_w <= 0 or canvas_h <= 0:
        return None

    poses = {}
    for item in layout.get("tiles", []):
        if item.get("asset") or item.get("ix") is None or item.get("iy") is None:
            continue
        poses[(int(item["ix"]), int(item["iy"]))] = np.array(
            [
                float(item.get("x", 0.0)) * canvas_w,
                float(item.get("y", 0.0)) * canvas_h,
            ],
            dtype=float,
        )
    refs = [
        r
        for r in _manifest_refs(man, tiles)
        if (int(r[0]["ix"]), int(r[0]["iy"])) in poses
    ]
    if len(refs) < 3:
        return None

    if existing and len(existing.get("stage_origin", [])) >= 2:
        stage_origin = [float(v) for v in existing["stage_origin"]]
    else:
        stage_origin = list(refs[0][2])

    stage_xy = np.array([r[2][:2] for r in refs], dtype=float)
    layout_xy = np.array(
        [poses[(int(r[0]["ix"]), int(r[0]["iy"]))] for r in refs], dtype=float
    )
    origin_delta = stage_xy - np.asarray(stage_origin[:2], dtype=float)

    # Keep the old world origin for an established reference. For a legacy
    # reference without a layout token, use the automatic position of its
    # anchor tile so migration does not shift the Canvas.
    world_origin = None
    if existing and (existing.get("layout_token") or existing.get("mapping_source") in ("live-overlap-basis-v1", "stage-calibration-v1")):
        world_origin = np.asarray(existing.get("world_origin", []), dtype=float)
    else:
        for tile, _cell, xyz in refs:
            if np.max(np.abs(np.asarray(xyz[:2]) - np.asarray(stage_origin[:2]))) < 0.5:
                if tile.get("auto_x") is not None and tile.get("auto_y") is not None:
                    world_origin = np.array(
                        [tile["auto_x"], tile["auto_y"]], dtype=float
                    )
                break
    if world_origin is None or world_origin.size != 2:
        world_origin = np.asarray(
            (existing or {}).get("world_origin", layout_xy[0]), dtype=float
        )
    if world_origin.size != 2:
        return None

    exact = np.where(
        np.linalg.norm(stage_xy - np.asarray(stage_origin[:2]), axis=1) < 0.5
    )[0]
    if len(exact):
        layout_origin = layout_xy[int(exact[0])]
    else:
        design = np.column_stack([origin_delta, np.ones(len(refs))])
        layout_origin = np.linalg.lstsq(design, layout_xy, rcond=None)[0][-1]
    targets = layout_xy - layout_origin
    if np.linalg.matrix_rank(origin_delta) < 2:
        return None
    matrix = np.linalg.lstsq(origin_delta, targets, rcond=None)[0]
    residual = float(
        np.sqrt(np.mean(np.sum((origin_delta @ matrix - targets) ** 2, axis=1)))
    )
    if (
        not np.isfinite(matrix).all()
        or np.linalg.cond(matrix) > 20
        or residual > 18
    ):
        return None

    tile0 = refs[0][0]
    fov = [float(tile0.get("w", 0) or 0), float(tile0.get("h", 0) or 0)]
    if min(fov) <= 0:
        fov = [
            float(layout.get("tile_w") or 0),
            float(layout.get("tile_h") or 0),
        ]
    return {
        "stage_origin": stage_origin,
        "world_origin": [float(v) for v in world_origin],
        "stage_to_world": matrix.tolist(),
        "fov": fov,
        "mapping_residual_px": residual,
        "layout_token": _layout_signature(layout),
        "mapping_source": "mac-refined-layout-v2",
    }


def reference(sid, tiles):
    path = store.session_dir(sid) / "canvas-reference.json"
    existing = json.loads(path.read_text()) if path.exists() else None
    man = store.read_manifest(sid)

    # Presentation edits (dragging, hiding, opacity, enhancement and import)
    # rewrite layout.json for the Canvas renderer.  They must not silently
    # redefine the physical origin or make the Camera PiP drift.  A source
    # refinement explicitly clears this lock before publishing its new
    # positions, so hardware-backed recalibration remains possible.
    if existing is not None and man.get("canvas_reference_locked"):
        return existing

    # A refined layout is authoritative for relative geometry. Refresh the
    # cached mapping when its token changes, but never use a manual tile as
    # evidence for the physical transform.
    refined = _refined_model(man, tiles, existing)
    if refined is not None:
        result = dict(existing or {})
        result.update(refined)
        result.setdefault("id", str(uuid.uuid4()))
        _write_atomic(path, result)
        return result
    live = man.get("live_geometry") or {}
    stride = man.get("stride_size") or []
    if (man.get("sync", {}).get("phase") in ("calibrating", "scanning", "stitching", "refining")
            and live.get("basis") and any(live.get("counts", []))
            and len(stride) >= 2 and all(stride[:2])):
        refs = _manifest_refs(man, tiles)
        if refs:
            tile, _, xyz = refs[0]
            result = dict(existing or {})
            fov = result.get("fov") or [tile["w"], tile["h"]]
            matrix = np.asarray(live["basis"]) * float(fov[0]) / np.asarray(stride[:2])[:, None]
            if np.isfinite(matrix).all() and np.linalg.cond(matrix) <= 20:
                origin = man.get("origin_xy") or xyz[:2]
                result.setdefault("id", str(uuid.uuid4()))
                result.setdefault("stage_origin", [*origin, xyz[2]])
                result.setdefault("world_origin", [0., 0.])
                result.update(stage_to_world=matrix.tolist(), fov=fov,
                              mapping_source="live-overlap-basis-v1")
                if result != existing:
                    _write_atomic(path, result)
                return result
    if existing is not None:
        return existing

    refs = _manifest_refs(man, tiles)
    if not refs:
        return None
    tile, _cell, xyz = refs[0]
    px = man.get("px_per_step")
    signs = man.get("axis_sign") or [-1, -1]
    residual = 0.0
    if px and len(px) >= 2 and all(float(v) > 0 for v in px[:2]):
        matrix = np.diag([float(px[i]) * signs[i] * float(tile["w"]) / 640 for i in range(2)])
    elif len(refs) >= 3:
        xy = np.array([p[2][:2] for p in refs], float)
        uv = np.array([[p[0]["auto_x"], p[0]["auto_y"]] for p in refs], float)
        if np.linalg.matrix_rank(xy - xy[0]) < 2:
            return None
        matrix = np.linalg.lstsq(xy - xy[0], uv - uv[0], rcond=None)[0]
        residual = float(
            np.sqrt(
                np.mean(np.sum(((xy - xy[0]) @ matrix - (uv - uv[0])) ** 2, axis=1)
            )
        )
        )
    else:
        return None
    if (
        not np.isfinite(matrix).all()
        or np.linalg.cond(matrix) > 20
        or residual > 15
    ):
        return None
    result = dict(
        id=str(uuid.uuid4()),
        stage_origin=[*(man.get("origin_xy") or xyz[:2]), xyz[2]],
        world_origin=[0., 0.],
        stage_to_world=matrix.tolist(),
        fov=[tile["w"], tile["h"]],
        mapping_residual_px=residual,
        mapping_source="stage-calibration-v1",
    )
    _write_atomic(path, result)
    return result


def world(ref, xyz):
    return (
        np.array(xyz[:2]) - ref["stage_origin"][:2]
    ) @ np.array(ref["stage_to_world"]) + ref["world_origin"]


def stage(ref, xy):
    return (
        np.array(xy) - ref["world_origin"]
    ) @ np.linalg.inv(np.array(ref["stage_to_world"])) + ref["stage_origin"][:2]
