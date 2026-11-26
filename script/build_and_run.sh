#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
app="$root/MicroWorld.app"
mode="${1:-run}"
if pgrep -x MicroWorld >/dev/null; then
  pkill -TERM -x MicroWorld
fi
"$root/script/build_app.sh"
case "$mode" in
  run) "$root/Open MicroWorld.command" & ;;
  --debug) lldb -- "$app/Contents/MacOS/MicroWorld" ;;
  --logs|--telemetry) "$root/Open MicroWorld.command" & /usr/bin/log stream --style compact --predicate 'process == "MicroWorld"' ;;
  --verify)
    "$root/Open MicroWorld.command" &
    sleep 2
    pgrep -x MicroWorld >/dev/null
    ;;
  *) echo "usage: $0 [--debug|--logs|--telemetry|--verify]" >&2; exit 2 ;;
esac
