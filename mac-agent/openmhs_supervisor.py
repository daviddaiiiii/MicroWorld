#!/usr/bin/env python3
"""Supervisor for the MicroWorld PC worker.

Read-only Canvas APIs can serve the microscope over a private network.
Worker and workspace administration is restricted to local requests.

  GET  /status
  POST /start
  POST /stop
  POST /workspace   { "path": "/abs/or~/folder" }
  POST /microscope  { "url": "http://pi-address:5000" }
"""

from __future__ import annotations

import json
import ipaddress
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
REPO = Path(os.environ.get("OPENMHS_REPO", str(ROOT.parent))).expanduser().resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PID_FILE = Path(os.environ.get("OPENMHS_WORKER_PID_FILE", "/tmp/openmhs-mac-worker.pid"))
LOG_FILE = Path(os.environ.get("OPENMHS_WORKER_LOG", "/tmp/openmhs-mac-worker.log"))
PORT = int(os.environ.get("OPENMHS_SUPERVISOR_PORT", "18765"))
# 0.0.0.0 so browser on Pi URL (10.x) can reach Mac mosaic APIs on LAN
HOST = os.environ.get("OPENMHS_SUPERVISOR_HOST", "0.0.0.0")
WORKER_SCRIPT = ROOT / "openmhs_worker.py"
ENV_FILE = ROOT / ".env"
_WORKSPACE_SUBDIRS = ("views", "lib", "notes", "captures", "runs")
INSTALL_STATUS = Path("/tmp/openmhs-install-{}.json".format(os.getuid()))


def _lan_ip() -> str:
    """Best-effort LAN IP for UI (Pi-hosted page → Mac mosaic)."""
    try:
        target = os.environ.get("OPENMHS_MICROSCOPE_URL", "http://127.0.0.1:5000")
        host = urlparse(target).hostname or "127.0.0.1"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 5000))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def _load_dotenv() -> None:
    if not ENV_FILE.exists():
        return
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            kept.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("OPENMHS_LLM_") or key == "OPENAI_API_KEY":
            continue
        kept.append(line)
    if kept != lines:
        ENV_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
        ENV_FILE.chmod(0o600)
    for line in kept:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and not key.startswith("OPENMHS_LLM_") and key != "OPENAI_API_KEY":
            os.environ[key] = val


def _apply_proxy() -> None:
    """VEEE / System Settings proxy → env (LaunchAgent otherwise misses it)."""
    try:
        from openmhs_proxy import apply_macos_system_proxy

        apply_macos_system_proxy()
    except Exception as exc:
        sys.stderr.write("[supervisor] proxy apply skipped: {}\n".format(exc))


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid() -> Optional[int]:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    if _pid_running(pid):
        return pid
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return None


def _current_workspace() -> str:
    raw = (os.environ.get("OPENMHS_WORKSPACE") or "").strip()
    if raw:
        return str(Path(raw).expanduser())
    return str(Path.home() / "OpenMHS-Workspace")


def worker_status() -> dict:
    pid = read_pid()
    lan = _lan_ip()
    try:
        from openmhs_proxy import proxy_status

        proxy = proxy_status()
    except Exception:
        proxy = {}
    return {
        "ok": True,
        "supervisor": True,
        "worker_running": pid is not None,
        "pid": pid,
        "log": str(LOG_FILE),
        "repo": str(REPO),
        "microscope": os.environ.get("OPENMHS_MICROSCOPE_URL", ""),
        "workspace": _current_workspace(),
        "host": HOST,
        "port": PORT,
        "lan_ip": lan,
        "base_url": "http://{}:{}".format(lan, PORT),
        "https_proxy": proxy.get("https_proxy"),
        "http_proxy": proxy.get("http_proxy"),
    }


def _upsert_dotenv(key: str, value: str) -> None:
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    out = []
    found = False
    for line in lines:
        if pattern.match(line):
            out.append("{}={}".format(key, shlex.quote(value)))
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append("{}={}".format(key, shlex.quote(value)))
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)


