"""MCP adapter pinned to the MicroWorld connection at process launch."""

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from network import HTTP, identify, read_json

ROOT = Path(sys.argv[1]).resolve()
STATE = ROOT / "State" / "connection.json"
LOCAL = "http://127.0.0.1:18766"
SCAN = "/api/v2/extensions/org.openmhs.scan-mosaic"
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,120}")
ACTION_PATH = re.compile(r"/api/v2/actions/([a-zA-Z0-9_-]{1,120})")
SESSION_PARAM = {"type": "object", "properties": {"session_id": {"type": "string"}},
                 "required": ["session_id"]}
EXTRA = [
    {"name": "microworld_session", "description": "Show the selected microscope ID and local workspace.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "microworld_list_canvases", "description": "List Canvas records for this instrument.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "microworld_get_canvas", "description": "Read a Canvas manifest and its image preview.",
     "inputSchema": SESSION_PARAM},
    {"name": "microworld_scan_2x2",
     "description": "Start one 2×2 scan from the current field on the connected microscope. Moves the stage and uses scan autofocus. Call only when the user requests acquisition. Poll microworld_scan_status with the returned action_id.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "microworld_scan_status",
     "description": "Read a scan action and final PC stitching status. When the refined Slide is ready, returns the native MicroWorld link and a browser link.",
     "inputSchema": {"type": "object", "properties": {"action_id": {"type": "string"}},
                     "required": ["action_id"]}},
    {"name": "microworld_get_slide_roi",
     "description": "Read the latest ROI drawn on the specified Slide in MicroWorld. Returns the immutable prompt ID, ROI geometry and crop image. Use this when the user asks to analyze their ROI.",
     "inputSchema": SESSION_PARAM},
]


def state():
    return json.loads(STATE.read_text(encoding="utf-8"))


def checked_connection(pinned):
    current = state()
    if any(current[k] != pinned[k] for k in ("id", "url", "pid")):
        raise RuntimeError("MicroWorld changed instruments. Start a new MCP session for the current device.")
    live = read_json(LOCAL + "/desktop/status", 2)
    if live.get("device_id") != pinned["id"] or live.get("microscope") != pinned["url"]:
        raise RuntimeError("The pinned microscope is disconnected in MicroWorld.")
    if identify(pinned["url"], 2)["id"] != pinned["id"]:
        raise RuntimeError("The device at this address changed. Reconnect from MicroWorld.")


def tool_result(message, payload, images=None, error=False):
    return {"jsonrpc": "2.0", "id": message.get("id"),
            "result": {"content": [{"type": "text", "text": payload}] + (images or []), "isError": error}}


def identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Provide a valid {name}.")
    return value


def slide_links(pinned, sid):
    query = urlencode({"device": pinned["id"], "slide": sid})
    return {"open_in_microworld": f"microworld://slide/{pinned['id']}/{sid}",
            "open_in_browser": f"{LOCAL}/?{query}"}


