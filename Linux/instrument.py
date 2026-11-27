"""Instrument operations for the native Linux panels via the shared desktop server."""

from datetime import datetime
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from desktop import LOCAL

SCAN = "/api/v2/extensions/org.microworld.scan-mosaic"
HARDWARE = SCAN + "/hardware"
PROMPTS = "/api/v2/extensions/org.microworld.visual-prompts"
STAGE = "/api/v2/instrument/state/stage/position"
CAMERA = "/api/v2/streams/snapshot"
CAMERA_SETTINGS = "/api/v2/instrument/settings/camera"
MAPPING = "org.openflexure.camera_stage_mapping/"
FOCUS = "/api/v2/extensions/org.openflexure.autofocus/fast_autofocus"
FOCUS_RANGE = {"Off": 0, "Coarse": 100, "Medium": 30, "Fine": 10, "Fast": 2000}
SESSION_ID = re.compile(r"[a-zA-Z0-9_-]{1,120}")
ACTION_PATH = re.compile(r"/api/v2/actions/[a-zA-Z0-9_-]{1,120}")


class Api:
    def __init__(self, connection):
        self.connection = connection
        self.workspace = Path(connection["workspace"])
        self.http = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.capabilities = {}

    def request(self, path, body=None, method=None, timeout=5):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid local instrument route.")
        data = json.dumps(body).encode() if body is not None else None
        request = Request(LOCAL + path, data=data, method=method,
                          headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with self.http.open(request, timeout=timeout) as reply:
                content = reply.read()
                if reply.headers.get_content_type() == "application/json":
                    return json.loads(content)
                return content
        except HTTPError as error:
            try:
                detail = json.load(error)
                message = detail.get("message") or detail.get("error")
            except (ValueError, AttributeError, OSError):
                message = None
            raise OSError(message or f"Instrument request failed (HTTP {error.code}).") from error

    def idle(self):
        if self.connection["openmhs"] and self.request(SCAN + "/hardware").get("busy"):
            raise RuntimeError("The microscope is busy.")

    def run(self, path, body, report=None, timeout=90):
        self.idle()
        self.request("/desktop/session")
        result = self.request(path, body, timeout=16)
        route = urlsplit(result.get("href", "")).path
        if not ACTION_PATH.fullmatch(route):
            raise RuntimeError("The microscope did not return an action to monitor.")
        end = time.monotonic() + timeout
        while True:
            if report:
                report(route, result)
            status = result.get("status")
            if status == "completed":
                return result.get("output")
            if status in ("error", "failed", "cancelled"):
                logs = result.get("log") or []
                last = logs[-1] if logs else ""
                message = last.get("message") if isinstance(last, dict) else last
                raise RuntimeError(message or f"Microscope action {status}.")
            if time.monotonic() >= end:
                raise RuntimeError("The microscope took too long to report completion.")
            time.sleep(.4)
            result = self.request(route)

    def move(self, xyz, absolute=False):
        return self.run("/api/v2/actions/stage/move", dict(zip("xyz", xyz), absolute=absolute))

    def write_settings(self, values):
        self.idle()
        self.request("/desktop/session")
        return self.request("/api/v2/instrument/settings", {"camera": values}, method="PUT")

    def identify_controls(self):
        info = self.request("/api/v2/")
        actions = info.get("actions", {})
        properties = info.get("properties", {})
        installed = {}
        try:
            installed = {item["title"]: item.get("links", {}) for item in self.request("/api/v2/extensions")}
        except (OSError, TypeError, KeyError):
            pass
        namespace = self.connection.get("api_namespace", "org.microworld")
        names = {
            "mosaic": (namespace + ".scan-mosaic/tile", actions),
            "stock": ("org.openflexure.scan/tile", actions),
            "smart": ("org.openflexure.smart-stack/tile", actions),
            "calibrate": (MAPPING + "calibrate_xy", actions),
            "calibration": (MAPPING + "get_calibration", properties),
            "center": (MAPPING + "move_in_image_coordinates", actions),
        }
        caps = {}
        for key, (name, entries) in names.items():
            forms = entries.get(name, {}).get("forms", [])
            extension, entry = name.split("/", 1)
            href = (forms[0].get("href") if forms else
                    installed.get(extension, {}).get(entry, {}).get("href"))
            parts = urlsplit(href or "")
            if parts.path.startswith("/api/v2/extensions/") and not parts.query and not parts.fragment:
                caps[key] = parts.path
        caps["skip_empty"] = any(name.startswith("org.openflexure.background-detect/") for name in actions)
        self.capabilities = caps
        return caps

    def calibration(self):
        path = self.capabilities.get("calibration")
        return self.request(path) if path else None

    def snapshot(self):
        jpeg = self.request(CAMERA, timeout=8)
        if not isinstance(jpeg, bytes) or not jpeg.startswith(b"\xff\xd8"):
            raise ValueError("The microscope did not return a JPEG.")
        folder = self.workspace / "captures"
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / datetime.now().strftime("Camera-%Y-%m-%dT%H-%M-%S.%f.jpg")
        temporary = out.with_suffix(".part")
        temporary.write_bytes(jpeg)
        temporary.replace(out)
        return out

    def scan(self, mode, fields, options, report):
        mosaic = mode == "Scan" and self.capabilities.get("mosaic")
        path = self.capabilities.get("smart") if mode == "Smart Stack" else mosaic or self.capabilities.get("stock")
        if not path:
            raise RuntimeError("The microscope does not support this acquisition mode.")
        grid, stride = options["grid"], options["stride_size"]
        if any(n > 1 and s == 0 for n, s in zip(grid, stride)):
            raise ValueError("Enter a nonzero stride for each axis with multiple images.")
        focus = options["focus"]
        request = {**fields, "grid": grid, "stride_size": stride, "style": options["style"].lower(),
                   "namemode": options["naming"].lower(), "fast_autofocus": focus == "Fast",
                   "autofocus_dz": FOCUS_RANGE[focus], "use_video_port": not fields.get("bayer", False),
                   "detect_empty_fields_and_skip_autofocus": options["skip_empty"] and self.capabilities["skip_empty"]}
        bayer = fields.get("bayer", False)
        if mosaic:
            stage = self.request(SCAN + "/hardware")
            updated = (focus == "Off" or request["namemode"] == "number" or
                       (request["style"] == "spiral" and grid[2] > 1))
            if (updated and not stage.get("advanced_capture")) or (bayer and not stage.get("bayer_scan")):
                raise RuntimeError("Update the MicroWorld Pi extension to use these scan options.")
            previous = self.request(SCAN + "/live_details")
            sid = previous.get("session_id") or ""
            prior = previous.get("cell_count") or 0
            if prior and SESSION_ID.fullmatch(sid):
                manifest = self.request(SCAN + f"/mac/session?session_id={sid}").get("manifest", {})
                if not manifest.get("sync", {}).get("complete") or len(manifest.get("cells", [])) < prior:
                    raise RuntimeError("The PC Worker is still archiving the previous scan.")
            current = self.request(SCAN + "/mac/views/current")
            if current.get("current_view_id"):
                request["view_id"] = current["current_view_id"]
            request.update(auto_stride=options["auto_stride"], target_overlap=.3,
                           mac_preview_only=grid[2] == 1 and not bayer and request["namemode"] == "coordinates",
                           keep_tiles=True, auto_refine=False, scan_focus_guard=False,
                           adaptive_focus_stack=False, center_tile=False)
        if mode == "Smart Stack":
            if grid[2] <= 1 or focus != "Fast":
                raise ValueError("Smart Stack needs Fast autofocus and more than one Z image.")
            request.update(threshold=.9, width=200, align_dist=options["align_dist"],
                           backlash=100, fit_style="chevy", m_and_m_index=10,
                           autofocus_dz=options["focus_span"])
        return self.run(path, request, report, timeout=3600)

    def canvases(self):
        path = "/mosaic/list" if self.connection["worker_running"] else SCAN + "/mac/list"
        return self.request(path).get("sessions", [])

    def gallery(self, worker_available):
        canvases, captures, notes = [], [], []
        if worker_available and self.connection["openmhs"]:
            try:
                canvases = self.canvases()
            except OSError as error:
                notes.append(f"Canvases: {error}")
        try:
            try:
                captures = self.request("/api/v2/captures")
            except OSError:
                captures = self.request(SCAN + "/captures")
        except OSError as error:
            notes.append(f"Microscope captures: {error}")
        if isinstance(captures, dict):
            captures = list(captures.values())
        local_frames = sorted((f for f in (self.workspace / "captures").glob("*") if
                               f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".tiff", ".tif"}),
                              reverse=True)
        return canvases, local_frames, captures, "\n".join(notes)
