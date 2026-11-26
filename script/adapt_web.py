"""Apply desktop-only presentation changes to a staged browser viewer."""

from pathlib import Path
import shutil
import sys

project, resources = map(Path, sys.argv[1:3])
src = project / "src"


def change(path, old, new):
    content = path.read_text(encoding="utf-8")
    if old not in content:
        raise RuntimeError(f"Update the desktop adaptation for {path.name}: {old[:80]}")
    path.write_text(content.replace(old, new, 1), encoding="utf-8")


index = project / "public/index.html"
change(index, "</head>", '''  <script src="/desktop/config.js"></script>
    <link rel="stylesheet" href="/microworld.css">
  </head>''')
shutil.copyfile(resources / "microworld.css", project / "public/microworld.css")

for path in src.rglob("*.vue"):
    content = path.read_text(encoding="utf-8")
    if "http://127.0.0.1:18765" in content:
        path.write_text(content.replace("http://127.0.0.1:18765", "http://127.0.0.1:18766"), encoding="utf-8")

change(src / "App.vue", "if (!completedTour)", "if (!completedTour && !window.__MICROWORLD__)")
tabs = src / "components/appContent.vue"
change(tabs, 'currentTab: "view"', 'currentTab: "openmhs"')
change(tabs, '''    // A global signal listener to switch tab''', '''    window.microworldSelectTab = id => {
      if (this.tabOrder.includes(id)) this.currentTab = id;
    };
    if (window.microworldRequestedTab) window.microworldSelectTab(window.microworldRequestedTab);
    // A global signal listener to switch tab''')

observation = src / "components/tabContentComponents/openmhsContent.vue"
change(observation, "<strong>MicroWorld</strong><span>Camera · Canvas</span>",
       "<strong>Live view</strong><span>Camera</span>")
change(observation, 'title="Stage controls"', 'title="Stage controls and coordinates"')
change(observation, "<span>XYZ</span>", "<span>Stage</span>")
change(observation, '''        <button title="Add canvas"''', '''        <button
          title="Run fast autofocus"
          :disabled="zControlsLocked || afBusy"
          @click.stop="runAutofocus"
        >
          <i class="material-icons">center_focus_strong</i>
          <span>{{ afBusy ? "Focusing…" : "Focus" }}</span>
        </button>
        <button class="mw-primary" title="Create or open a canvas"''')
change(observation, "<span>Add canvas</span>", "<span>Canvas</span>")
change(observation, '''    this.$nextTick(() => {
      this.tryRestoreScanSession();
      this.syncCameraViewToPc().then(() => this.restoreSharedCanvas());
    });''', '''    this.$nextTick(() => {
      const query = new URLSearchParams(window.location.search);
      const slide = query.get("slide");
      if (slide) {
        // The local server already validated the device and refined file.
        // Open the requested record without waiting on the Pi's view index.
        this.sessionId = slide;
        this.macSessionId = slide;
        this.macArchived = true;
        this.scanSession = true;
        this.studioMode = "slide";
        this.studioSession = slide;
      } else {
        this.tryRestoreScanSession();
        this.syncCameraViewToPc().then(() => this.restoreSharedCanvas());
      }
    });''')
studio = src / "components/tabContentComponents/openmhsComponents/canvasStudio.vue"
change(studio, '''          `${this.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`,
          { ...body, operation: "canvas", session_id: this.session },''', '''          new URLSearchParams(window.location.search).has("slide")
            ? `/mosaic/${encodeURIComponent(this.session)}/canvas`
            : `${this.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`,
          { ...body, operation: "canvas", session_id: this.session },''')
change(index, "</body>", '''    <script>
      const mwQuery = new URLSearchParams(window.location.search);
      const mwSlide = mwQuery.get("slide");
      const mwDevice = mwQuery.get("device");
      if (mwSlide && mwDevice && mwQuery.get("native") !== "1") {
        const link = document.createElement("a");
        link.className = "mw-open-native";
        link.href = "microworld://slide/" + encodeURIComponent(mwDevice) + "/" + encodeURIComponent(mwSlide);
        link.textContent = "Open in MicroWorld";
        document.body.appendChild(link);
      }
    </script>
  </body>''')
# Keep settings, including microscope settings, accessible; desktop owns worker paths.
settings = src / "components/tabContentComponents/settingsComponents/openMHSSettings.vue"
change(settings, 'return this.$store.getters.baseUri || window.location.origin;',
       'return (window.__MICROWORLD__ && window.__MICROWORLD__.microscopeURL) || this.$store.getters.baseUri || window.location.origin;')

stream = src / "components/tabContentComponents/settingsComponents/streamSettings.vue"
change(stream, 'const localMode = ["localhost"', 'const localMode = !window.__MICROWORLD__ && ["localhost"')
agent = src / "components/tabContentComponents/openmhsComponents/openmhsAgentTool.vue"
change(agent, 'codexConnected ? "● PC connect" : "○ PC offline"',
       'codexConnected ? "● MCP active" : "○ MCP ready"')
