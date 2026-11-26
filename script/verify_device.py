"""Verify the selected instrument through MicroWorld's packaged service."""

import argparse
from http.cookiejar import CookieJar
import io
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
LOCAL = "http://127.0.0.1:18766"
SCAN = "/api/v2/extensions/org.microworld.scan-mosaic"
VP = "/api/v2/extensions/org.microworld.visual-prompts"
STAGE = "/api/v2/instrument/state/stage/position"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                   urllib.request.HTTPCookieProcessor(CookieJar()))


def fetch(path, body=None, timeout=8):
    request = urllib.request.Request(LOCAL + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
    with HTTP.open(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if response.headers.get_content_type() == "application/json" else raw


def wait_for(label, probe, accepts, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            data = probe()
            if accepts(data):
                return data
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise TimeoutError(label)


def action(path, body, timeout=90):
    request = fetch(path, body)
    route = request["href"].removeprefix(LOCAL)
    if not route.startswith("/api/v2/actions/"):
        raise ValueError(f"Unexpected action route {route}")
    completed = wait_for("action did not finish: " + route, lambda: fetch(route),
                         lambda data: data.get("status") in ("completed", "error", "cancelled"), timeout)
    if completed.get("status") != "completed":
        raise RuntimeError(completed.get("log", []))
    return completed


def mcp(name, arguments=None):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": name, "arguments": arguments or {}}},
    ]
    process = subprocess.run([str(ROOT / "script/mcp")],
                             input="\n".join(map(json.dumps, messages)) + "\n",
                             capture_output=True, text=True, timeout=22, check=True)
    results = [json.loads(line) for line in process.stdout.splitlines()]
    if len(results) != 3 or results[2]["result"].get("isError"):
        raise RuntimeError(results)
    return results[2]["result"]["content"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True, help="Expected microscope ID or its suffix")
    parser.add_argument("--acquire", action="store_true", help="Run the live stage, focus and 2×2 scan checks")
    parser.add_argument("--scan-id", help="Verify an existing scan without moving the stage")
    args = parser.parse_args()
    status = fetch("/desktop/status")
    if not status["device_id"].lower().endswith(args.device.lower()):
        raise RuntimeError(f"The app selected a different instrument: {status['device_id']}")
    fetch("/")  # establish the same connection cookie as the desktop web view
    details = fetch("/api/v2/")
    assert status["device_id"] in details["title"]
    start = fetch(STAGE)
    hw = fetch(SCAN + "/hardware")
    assert not hw.get("busy"), "The instrument is busy."
    print("Device", status["device_id"], status["microscope"], flush=True)
    wait_for("Pi did not see the bundled worker", lambda: fetch(VP + "/status"),
             lambda data: data.get("agent", {}).get("worker_online")
             and str(data.get("agent", {}).get("supervisor_url")).endswith(":18766"), 35)
    camera = Image.open(io.BytesIO(fetch("/api/v2/streams/snapshot")))
    camera.load()
    session = json.loads(mcp("microworld_session")[0]["text"])
    assert session["id"] == status["device_id"]
    assert Path(session["workspace"]).is_relative_to(ROOT / "Workspace")
    mcp("microscope_get_state")
    assert any(item["type"] == "image" for item in mcp("microscope_snapshot"))
    mcp("microworld_list_canvases")
    report = {"device": status["device_id"], "url": status["microscope"],
              "camera_size": list(camera.size), "worker": "connected",
              "workspace": session["workspace"], "history": session["history"],
              "mcp": ["initialize", "tools/list", "session", "state", "snapshot", "canvases"]}
    print("Camera, worker, workspace and MCP verified", flush=True)
    sid = args.scan_id
    if args.acquire:
        old = fetch(SCAN + "/live_details")
        if old.get("cell_count"):
            sid = old["session_id"]
            wait_for("Previous capture has not been archived in this workspace",
                     lambda: fetch(f"/mosaic/{sid}")["manifest"],
                     lambda manifest: manifest.get("sync", {}).get("complete")
                     and len(manifest.get("cells", [])) >= old["cell_count"], 45)
        action("/api/v2/actions/stage/move", {"absolute": False, "x": 20, "y": 0, "z": 0})
        try:
            moved = fetch(STAGE)
            assert moved["x"] == start["x"] + 20, moved
        finally:
            action("/api/v2/actions/stage/move", {"absolute": True, **start})
        assert fetch(STAGE) == start
        print("Stage moved 20 X steps and returned", flush=True)
        action("/api/v2/extensions/org.openflexure.autofocus/fast_autofocus", {"dz": 2000})
        print("Fast autofocus complete", flush=True)
        filename = time.strftime("microworld_verify_%Y%m%d_%H%M%S")
        scanned = action(SCAN + "/tile", {
            "filename": filename, "grid": [2, 2, 1], "style": "snake", "temporary": False,
            "stride_size": [2000, 1500, 100], "auto_stride": False, "fast_autofocus": True,
            "autofocus_dz": 2000, "scan_focus_guard": True, "adaptive_focus_stack": True,
            "mac_preview_only": True, "keep_tiles": True, "use_video_port": True,
            "center_tile": False, "auto_refine": False, "tags": ["microworld-verification"],
        }, 200)
        output = scanned["output"]
        sid = output["session_id"]
        assert output.get("tiles_seen") == 4, output
        print(f"Acquired 4 tiles for {sid}; waiting for PC refine", flush=True)
        report.update({"stage_move": "20 X steps and restored", "autofocus": "completed"})
    if sid:
        refine = wait_for("PC refine did not finish", lambda: fetch(f"/mosaic/{sid}/refine_status"),
                          lambda data: data.get("phase") == "done" and data.get("refined"), 180)
        manifest = fetch(f"/mosaic/{sid}")["manifest"]
        assert manifest.get("sync", {}).get("complete") and len(manifest["cells"]) == 4
        refined_image = Image.open(io.BytesIO(fetch(f"/mosaic/{sid}/refined.jpg")))
        refined_image.load()
        assert any(item["type"] == "image" for item in mcp("microworld_get_canvas", {"session_id": sid}))
        report.update({"scan": sid, "tiles": 4, "refined_size": list(refined_image.size),
                       "registration_warnings": refine.get("registration", {}).get("warning_edges")})
        print("Refined Canvas and MCP preview verified", flush=True)
    folder = ROOT / "Verification"
    folder.mkdir(exist_ok=True)
    (folder / (status["device_id"][-5:] + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
