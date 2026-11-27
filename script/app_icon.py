"""Install the MicroWorld icon into built web and macOS app resources."""

import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
from tempfile import TemporaryDirectory

SOURCE = Path(__file__).resolve().parents[1] / "Resources" / "MicroWorld.png"


def install_web_icon(web):
    index = web / "index.html"
    html = index.read_text(encoding="utf-8")
    old = '<link rel="icon" href="/favicon.ico">'
    new = '<link rel="icon" type="image/png" href="/microworld-icon.png">'
    if old not in html and new not in html:
        raise ValueError(f"Could not identify the favicon link in {index}")
    shutil.copyfile(SOURCE.with_name("MicroWorld-web.png"), web / "microworld-icon.png")
    if old in html:
        index.write_text(html.replace(old, new), encoding="utf-8")


def install_app_icon(app):
    contents = app / "Contents"
    with TemporaryDirectory(prefix="microworld-icon-") as temporary:
        iconset = Path(temporary) / "MicroWorld.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ""), (2, "@2x")):
                pixels = str(size * scale)
                target = iconset / f"icon_{size}x{size}{suffix}.png"
                subprocess.run(
                    ["/usr/bin/sips", "-z", pixels, pixels, str(SOURCE), "--out", str(target)],
                    check=True, stdout=subprocess.DEVNULL,
                )
        subprocess.run(
            ["/usr/bin/iconutil", "-c", "icns", str(iconset),
             "-o", str(contents / "Resources" / "MicroWorld.icns")],
            check=True,
        )

    plist = contents / "Info.plist"
    with plist.open("rb") as source:
        info = plistlib.load(source)
    if info.get("CFBundleIconFile") != "MicroWorld":
        info["CFBundleIconFile"] = "MicroWorld"
        with plist.open("wb") as destination:
            plistlib.dump(info, destination, sort_keys=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--web", type=Path)
    target.add_argument("--app", type=Path)
    args = parser.parse_args()
    if args.web:
        install_web_icon(args.web)
    else:
        install_app_icon(args.app)
        install_web_icon(args.app / "Contents" / "Resources" / "Web")


if __name__ == "__main__":
    main()
