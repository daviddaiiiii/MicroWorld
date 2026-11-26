"""Launch an installed Codex CLI with MicroWorld-only storage."""

import json
import os
from pathlib import Path
import shutil
import sys

from network import read_json

root = Path(sys.argv[1]).resolve()
current = json.loads((root / "State/connection.json").read_text(encoding="utf-8"))
status = read_json("http://127.0.0.1:18766/desktop/status")
if status.get("device_id") != current["id"]:
    sys.exit("Connect to the microscope in MicroWorld first.")
bundled_cli = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
cli = shutil.which("codex") or (str(bundled_cli) if bundled_cli.is_file() else None)
if not cli:
    sys.exit("Install the Codex CLI to use chat. MicroWorld and MCP can run independently.")
env = os.environ.copy()
env["CODEX_HOME"] = str(Path(current["history"]) / "Codex")
os.chdir(current["workspace"])
os.execve(cli, [cli, "-c", 'cli_auth_credentials_store="keyring"'], env)
