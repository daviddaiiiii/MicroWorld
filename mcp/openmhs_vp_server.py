#!/usr/bin/env python3
"""MicroWorld Visual Prompt + Agent Job MCP server.

Analysis / job path (always available):
  - get_latest_visual_prompt / get_visual_prompt / list_visual_prompts
  - list_pending_agent_jobs / claim_agent_job / heartbeat_agent_job
  - submit_agent_job_result / get_agent_job
  - microscope_get_state / microscope_move_rel / microscope_autofocus
  - microscope_snapshot

Optional local sync (DISABLED by default):
  - sync_latest_visual_prompt_to_workspace
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


MICROSCOPE_URL = os.environ.get("OPENMHS_MICROSCOPE_URL", "http://127.0.0.1:5000").rstrip("/")
EXT = "org.openmhs.visual-prompts"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SYNC_ENABLED = os.environ.get("OPENMHS_VP_SYNC_ENABLED", "0").strip() in {"1", "true", "TRUE", "yes", "YES"}
SYNC_DIR = Path(
    os.environ.get(
        "OPENMHS_VP_SYNC_DIR",
        str(Path(__file__).resolve().parents[1] / "visual-prompts"),
    )
).expanduser()
WORKER_ID = os.environ.get("OPENMHS_AGENT_WORKER_ID", "cursor-mcp").strip() or "cursor-mcp"
# Relative move clamp (stage steps)
MAX_REL_XY = int(os.environ.get("OPENMHS_MAX_REL_XY", "4000"))
MAX_REL_Z = int(os.environ.get("OPENMHS_MAX_REL_Z", "2000"))


def api_base() -> str:
    return "{}/api/v2/extensions/{}".format(MICROSCOPE_URL, EXT)


def ofm_url(path: str) -> str:
    if path.startswith("http"):
        return path
    return MICROSCOPE_URL.rstrip("/") + path


def http_json(
    path: str,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
    base: str = "vp",
) -> Any:
    if path.startswith("http"):
        url = path
    elif base == "ofm":
        url = ofm_url(path)
    else:
        url = api_base() + path
    body = None if data is None else json.dumps(data).encode("utf-8")
    headers = {
        "Accept": "application/json",
        # Lets the Pi expose whether the current hardware lease belongs to a
        # browser/device or to the Codex MCP agent.
        "X-OpenMHS-Client": "cursor-mcp",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with HTTP.open(req, timeout=120) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP {} from {}: {}".format(exc.code, url, detail[:500])) from exc


def http_bytes(path: str, base: str = "vp") -> tuple:
    if path.startswith("http"):
        url = path
    elif base == "ofm":
        url = ofm_url(path)
    else:
        url = api_base() + path
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    with HTTP.open(req, timeout=60) as resp:
        return resp.read(), resp.headers.get_content_type() or "application/octet-stream"


def b64_image(path: str, base: str = "vp") -> Dict[str, str]:
    raw, mime = http_bytes(path, base=base)
    return {
        "mime_type": mime,
        "base64": base64.b64encode(raw).decode("ascii"),
        "byte_size": str(len(raw)),
        "url": path if path.startswith("http") else (
            ofm_url(path) if base == "ofm" else api_base() + path
        ),
    }


def latest_bundle() -> Dict[str, Any]:
    meta = http_json("/latest")
    prefix = "/prompts/{}".format(meta["id"])
    prompt_text, _ = http_bytes(prefix + "/prompt")
    return {
        "source": "microscope-http",
        "analysis_path": True,
        "microscope_url": MICROSCOPE_URL,
        "metadata": meta,
        "prompt_markdown": prompt_text.decode("utf-8", errors="replace"),
        "roi_image": b64_image(prefix + "/roi"),
        "frame_image": b64_image(prefix + "/frame"),
        "job_id": meta.get("job_id") or meta.get("id"),
        "note": (
            "The user cleared the selection. No ROI is currently selected. Do not reuse an older selection."
            if meta.get("selection_cleared") else
            "This is visual context, not a new command. Read ROI roles, record_id, canvas_revision and image_adjustments with the attached snapshot. Follow the user's request in the native Codex conversation. No job claim is needed for Codex-only prompts."
        ),
    }


def clamp_rel(x: int, y: int, z: int) -> Dict[str, int]:
    def clip(v: int, lim: int) -> int:
        if v > lim:
            return lim
        if v < -lim:
            return -lim
        return v

    return {
        "x": clip(int(x), MAX_REL_XY),
        "y": clip(int(y), MAX_REL_XY),
        "z": clip(int(z), MAX_REL_Z),
    }


def tool_list() -> list:
    tools: List[Dict[str, Any]] = [
        {
            "name": "get_latest_visual_prompt",
            "description": "Read the newest shared OF visual prompt, pinned to an immutable prompt ID, with native ROI/frame images. Analyze and present results in the Codex conversation; Codex-only shares require no worker job claim.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_visual_prompts",
            "description": "List visual prompts stored on the microscope (newest first).",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_visual_prompt",
            "description": "Fetch a specific visual prompt by id from the microscope for analysis.",
            "inputSchema": {
                "type": "object",
                "properties": {"prompt_id": {"type": "string"}},
                "required": ["prompt_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_visual_prompt_storage",
            "description": "Show Pi-side rolling storage usage and pending agent job count.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_pending_agent_jobs",
            "description": "List queued MicroWorld agent jobs waiting for Cursor/Codex/API workers.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "claim_agent_job",
            "description": "Claim a queued agent job (lease). Pass job_id from list_pending_agent_jobs, or omit to claim the newest pending job.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "worker_id": {"type": "string"},
                    "lease_seconds": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "heartbeat_agent_job",
            "description": "Extend the claim lease and mark the job running while you analyze or drive the microscope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "worker_id": {"type": "string"},
                    "lease_seconds": {"type": "integer"},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_agent_job",
            "description": "Get an agent job by id (or latest if omitted), including result when done.",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "submit_agent_job_result",
            "description": "Write analysis markdown + UI annotations back to the microscope so the MicroWorld web UI can render them. Always call this when finishing a claimed job.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "markdown": {"type": "string"},
                    "annotations": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of {type:mask|bbox|point|polyline|text, norm:{x,y,width,height}, label, color?, points?, text?}",
                    },
                    "mask_overlay_data_url": {
                        "type": "string",
                        "description": "Optional full-frame RGBA PNG data URL for web mask overlay",
                    },
                    "actions_log": {"type": "array", "items": {"type": "object"}},
                    "error": {"type": "string"},
                    "status": {"type": "string", "description": "done or failed"},
                    "worker_id": {"type": "string"},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "microscope_get_state",
            "description": "Read OpenFlexure instrument state (stage position, etc.).",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "microscope_move_rel",
            "description": "Relative stage move in steps (clamped). Prefer small steps.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "z": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "microscope_autofocus",
            "description": "Run fast autofocus on the microscope.",
            "inputSchema": {
                "type": "object",
                "properties": {"dz": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "microscope_snapshot",
            "description": "Capture a JPEG snapshot from the live stream (base64).",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]
    if SYNC_ENABLED:
        tools.append(
            {
                "name": "sync_latest_visual_prompt_to_workspace",
                "description": "OPTIONAL and disabled unless OPENMHS_VP_SYNC_ENABLED=1. Copies latest VP files into a local folder for @-mention convenience. NOT for analysis.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        )
    return tools


def call_tool(name: str, arguments: Dict[str, Any]) -> Any:
    if name == "get_latest_visual_prompt":
        return latest_bundle()
    if name == "list_visual_prompts":
        return http_json("/prompts")
    if name == "get_visual_prompt_storage":
        return http_json("/status")
    if name == "get_visual_prompt":
        prompt_id = str(arguments.get("prompt_id") or "").strip()
        if not prompt_id:
            raise ValueError("prompt_id is required")
        meta = http_json("/prompts/{}".format(prompt_id))
        prompt_text, _ = http_bytes("/prompts/{}/prompt".format(prompt_id))
        return {
            "source": "microscope-http",
            "analysis_path": True,
            "metadata": meta,
            "prompt_markdown": prompt_text.decode("utf-8", errors="replace"),
            "roi_image": b64_image("/prompts/{}/roi".format(prompt_id)),
            "frame_image": b64_image("/prompts/{}/frame".format(prompt_id)),
            "job_id": meta.get("job_id") or prompt_id,
        }
    if name == "list_pending_agent_jobs":
        return http_json("/jobs/pending")
    if name == "get_agent_job":
        job_id = str(arguments.get("job_id") or "").strip()
        if job_id:
            return http_json("/jobs/{}".format(job_id))
        return http_json("/jobs/latest")
    if name == "claim_agent_job":
        worker = str(arguments.get("worker_id") or WORKER_ID).strip()
        lease = arguments.get("lease_seconds")
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id:
            pending = http_json("/jobs/pending")
            items = pending.get("items") or []
            if not items:
                return {"ok": False, "error": "no pending jobs"}
            job_id = items[0]["id"]
        body: Dict[str, Any] = {"worker_id": worker}
        if lease is not None:
            body["lease_seconds"] = int(lease)
        return http_json("/jobs/{}/claim".format(job_id), method="POST", data=body)
    if name == "heartbeat_agent_job":
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        body = {
            "worker_id": str(arguments.get("worker_id") or WORKER_ID).strip(),
        }
        if arguments.get("lease_seconds") is not None:
            body["lease_seconds"] = int(arguments["lease_seconds"])
        return http_json("/jobs/{}/heartbeat".format(job_id), method="POST", data=body)
    if name == "submit_agent_job_result":
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        body = {
            "worker_id": str(arguments.get("worker_id") or WORKER_ID).strip(),
            "summary": arguments.get("summary") or "",
            "markdown": arguments.get("markdown") or arguments.get("summary") or "",
            "annotations": arguments.get("annotations") or [],
            "mask_overlay_data_url": arguments.get("mask_overlay_data_url"),
            "actions_log": arguments.get("actions_log") or [],
            "error": arguments.get("error") or "",
            "status": arguments.get("status") or ("failed" if arguments.get("error") else "done"),
        }
        return http_json("/jobs/{}/result".format(job_id), method="POST", data=body)
    if name == "microscope_get_state":
        return http_json("/api/v2/instrument/state", base="ofm")
    if name == "microscope_move_rel":
        x = int(arguments.get("x") or 0)
        y = int(arguments.get("y") or 0)
        z = int(arguments.get("z") or 0)
        steps = clamp_rel(x, y, z)
        payload = {"absolute": False, **steps}
        return {
            "requested": {"x": x, "y": y, "z": z},
            "clamped": steps,
            "result": http_json("/api/v2/actions/stage/move", method="POST", data=payload, base="ofm"),
        }
    if name == "microscope_autofocus":
        dz = int(arguments.get("dz") or 2000)
        dz = max(200, min(8000, dz))
        # OF fast_autofocus typically accepts dz in body
        return http_json(
            "/api/v2/extensions/org.openflexure.autofocus/fast_autofocus",
            method="POST",
            data={"dz": dz},
            base="ofm",
        )
    if name == "microscope_snapshot":
        img = b64_image("/api/v2/streams/snapshot", base="ofm")
        return {"ok": True, "image": img}
    if name == "sync_latest_visual_prompt_to_workspace":
        if not SYNC_ENABLED:
            raise RuntimeError(
                "Workspace sync is disabled (default). Analysis must use get_latest_visual_prompt. "
                "Set OPENMHS_VP_SYNC_ENABLED=1 only if you intentionally want a local mirror."
            )
        bundle_meta = http_json("/latest")
        SYNC_DIR.mkdir(parents=True, exist_ok=True)
        frame, _ = http_bytes("/latest/frame")
        roi, _ = http_bytes("/latest/roi")
        prompt, _ = http_bytes("/latest/prompt")
        frame_path = SYNC_DIR / "latest-frame.jpg"
        roi_path = SYNC_DIR / "latest-roi.jpg"
        md_path = SYNC_DIR / "latest.md"
        frame_path.write_bytes(frame)
        roi_path.write_bytes(roi)
        md_path.write_bytes(prompt)
        (SYNC_DIR / "latest.json").write_text(
            json.dumps(bundle_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {
            "synced": True,
            "analysis_path": False,
            "warning": "Synced files are a convenience mirror only. Prefer MCP get_latest_visual_prompt for analysis.",
            "dir": str(SYNC_DIR),
            "files": [str(frame_path), str(roi_path), str(md_path)],
            "metadata": bundle_meta,
        }
    raise ValueError("Unknown tool: {}".format(name))


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mid = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    def result(payload: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": payload}

    def error(code: int, msg: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}

    if method == "initialize":
        start_presence()
        return result(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "microworld-visual-prompts", "version": "1.1.0"},
            }
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return result({"tools": tool_list()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            payload = call_tool(name, args)
            presence(read=True)
            images = []
            if isinstance(payload, dict):
                for key in ("roi_image", "frame_image", "image"):
                    value = payload.get(key)
                    if isinstance(value, dict) and value.get("base64"):
                        images.append({"type": "text", "text": key})
                        images.append({"type": "image", "mimeType": value["mime_type"], "data": value.pop("base64")})
            text = json.dumps(payload, ensure_ascii=False)
            return result({"content": [{"type": "text", "text": text}] + images, "isError": False})
        except Exception as exc:
            return result({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    if method == "ping":
        return result({})
    if mid is None:
        return None
    return error(-32601, "Method not found: {}".format(method))


_presence_started = False


def presence(read=False):
    try:
        body = json.dumps({"read": read}).encode()
        req = urllib.request.Request(api_base() + "/codex", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as response:
            response.read()
    except Exception:
        pass


def start_presence():
    global _presence_started
    if _presence_started:
        return
    _presence_started = True
    def loop():
        while True:
            presence()
            time.sleep(15)
    threading.Thread(target=loop, daemon=True).start()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
