"""Sandboxed local workspace for MicroWorld Mac agent (Cursor-like folder).

All agent file tools resolve paths under OPENMHS_WORKSPACE only.
Default: ~/OpenMHS-Workspace with views/, lib/, notes/, captures/, runs/.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("openmhs.workspace")

_DEFAULT_SUBDIRS = ("views", "lib", "notes", "captures", "runs")

# The camera/canvas browser owns ``views``.  These implementation and legacy
# directories stay available to the worker, but do not belong in the compact
# Files tree shown to a microscope user.
_HIDDEN_ROOT_ENTRIES = {
    ".trash",
    ".ui",
    "captures",
    "codex-workbench",
    "datasets",
    "models",
    "train",
    "views",
}


def workspace_root() -> Path:
    raw = (os.environ.get("OPENMHS_WORKSPACE") or "").strip()
    if raw:
        root = Path(raw).expanduser()
    else:
        root = Path.home() / "OpenMHS-Workspace"
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in _DEFAULT_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    try:
        from openmhs_views import seed_lib

        seed_lib(root / "lib")
    except Exception:
        LOG.debug("lib seed skipped", exc_info=True)
    return root


def workspace_info() -> Dict[str, Any]:
    root = workspace_root()
    return {
        "ok": True,
        "root": str(root),
        "subdirs": list(_DEFAULT_SUBDIRS),
        "exists": root.is_dir(),
    }


def _safe_resolve(rel_or_abs: str, *, must_exist: bool = False) -> Path:
    """Resolve a path that must stay inside the workspace root."""
    root = workspace_root()
    raw = (rel_or_abs or "").strip() or "."
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = root / p
    try:
        resolved = p.resolve()
    except OSError as exc:
        raise ValueError("invalid path: {}".format(exc)) from exc
    root_s = str(root)
    res_s = str(resolved)
    if res_s != root_s and not res_s.startswith(root_s + os.sep):
        raise PermissionError(
            "path escapes workspace ({} not under {})".format(res_s, root_s)
        )
    if must_exist and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def list_dir(path: str = ".", *, max_entries: int = 200) -> Dict[str, Any]:
    target = _safe_resolve(path, must_exist=True)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    entries: List[Dict[str, Any]] = []
    children = list(target.iterdir())
    if target == workspace_root():
        children = [c for c in children if c.name not in _HIDDEN_ROOT_ENTRIES]
    for child in sorted(children, key=lambda c: (not c.is_dir(), c.name.lower())):
        if len(entries) >= max_entries:
            break
        try:
            st = child.stat()
            entries.append(
                {
                    "name": child.name,
                    "path": str(child.relative_to(workspace_root())),
                    "type": "dir" if child.is_dir() else "file",
                    "size": st.st_size if child.is_file() else None,
                    "mtime": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)
                    ),
                }
            )
        except OSError:
            continue
    return {
        "ok": True,
        "root": str(workspace_root()),
        "path": (
            str(target.relative_to(workspace_root()))
            if target != workspace_root()
            else "."
        ),
        "entries": entries,
        "truncated": len(children) > len(entries),
    }


def read_file(
    path: str,
    *,
    max_bytes: int = 200_000,
    encoding: str = "utf-8",
) -> Dict[str, Any]:
    target = _safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(str(target))
    data = target.read_bytes()
    truncated = len(data) > max_bytes
    chunk = data[:max_bytes]
    text: Optional[str] = None
    binary = False
    try:
        text = chunk.decode(encoding)
    except UnicodeDecodeError:
        binary = True
        import base64

        text = None
        b64 = base64.b64encode(chunk).decode("ascii")
        return {
            "ok": True,
            "path": str(target.relative_to(workspace_root())),
            "binary": True,
            "size": len(data),
            "truncated": truncated,
            "base64": b64 if len(b64) < 120_000 else b64[:120_000],
            "note": "binary file; base64 may be truncated",
        }
    return {
        "ok": True,
        "path": str(target.relative_to(workspace_root())),
        "binary": binary,
        "size": len(data),
        "truncated": truncated,
        "content": text,
    }


def write_file(
    path: str,
    content: str,
    *,
    mkdir: bool = True,
    append: bool = False,
) -> Dict[str, Any]:
    target = _safe_resolve(path, must_exist=False)
    if mkdir:
        target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8") as f:
        f.write(content if content is not None else "")
    return {
        "ok": True,
        "path": str(target.relative_to(workspace_root())),
        "abs": str(target),
        "size": target.stat().st_size,
        "append": append,
    }


def mkdir(path: str) -> Dict[str, Any]:
    target = _safe_resolve(path, must_exist=False)
    target.mkdir(parents=True, exist_ok=True)
    return {
        "ok": True,
        "path": str(target.relative_to(workspace_root())),
        "abs": str(target),
    }


def delete_path(path: str) -> Dict[str, Any]:
    """Delete a file or directory inside the workspace (no escape)."""
    import shutil

    target = _safe_resolve(path, must_exist=True)
    root = workspace_root()
    if target == root:
        raise PermissionError("cannot delete workspace root")
    rel = str(target.relative_to(root))
    if target.is_dir():
        shutil.rmtree(target)
        kind = "dir"
    else:
        target.unlink()
        kind = "file"
    return {"ok": True, "path": rel, "deleted": kind}


def run_python(
    path: str,
    extra_args: Optional[List[str]] = None,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Run a .py file inside the workspace with cwd=workspace root."""
    import subprocess
    import sys

    target = _safe_resolve(path, must_exist=True)
    if target.suffix.lower() != ".py" or not target.is_file():
        raise ValueError("run_python expects a .py file in the workspace")
    timeout = max(5.0, min(600.0, float(timeout or 120)))
    argv = [sys.executable, str(target)]
    for a in extra_args or []:
        argv.append(str(a))
    env = os.environ.copy()
    env["OPENMHS_WORKSPACE"] = str(workspace_root())
    proc = subprocess.run(
        argv,
        cwd=str(workspace_root()),
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "path": str(target.relative_to(workspace_root())),
        "returncode": proc.returncode,
        "stdout": stdout[:12000],
        "stderr": stderr[:8000],
        "truncated": len(stdout) > 12000 or len(stderr) > 8000,
    }


def run_shell(
    argv: List[str],
    cwd: str = ".",
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Run argv (no shell) with cwd under the workspace."""
    import subprocess

    if not argv or not isinstance(argv, list):
        raise ValueError("argv must be a non-empty list")
    work = _safe_resolve(cwd or ".", must_exist=True)
    if not work.is_dir():
        raise NotADirectoryError(str(work))
    timeout = max(5.0, min(600.0, float(timeout or 60)))
    cmd = [str(x) for x in argv]
    env = os.environ.copy()
    env["OPENMHS_WORKSPACE"] = str(workspace_root())
    proc = subprocess.run(
        cmd,
        cwd=str(work),
        capture_output=True,
        timeout=timeout,
        env=env,
        shell=False,
    )
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "argv": cmd,
        "cwd": str(work.relative_to(workspace_root())),
        "returncode": proc.returncode,
        "stdout": stdout[:12000],
        "stderr": stderr[:8000],
    }


def resolve_under_workspace(path: str) -> str:
    """Public helper for training tools: relative → absolute under workspace."""
    return str(_safe_resolve(path, must_exist=False))


def default_models_dir() -> Path:
    return workspace_root() / "models"


def default_train_dir() -> Path:
    return workspace_root() / "train"


def default_datasets_dir() -> Path:
    return workspace_root() / "datasets"
