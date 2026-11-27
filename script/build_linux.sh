#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
arch="$(dpkg --print-architecture)"
case "$arch" in arm64|amd64) ;; *) echo "Unsupported Linux architecture: $arch" >&2; exit 1 ;; esac
python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
version="$(python3.12 -c 'import plistlib,sys; print(plistlib.load(open(sys.argv[1],"rb"))["CFBundleShortVersionString"])' "$root/Resources/Info.plist")"
web="${1:-$root/.build/Web}"
if [[ ! -f "$web/index.html" ]]; then
  "$root/script/build_web.sh"
fi
test -f "$web/index.html"
build="$root/.build/linux"
mkdir -p "$build"
if [[ ! -x "$build/venv/bin/pip" ]]; then
  python3.12 -m venv "$build/venv"
fi
stage="$(mktemp -d -p "$build" package.XXXXXX)"
pkg="$stage/usr/lib/microworld"
mkdir -p "$stage"/{DEBIAN,usr/bin,usr/share/applications,usr/share/icons/hicolor/1024x1024/apps} "$pkg"/{Runtime,Worker,MCP,Web,Python,Linux}
install -m 644 "$root"/Runtime/*.py "$pkg/Runtime/"
install -m 644 "$root"/mac-agent/*.py "$pkg/Worker/"
install -m 644 "$root/mcp/openmhs_vp_server.py" "$pkg/MCP/"
install -m 644 "$root/Linux/microworld.py" "$pkg/Linux/"
install -m 644 "$root/LICENSE" "$root/NOTICE.md" "$pkg/"
cp -a "$web/." "$pkg/Web/"
python3.12 "$root/script/app_icon.py" --web "$pkg/Web"
"$build/venv/bin/pip" --disable-pip-version-check install --only-binary=:all: --no-compile   --target "$pkg/Python" -r "$root/Resources/requirements.txt"
install -m 644 "$root/Linux/org.microworld.app.desktop" "$stage/usr/share/applications/"
install -m 644 "$root/Resources/MicroWorld.png" "$stage/usr/share/icons/hicolor/1024x1024/apps/org.microworld.app.png"
cat > "$stage/usr/bin/microworld" <<'LAUNCHER'
#!/bin/sh
exec /usr/bin/python3 -B -s /usr/lib/microworld/Linux/microworld.py "$@"
LAUNCHER
chmod 755 "$stage/usr/bin/microworld"
installed_size="$(du -sk "$stage/usr" | cut -f1)"
cat > "$stage/DEBIAN/control" <<CONTROL
Package: microworld
Version: $version
Architecture: $arch
Maintainer: MicroWorld contributors
Section: science
Priority: optional
Installed-Size: $installed_size
Depends: python3.12, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, iproute2, libgomp1, xdg-utils
Recommends: avahi-utils
Description: Desktop workspace for OpenFlexure microscopy
 Discover microscopes, observe and move the stage, capture scans, edit
 Canvas layers and open selected regions through the MicroWorld MCP server.
CONTROL
package="$build/microworld_${version}_${arch}.deb"
dpkg-deb --build --root-owner-group "$stage" "$package"
sha256sum "$package" | awk '{print $1}'
printf '%s\n' "$package"