def _workspace_path(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Use an absolute path or a path beginning with ~/.")
    root = candidate.resolve()
    if root == Path.home().resolve() or root == Path("/"):
        raise ValueError("Choose a dedicated workspace folder, not home or the disk root.")
    if root == REPO or root in REPO.parents or REPO in root.parents:
        raise ValueError("The workspace and application installation must be separate folders.")
    return root


def _install_path(raw: str) -> Path:
    if not re.fullmatch(r"[\w ./~-]+", raw):
        raise ValueError("Use a path without shell or XML control characters.")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Use an absolute path or a path beginning with ~/.")
    target = candidate.resolve()
    workspace = Path(_current_workspace()).expanduser().resolve()
    if target in (Path("/"), Path.home().resolve()):
        raise ValueError("Choose a dedicated installation folder.")
    if target == REPO or target in REPO.parents or REPO in target.parents:
        raise ValueError("The new installation must be separate from the current one.")
    if target == workspace or target in workspace.parents or workspace in target.parents:
        raise ValueError("The installation and workspace must be separate folders.")
    if target.exists() and any(target.iterdir()) and not (target / ".openmhs-installing").is_file():
        raise ValueError("Choose an empty folder to avoid overwriting existing files.")
    return target


def prepare_installation(path: str) -> dict:
    """Copy a PC runtime and switch login startup after its venv is ready."""
    if sys.platform != "darwin":
        return {"ok": False, "error": "Installation relocation is supported on macOS only."}
    try:
        if INSTALL_STATUS.exists():
            previous = json.loads(INSTALL_STATUS.read_text(encoding="utf-8"))
            if previous.get("state") in ("copying", "dependencies", "switching") and time.time() - INSTALL_STATUS.stat().st_mtime < 900:
                raise ValueError("An installation is already in progress.")
        target = _install_path((path or "").strip())
        helper = ROOT / "openmhs_relocate.py"
        if not helper.is_file():
            raise ValueError("The relocation helper is missing. Update the PC worker first.")
        target.parent.mkdir(parents=True, exist_ok=True)
        log_path = Path("/tmp/openmhs-install-{}.log".format(os.getuid()))
        INSTALL_STATUS.write_text(json.dumps({"ok": True, "state": "copying", "target": str(target), "detail": "Starting installation."}) + "\n", encoding="utf-8")
        with log_path.open("w", encoding="utf-8") as log:
            subprocess.Popen(
                [sys.executable, str(helper), "--source", str(REPO), "--target", str(target)],
                cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return {"ok": True, "preparing": True, "target": str(target), "log": str(log_path)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def set_workspace(path: str) -> dict:
    raw = (path or "").strip()
    if not raw:
        return {"ok": False, "error": "path required"}
    try:
        root = _workspace_path(raw)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "invalid path: {}".format(exc)}
    try:
        root.mkdir(parents=True, exist_ok=True)
        for name in _WORKSPACE_SUBDIRS:
            (root / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "cannot create workspace: {}".format(exc)}
    abs_path = str(root)
    _upsert_dotenv("OPENMHS_WORKSPACE", abs_path)
    os.environ["OPENMHS_WORKSPACE"] = abs_path
    # Restart worker so file tools / training pick up the new root
    was_running = read_pid() is not None
    if was_running:
        stop_worker()
        start_worker()
    return {
        "ok": True,
        "workspace": abs_path,
        "restarted": was_running,
        **worker_status(),
    }


def set_microscope_url(url: str) -> dict:
    """Persist the Pi endpoint and restart the worker without moving user data."""
    raw = (url or "").strip().rstrip("/")
    try:
        parsed = urlparse(raw)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Enter a plain http(s) microscope origin, without a path or credentials.")
        _ = parsed.port
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    _upsert_dotenv("OPENMHS_MICROSCOPE_URL", raw)
    os.environ["OPENMHS_MICROSCOPE_URL"] = raw
    was_running = read_pid() is not None
    if was_running:
        stop_worker()
        start_worker()
    return {"ok": True, "microscope": raw, "restarted": was_running, **worker_status()}


def start_worker() -> dict:
    existing = read_pid()
    if existing:
        return {"ok": True, "started": False, "already": True, **worker_status()}

    _load_dotenv()
    _apply_proxy()
    env = os.environ.copy()
    env.setdefault("OPENMHS_MICROSCOPE_URL", "http://127.0.0.1:5000")
    env.setdefault("OPENMHS_AGENT_WORKER_ID", "pc-worker")
    # Prefer OPENMHS_PYTHON when set (otherwise the LaunchAgent interpreter).
    python_bin = (
        os.environ.get("OPENMHS_PYTHON")
        or os.environ.get("OPENMHS_WORKER_PYTHON")
        or sys.executable
    )
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [python_bin, str(WORKER_SCRIPT)],
        cwd=str(REPO),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid) + "\n", encoding="utf-8")
    time.sleep(0.4)
    return {"ok": True, "started": True, "already": False, **worker_status()}


