"""Camera views + Records in the Mac workspace (Codex-style agent field).

Layout:
  views/<view-id>/view.json
  views/<view-id>/live/           # dynamic playground thread
  views/<view-id>/records/<rid>/  # static mosaic or video field
  lib/                            # shared env, algorithms, checkpoints
  captures/mosaics/<sid>/         # legacy (read + optional import)
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("openmhs.views")

INDEX_NAME = "index.json"
VIEW_JSON = "view.json"
RECORD_JSON = "record.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


def _safe_id(raw: str) -> str:
    sid = (raw or "").strip()
    if not sid or "/" in sid or "\\" in sid or ".." in sid:
        raise ValueError("invalid id")
    return sid


def views_root() -> Path:
    from openmhs_workspace import workspace_root

    d = workspace_root() / "views"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lib_root() -> Path:
    from openmhs_workspace import workspace_root

    d = workspace_root() / "lib"
    d.mkdir(parents=True, exist_ok=True)
    return d


def legacy_mosaics_root() -> Path:
    from openmhs_workspace import workspace_root

    d = workspace_root() / "captures" / "mosaics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def seed_lib() -> None:
    root = lib_root()
    readme = root / "README.md"
    if not readme.is_file():
        readme.write_text(
            "# MicroWorld lib\n\n"
            "Agent-maintained shared knowledge — not tied to one Camera view.\n\n"
            "- `env.md` — Python / conda notes for this Mac\n"
            "- `algorithms/` — classic algorithm wrappers (same calling shape)\n"
            "- `models/` — checkpoints and how to load them\n",
            encoding="utf-8",
        )
    env = root / "env.md"
    if not env.is_file():
        env.write_text(
            "# Environment\n\n"
            "Record the interpreter, OpenCV, and any extra packages the agent "
            "should assume when writing tools.\n",
            encoding="utf-8",
        )
    for sub, text in (
        (
            "algorithms/README.md",
            "# Algorithms\n\n"
            "Put reusable scripts here. Prefer a `run(image_path, **kwargs)` entry.\n",
        ),
        (
            "models/README.md",
            "# Models\n\nStore checkpoints and a small loader script per model.\n",
        ),
    ):
        p = root / sub
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file():
            p.write_text(text, encoding="utf-8")


def _index_path() -> Path:
    return views_root() / INDEX_NAME


def read_index() -> Dict[str, Any]:
    path = _index_path()
    if not path.is_file():
        return {"current_view_id": None, "records": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("current_view_id", None)
    data.setdefault("records", {})
    if not isinstance(data["records"], dict):
        data["records"] = {}
    return data


def write_index(data: Dict[str, Any]) -> Dict[str, Any]:
    path = _index_path()
    body = dict(data or {})
    body.setdefault("current_view_id", None)
    body.setdefault("records", {})
    body["updated_at"] = _now()
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return body


def view_dir(view_id: str) -> Path:
    return views_root() / _safe_id(view_id)


def record_dir_for(view_id: str, record_id: str) -> Path:
    return view_dir(view_id) / "records" / _safe_id(record_id)


def record_is_trashed(record_id):
    rid=_safe_id(record_id)
    return (_ws()/'.trash'/'canvases'/'tombstones'/(rid+'.json')).is_file() or rid in read_index().get('trashed_records',{})

def resolve_record_dir(record_id: str) -> Optional[Path]:
    """Find a record/mosaic folder by id (views first, then legacy)."""
    rid = _safe_id(record_id)
    idx = read_index()
    if record_is_trashed(rid):return None
    vid = (idx.get("records") or {}).get(rid)
    if vid:
        d = record_dir_for(str(vid), rid)
        if d.is_dir():
            return d
    for child in views_root().iterdir() if views_root().is_dir() else []:
        if not child.is_dir():
            continue
        cand = child / "records" / rid
        if cand.is_dir():
            return cand
    legacy = legacy_mosaics_root() / rid
    if legacy.is_dir():
        return legacy
    return None


def current_view_id() -> Optional[str]:
    vid = read_index().get("current_view_id")
    if not vid:
        return None
    if not (view_dir(str(vid)) / VIEW_JSON).is_file():
        return None
    return str(vid)


def set_current_view(view_id: str) -> Dict[str, Any]:
    vid = _safe_id(view_id)
    if not (view_dir(vid) / VIEW_JSON).is_file():
        raise FileNotFoundError(vid)
    idx = read_index()
    idx["current_view_id"] = vid
    write_index(idx)
    return read_view(vid)


def set_active_record(view_id: str, record_id: Optional[str] = None) -> Dict[str, Any]:
    """Persist the Canvas currently open in a Camera view.

    This is intentionally separate from browser localStorage: LAN, VPN, and
    public-link clients should converge on the same active Canvas.  Clearing
    the value means the shared view is back in Camera mode.
    """
    vid = _safe_id(view_id)
    if not (view_dir(vid) / VIEW_JSON).is_file():
        raise FileNotFoundError(vid)
    rid = str(record_id or "").strip()
    if rid:
        rid = _safe_id(rid)
        if not record_dir_for(vid, rid).is_dir():
            raise FileNotFoundError(rid)
    data = _write_view_json(vid, {"active_record_id": rid or None})
    data["records"] = list_records(vid)
    data["current"] = current_view_id() == vid
    return {"ok": True, "view": data, "active_record_id": rid or None}


def _write_view_json(view_id: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    d = view_dir(view_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "live").mkdir(exist_ok=True)
    (d / "records").mkdir(exist_ok=True)
    path = d / VIEW_JSON
    data: Dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(extra or {})
    data["id"] = view_id
    data.setdefault("created_at", _now())
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def create_view(
    title: Optional[str] = None,
    make_current: bool = True,
    view_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Camera view. Never called implicitly — UI New only.

    If view_id is given (browser localStorage restore), persist that id.
    """
    seed_lib()
    if view_id:
        vid = _safe_id(view_id)
        if (view_dir(vid) / VIEW_JSON).is_file():
            if make_current:
                set_current_view(vid)
            return {
                "ok": True,
                "view": read_view(vid),
                "current_view_id": current_view_id(),
                "already": True,
            }
    else:
        vid = _new_id()
    title = (title or "").strip() or ("Camera " + vid[:15])
    data = _write_view_json(vid, {"title": title})
    (view_dir(vid) / "live" / "thread.jsonl").touch()
    idx = read_index()
    if make_current or not idx.get("current_view_id"):
        idx["current_view_id"] = vid
        write_index(idx)
    return {"ok": True, "view": data, "current_view_id": read_index().get("current_view_id")}


