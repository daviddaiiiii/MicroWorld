"""Process-wide hardware arbitration for MicroWorld.

OpenFlexure exposes several independent action entry points.  Its normal
locks protect individual components, but they do not express the stronger
invariant MicroWorld needs: a stage move, autofocus, calibration, retake, or
scan must own the microscope for the complete operation.

The gate is deliberately small and in-process.  The JSON state is only a
cross-device status/diagnostic surface; the mutex and thread-local lease are
the authority that prevents two requests in this server process from running
at the same time.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from ipaddress import ip_address
from typing import Any, Dict, Optional

from flask import g, has_request_context, request
from werkzeug.exceptions import Conflict

CONNECTION_HEADER = "X-MicroWorld-Connection"
CONNECTION_GRACE = 30


class HardwareBusy(Conflict):
    """HTTP 409 raised when another request owns the microscope."""

    code = 409

    def __init__(self, state: Optional[Dict[str, Any]] = None):
        self.hardware_state = dict(state or {})
        operation = self.hardware_state.get("operation") or "another operation"
        phase = self.hardware_state.get("phase") or "busy"
        super().__init__(
            description="Microscope is busy with {} ({})".format(operation, phase)
        )


class ConnectionLost(Conflict):
    description = "This computer no longer controls the microscope. Connect again to regain control."


class HardwareGate:
    """A re-entrant, process-wide lease around hardware scheduling."""

    def __init__(self, state_path: str):
        self.state_path = state_path
        self._mutex = threading.RLock()
        self._local = threading.local()
        self._active: Optional[Dict[str, Any]] = None
        self._connection = None
        self._managed = False
        self._writes = 0
        self._processing = None
        self._pid = os.getpid()
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        self._clear_stale_state()

    def _clear_stale_state(self) -> None:
        """Do not advertise a lease left by a previous server process."""
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            if state.get("busy"):
                self._write_state({"busy": False, "updated_at": time.time()})
        except (FileNotFoundError, ValueError, OSError):
            pass

    def _write_state(self, state: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.state_path)
        fd, tmp = tempfile.mkstemp(prefix="hardware.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def install_connection_guard(self, app, api_prefix):
        """Install once before the first managed connection can be granted."""
        with self._mutex:
            if app.extensions.get("microworld_connection_guard") is self:
                return
            prefix = api_prefix.rstrip("/") + "/"

            def authorize():
                if not request.path.startswith(prefix) or request.path.rstrip("/").endswith((
                    "/org.microworld.scan-mosaic/connection", "/org.openmhs.scan-mosaic/connection"
                )):
                    return
                write = request.method not in {"GET", "HEAD", "OPTIONS"}
                token = request.headers.get(CONNECTION_HEADER)
                with self._mutex:
                    discovery = not write and request.path.rstrip("/") == prefix.rstrip("/")
                    local_read = not write and request.remote_addr and ip_address(request.remote_addr).is_loopback
                    if token or (self._managed and not (discovery or local_read)):
                        self._check_connection(token)
                    if write:
                        self._writes += 1
                        g.microworld_write = True

            def finish_request(_error):
                if g.pop("microworld_write", False):
                    with self._mutex:
                        self._writes -= 1

            # Extensions can be loaded outside an application context, so bind
            # at the first extension request, before accepting any claim.
            app.before_request_funcs.setdefault(None, []).insert(0, authorize)
            app.teardown_request_funcs.setdefault(None, []).append(finish_request)
            app.extensions["microworld_connection_guard"] = self

    def _check_connection(self, token):
        connection = self._connection
        if not token or not token.isascii() or connection is None or not secrets.compare_digest(token, connection["token"]):
            raise ConnectionLost()
        return connection

    def _connected_recently(self):
        return self._connection is not None and time.monotonic() - self._connection["seen"] < CONNECTION_GRACE

    def connection_status(self):
        with self._mutex:
            current = self._connection or {}
            live = self._connected_recently()
            processing = self._processing if live else None
            return {
                "managed": self._managed,
                "connected": live,
                "computer": current.get("computer") if live else None,
                "supervisor_url": current.get("supervisor_url") if live else None,
                "connection_id": current.get("id") if live else None,
                "processing": processing,
            }

    def connection_command(self, operation, token, *, computer="", host="", takeover=False, session_id=None, actions_busy=None):
        with self._mutex:
            if operation == "renew":
                self._check_connection(token)["seen"] = time.monotonic()
                return {"connected": True}
            if operation == "finish":
                self._check_connection(token)
                if self._processing == session_id:
                    self._processing = None
                return {"connected": True}
            if operation == "release":
                self._check_connection(token)
            elif operation != "claim":
                raise ValueError("Unknown connection action")

            busy = self._active or (self._processing and self._connected_recently() and {
                "operation": "scan processing", "phase": "processing", "session_id": self._processing,
            })
            if busy or self._writes or (actions_busy and actions_busy()):
                raise HardwareBusy(busy)
            if operation == "release":
                self._connection = None
                return {"connected": False}
            if self._connected_recently() and not takeover:
                raise Conflict("Another computer is connected. Select Connect to take over when the microscope is idle.")

            self._managed = True
            self._processing = None
            self._connection = {
                "id": uuid.uuid4().hex,
                "token": secrets.token_urlsafe(32),
                "computer": computer[:100],
                "supervisor_url": "http://{}:18766".format(host),
                "seen": time.monotonic(),
            }
            return {"connected": True, "token": self._connection["token"]}

    def mark_processing(self, session_id):
        if not has_request_context() or not request.headers.get(CONNECTION_HEADER) or not session_id:
            return
        with self._mutex:
            self._check_connection(request.headers[CONNECTION_HEADER])
            self._processing = str(session_id)

    def status(self) -> Dict[str, Any]:
        with self._mutex:
            state = dict(self._active or {})
            if not state and self._processing and self._connected_recently():
                state = {"busy": True, "operation": "scan processing",
                         "phase": "processing", "session_id": self._processing}
            if not state:
                state = {"busy": False}
            state.setdefault("busy", bool(self._active))
            return state

    def owns_current_thread(self) -> bool:
        lease = getattr(self._local, "lease", None)
        return bool(lease and lease.get("gate") is self)

    def update(
        self,
        *,
        phase: Optional[str] = None,
        session_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._mutex:
            if not self._active:
                return self.status()
            if phase:
                self._active["phase"] = str(phase)
            if session_id:
                self._active["session_id"] = str(session_id)
            if message is not None:
                self._active["message"] = str(message)
            self._active["updated_at"] = time.time()
            self._write_state(self._active)
            return dict(self._active)

    @staticmethod
    def _request_owner(default: str) -> str:
        try:
            from flask import request

            value = (
                request.headers.get("X-MicroWorld-Client")
                or request.headers.get("X-OpenMHS-Client")
                or request.headers.get("X-OpenMHS-Agent")
                or request.remote_addr
            )
            return str(value or default)[:120]
        except RuntimeError:
            return default

    @contextmanager
    def hold(
        self,
        operation: str,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        phase: Optional[str] = None,
    ):
        """Acquire the microscope lease or raise a clean HTTP 409.

        Nested calls made by the same request (for example scan -> autofocus
        -> stage.move_abs) reuse the existing lease instead of deadlocking.
        """
        current = getattr(self._local, "lease", None)
        if current and current.get("gate") is self:
            current["depth"] = int(current.get("depth", 1)) + 1
            try:
                yield dict(self._active or {})
            finally:
                current["depth"] -= 1
            return

        with self._mutex:
            if has_request_context() and self._managed:
                self._check_connection(request.headers.get(CONNECTION_HEADER))
            if self._active:
                raise HardwareBusy(self._active)
            if self._processing and self._connected_recently():
                raise HardwareBusy(self.status())
            now = time.time()
            state = {
                "busy": True,
                "operation": str(operation),
                "phase": str(phase or operation),
                "owner": str(owner or self._request_owner("openmhs"))[:120],
                "session_id": str(session_id) if session_id else None,
                "request_id": uuid.uuid4().hex,
                "pid": self._pid,
                "started_at": now,
                "updated_at": now,
            }
            self._active = state
            self._write_state(state)
            self._local.lease = {"gate": self, "depth": 1}

        try:
            yield dict(state)
        finally:
            with self._mutex:
                lease = getattr(self._local, "lease", None)
                if lease and lease.get("gate") is self:
                    self._local.lease = None
                self._active = None
                self._write_state({"busy": False, "updated_at": time.time()})

    def _wrap_stage_method(self, stage: Any, name: str) -> bool:
        original = getattr(stage, name, None)
        if not callable(original) or getattr(original, "_openmhs_hardware_gate", False):
            return False

        @wraps(original)
        def guarded(*args, **kwargs):
            if self.owns_current_thread():
                return original(*args, **kwargs)
            with self.hold("move", phase="moving"):
                return original(*args, **kwargs)

        guarded._openmhs_hardware_gate = True
        guarded._openmhs_original = original
        setattr(stage, name, guarded)
        return True

    def install_stage_hooks(self, microscope: Any) -> bool:
        stage = getattr(microscope, "stage", None)
        if stage is None:
            return False
        changed = False
        for name in ("move_abs", "move_rel", "zero_position"):
            changed = self._wrap_stage_method(stage, name) or changed
        return changed or any(
            getattr(getattr(stage, name, None), "_openmhs_hardware_gate", False)
            for name in ("move_abs", "move_rel", "zero_position")
        )

    def _wrap_autofocus_method(self, extension: Any, name: str) -> bool:
        original = getattr(extension, name, None)
        if not callable(original) or getattr(original, "_openmhs_hardware_gate", False):
            return False

        @wraps(original)
        def guarded(*args, **kwargs):
            if self.owns_current_thread():
                return original(*args, **kwargs)
            with self.hold("autofocus", phase="autofocus"):
                return original(*args, **kwargs)

        guarded._openmhs_hardware_gate = True
        guarded._openmhs_original = original
        setattr(extension, name, guarded)
        return True

    def install_autofocus_hooks(self, extension: Any) -> bool:
        if extension is None:
            return False
        changed = False
        for name in ("fast_autofocus", "autofocus"):
            changed = self._wrap_autofocus_method(extension, name) or changed
        return changed or any(
            getattr(extension, name, None) is not None
            and getattr(getattr(extension, name), "_openmhs_hardware_gate", False)
            for name in ("fast_autofocus", "autofocus")
        )
