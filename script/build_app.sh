#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cache="$root/.build/cache"
case "$(uname -m)" in
  arm64) triple=aarch64-apple-darwin; sha=3afb0567b2a2b30b3a7341bd143cb4e8a69f3d8e95d4641218eaa45f0fabbd07 ;;
  *) echo "This build currently targets Apple silicon." >&2; exit 1 ;;
esac
archive="$cache/python.tar.gz"
mkdir -p "$cache"
if [[ ! -f "$archive" ]]; then
  curl -fL --retry 2 -o "$archive.part" "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.11.18%2B20261110-$triple-install_only_stripped.tar.gz"
  mv "$archive.part" "$archive"
fi
printf '%s  %s\n' "$sha" "$archive" | shasum -a 256 -c
if ! cmp -s "$cache/python/.microworld-dependencies" "$root/Resources/requirements.txt"; then
  mkdir -p "$cache/python"
  tar xzf "$archive" -C "$cache/python" --strip-components 1
  "$cache/python/bin/python3" -m pip --disable-pip-version-check install \
    --only-binary=:all: -r "$root/Resources/requirements.txt"
  cp "$root/Resources/requirements.txt" "$cache/python/.microworld-dependencies"
fi
if [[ "${MICROWORLD_REUSE_WEB:-0}" != 1 || ! -f "$root/.build/Web/index.html" ]]; then
  "$root/script/build_web.sh"
fi
swift build --package-path "$root" -c release
binary="$(swift build --package-path "$root" -c release --show-bin-path)/MicroWorld"
# Build the signature before iCloud Drive can add Finder metadata.
staging="$(mktemp -d -t microworld-package)"
trap 'rm -rf "$staging"' EXIT
app="$staging/MicroWorld.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
resources="$app/Contents/Resources"
cp "$binary" "$app/Contents/MacOS/MicroWorld"
cp "$root/Resources/Info.plist" "$app/Contents/Info.plist"
mkdir -p "$resources"/{Python,Runtime,Worker,MCP,Web,Notices}
rsync -a --delete --exclude '__pycache__' "$cache/python/" "$resources/Python/"
rsync -a --delete --exclude '__pycache__' "$root/Runtime/" "$resources/Runtime/"
rsync -a --delete "$root/.build/Web/" "$resources/Web/"
rsync -a --delete --include '*.py' --exclude '*' "$root/mac-agent/" "$resources/Worker/"
cp "$root/mcp/openmhs_vp_server.py" "$resources/MCP/"
cp "$root/LICENSE" "$root/NOTICE.md" "$resources/Notices/"
python3 "$root/script/app_icon.py" --app "$app"
/usr/bin/xattr -cr "$app"
/usr/bin/codesign --force --sign "${MICROWORLD_SIGN_IDENTITY:--}" "$app"
/usr/bin/codesign --verify "$app"
mkdir -p "$root/Workspace" "$root/ConversationHistory"
if [[ -d "$root/MicroWorld.app" ]]; then
  rm -rf "$root/MicroWorld.app"
fi
mv "$app" "$root/MicroWorld.app"
/usr/bin/xattr -dr com.apple.FinderInfo "$root/MicroWorld.app" 2>/dev/null || true
echo "$root/MicroWorld.app"
