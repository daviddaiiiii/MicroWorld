#!/usr/bin/env python3
"""MicroWorld PC worker: synchronize microscope scans and Canvas data.

Usage:
  # connection settings in mac-agent/.env (see .env.example)
  ./mac-agent/start_worker.command
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and not key.startswith("OPENMHS_LLM_") and key != "OPENAI_API_KEY":
            os.environ[key] = val


_load_dotenv()

try:
    from openmhs_proxy import apply_macos_system_proxy  # noqa: E402

    apply_macos_system_proxy()
except Exception:
    pass

LOG = logging.getLogger("openmhs.pc_worker")
MICROSCOPE_URL = os.environ.get("OPENMHS_MICROSCOPE_URL", "http://127.0.0.1:5000").rstrip("/")
EXT = "org.openmhs.visual-prompts"
WORKER_ID = os.environ.get("OPENMHS_AGENT_WORKER_ID", "pc-worker")
POLL_S = float(os.environ.get("OPENMHS_WORKER_POLL_S", "1.5"))

_last_heartbeat_at = 0.0
_last_ingest_at = 0.0


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    """Prefer IPv4 — macOS VPN/utun often breaks IPv6 → Errno 65 No route to host."""

    def http_open(self, req):  # noqa: N802
        return self.do_open(_IPv4HTTPConnection, req)


def _lan_bind_ip() -> Optional[str]:
    """Source IP on LAN toward the microscope (avoids VPN steal / aids routing)."""
    env = (os.environ.get("OPENMHS_BIND_IP") or "").strip()
    if env:
        return env
    try:
        from urllib.parse import urlparse

        host = urlparse(MICROSCOPE_URL).hostname or "127.0.0.1"
    except Exception:
        host = "127.0.0.1"
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 5000))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


class _IPv4HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        import socket

        err = None
        bind_ip = _lan_bind_ip()
        for res in socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM):
            af, socktype, proto, _canon, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                if bind_ip:
                    try:
                        sock.bind((bind_ip, 0))
                    except OSError:
                        pass
                sock.connect(sa)
                self.sock = sock
                return
            except OSError as exc:
                err = exc
                if sock is not None:
                    sock.close()
        if err is not None:
            raise err
        raise OSError("getaddrinfo returned empty IPv4 list for %s" % self.host)


_HTTP_OPENER = urllib.request.build_opener(_IPv4HTTPHandler)


def api(path: str) -> str:
    return "{}/api/v2/extensions/{}{}".format(MICROSCOPE_URL, EXT, path)


def _curl_json(url: str, method: str, body: Optional[bytes], timeout: float) -> Any:
    """Fallback when LaunchAgent Python is blocked by macOS Local Network TCC."""
    import subprocess
    import tempfile

    cmd = [
        "/usr/bin/curl",
        "-sS",
        "-m",
        str(int(max(3, timeout))),
        "-X",
        method,
        "-H",
        "Accept: application/json",
    ]
    bind_ip = _lan_bind_ip()
    if bind_ip:
        cmd.extend(["--interface", bind_ip])
    tmp = None
    try:
        if body is not None:
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp.write(body)
            tmp.close()
            cmd.extend(["-H", "Content-Type: application/json", "--data-binary", "@" + tmp.name])
        cmd.append(url)
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[:300]
            raise OSError("curl failed (%s): %s" % (proc.returncode, err or "no output"))
        raw = proc.stdout or b""
        return json.loads(raw.decode("utf-8")) if raw else {}
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def http_json(path: str, method: str = "GET", data: Optional[Dict[str, Any]] = None) -> Any:
    url = path if path.startswith("http") else api(path)
    body = None if data is None else json.dumps(data).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _HTTP_OPENER.open(req, timeout=120) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        # Errno 65 often = Local Network privacy denial for LaunchAgent Python
        msg = str(exc)
        if "Errno 65" in msg or "No route to host" in msg or "Network is unreachable" in msg:
            LOG.warning("urllib blocked (%s); retry via /usr/bin/curl", msg[:120])
            return _curl_json(url, method, body, 120)
        raise


def heartbeat_loop() -> None:
    """Keep the Pi informed that the PC scan/Canvas worker is available."""
    while True:
        try:
            report_heartbeat(force=True)
        except Exception:
            LOG.exception("background heartbeat error")
        time.sleep(5.0)


def _supervisor_url() -> str:
    port = int(os.environ.get("OPENMHS_SUPERVISOR_PORT", "18765"))
    explicit = (os.environ.get("OPENMHS_SUPERVISOR_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        import socket
        from urllib.parse import urlparse

        host = urlparse(MICROSCOPE_URL).hostname or "127.0.0.1"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 5000))
            lan = s.getsockname()[0]
        finally:
            s.close()
        return "http://{}:{}".format(lan, port)
    except Exception:
        return "http://127.0.0.1:{}".format(port)


_archived_sessions = set()
_finalize_busy = set()
_sync_lock = threading.Lock()


def _finalize_after_sync(sid: str) -> None:
    """Refine on Mac, then drop Pi copies; Continue can hydrate from PC."""
    try:
        from openmhs_mosaic_store import (
            pull_slide_image,
            read_manifest,
            refine_status,
            start_refine,
            sync_pull_incremental,
        )

        if not sync_pull_incremental(sid).get("sync", {}).get("complete"):
            raise RuntimeError("Waiting for all retained tile revisions to sync")
        # Pi publishes only a stage-coordinate preview. The complete
        # registration/fusion belongs on the Mac, where memory and OpenCV are
        # available. Keep Pi source tiles until this succeeds.
        start_refine(sid, align="full")
        deadline = time.time() + 900.0
        while time.time() < deadline:
            status = refine_status(sid)
            phase = status.get("phase")
            if phase == "done" and status.get("refined"):
                break
            if phase == "error":
                raise RuntimeError(
                    "Mac refine failed: {}".format(status.get("error") or "unknown error")
                )
            time.sleep(2.0)
        else:
            raise TimeoutError("Mac refine timed out")

        man = read_manifest(sid)
        pull_slide_image(sid, man.get("mosaic_id") or man.get("slide_id"))

        if os.environ.get("OPENMHS_PRESERVE_NON_STREAMED_CAPTURES") == "1" and not man.get("mac_preview_only"):
            with _sync_lock:
                _archived_sessions.add(sid)
            LOG.info("Pi source dataset retained for %s", sid)
            return

        try:
            http_json(
                "{}/api/v2/extensions/org.openmhs.scan-mosaic/cleanup_tiles".format(
                    MICROSCOPE_URL
                ),
                method="POST",
                data={
                    "save_slide": True,
                    "tiles_only": True,
                    "keep_preview": True,
                    "dataset_id": man.get("dataset_id"),
                },
            )
            LOG.info("Pi tiles cleaned after PC sync for %s", sid)
        except Exception as exc:
            LOG.warning("Pi tiles-only cleanup failed: %s", exc)
        with _sync_lock:
            _archived_sessions.add(sid)
    except Exception:
        LOG.exception("finalize after sync failed for %s", sid)
    finally:
        with _sync_lock:
            _finalize_busy.discard(sid)


def maybe_ingest_workspace(skip_session_id: Optional[str] = None) -> None:
    """When a Camera view exists, fold leftover captures/mosaics into it."""
    global _last_ingest_at
    now = time.time()
    if now - _last_ingest_at < 20.0:
        return
    _last_ingest_at = now
    try:
        from openmhs_views import ingest_into_current_view

        skip = [skip_session_id] if skip_session_id else None
        result = ingest_into_current_view(skip_ids=skip)
        moved = result.get("moved") or []
        if moved:
            LOG.info("ingested legacy mosaics into view: %s", moved)
    except Exception:
        LOG.debug("workspace ingest skipped", exc_info=True)


def maybe_sync_scan() -> None:
    """During/after scan: continuously sync tiles Pi→Mac; finalize when done."""
    try:
        from openmhs_mosaic_store import sync_pull_incremental
    except Exception:
        return
    try:
        details = http_json(
            "{}/api/v2/extensions/org.openmhs.scan-mosaic/live_details".format(
                MICROSCOPE_URL
            )
        )
    except Exception:
        return
    try:
        vid = str(details.get("view_id") or "").strip()
        if vid:
            from openmhs_views import current_view_id, set_current_view

            if not current_view_id():
                try:
                    set_current_view(vid)
                except Exception:
                    pass
    except Exception:
        pass
    sid = details.get("session_id")
    phase = details.get("scan_phase") or ""
    cells = int(details.get("cell_count") or 0)
    maybe_ingest_workspace(skip_session_id=sid if phase not in ("", "idle", "done") else None)
    if not sid or cells < 1:
        return
    if phase not in (
        "calibrating",
        "scanning",
        "stitching",
        "refining",
        "done",
    ):
        return
    with _sync_lock:
        # Continue reuses the same session id. A newly active scan must reopen
        # an already archived session so its additional tiles are pulled live.
        if sid in _archived_sessions:
            if phase in ("calibrating", "scanning", "stitching", "refining"):
                _archived_sessions.discard(sid)
                LOG.info("Continue reopened archived session %s", sid)
            else:
                return
        if sid in _finalize_busy:
            return

    try:
        result = sync_pull_incremental(sid)
        sync = result.get("sync") or {}
        LOG.info(
            "Mac sync %s phase=%s mac=%s/%s new=%s",
            sid,
            phase,
            sync.get("mac_cells"),
            sync.get("pi_cells"),
            sync.get("new"),
        )
    except Exception:
        LOG.exception("incremental sync failed for %s", sid)
        return

    if phase == "done":
        if sync.get("complete"):
            with _sync_lock:
                if sid in _finalize_busy or sid in _archived_sessions:
                    return
                _finalize_busy.add(sid)
            threading.Thread(
                target=_finalize_after_sync,
                args=(sid,),
                name="mosaic-finalize-" + sid,
                daemon=True,
            ).start()


# Back-compat alias
def maybe_archive_scan() -> None:
    maybe_sync_scan()


def report_heartbeat(force: bool = False) -> None:
    global _last_heartbeat_at
    now = time.time()
    if not force and now - _last_heartbeat_at < 5.0:
        return
    _last_heartbeat_at = now
    try:
        from openmhs_workspace import workspace_root

        payload = {
            "worker_id": WORKER_ID,
            "workspace": str(workspace_root()),
            "supervisor_url": _supervisor_url(),
        }
        last_error = None
        for attempt in range(3):
            try:
                http_json("/agent/heartbeat", method="POST", data=payload)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        if last_error is not None:
            raise last_error
    except Exception:
        LOG.warning("PC worker heartbeat unavailable; will retry", exc_info=True)


def loop() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOG.info("MicroWorld PC scan/Canvas worker started → %s (poll=%.1fs)", MICROSCOPE_URL, POLL_S)
    threading.Thread(target=heartbeat_loop, name="openmhs-hb", daemon=True).start()
    report_heartbeat(force=True)
    while True:
        try:
            maybe_archive_scan()
        except Exception:
            LOG.exception("mosaic archive tick failed")
        time.sleep(POLL_S)


if __name__ == "__main__":
    loop()
