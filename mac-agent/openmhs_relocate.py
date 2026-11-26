#!/usr/bin/env python3
"""Prepare a separate PC installation, then switch macOS login startup."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.request import urlopen


STATUS = Path("/tmp/openmhs-install-{}.json".format(os.getuid()))


def report(state: str, target: Path, detail: str = "") -> None:
    payload = {"ok": state != "failed", "state": state, "target": str(target), "detail": detail}
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    temporary.replace(STATUS)


def set_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(key + "=")]
    lines.append("{}={}".format(key, shlex.quote(value)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def wait_for_runtime(target: Path, port: int) -> None:
    """Accept the switch only when the new supervisor and worker are online."""
    endpoint = "http://127.0.0.1:{}/status".format(port)
    for _ in range(30):
        try:
            with urlopen(endpoint, timeout=2) as response:
                status = json.load(response)
            if Path(status.get("repo") or "").resolve() == target and status.get("worker_running"):
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("The new PC supervisor or worker did not become ready.")


def relocate(source: Path, target: Path) -> None:
    runtime_source = source / "pc-agent"
    if not runtime_source.is_dir():
        runtime_source = source / "mac-agent"
    if not (runtime_source / "openmhs_supervisor.py").is_file():
        raise RuntimeError("The current PC runtime could not be found.")
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("The new installation must be separate from the current one.")

    report("copying", target, "Copying the PC runtime; the old installation remains intact.")
    target.mkdir(parents=True, exist_ok=True)
    marker = target / ".openmhs-installing"
    marker.touch(exist_ok=True)
    runtime_target = target / runtime_source.name
    runtime_target.mkdir(exist_ok=True)
    for entry in runtime_source.iterdir():
        if entry.suffix in (".py", ".command") or entry.name in ("requirements.txt", ".env"):
            shutil.copy2(entry, runtime_target / entry.name)
    mcp_source = source / "mcp"
    if mcp_source.is_dir():
        mcp_target = target / "mcp"
        mcp_target.mkdir(exist_ok=True)
        for entry in mcp_source.iterdir():
            if entry.is_file() and entry.suffix in (".py", ".json"):
                shutil.copy2(entry, mcp_target / entry.name)

    report("dependencies", target, "Creating a private Python environment and installing dependencies.")
    python = target / ".venv" / "bin" / "python3"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(target / ".venv")], check=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "-r", str(runtime_target / "requirements.txt")],
        check=True,
    )
    subprocess.run([str(python), "-m", "py_compile", str(runtime_target / "openmhs_supervisor.py")], check=True)
    set_env(runtime_target / ".env", "OPENMHS_PYTHON", str(python))

    report("switching", target, "Switching macOS login startup to the new installation.")
    env = os.environ.copy()
    env["OPENMHS_PYTHON"] = str(python)
    try:
        subprocess.run(["bash", str(runtime_target / "install-login-autostart.command")], env=env, check=True)
        wait_for_runtime(target, int(os.environ.get("OPENMHS_SUPERVISOR_PORT", "18765")))
    except Exception as exc:
        report("switching", target, "New startup failed; restoring the previous installation.")
        try:
            subprocess.run(["bash", str(runtime_source / "install-login-autostart.command")], check=True)
        except Exception as rollback_error:
            raise RuntimeError("{} Automatic rollback also failed: {}".format(exc, rollback_error)) from exc
        raise RuntimeError("{} Previous login startup was restored.".format(exc)) from exc
    marker.unlink(missing_ok=True)
    report("complete", target, "PC worker now starts from the new installation. The previous installation was retained.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    try:
        relocate(args.source.resolve(), args.target.resolve())
    except Exception as exc:
        report("failed", args.target, str(exc))
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