def stop_worker() -> dict:
    pid = read_pid()
    if not pid:
        return {"ok": True, "stopped": False, "already": True, **worker_status()}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(20):
        if not _pid_running(pid):
            break
        time.sleep(0.1)
    if _pid_running(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return {"ok": True, "stopped": True, **worker_status()}


def restart_worker() -> dict:
    """Stop then start the worker when the UI requests connection repair."""
    stopped = stop_worker()
    time.sleep(0.35)
    started = start_worker()
    return {
        "ok": True,
        "restarted": True,
        "stopped": stopped,
        "started": started,
        **worker_status(),
    }


class Handler(BaseHTTPRequestHandler):
    def _local_admin_request(self) -> bool:
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                return False
        except ValueError:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        configured = (os.environ.get("OPENMHS_MICROSCOPE_URL") or "").rstrip("/")
        extra = {item.strip().rstrip("/") for item in os.environ.get("OPENMHS_ADMIN_ORIGINS", "").split(",") if item.strip()}
        if origin.rstrip("/") in ({configured} | extra):
            return True
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.hostname == "localhost" or parsed.hostname.endswith(".local"):
            return True
        try:
            address = ipaddress.ip_address(parsed.hostname)
            return address.is_private or address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            return False

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Access-Control-Request-Private-Network",
        )
        # Chrome Private Network Access: page on Pi IP → Mac localhost/LAN
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(
        self,
        code: int,
        data: bytes,
        content_type: str,
        cache_control: str = "no-store, no-cache, must-revalidate, max-age=0",
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self._cors()
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return body if isinstance(body, dict) else {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path in ("/", "/status"):
            self._json(200, worker_status())
            return
        if path == "/installation":
            if not self._local_admin_request():
                self._json(403, {"ok": False, "error": "Local PC access is required."})
                return
            try:
                self._json(200, json.loads(INSTALL_STATUS.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                self._json(200, {"ok": True, "state": "idle"})
            return
        if path == "/views":
            try:
                from openmhs_views import list_views

                self._json(200, list_views())
            except Exception as extra:
                self._json(500, {"ok": False, "error": str(extra)})
            return
        if path == "/views/current":
            try:
                from openmhs_views import current_view_state

                self._json(200, current_view_state())
            except Exception as extra:
                self._json(500, {"ok": False, "error": str(extra)})
            return
        if path == "/mosaic/list":
            try:
                from openmhs_mosaic_store import list_sessions

                self._json(200, {"ok": True, "sessions": list_sessions()})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/workspace/ls":
            rel = (qs.get("path") or ["."])[0]
            try:
                from openmhs_workspace import list_dir, workspace_info

                info = workspace_info()
                listing = list_dir(rel or ".")
                listing["workspace"] = info.get("root")
                self._json(200, listing)
            except FileNotFoundError:
                self._json(404, {"ok": False, "error": "not found"})
            except PermissionError as exc:
                self._json(403, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/workspace/resolve":
            rel = (qs.get("path") or [""])[0]
            try:
                from openmhs_mosaic_store import session_id_from_relpath

                sid = session_id_from_relpath(rel)
                self._json(200, {"ok": True, "path": rel, "session_id": sid})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/workspace/info":
            try:
                from openmhs_workspace import workspace_info

                self._json(200, workspace_info())
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/ui/events":
            try:
                from openmhs_ui_events import list_ui_events

                after = int((qs.get("after") or ["0"])[0] or 0)
                limit = int((qs.get("limit") or ["100"])[0] or 100)
                self._json(200, list_ui_events(after=after, limit=limit))
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        # /mosaic/{id} ...
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "mosaic":
            sid = parts[1]
            try:
                from openmhs_mosaic_store import (
                    get_layout,
                    read_agent_snapshot,
                    read_manifest,
                    refine_status,
                    session_dir,
                    tile_path,
                )

                if len(parts) == 2:
                    self._json(200, {"ok": True, "manifest": read_manifest(sid)})
                    return
                sub = parts[2]
                if sub == "layout":
                    self._json(200, get_layout(sid))
                    return
                if sub == "agent":
                    self._json(200, read_agent_snapshot(sid))
                    return
                if sub == "refine_status":
                    self._json(200, {"ok": True, **refine_status(sid)})
                    return
                if sub == "preview.jpg" or sub == "preview":
                    # This route is intentionally overview-only.  Full
                    # refined.jpg is served through the pyramid tile route so
                    # a browser never downloads a whole Slide for a zoom.
                    from openmhs_canvas import _published_slide

                    if refine_status(sid).get("refined"):
                        payload = _published_slide(sid)
                        if payload:
                            self._bytes(200, payload, "image/jpeg", "private, max-age=3")
                            return
                    p = session_dir(sid) / "preview.jpg"
                    if p.is_file():
                        self._bytes(
                            200,
                            p.read_bytes(),
                            "image/jpeg",
                            "private, max-age=3",
                        )
                        return
                    self._json(404, {"ok": False, "error": "no preview"})
                    return
                if sub == "refined.jpg":
                    p = session_dir(sid) / "refined.jpg"
                    if not p.is_file():
                        self._json(404, {"ok": False, "error": "no refined image"})
                        return
                    self._bytes(200, p.read_bytes(), "image/jpeg", "private, max-age=86400")
                    return
                if sub == "pyramid" and len(parts) >= 4:
                    from openmhs_mosaic_store import get_pyramid_info, get_pyramid_tile

                    kind = parts[3]
                    if kind in ("info.json", "info"):
                        self._json(200, get_pyramid_info(sid))
                        return
                    if kind == "tile":
                        try:
                            level = int((qs.get("level") or [None])[0])
                            x = int((qs.get("x") or [None])[0])
                            y = int((qs.get("y") or [None])[0])
                        except (TypeError, ValueError):
                            self._json(400, {"ok": False, "error": "level, x and y required"})
                            return
                        data = get_pyramid_tile(sid, level, x, y)
                        self._bytes(
                            200,
                            data,
                            "image/jpeg",
                            "public, max-age=31536000, immutable",
                        )
                    return
                if sub == "tile":
                    try:
                        ix = int((qs.get("ix") or [None])[0])
                        iy = int((qs.get("iy") or [None])[0])
                    except (TypeError, ValueError):
                        self._json(400, {"ok": False, "error": "ix and iy required"})
                        return
                    p = tile_path(sid, ix, iy)
                    if not p.is_file():
                        self._json(404, {"ok": False, "error": "tile not found"})
                        return
                    if str((qs.get("thumb") or [""])[0]).lower() in ("1", "true", "yes"):
                        # Tile view starts with small cached previews.  The
                        # browser promotes the selected tile to the original
                        # JPEG, so this path never needs to hold a full Canvas
                        # in memory or serialize it into a JSON response.
                        from PIL import Image

                        import io

                        width = max(96, min(640, int((qs.get("width") or ["320"])[0])))
                        with Image.open(p) as source:
                            image = source.convert("RGB")
                            image.thumbnail((width, width), Image.Resampling.LANCZOS)
                            buf = io.BytesIO()
                            image.save(buf, format="JPEG", quality=82, optimize=False)
                        self._bytes(200, buf.getvalue(), "image/jpeg", "public, max-age=86400")
                    else:
                        self._bytes(200, p.read_bytes(), "image/jpeg", "public, max-age=300")
                    return
            except FileNotFoundError:
                self._json(404, {"ok": False, "error": "session not found"})
                return
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
                return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/start", "/stop", "/restart", "/workspace", "/microscope", "/installation"):
            if not self._local_admin_request():
                self._json(403, {"ok": False, "error": "Local PC access is required."})
                return
        if path == "/start":
            self._json(200, start_worker())
            return
        if path == "/stop":
            self._json(200, stop_worker())
            return
        if path == "/restart":
            self._json(200, restart_worker())
            return
        if path == "/workspace":
            body = self._read_json()
            result = set_workspace((body or {}).get("path") or "")
            self._json(200 if result.get("ok") else 400, result)
            return
        if path == "/microscope":
            body = self._read_json()
            result = set_microscope_url((body or {}).get("url") or "")
            self._json(200 if result.get("ok") else 400, result)
            return
        if path == "/installation":
            body = self._read_json()
            result = prepare_installation((body or {}).get("path") or "")
            self._json(202 if result.get("ok") else 400, result)
            return
        if path == "/views":
            body = self._read_json() or {}
            try:
                from openmhs_views import (
                    create_view,
                    ingest_into_current_view,
                    set_active_record,
                )

                if str(body.get("action") or "").strip().lower() in (
                    "active-record",
                    "active_record",
                ):
                    result = set_active_record(
                        view_id=str(body.get("view_id") or ""),
                        record_id=body.get("record_id") or body.get("session_id"),
                    )
                    self._json(200, result)
                    return

                result = create_view(
                    title=body.get("title"),
                    view_id=body.get("view_id") or body.get("id"),
                )
                try:
                    result["ingest"] = ingest_into_current_view(
                        result.get("current_view_id")
                    )
                except Exception:
                    pass
                self._json(200, result)
            except Exception as extra:
                self._json(500, {"ok": False, "error": str(extra)})
            return
        if path == "/views/current":
            body = self._read_json() or {}
            try:
                from openmhs_views import ingest_into_current_view, set_current_view

                vid = str(body.get("view_id") or body.get("id") or "").strip()
                view = set_current_view(vid)
                ingest = {}
                try:
                    ingest = ingest_into_current_view(vid)
                except Exception:
                    pass
                self._json(200, {"ok": True, "view": view, "ingest": ingest})
            except FileNotFoundError:
                self._json(404, {"ok": False, "error": "view not found"})
            except Exception as extra:
                self._json(400, {"ok": False, "error": str(extra)})
            return
        if path == "/views/ingest":
            body = self._read_json() or {}
            try:
                from openmhs_views import ingest_into_current_view

                self._json(
                    200,
                    ingest_into_current_view(view_id=body.get("view_id")),
                )
            except Exception as extra:
                self._json(500, {"ok": False, "error": str(extra)})
            return
        if path == "/views/import-legacy":
            body = self._read_json() or {}
            try:
                from openmhs_views import import_legacy_mosaics

                self._json(
                    200,
                    import_legacy_mosaics(view_id=body.get("view_id")),
                )
            except Exception as extra:
                self._json(500, {"ok": False, "error": str(extra)})
            return
        if path in ("/views/bind-session", "/views/bind_session"):
            body = self._read_json() or {}
            try:
                from openmhs_views import bind_session

                self._json(
                    200,
                    bind_session(
                        view_id=str(body.get("view_id") or ""),
                        session_id=str(body.get("session_id") or ""),
                    ),
                )
            except FileNotFoundError:
                self._json(404, {"ok": False, "error": "view not found"})
            except Exception as extra:
                self._json(400, {"ok": False, "error": str(extra)})
            return
        if path in ("/views/import-record", "/views/import_record"):
            body = self._read_json() or {}
            try:
                from openmhs_views import import_record

                self._json(
                    200,
                    import_record(
                        source_view_id=str(
                            body.get("source_view_id") or body.get("from_view_id") or ""
                        ),
                        record_id=str(body.get("record_id") or body.get("id") or ""),
                        view_id=body.get("view_id") or body.get("dest_view_id"),
                        copy=bool(body.get("copy", True)),
                    ),
                )
            except FileNotFoundError as extra:
                self._json(404, {"ok": False, "error": str(extra)})
            except Exception as extra:
                self._json(400, {"ok": False, "error": str(extra)})
            return
        if path == "/views/records":
            body = self._read_json() or {}
            try:
                from openmhs_views import create_record

                self._json(
                    200,
                    create_record(
                        view_id=body.get("view_id"),
                        kind=str(body.get("kind") or "mosaic"),
                        record_id=body.get("record_id") or body.get("session_id"),
                        title=body.get("title"),
                    ),
                )
            except Exception as extra:
                self._json(400, {"ok": False, "error": str(extra)})
            return
        if path == "/views/analysis":
            body = self._read_json() or {}
            try:
                from openmhs_views import save_analysis

                self._json(
                    200,
                    save_analysis(
                        view_id=body.get("view_id"),
                        record_id=body.get("record_id") or body.get("session_id"),
                        markdown=str(body.get("markdown") or ""),
                        annotations=body.get("annotations") if isinstance(body.get("annotations"), list) else [],
                        chatMessages=body.get("chatMessages") if isinstance(body.get("chatMessages"), list) else [],
                        rois=body.get("rois") if isinstance(body.get("rois"), list) else [],
                        summary=str(body.get("summary") or ""),
                    ),
                )
            except Exception as extra:
                self._json(400, {"ok": False, "error": str(extra)})
            return
        if path == "/mosaic/ingest":
            body = self._read_json()
            try:
                from openmhs_mosaic_store import ingest_pull

                result = ingest_pull(body.get("session_id"))
                self._json(200, result)
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "mosaic" and parts[2] == "canvas":
            try:
                import openmhs_canvas
                self._json(200, openmhs_canvas.request(parts[1], self._read_json() or {}))
            except Exception as exc:
                self._json(400, {"error": str(exc)})
            return
        if len(parts) == 3 and parts[0] == "mosaic" and parts[2] in ("recapture-plan", "recapture", "recapture-status"):
            body = self._read_json() or {}
            try:
                import openmhs_recapture as retake
                if parts[2] == "recapture-plan":
                    result = retake.plan(parts[1], body.get("roi") or {})
                elif parts[2] == "recapture":
                    result = retake.start(parts[1], body.get("plan_id"), body.get("confirm_same_sample"))
                else:
                    result = retake.status(parts[1], body.get("job_id"))
                self._json(200, result)
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if len(parts) == 3 and parts[0] == "mosaic" and parts[2] == "refine":
            body = self._read_json()
            try:
                from openmhs_mosaic_store import start_refine

                align = (body or {}).get("align") or "fine"
                self._json(200, start_refine(parts[1], align=align))
            except FileNotFoundError:
                self._json(404, {"ok": False, "error": "session not found"})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if len(parts) == 3 and parts[0] == "mosaic" and parts[2] == "agent":
            body = self._read_json()
            try:
                from openmhs_mosaic_store import write_agent_snapshot

                self._json(200, write_agent_snapshot(parts[1], body or {}))
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] == "mosaic":
            try:
                from openmhs_mosaic_store import delete_session

                self._json(200, delete_session(parts[1]))
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[supervisor] " + (fmt % args) + "\n")


def main() -> None:
    _load_dotenv()
    _apply_proxy()
    if os.environ.get("OPENMHS_WORKER_AUTOSTART", "1").strip() not in (
        "0",
        "false",
        "False",
        "no",
    ):
        try:
            start_worker()
            print("worker autostart:", worker_status())
        except Exception as exc:
            print("worker autostart failed:", exc)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("MicroWorld supervisor on http://{}:{}".format(HOST, PORT))
    print("  GET  /status /installation   POST /start /stop /workspace /microscope /installation /mosaic/ingest")
    print("  GET  /workspace/ls /mosaic/list /mosaic/{id}/tile|preview|layout|agent")
    print("  POST /mosaic/{id}/refine /mosaic/{id}/agent")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
