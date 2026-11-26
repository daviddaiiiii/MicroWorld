"""UI command bus: agent tools → Mac event log → browser polls and applies.

Events live under OPENMHS_WORKSPACE/.ui/events.jsonl (one JSON object per line).
Each event has: id (monotonic int), at (ISO), type, payload.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()


def _ws() -> Path:
    from openmhs_workspace import workspace_root

    return workspace_root()


def _ui_dir() -> Path:
    d = _ws() / ".ui"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _events_path() -> Path:
    return _ui_dir() / "events.jsonl"


def _state_path() -> Path:
    return _ui_dir() / "state.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {"next_id": 1}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"next_id": 1}


def _write_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def emit_ui_event(event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append one UI event; return the stored event dict."""
    with _LOCK:
        state = _read_state()
        eid = int(state.get("next_id") or 1)
        state["next_id"] = eid + 1
        _write_state(state)
        ev = {
            "id": eid,
            "at": _now(),
            "type": str(event_type or "").strip() or "noop",
            "payload": payload or {},
        }
        with _events_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev


def list_ui_events(after: int = 0, limit: int = 100) -> Dict[str, Any]:
    """Return events with id > after (oldest first), capped."""
    after = max(0, int(after or 0))
    limit = max(1, min(500, int(limit or 100)))
    path = _events_path()
    if not path.is_file():
        return {"ok": True, "after": after, "events": [], "latest_id": after}
    out: List[Dict[str, Any]] = []
    latest = after
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                eid = int(ev.get("id") or 0)
                if eid > latest:
                    latest = eid
                if eid > after:
                    out.append(ev)
    except OSError:
        return {"ok": True, "after": after, "events": [], "latest_id": after}
    if len(out) > limit:
        out = out[-limit:]
    return {
        "ok": True,
        "after": after,
        "events": out,
        "latest_id": latest,
    }
