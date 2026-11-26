# MicroWorld for macOS

MicroWorld is a desktop client for OpenFlexure microscopes running OpenMHS. Its SwiftUI panels read the camera stream and operate the stage and scan API on the selected Pi. The bundled PC Worker stores canvases and renders Slides on the Mac. The browser viewer is available for HTTP links returned by MCP.

## Download and connect

Download the Apple silicon ZIP from [Releases](https://github.com/daviddaiiiii/MicroWorld/releases) and extract it into a folder you can write to. Keep the included launchers and the `script` folder next to `MicroWorld.app` so the Codex and MCP entry points can find the bundled runtime. The app targets macOS 14 or newer. The preview build is ad-hoc signed, and macOS may require local approval before opening it.

1. Open **MicroWorld.app** or double-click **Open MicroWorld.command** in the extracted folder. Allow Local Network access when prompted.
2. Select a microscope by its device ID suffix and address. Click **Connect**. The arrow beside Connect lets you pick an alternate address for the same device.
3. Use **Observe** for the live image, ROI, Fast AF, and Canvas acquisition. **Navigate** provides stage control and calibration. **Gallery** opens scans and captures, **Capture** controls image acquisition, and **Settings** shows the instrument and camera settings. The toolbar menu opens the workspace, conversation history, or Codex.

Discovery checks Bonjour, the local IPv4 network, and peers returned by the installed Tailscale CLI. Tailscale must be connected and permit traffic to the Pi. MicroWorld uses the OpenFlexure identity returned by the device to group addresses and rechecks it before opening a control session. On larger networks, automatic IPv4 discovery checks the local /24. Use **Add by Address** for another subnet, custom port, VPN address, or hostname. For PC processing over VPN, the Pi must also be able to reach the Mac on TCP 18766.

The app uses the bundled PC Worker on port 18766. If the legacy local worker on 18765 is already serving the same instrument, MicroWorld temporarily pauses that worker and resumes it after disconnecting. A different instrument can continue using its existing worker.

## Scan modes

**Full** finds tissue boundaries from the current field and checks the perimeter.
**Fixed grids** scan the selected number of columns and rows from the current
field. On supported Pis, both use continuous 1280×960 acquisition for rows and
stop for fields that need a clearer image or have no usable frame. Full also
checks uncertain fields for tissue before capturing them. A single field is
captured while stationary. Continuing a saved scan uses stationary capture.

Both modes show live tiles using the same stage origin as the Camera FOV.
The Mac learns the preview scale from adjacent tiles as they arrive and reuses
previously measured pairs. During continuous capture, the Pi aligns frames within
each row to decide which JPEGs to keep.

Both modes register the final Slide on the PC. The worker estimates horizontal
and vertical stage-to-image vectors from the overlaps and rejects inconsistent
matches before fusion. Blank or disconnected regions retain stage placement.
An already saved Slide is unchanged until it is refined again.

## Where files go

| Location | Contents |
| --- | --- |
| MicroWorld.app | Native app, desktop UI, Python, Worker, and MCP server |
| Workspace/<device-id>/ | Canvas, scans, captures, ROI analysis, and OpenMHS conversation snapshots |
| ConversationHistory/<device-id>/ | MCP activity and Codex sessions started with Open Codex |
| State/ and Logs/ | Discovered devices, selected connection, and local runtime logs |
| .codex/config.toml and mcp.json | Automatically generated project MCP configuration |

The new workspace does not require the old OpenMHS-Workspace directory. Existing archives are not migrated automatically. Select a microscope before starting a new MCP session. An existing MCP process stays bound to its original connection and rejects tool calls after a switch. Start a new MCP session after changing devices.

**Open Codex.command** runs an installed Codex CLI in the active device's workspace, with that device's conversation folder as its Codex data directory. Sign in through the CLI if prompted. Conversations started in another Codex project or another client follow that client's storage settings. The Pi keeps its existing rolling store for prompts and captures. If this folder is in iCloud Drive, the data is subject to that folder's sync settings.

## Build

On Apple silicon with the Swift Command Line Tools, Node.js and Python 3, run from this repository:

    ./script/build_app.sh

The build includes the native SwiftUI app, its Python dependencies, and the browser viewer used by HTTP Slide links. This repository includes the required Worker, MCP and viewer source. Canvas acquisition and PC refinement require a compatible [OpenMHS](https://github.com/daviddaiiiii/OpenMHS) installation on the microscope. The current build targets Apple silicon and macOS 14 or newer. It is signed for local execution and is not notarized for public distribution. A build machine with a signing identity can set MICROWORLD_SIGN_IDENTITY.

## Design references

- [openUC2 ImSwitch](https://github.com/openUC2/ImSwitch) keeps live imaging central and groups instrument controls next to the image.
- [Open Micro-Manipulator GUI](https://github.com/DmitrySorda/micromanipulatorstepper#open-micro-manipulator-gui) places stage movement and camera feedback together.
- [OpenAI SwiftUI Patterns](https://github.com/openai/plugins/blob/main/plugins/build-macos-apps/skills/swiftui-patterns/SKILL.md) and [Apple's macOS HIG](https://developer.apple.com/design/human-interface-guidelines/designing-for-macos) informed the native split window, toolbar, and keyboard access.

## Use with Codex

Connect the microscope and open Codex in the MicroWorld folder or the
device's workspace to load its project MCP configuration. The **Open Codex**
menu item opens a separate Codex CLI session for that workspace.

Ask Codex to start a 2×2 scan with `microworld_scan_2x2` and check
`microworld_scan_status` until the refined Slide is ready. Open the
`microworld://slide/…` result in the app. Clients that only render HTTP
links can open the same Slide in their browser and choose **Open in
MicroWorld**.

In the Slide view, choose **Mark ROI**, draw a target and wait for the
selection sync message. Ask for segmentation and counting in the same
Codex conversation. `microworld_get_slide_roi` takes the scan's session ID
and returns the crop, selection geometry and immutable prompt ID. If the
device changes, reconnect MCP before issuing another request.
