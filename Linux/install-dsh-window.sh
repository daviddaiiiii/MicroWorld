#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
runtime="${DEEPSEEK_HARNESS_HOME:-$HOME/DeepSeek-Harness}"
test -x "$runtime/node_modules/.bin/dsh" || { echo "Install the official @deepseek-ai/dsh package in $runtime first." >&2; exit 1; }
python3 -c 'import gi; gi.require_version("WebKit2", "4.1")'
application="${XDG_DATA_HOME:-$HOME/.local/share}/deepseek-harness"
shortcuts="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
install -d "$application" "$shortcuts"
install -m 644 "$here/deepseek-harness.py" "$application/deepseek-harness.py"
python3 - "$application" "$shortcuts" <<'PY'
from pathlib import Path
import sys
folder, shortcuts = map(Path, sys.argv[1:])
(shortcuts / "deepseek-harness.desktop").write_text(
    '[Desktop Entry]\nType=Application\nName=DeepSeek Harness\n'
    f'Exec=/usr/bin/python3 "{folder}/deepseek-harness.py"\n'
    'Icon=utilities-terminal\nCategories=Development;\nTerminal=false\nStartupNotify=true\n',
    encoding="utf-8",
)
PY
