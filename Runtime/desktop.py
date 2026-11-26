"""Local UI and isolated PC Worker for a single selected microscope."""

import argparse
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import secrets
from http.cookies import SimpleCookie
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from network import HTTP, LEGACY_API, MICROWORLD_API, discover, identify, read_json

RESOURCES = Path(__file__).resolve().parents[1]
WEB = RESOURCES / "Web"
PORT = 18766
LOCAL = f"http://127.0.0.1:{PORT}"
EXTENSIONS = "/api/v2/extensions/"
LEGACY = "http://127.0.0.1:18765"
HOP_HEADERS = {"connection", "content-length", "transfer-encoding", "date", "server",
               "keep-alive", "access-control-allow-origin", "vary"}


def write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + f".{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def request_json(url, body):
    request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with HTTP.open(request, timeout=12) as response:
        return json.load(response)


def routed_address(url):
    parts = urlsplit(url)
    host = socket.gethostbyname(parts.hostname)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((host, parts.port))
        return route.getsockname()[0], host


def configure_project(root, workspace, history):
    command = str(root / "script" / "mcp")
    entry = {"command": command, "args": []}
    write_atomic(root / "mcp.json", json.dumps({"mcpServers": {"microworld": entry}}, indent=2) + "\n")
    write_atomic(root / ".codex" / "config.toml",
                 "[mcp_servers.microworld]\ncommand = " + json.dumps(command, ensure_ascii=False) + "\n"
                 "startup_timeout_sec = 12\ntool_timeout_sec = 150\n")
    # Keep the standalone Codex launcher fully project scoped as well.
    project_config = (root / ".codex" / "config.toml").read_text(encoding="utf-8")
    write_atomic(workspace / ".codex" / "config.toml", project_config)
    codex_config = history / "Codex" / "config.toml"
    if not codex_config.exists():
        trusted = "\n[projects." + json.dumps(str(workspace), ensure_ascii=False) + "]\ntrust_level = \"trusted\"\n"
        write_atomic(codex_config, project_config + trusted)