def bind_session(view_id: str, session_id: str) -> Dict[str, Any]:
    """Link a Pi chat session to a Camera view (Camera ≡ chat)."""
    vid = _safe_id(view_id)
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id required")
    if not (view_dir(vid) / VIEW_JSON).is_file():
        raise FileNotFoundError(vid)
    data = _write_view_json(vid, {"session_id": sid})
    data["records"] = list_records(vid)
    data["current"] = current_view_id() == vid
    return {"ok": True, "view": data}


def import_record(
    *,
    source_view_id: str,
    record_id: str,
    view_id: Optional[str] = None,
    copy: bool = True,
) -> Dict[str, Any]:
    """Import a Canvas (record) from another Camera into the target Camera."""
    src_vid = _safe_id(source_view_id)
    rid = _safe_id(record_id)
    dest_vid = _safe_id(view_id) if view_id else current_view_id()
    if not dest_vid:
        raise ValueError("no camera view — create one with New first")
    if not (view_dir(dest_vid) / VIEW_JSON).is_file():
        raise FileNotFoundError(dest_vid)
    preferred = record_dir_for(src_vid, rid)
    src = preferred if preferred.is_dir() else resolve_record_dir(rid)
    if src is None:
        legacy = legacy_mosaics_root() / rid
        if legacy.is_dir():
            src = legacy
        else:
            raise FileNotFoundError(rid)
    # Preserve original title/kind from source meta when present
    src_title = rid
    kind = "mosaic"
    src_rj = src / RECORD_JSON
    if src_rj.is_file():
        try:
            sm = json.loads(src_rj.read_text(encoding="utf-8"))
            src_title = str(sm.get("title") or src_title)
            kind = str(sm.get("kind") or kind)
        except Exception:
            pass
    elif (src / "video").is_dir() and not (src / "manifest.json").is_file():
        kind = "video"
    # Copy under a fresh id so source Camera keeps its Canvas mapping
    new_rid = _new_id() if copy else rid
    dest = record_dir_for(dest_vid, new_rid)
    if dest.exists():
        return {
            "ok": True,
            "view_id": dest_vid,
            "record": _record_summary(dest, dest_vid),
            "already": True,
        }
    dest.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(str(src), str(dest))
    else:
        if dest_vid == src_vid and new_rid == rid:
            return {
                "ok": True,
                "view_id": dest_vid,
                "record": _record_summary(src, dest_vid),
                "already": True,
            }
        shutil.move(str(src), str(dest))
    title = src_title if src_title != rid else ("Imported " + rid[:12])
    meta = _init_record_folder(dest, dest_vid, new_rid, kind, title)
    return {
        "ok": True,
        "view_id": dest_vid,
        "record": _record_summary(dest, dest_vid) or meta,
        "source_record_id": rid,
        "copied": bool(copy),
    }


