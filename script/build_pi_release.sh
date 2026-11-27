#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="${1:-0.1.10}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Expected a version such as 0.1.2." >&2; exit 2; }
name="MicroWorld-$version-Pi"
output="$root/.build/github-release"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
bundle="$stage/$name"
mkdir -p "$bundle/backend" "$output"
cp "$root/pi_setup/README.md" "$root/LICENSE" "$root/NOTICE.md" "$bundle/"
install -m 0755 "$root/pi_setup/install-microworld-pi.sh" "$bundle/install-microworld-pi.sh"
for file in org_microworld_scan_mosaic.py org_microworld_visual_prompts.py microworld_scan_plan.py microworld_hardware.py microworld_api_compat.py; do
    cp "$root/pi_setup/backend/$file" "$bundle/backend/"
done
cp "$root/mac-agent/openmhs_registration.py" "$bundle/backend/microworld_registration.py"
COPYFILE_DISABLE=1 tar -C "$stage" -czf "$output/$name.tar.gz" "$name"
(cd "$output" && shasum -a 256 "$name.tar.gz" > "$name.tar.gz.sha256")
echo "$output/$name.tar.gz"
