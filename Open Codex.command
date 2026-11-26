#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
resources="$root/MicroWorld.app/Contents/Resources"
exec "$resources/Python/bin/python3" -E -s -B "$resources/Runtime/codex_entry.py" "$root"
