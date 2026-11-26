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
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from typing import Any, Dict, Optional

from werkzeug.exceptions import Conflict


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


class HardwareGate:
    """A re-entrant, process-wide lease around hardware scheduling."""

    def __init__(self, state_path: str):
        self.state_path = state_path
        self._mutex = threading.RLock()
        self._local = threading.local()
        self._active: Optional[Dict[str, Any]] = None
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

    def status(self) -> Dict[str, Any]:
        with self._mutex:
            state = dict(self._active or {})
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
                request.headers.get("X-OpenMHS-Client")
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
            if self._active:
                raise HardwareBusy(self._active)
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
