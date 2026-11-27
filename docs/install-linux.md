# Install MicroWorld on Ubuntu

The Linux preview was built for Ubuntu 24.04 ARM64. The application uses a GTK window and WebKitGTK. The Pi extensions, scan and stitching logic, Canvas data, and MCP tools are shared with [the macOS version](install-macos.md).

1. Install the Pi extensions once using the [Pi installation guide](../pi_setup/README.md). Connect the Ubuntu computer and Pi to the same private network. For scan processing the Pi needs access to TCP port 18766 on the computer that runs the PC Worker.
2. Download [microworld_0.1.3_arm64.deb](https://github.com/daviddaiiiii/MicroWorld/releases/download/v0.1.3/microworld_0.1.3_arm64.deb) and the release checksum file. From the download directory, compare `sha256sum microworld_0.1.3_arm64.deb` with the matching checksum.
3. Run `sudo apt install ./microworld_0.1.3_arm64.deb` and open **MicroWorld** in the application menu.
4. Pick the Pi in **Microscopes** or enter its address, such as `microscope.local:5000`. Choose **Connect**. Linux discovery checks local IPv4 addresses, Avahi if installed, and Tailscale peers if Tailscale is running.

## Camera, stage and scans

Use the **Live view** toolbar for **ROI**, **Stage**, **Focus**, and **Canvas**. Open advanced camera and calibration controls from **Settings**. Choose a grid or **Full** from Canvas. Once a scan finishes, open the refined Slide, inspect source tiles, or mark an ROI. Both desktop versions use the same Pi scanning and PC stitching implementation. [Work with the result in Codex](../README.md#use-with-codex).

**PC Worker · Automatic** starts local processing when the microscope is idle and no other computer owns the Worker. When another Worker is active, you can open the camera and operate the stage subject to the Pi's acquisition lock. To process scans on Ubuntu, disconnect the other Worker, wait for its status to expire, and reconnect with **Run on this computer**.

MicroWorld keeps its workspaces in `~/MicroWorld/Workspace/<device-id>/`, MCP activity in `~/MicroWorld/ConversationHistory/<device-id>/`, and connection state in `~/MicroWorld/State/`. The application writes `~/MicroWorld/mcp.json` and project Codex configuration on connection. Open the workspace as a trusted project in your MCP client. Slide links open in the application through `microworld://slide/…`, with the loopback viewer as an alternative in clients that only support HTTP links.

If you used the earlier browser preview, stop its user service with `systemctl --user disable --now microworld.service` before launching the installed app. If it created `~/.local/share/applications/org.microworld.app.desktop`, move that shortcut aside so the system uses the packaged launcher. The installed app opens the existing `~/MicroWorld` workspace.

## Optional DeepSeek Harness window

DeepSeek Harness remains a separate application with its own workspace and conversation history. Install the [official npm runtime](https://github.com/deepseek-ai/deepseek-harness) with a compatible Node.js version. The desktop launcher was tested with DSH `0.2.2-rc.5` and Node.js 24.

From a MicroWorld source checkout on Ubuntu, run:

```bash
npm install --prefix "$HOME/DeepSeek-Harness" --save-exact @deepseek-ai/dsh@0.2.2-rc.5
./Linux/install-dsh-window.sh
```

Open **DeepSeek Harness** from the application menu and configure the model account in its window. The launcher starts the official runtime on loopback. If you choose to connect a compatible MCP client to MicroWorld, use the project MCP settings generated after selecting the microscope.

## Build from source

To build on Ubuntu 24.04, install Node.js with npm, Python 3.12 with its venv package, and Debian packaging tools. Run `./script/build_linux.sh`. The script builds the shared browser viewer and places an architecture-specific package under `.build/linux/`. A build machine can optionally supply a prebuilt viewer directory from the same checkout as the first argument.