def serve(args):
    target = identify(args.url)
    if target["id"] != args.id:
        raise RuntimeError("The address now belongs to a different microscope. Refresh Devices.")
    base = target["url"]
    namespace = target["api_namespace"]
    session_cookie = secrets.token_urlsafe(24)
    root = Path(args.root).resolve()
    workspace = root / "Workspace" / args.id
    history = root / "ConversationHistory" / args.id
    for folder in (workspace, history, root / "State" / "tmp", root / "Logs"):
        folder.mkdir(parents=True, exist_ok=True)
    local_ip, remote_ip = routed_address(base)
    allowed_ips = {remote_ip}
    for alias in args.alias:
        try:
            if identify(alias, 1)["id"] == args.id:
                allowed_ips.add(socket.gethostbyname(urlsplit(alias).hostname))
        except (OSError, ValueError):
            pass
    os.environ.update(
        OPENMHS_WORKSPACE=str(workspace),
        OPENMHS_REPO=str(root),
        OPENMHS_MICROSCOPE_URL=base,
        MICROWORLD_API_NAMESPACE=namespace,
        OPENMHS_SUPERVISOR_HOST=local_ip,
        OPENMHS_SUPERVISOR_PORT=str(PORT),
        OPENMHS_SUPERVISOR_URL=f"http://{local_ip}:{PORT}",
        OPENMHS_BIND_IP=local_ip,
        OPENMHS_WORKER_PID_FILE=str(root / "State" / "worker.pid"),
        OPENMHS_WORKER_LOG=str(root / "Logs" / "worker.log"),
        OPENMHS_PYTHON=sys.executable,
        OPENMHS_AGENT_WORKER_ID="microworld",
        OPENMHS_PRESERVE_NON_STREAMED_CAPTURES="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        TMPDIR=str(root / "State" / "tmp"),
    )
    sys.path.insert(0, str(RESOURCES / "Worker"))
    import openmhs_supervisor as worker

    class WorkerHandler(worker.Handler):
        def log_request(self, code="-", size="-"):
            if self.command != "GET" or (isinstance(code, int) and code >= 400):
                super().log_request(code, size)

        def parse_request(self):
            if not super().parse_request():
                return False
            client = ipaddress.ip_address(self.client_address[0])
            if not client.is_loopback and str(client) not in allowed_ips:
                self._json(403, {"error": "This worker belongs to the selected microscope."})
                return False
            return True

    class DesktopHandler(WorkerHandler):
        def _cors(self):
            if urlsplit(self.path).path in {"/", "/desktop/session"} and self.command == "GET":
                self.send_header("Set-Cookie", f"microworld={session_cookie}; Path=/; HttpOnly; SameSite=Strict")

        def accepts_write(self):
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                active = cookie.get("microworld")
                return active is not None and secrets.compare_digest(active.value, session_cookie)
            except (TypeError, ValueError):
                return False

        def parse_request(self):
            if not super().parse_request():
                return False
            if self.headers.get("Host") != f"127.0.0.1:{PORT}":
                self._json(403, {"error": "Use the MicroWorld loopback address."})
                return False
            return True

        def static_file(self):
            path = urlsplit(self.path).path
            if path == "/desktop/config.js":
                payload = json.dumps({"microscopeURL": base, "supervisorURL": LOCAL,
                                      "apiNamespace": namespace})
                self._bytes(200, f"window.__MICROWORLD__ = {payload};".encode(), "text/javascript", "no-store")
                return True
            if path == "/desktop/status":
                self._json(200, {**worker.worker_status(), "device_id": args.id,
                                 "openmhs": target["openmhs"], "api_namespace": namespace,
                                 "history": str(history)})
                return True
            if path == "/desktop/session":
                self._json(200, {"device_id": args.id})
                return True
            match = re.fullmatch(r"/desktop/canvas/([a-zA-Z0-9_-]{1,120})/layer", path)
            if match:
                try:
                    import io
                    import openmhs_canvas as canvas
                    from PIL import Image

                    sid = match[1]
                    query = parse_qs(urlsplit(self.path).query)
                    layer_id = query["id"][0]
                    width = max(96, min(2048, int(query.get("width", ["320"])[0])))
                    doc = canvas.document(sid)
                    layer = next(t for t in doc["tiles"] if t["id"] == layer_id)
                    with Image.open(canvas.tile_path(sid, layer)) as source:
                        image = source.convert("RGB")
                        image.thumbnail((width, width), Image.Resampling.LANCZOS)
                    settings = layer.get("adjustments") or doc["adjustments"]
                    if settings != canvas.DEFAULTS:
                        image = Image.fromarray(canvas.adjusted(canvas.np.asarray(image), settings))
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=90)
                    self._bytes(200, buffer.getvalue(), "image/jpeg", "private, max-age=3600")
                except (KeyError, ValueError, StopIteration, FileNotFoundError):
                    self._json(404, {"error": "Layer unavailable."})
                return True
            if path == "/":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if "slide" in query:
                    if query.get("device") != [args.id]:
                        self.send_error(409, "Connect the microscope named by this Slide link in MicroWorld.")
                        return True
                    sid = query["slide"][0]
                    if len(query["slide"]) != 1 or not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", sid):
                        self.send_error(400, "Invalid Slide ID.")
                        return True
                    from openmhs_views import resolve_record_dir
                    record = resolve_record_dir(sid)
                    if record is None or not (record / "refined.jpg").is_file():
                        self.send_error(404, "The refined Slide is unavailable in this workspace.")
                        return True
            requested = (WEB / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
            if WEB not in requested.parents or not requested.is_file():
                return False
            mime = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
            self._bytes(200, requested.read_bytes(), mime, "no-cache")
            return True

        def proxy(self):
            if self.command not in ("GET", "HEAD"):
                if not self.accepts_write() or self.headers.get("Origin", LOCAL) != LOCAL:
                    self._json(403, {"error": "Open this control in MicroWorld."})
                    return
                try:
                    if identify(base, 2)["id"] != args.id:
                        raise ValueError("Microscope identity changed. Reconnect from Devices.")
                except (OSError, ValueError) as exc:
                    self._json(409, {"error": str(exc)})
                    return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 64 * 1024 * 1024:
                    raise ValueError("Request is too large.")
                body = self.rfile.read(size) if size else None
                headers = {k: self.headers[k] for k in ("Accept", "Content-Type", "Range") if k in self.headers}
                client = self.headers.get("X-MicroWorld-Client") or self.headers.get("X-OpenMHS-Client")
                if client:
                    header = "X-OpenMHS-Client" if namespace == LEGACY_API else "X-MicroWorld-Client"
                    headers[header] = client
                path = self.path
                canonical_prefix = EXTENSIONS + MICROWORLD_API + "."
                if namespace == LEGACY_API and path.startswith(canonical_prefix):
                    path = EXTENSIONS + LEGACY_API + "." + path[len(canonical_prefix):]
                req = Request(base + path, data=body, headers=headers, method=self.command)
                try:
                    upstream = HTTP.open(req, timeout=125)
                except HTTPError as exc:
                    upstream = exc
                with upstream:
                    mime = upstream.headers.get_content_type()
                    if mime == "application/json":
                        raw = upstream.read()
                        # Thing Description, action hrefs, and capture links
                        # must stay on this application's verified origin.
                        raw = raw.replace(base.encode(), LOCAL.encode())
                        self._bytes(upstream.code, raw, mime, "no-store")
                        return
                    self.send_response(upstream.code)
                    for key, value in upstream.headers.items():
                        if key.lower() not in HOP_HEADERS:
                            self.send_header(key, value.replace(base, LOCAL) if key.lower() == "location" else value)
                    self.end_headers()
                    while chunk := upstream.read1(65536):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (OSError, ValueError, URLError) as exc:
                self._json(502, {"error": str(exc)})

        def do_GET(self):
            if self.path.startswith("/api/"):
                return self.proxy()
            if not self.static_file():
                super().do_GET()

        def do_HEAD(self):
            if self.path.startswith("/api/"):
                return self.proxy()
            self.send_error(405)

        def do_POST(self):
            path = urlsplit(self.path).path
            if path in {"/workspace", "/microscope", "/installation", "/start", "/stop", "/restart"}:
                return self._json(403, {"error": "Use the MicroWorld device selector."})
            if self.path.startswith("/api/"):
                return self.proxy()
            if not self.accepts_write():
                return self._json(403, {"error": "Reload the MicroWorld window."})
            super().do_POST()

        def do_PUT(self):
            if self.path.startswith("/api/"):
                return self.proxy()
            self.send_error(405)

        do_PATCH = do_PUT

        def do_DELETE(self):
            if self.path.startswith("/api/"):
                return self.proxy()
            if not self.accepts_write():
                return self._json(403, {"error": "Reload the MicroWorld window."})
            super().do_DELETE()

    desktop = ThreadingHTTPServer(("127.0.0.1", PORT), DesktopHandler)
    receiver = None
    legacy_url = None
    started = False

    def stop(*_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if local_ip != "127.0.0.1":
            receiver = ThreadingHTTPServer((local_ip, PORT), WorkerHandler)
            threading.Thread(target=receiver.serve_forever, daemon=True).start()
        if target["openmhs"]:
            same_device = False
            try:
                legacy = read_json(LEGACY + "/status", 1)
                old_url = legacy.get("microscope")
                same_device = bool(legacy.get("worker_running") and old_url and
                                   identify(old_url, 1)["id"] == args.id)
            except (OSError, ValueError):
                pass
            if same_device:
                if read_json(base + EXTENSIONS + namespace + ".scan-mosaic/hardware").get("busy"):
                    raise RuntimeError("The selected microscope is running an acquisition. Connect after it finishes.")
                stopped = request_json(LEGACY + "/stop", {})
                if stopped.get("worker_running"):
                    raise RuntimeError("The existing PC Worker is still running for this microscope.")
                legacy_url = old_url
            started = True
            if not worker.start_worker().get("worker_running"):
                raise RuntimeError("The bundled PC Worker did not start. See Logs/worker.log.")
        configure_project(root, workspace, history)
        write_atomic(root / "State" / "connection.json", json.dumps({
            "id": args.id, "url": base, "workspace": str(workspace), "history": str(history),
            "desktop": LOCAL, "pid": os.getpid(), "openmhs": target["openmhs"],
            "api_namespace": namespace,
        }, indent=2) + "\n")
        if args.parent:
            def watch_parent():
                while os.getppid() == args.parent:
                    time.sleep(1)
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=watch_parent, daemon=True).start()
        print("Connected", args.id, base, flush=True)
        desktop.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        desktop.server_close()
        if receiver:
            receiver.server_close()
        if started:
            worker.stop_worker()
        if legacy_url:
            try:
                if read_json(LEGACY + "/status", 2).get("microscope") == legacy_url:
                    request_json(LEGACY + "/start", {})
            except (OSError, ValueError):
                print("Could not resume the previous PC Worker.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("discover")
    search.add_argument("saved", nargs="*")
    check = commands.add_parser("identify")
    check.add_argument("url")
    server = commands.add_parser("serve")
    server.add_argument("--root", required=True)
    server.add_argument("--url", required=True)
    server.add_argument("--id", required=True)
    server.add_argument("--alias", action="append", default=[])
    server.add_argument("--parent", type=int)
    args = parser.parse_args()
    if args.command == "discover":
        print(json.dumps(discover(args.saved)))
    elif args.command == "identify":
        print(json.dumps(identify(args.url)))
    else:
        serve(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