def start_scan(pinned):
    base = pinned["url"]
    if not pinned.get("openmhs") or not read_json(LOCAL + "/desktop/status").get("worker_running"):
        raise RuntimeError("Connect to a microscope with the MicroWorld Pi extensions and a running PC Worker first.")
    if read_json(base + SCAN + "/hardware").get("busy"):
        raise RuntimeError("The microscope is busy.")
    previous = read_json(base + SCAN + "/live_details")
    if previous.get("cell_count"):
        previous_id = identifier(previous.get("session_id"), "previous session ID")
        try:
            manifest = read_json(LOCAL + f"/mosaic/{previous_id}")["manifest"]
        except HTTPError as exc:
            raise RuntimeError("The PC Worker is still archiving the previous scan. Check before retrying.") from exc
        if not manifest.get("sync", {}).get("complete") or len(manifest.get("cells", [])) < previous["cell_count"]:
            raise RuntimeError("The PC Worker is still archiving the previous scan. Check before retrying.")
    body = {
        "filename": datetime.now().strftime("microworld_%Y%m%d_%H%M%S"),
        "temporary": False, "grid": [2, 2, 1], "stride_size": [2000, 1500, 100],
        "auto_stride": False, "style": "snake", "center_tile": False,
        "autofocus_dz": 2000, "fast_autofocus": True, "scan_focus_guard": True,
        "adaptive_focus_stack": True, "mac_preview_only": True, "use_video_port": True,
        "namemode": "coordinates", "keep_tiles": True, "auto_refine": False,
        "tags": ["openmhs-scan", "microworld-mcp"],
    }
    checked_connection(pinned)
    request = Request(base + SCAN + "/tile", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with HTTP.open(request, timeout=12) as response:
        action = json.load(response)
    route = ACTION_PATH.fullmatch(urlsplit(action["href"]).path)
    if not route:
        raise RuntimeError("The scan request was accepted but returned an unexpected action URL. Check the Viewer.")
    return {"status": action.get("status", "pending"), "action_id": route[1], "device_id": pinned["id"]}


def scan_status(pinned, action_id):
    action_id = identifier(action_id, "scan action ID")
    action = read_json(pinned["url"] + f"/api/v2/actions/{action_id}")
    data = {"action_id": action_id, "device_id": pinned["id"], "status": action["status"]}
    if action["status"] in {"error", "cancelled"}:
        data["log"] = action.get("log", [])
    if action["status"] != "completed":
        return data
    output = action.get("output") or {}
    sid = identifier(output.get("session_id"), "completed scan session ID")
    tiles = output.get("tiles_seen")
    data.update(session_id=sid, tiles=tiles)
    if tiles != 4:
        return {**data, "status": "incomplete"}
    refine = read_json(LOCAL + f"/mosaic/{sid}/refine_status", 15)
    data["refine_phase"] = refine.get("phase")
    data["refine_progress"] = refine.get("refine_progress")
    if refine.get("phase") == "done" and refine.get("refined"):
        data.update(status="ready", **slide_links(pinned, sid))
    elif refine.get("phase") in {"error", "failed"}:
        data.update(status="refine_failed", error=refine.get("error"))
    else:
        data["status"] = "processing"
    return data


def slide_roi(upstream, sid):
    items = upstream.http_json("/prompts").get("items", [])
    selection = next((item for item in items if item.get("record_id") == sid
                      and item.get("surface") == "mosaic"), None)
    if (not selection or selection.get("selection_cleared") or selection.get("canvas_revision") is None
            or not any(roi.get("role") == "target" for roi in selection.get("rois", []))):
        raise RuntimeError("No target ROI is selected on this Slide. Draw it in MicroWorld and wait for selection sync.")
    prompt_id = identifier(selection["id"], "ROI prompt ID")
    crop = upstream.b64_image(f"/prompts/{prompt_id}/roi")
    image = {"type": "image", "mimeType": crop["mime_type"], "data": crop["base64"]}
    return {"session_id": sid, "prompt_id": prompt_id, "canvas_revision": selection.get("canvas_revision"),
            "created_at": selection.get("created_at"), "roi": selection["roi"], "rois": selection["rois"]}, [image]


def run_extra(message, pinned, upstream):
    params = message.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    images = []
    if name == "microworld_session":
        data = {k: pinned[k] for k in ("id", "url", "workspace", "history")}
    elif name == "microworld_list_canvases":
        data = read_json(LOCAL + "/mosaic/list")
    elif name == "microworld_scan_2x2":
        data = start_scan(pinned)
    elif name == "microworld_scan_status":
        data = scan_status(pinned, args.get("action_id"))
    elif name == "microworld_get_slide_roi":
        data, images = slide_roi(upstream, identifier(args.get("session_id"), "Canvas session ID"))
    else:
        sid = identifier(args.get("session_id"), "Canvas session ID")
        data = read_json(LOCAL + f"/mosaic/{sid}")
        data.update(slide_links(pinned, sid))
        with HTTP.open(LOCAL + f"/mosaic/{sid}/preview.jpg", timeout=10) as res:
            images = [{"type": "image", "mimeType": res.headers.get_content_type(),
                       "data": base64.b64encode(res.read()).decode()}]
    return tool_result(message, json.dumps(data, ensure_ascii=False), images)


def main():
    pinned = state()
    os.environ["OPENMHS_MICROSCOPE_URL"] = pinned["url"]
    os.environ["OPENMHS_WORKSPACE"] = pinned["workspace"]
    os.environ["OPENMHS_VP_SYNC_DIR"] = str(Path(pinned["workspace"]) / "visual-prompts")
    os.environ["OPENMHS_VP_SYNC_ENABLED"] = "0"
    os.environ["OPENMHS_AGENT_WORKER_ID"] = "microworld-mcp"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "MCP"))
    import openmhs_vp_server as upstream

    presence = upstream.presence

    def checked_presence(read=False):
        try:
            checked_connection(pinned)
            presence(read=read)
        except (OSError, ValueError, RuntimeError):
            pass

    upstream.presence = checked_presence
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        if method == "tools/call":
            name = (message.get("params") or {}).get("name")
            try:
                checked_connection(pinned)
                result = (run_extra(message, pinned, upstream) if any(name == item["name"] for item in EXTRA)
                          else upstream.handle(message))
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                result = tool_result(message, str(exc), error=True)
            event = {"at": datetime.now(timezone.utc).isoformat(), "tool": name,
                     "error": result["result"].get("isError", False)}
            try:
                with (Path(pinned["history"]) / "mcp-events.jsonl").open("a", encoding="utf-8") as history:
                    history.write(json.dumps(event) + "\n")
            except OSError:
                pass
        else:
            result = upstream.handle(message)
            if method == "tools/list":
                result["result"]["tools"].extend(EXTRA)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError) as exc:
        sys.exit(f"Connect to a microscope in MicroWorld before starting MCP. {exc}")
