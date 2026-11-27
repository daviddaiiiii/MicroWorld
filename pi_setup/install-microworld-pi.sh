#!/usr/bin/env bash
set -Eeuo pipefail

bundle="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
extension_dir="${OPENFLEXURE_EXTENSION_DIR:-/var/openflexure/extensions/microscope_extensions}"
service="${OPENFLEXURE_SERVICE:-openflexure-microscope-server}"
local_url="${MICROWORLD_PI_URL:-${OPENMHS_LOCAL_URL:-http://127.0.0.1:5000}}"
backup_parent="${MICROWORLD_PI_BACKUP_DIR:-${OPENMHS_BACKUP_DIR:-/var/openflexure}}"
extension_api="${local_url%/}/api/v2/extensions"
scan_api="$extension_api/org.microworld.scan-mosaic"
legacy_scan_api="$extension_api/org.openmhs.scan-mosaic"
backend_files=(
    org_microworld_scan_mosaic.py
    org_microworld_visual_prompts.py
    microworld_scan_plan.py
    microworld_hardware.py
    microworld_registration.py
    microworld_api_compat.py
)
legacy_files=(
    org_openmhs_scan_mosaic.py
    org_openmhs_visual_prompts.py
    openmhs_scan_plan.py
    openmhs_hardware.py
    openmhs_registration.py
)

die() {
    echo "$*" >&2
    exit 1
}

restore_backup() {
    local backup="$1" file
    mkdir -p -- "$extension_dir" || return 1
    for file in "${backend_files[@]}" "${legacy_files[@]}"; do
        if [[ -f "$backup/extensions/$file" ]]; then
            cp -p -- "$backup/extensions/$file" "$extension_dir/$file" || return 1
        else
            rm -f -- "$extension_dir/$file" || return 1
        fi
    done
    systemctl restart -- "$service" || return 1
    systemctl is-active --quiet -- "$service"
}

check_idle() {
    local hardware_json
    hardware_json="$(curl -fsS --max-time 4 "$scan_api/hardware" 2>/dev/null ||
        curl -fsS --max-time 4 "$legacy_scan_api/hardware" 2>/dev/null || true)"
    if [[ -n "$hardware_json" ]] && printf '%s' "$hardware_json" | python3 -c \
        'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get("busy") else 1)'; then
        die "The microscope is busy. Finish the hardware operation before continuing."
    fi
}

if [[ "$EUID" -ne 0 ]]; then
    die "Run this installer with sudo."
fi
if [[ "${1:-}" == --rollback && "$#" -eq 2 ]]; then
    backup_root="$2"
    [[ -f "$backup_root/INSTALL_TARGET" && -d "$backup_root/extensions" ]] ||
        die "This directory is not a MicroWorld Pi backup: $backup_root"
    {
        IFS= read -r extension_dir
        IFS= read -r service
    } < "$backup_root/INSTALL_TARGET"
    [[ "$extension_dir" == /* && -n "$service" ]] || die "Invalid backup metadata."
    check_idle
    restore_backup "$backup_root" || die "Rollback failed. Backup kept at $backup_root."
    echo "Previous extensions restored from $backup_root"
    exit 0
fi
[[ "$#" -eq 0 ]] || die "Usage: sudo $0 [--rollback BACKUP_DIRECTORY]"

[[ "$extension_dir" == /* && "$backup_parent" == /* ]] ||
    die "The OpenFlexure extension and backup directories must have absolute paths."
for file in "${backend_files[@]}"; do
    [[ -f "$bundle/backend/$file" ]] || die "Missing backend/$file. Use the Pi archive from Releases."
done
python3 - "$bundle/backend" "${backend_files[@]}" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
for name in sys.argv[2:]:
    path = root / name
    compile(path.read_bytes(), str(path), "exec")
PY
systemctl is-active --quiet -- "$service" ||
    die "The $service service is not active. Start OpenFlexure before installing."
curl -fsS --max-time 5 "${local_url%/}/api/v2/" >/dev/null ||
    die "The OpenFlexure v2 API did not respond at $local_url."
check_idle

backup_root="$(mktemp -d "$backup_parent/microworld-pi-backup-XXXXXXXX")"
mkdir "$backup_root/extensions"
for file in "${backend_files[@]}" "${legacy_files[@]}"; do
    if [[ -f "$extension_dir/$file" ]]; then
        cp -p -- "$extension_dir/$file" "$backup_root/extensions/$file"
    fi
done
printf '%s\n' "$extension_dir" "$service" > "$backup_root/INSTALL_TARGET"
echo "Backup: $backup_root"

on_failure() {
    local status="$?"
    trap - EXIT
    if [[ "$status" -ne 0 ]]; then
        if restore_backup "$backup_root"; then
            echo "Installation failed. Previous extensions restored." >&2
        else
            echo "Installation failed. Retry rollback with sudo $0 --rollback $backup_root" >&2
        fi
    fi
    exit "$status"
}
trap on_failure EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p -- "$extension_dir"
for file in "${backend_files[@]}"; do
    install -m 0644 -- "$bundle/backend/$file" "$extension_dir/$file"
done
# The new backend itself serves the legacy routes. Retire its predecessors
# before restart so OpenFlexure never registers a second scheduler or store.
for file in "${legacy_files[@]}"; do
    rm -f -- "$extension_dir/$file"
done
systemctl restart -- "$service"
for endpoint in \
    "$scan_api/status" \
    "$scan_api/connection" \
    "$extension_api/org.microworld.visual-prompts/status" \
    "$legacy_scan_api/status" \
    "$extension_api/org.openmhs.visual-prompts/status"; do
    ready=0
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 "$endpoint" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 1
    done
    [[ "$ready" == 1 ]] || die "Endpoint check failed: $endpoint"
done

trap - EXIT INT TERM
echo "MicroWorld Pi extensions are ready."
echo "To restore the previous extensions: sudo $0 --rollback $backup_root"