def ensure_current_view() -> Optional[str]:
    """Return the current Camera view id, or None. Does not create a view."""
    return current_view_id()


def read_view(view_id: str) -> Dict[str, Any]:
    path = view_dir(view_id) / VIEW_JSON
    if not path.is_file():
        raise FileNotFoundError(view_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["records"] = list_records(view_id)
    data["current"] = current_view_id() == view_id
    return data


def current_view_state() -> Dict[str, Any]:
    """Return only the shared Camera/Canvas pointer.

    Unlike ``list_views`` this never walks every historical record, so all
    browsers can use it during startup without turning history inspection into
    a synchronization bottleneck.
    """
    vid = current_view_id()
    if not vid:
        return {"ok": True, "current_view_id": None, "current_view": None}
    path = view_dir(vid) / VIEW_JSON
    data = json.loads(path.read_text(encoding="utf-8"))
    view = {
        "id": vid,
        "title": data.get("title") or vid,
        "session_id": data.get("session_id"),
        "active_record_id": data.get("active_record_id"),
        "updated_at": data.get("updated_at"),
    }
    return {"ok": True, "current_view_id": vid, "current_view": view}


def list_records(view_id: str) -> List[Dict[str, Any]]:
    rec_root = view_dir(view_id) / "records"
    if not rec_root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for child in sorted(rec_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        info = _record_summary(child, view_id)
        if info:
            out.append(info)
    return out


def _record_summary(folder: Path, view_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    rid = folder.name
    meta: Dict[str, Any] = {}
    rj = folder / RECORD_JSON
    if rj.is_file():
        try:
            meta = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    man = folder / "manifest.json"
    cells = 0
    refined = False
    if man.is_file():
        try:
            md = json.loads(man.read_text(encoding="utf-8"))
            cells = len(md.get("cells") or [])
            refined = bool(md.get("refined"))
            status = folder / "refine_status.json"
            if not refined and (folder / "refined.jpg").is_file() and status.is_file():
                final = json.loads(status.read_text(encoding="utf-8"))
                refined = final.get("phase") == "done" and bool(final.get("refined"))
        except Exception:
            pass
    kind = meta.get("kind") or ("mosaic" if man.is_file() or (folder / "tiles").is_dir() else "video")
    return {
        "id": rid,
        "view_id": view_id or meta.get("view_id"),
        "kind": kind,
        "title": meta.get("title") or rid,
        "cells": cells,
        "refined": refined,
        "preview": (folder / "preview.jpg").is_file() or (folder / "refined.jpg").is_file(),
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(folder.stat().st_mtime)),
        "relpath": str(folder.relative_to(_ws())),
    }


def _ws() -> Path:
    from openmhs_workspace import workspace_root

    return workspace_root()


def list_views() -> Dict[str, Any]:
    seed_lib()
    cur = current_view_id()
    items: List[Dict[str, Any]] = []
    for child in sorted(views_root().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir() or not (child / VIEW_JSON).is_file():
            continue
        try:
            data = json.loads((child / VIEW_JSON).read_text(encoding="utf-8"))
        except Exception:
            continue
        data["id"] = child.name
        data["current"] = child.name == cur
        data["records"] = list_records(child.name)
        items.append(data)
    legacy = list_legacy_mosaics()
    return {
        "ok": True,
        "current_view_id": cur,
        "views": items,
        "legacy_mosaics": legacy,
    }


def list_legacy_mosaics() -> List[Dict[str, Any]]:
    root = legacy_mosaics_root()
    idx = read_index()
    mapped = set((idx.get("records") or {}).keys())
    out: List[Dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir() or child.name in mapped:
            continue
        if not (child / "manifest.json").is_file():
            continue
        info = _record_summary(child, view_id=None)
        if info:
            info["legacy"] = True
            out.append(info)
    return out


def _init_record_folder(d: Path, view_id: str, record_id: str, kind: str, title: Optional[str]) -> Dict[str, Any]:
    if record_is_trashed(record_id):raise FileNotFoundError('Canvas is in recovery trash')
    d.mkdir(parents=True, exist_ok=True)
    (d / "thread").mkdir(exist_ok=True)
    (d / "tools").mkdir(exist_ok=True)
    if kind == "mosaic":
        (d / "tiles").mkdir(exist_ok=True)
    elif kind == "video":
        (d / "video").mkdir(exist_ok=True)
        note = d / "video" / "README.md"
        if not note.is_file():
            note.write_text(
                "# Video record\n\n"
                "Placeholder: capture a stage-operated clip into this folder.\n"
                "Timeline + stage log will land here when video Record is wired.\n",
                encoding="utf-8",
            )
    meta = {
        "id": record_id,
        "view_id": view_id,
        "kind": kind,
        "title": (title or "").strip() or record_id,
        "created_at": _now(),
        "updated_at": _now(),
    }
    (d / RECORD_JSON).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    (d / "thread" / "thread.jsonl").touch()
    idx = read_index()
    recs = dict(idx.get("records") or {})
    recs[record_id] = view_id
    idx["records"] = recs
    write_index(idx)
    return meta


def create_record(
    *,
    view_id: Optional[str] = None,
    kind: str = "mosaic",
    record_id: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    kind = (kind or "mosaic").strip().lower()
    if kind not in ("mosaic", "video"):
        raise ValueError("kind must be mosaic or video")
    vid = _safe_id(view_id) if view_id else current_view_id()
    if not vid:
        raise ValueError("no camera view — create one with New first")
    if not (view_dir(vid) / VIEW_JSON).is_file():
        raise FileNotFoundError(vid)
    rid = _safe_id(record_id) if record_id else _new_id()
    existing = resolve_record_dir(rid)
    if existing is not None:
        return {
            "ok": True,
            "record": _record_summary(existing, vid) or {"id": rid, "view_id": vid, "kind": kind},
            "path": str(existing),
            "already": True,
        }
    d = record_dir_for(vid, rid)
    meta = _init_record_folder(d, vid, rid, kind, title)
    return {"ok": True, "record": meta, "path": str(d), "relpath": str(d.relative_to(_ws()))}


def ensure_record_for_session(session_id: str, view_id: Optional[str] = None) -> Path:
    """Create (or reuse) a mosaic Record for a Pi/Mac session id."""
    sid = _safe_id(session_id)
    if record_is_trashed(sid):raise FileNotFoundError('Canvas is in recovery trash')
    found = resolve_record_dir(sid)
    if found is not None:
        (found / "tiles").mkdir(exist_ok=True)
        return found
    out = create_record(view_id=view_id, kind="mosaic", record_id=sid, title="Mosaic " + sid[:15])
    d = Path(out["path"])
    (d / "tiles").mkdir(exist_ok=True)
    return d

def trash_record(record_id):
    rid=_safe_id(record_id)
    idx=read_index()
    if rid in idx.get('trashed_records',{}):return dict(ok=True,already=True,**idx['trashed_records'][rid])
    folder=resolve_record_dir(rid)
    if folder is None:raise FileNotFoundError(rid)
    root=_ws().resolve();source=folder.resolve()
    if root not in source.parents or source.name!=rid:raise ValueError('Invalid canvas folder')
    trash=root/'.trash'/'canvases'/str(uuid.uuid4());trash.mkdir(parents=True)
    metadata=dict(record_id=rid,view_id=idx.get('records',{}).get(rid),source=str(source.relative_to(root)),trash=str((trash/rid).relative_to(root)),trashed_at=_now())
    (trash/'recovery.json').write_text(json.dumps(metadata,indent=2))
    tombstone=root/'.trash'/'canvases'/'tombstones'/(rid+'.json');tombstone.parent.mkdir(exist_ok=True)
    tombstone.write_text(json.dumps(metadata,indent=2))
    try:shutil.move(str(source),str(trash/rid))
    except Exception:
        tombstone.unlink();raise
    idx.setdefault('trashed_records',{})[rid]=metadata
    idx.setdefault('records',{}).pop(rid,None)
    try:write_index(idx)
    except Exception:
        shutil.move(str(trash/rid),str(source));tombstone.unlink();raise
    return dict(ok=True,**metadata)


def save_analysis(
    *,
    view_id: Optional[str] = None,
    record_id: Optional[str] = None,
    markdown: str = "",
    annotations: Optional[List[Dict[str, Any]]] = None,
    chatMessages: Optional[List[Dict[str, Any]]] = None,
    rois: Optional[List[Dict[str, Any]]] = None,
    summary: str = "",
) -> Dict[str, Any]:
    """Persist analysis markdown + annotations into Observation live/ or Comprehension folder."""
    anns = annotations if isinstance(annotations, list) else []
    msgs = chatMessages if isinstance(chatMessages, list) else []
    roi_list = rois if isinstance(rois, list) else []
    payload = {
        "summary": (summary or "")[:2000],
        "markdown": (markdown or "")[:20000],
        "annotations": anns[:200],
        "chatMessages": msgs[-40:],
        "rois": roi_list[:64],
        "updated_at": _now(),
        "view_id": view_id,
        "record_id": record_id,
    }
    target: Optional[Path] = None
    rel = None
    if record_id:
        d = resolve_record_dir(record_id)
        if d is None and view_id:
            try:
                d = ensure_record_for_session(record_id, view_id)
            except Exception:
                d = None
        if d is not None:
            target = d
    if target is None:
        vid = _safe_id(view_id) if view_id else current_view_id()
        if not vid:
            return {"ok": False, "error": "no observation"}
        target = view_dir(vid) / "live"
        target.mkdir(parents=True, exist_ok=True)
    (target / "analysis.md").write_text(
        (payload["markdown"] or payload["summary"] or "# Analysis\n"),
        encoding="utf-8",
    )
    (target / "annotations.json").write_text(
        json.dumps(
            {
                "annotations": payload["annotations"],
                "rois": payload["rois"],
                "updated_at": payload["updated_at"],
                "summary": payload["summary"],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "agent.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        rel = str(target.relative_to(_ws()))
    except Exception:
        rel = str(target)
    return {
        "ok": True,
        "path": rel,
        "annotation_count": len(payload["annotations"]),
        "files": ["analysis.md", "annotations.json", "agent.json"],
    }


def append_thread(
    event: Dict[str, Any],
    *,
    view_id: Optional[str] = None,
    record_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one JSONL event to a Record thread, or the view live thread."""
    body = dict(event or {})
    body["at"] = body.get("at") or _now()
    line = json.dumps(body, ensure_ascii=False) + "\n"
    if record_id:
        d = resolve_record_dir(record_id)
        if d is None:
            try:
                d = ensure_record_for_session(record_id, view_id)
            except ValueError:
                return {"ok": False, "skipped": True, "reason": "no_view"}
        path = d / "thread" / "thread.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        vid = view_id or current_view_id()
        if not vid:
            return {"ok": False, "skipped": True, "reason": "no_view"}
        path = view_dir(vid) / "live" / "thread.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    return {"ok": True, "path": str(path.relative_to(_ws()))}


def ingest_into_current_view(
    view_id: Optional[str] = None,
    skip_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Move leftover captures/mosaics into the current Camera view (PC online)."""
    vid = _safe_id(view_id) if view_id else current_view_id()
    if not vid:
        return {"ok": False, "error": "no camera view", "moved": [], "skipped": []}
    if not (view_dir(vid) / VIEW_JSON).is_file():
        return {"ok": False, "error": "view not found", "moved": [], "skipped": []}
    return import_legacy_mosaics(view_id=vid, skip_ids=skip_ids)


def import_legacy_mosaics(
    view_id: Optional[str] = None,
    skip_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Move leftover captures/mosaics/<sid> into the current (or given) view."""
    vid = _safe_id(view_id) if view_id else current_view_id()
    if not vid:
        raise ValueError("no camera view — create one with New first")
    moved: List[str] = []
    skipped: List[str] = []
    skip = {str(s) for s in (skip_ids or []) if s}
    for item in list_legacy_mosaics():
        sid = item["id"]
        if sid in skip:
            skipped.append(sid)
            continue
        src = legacy_mosaics_root() / sid
        dest = record_dir_for(vid, sid)
        if dest.exists():
            skipped.append(sid)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        _init_record_folder(dest, vid, sid, "mosaic", item.get("title") or sid)
        moved.append(sid)
    return {"ok": True, "view_id": vid, "moved": moved, "skipped": skipped}
