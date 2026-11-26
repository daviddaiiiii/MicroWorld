#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
exec "$root/MicroWorld.app/Contents/MacOS/MicroWorld"
