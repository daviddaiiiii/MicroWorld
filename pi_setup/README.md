# MicroWorld Pi installation

Use this package on a Raspberry Pi with a running OpenFlexure microscope server that exposes the v2 API. You need a terminal or SSH access to the Pi and sudo privileges. Finish any movement, autofocus or scanning before installing.

## Install or update

Download the Pi archive and its SHA-256 file from the [MicroWorld release](https://github.com/daviddaiiiii/MicroWorld/releases/tag/v0.1.10). Either download on the Pi or copy the two files to it. Run these commands on the Pi from the download directory:

```bash
sha256sum -c MicroWorld-0.1.10-Pi.tar.gz.sha256
tar -xzf MicroWorld-0.1.10-Pi.tar.gz
cd MicroWorld-0.1.10-Pi
sudo ./install-microworld-pi.sh
```

The installer backs up previously installed extension files, installs the Canvas scan and visual-prompt extensions with their helpers, restarts OpenFlexure, and checks the current and compatibility endpoints. It leaves the existing web interface and microscope data in place. Keep the printed backup path. If an endpoint check fails, the installer tries to restore the previous extensions automatically.

The defaults are the `openflexure-microscope-server` systemd service, the v2 API at `http://127.0.0.1:5000`, and the extension directory at `/var/openflexure/extensions/microscope_extensions`. On a custom installation use `sudo env` to set `OPENFLEXURE_SERVICE`, `MICROWORLD_PI_URL`, or `OPENFLEXURE_EXTENSION_DIR` to the corresponding values.

## API names

The primary endpoints are `/api/v2/extensions/org.microworld.scan-mosaic` and `/api/v2/extensions/org.microworld.visual-prompts`. The previous namespace remains available as an alias. Both routes use the same scan queue, hardware lease and ROI store.

## Verify and connect

Look for `MicroWorld Pi extensions are ready.` after the installer verifies the scan and ROI endpoints.

Open MicroWorld on your computer and connect to the Pi. If the app was already connected before installing, disconnect and reconnect so it can discover the new capabilities. Follow the [desktop setup](https://github.com/daviddaiiiii/MicroWorld#2-install-the-desktop-app) to try a 2 × 2 canvas.

## Restore the previous extensions

Run the following on the Pi from the extracted package directory. Replace the example path with the backup path printed by the installer. Keep the microscope idle during rollback.

```bash
sudo ./install-microworld-pi.sh --rollback /var/openflexure/microworld-pi-backup-EXAMPLE
```
