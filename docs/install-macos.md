# Install MicroWorld on macOS

The preview supports Apple silicon Macs with macOS 14 or later. The Pi uses the shared OpenFlexure v2 extensions. The computer runs the PC Worker for registration, fusion, and its local Canvas library.

1. Install the Pi extensions once using the [Pi installation guide](../pi_setup/README.md).
2. Download [MicroWorld-0.1.7-macOS-arm64.zip](https://github.com/daviddaiiiii/MicroWorld/releases/download/v0.1.7/MicroWorld-0.1.7-macOS-arm64.zip) and the release checksum file. From the download directory run `shasum -a 256 MicroWorld-0.1.7-macOS-arm64.zip` and compare the result with the matching line.
3. Extract the entire **MicroWorld** folder to a writable location such as `~/Documents/MicroWorld`. Keep the launchers and `script/` beside `MicroWorld.app`.
4. Open **MicroWorld.app** and allow Local Network access. The preview uses ad-hoc signing. If macOS asks for approval, use [Apple's instructions](https://support.apple.com/en-us/102445).
5. In **Microscopes**, select the Pi and choose **Connect**. Use **Add by Address** if discovery misses it. For scan processing the Pi needs access to TCP port 18766 on the computer running the PC Worker.

**Observe** shows the live camera, ROI, focus and scan controls. **Navigate** provides stage controls. **Gallery** reopens Canvas records. Use **Open Codex** if the Codex CLI is installed. For another MCP client, use the generated `mcp.json` beside the app. [Capture a scan and work with an ROI](../README.md#use-with-codex).

Each microscope grants control to one computer. Click **Connect** to take over an idle microscope. The previous computer disconnects and its PC Worker stops. A stage operation, autofocus, acquisition, or pending Slide processing blocks handoff. A saved Slide link never automatically replaces an active connection.

To upgrade, quit MicroWorld and replace the app bundle in its existing folder. Your per-microscope `Workspace/` and `ConversationHistory/` are beside the app. The bundle identifier is `org.microworld.app` and Slide links use `microworld://slide/…`.
