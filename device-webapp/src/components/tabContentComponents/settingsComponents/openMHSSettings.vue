<template>
  <section class="openmhs-settings" aria-label="MicroWorld settings">
    <header class="section-heading">
      <div>
        <p class="eyebrow">MicroWorld</p>
        <h2>Connections &amp; storage</h2>
        <p class="subtitle">
          The microscope acquires images. The PC worker stores canvases and
          builds the final slide.
        </p>
      </div>
      <button
        class="uk-button uk-button-default compact-button"
        type="button"
        :disabled="refreshing"
        @click="refreshAll"
      >
        {{ refreshing ? "Checking…" : "Refresh" }}
      </button>
    </header>

    <div
      v-if="notice"
      class="notice"
      :class="{ error: noticeError }"
      role="status"
    >
      {{ notice }}
    </div>

    <div class="status-grid">
      <div class="status-card">
        <div class="status-topline">
          <span class="status-name">Microscope Pi</span>
          <span class="state" :class="piOnline ? 'online' : 'offline'">
            <span class="state-dot"></span>{{ piOnline ? "Online" : "Offline" }}
          </span>
        </div>
        <p class="status-detail">{{ microscopeOrigin }}</p>
        <p class="status-caption">
          {{
            piOnline
              ? hardwareBusy
                ? "Hardware action in progress"
                : "Hardware idle"
              : "Check the microscope network connection."
          }}
        </p>
      </div>
      <div class="status-card">
        <div class="status-topline">
          <span class="status-name">PC worker</span>
          <span class="state" :class="pcConnected ? 'online' : 'offline'">
            <span class="state-dot"></span
            >{{ pcConnected ? "Connected" : "Not connected" }}
          </span>
        </div>
        <p class="status-detail">
          {{
            localPc
              ? localPc.worker_running
                ? "Running on this computer"
                : "Local supervisor ready"
              : (pcAgent && pcAgent.supervisor_url) ||
                "No local supervisor detected"
          }}
        </p>
        <p class="status-caption">
          {{
            localPc
              ? "Local controls available below."
              : "Start or configure the worker on the computer that owns the workspace."
          }}
        </p>
      </div>
    </div>

    <div class="settings-card">
      <div class="card-heading">
        <div>
          <h3>PC worker</h3>
          <p>These controls act only on the computer displaying this page.</p>
        </div>
        <button
          class="uk-button uk-button-primary compact-button"
          type="button"
          :disabled="
            busyAction !== '' ||
              hardwareBusy ||
              (localPc && localPc.worker_running)
          "
          @click="startPc"
        >
          {{
            busyAction === "start"
              ? "Starting…"
              : localPc && localPc.worker_running
              ? "Worker running"
              : "Start PC worker"
          }}
        </button>
      </div>
      <div v-if="!localPc" class="onboarding">
        <strong>First time on this computer?</strong>
        <p>
          The browser cannot install software by itself. Download the
          <a
            href="https://github.com/daviddaiiiii/MicroWorld#install-and-connect"
            target="_blank"
            rel="noopener noreferrer"
            >PC setup guide</a
          >
          then run the PC installer and macOS startup helper once. You can
          choose both paths during setup; after that, manage them here and start
          the worker with one click.
        </p>
      </div>
      <div class="field install-field">
        <label for="openmhs-install-path"
          >Install PC Worker at a new path</label
        >
        <input
          id="openmhs-install-path"
          v-model.trim="installationPath"
          class="uk-input"
          type="text"
          placeholder="~/Documents/MicroWorld"
          :disabled="!localPc || busyAction !== '' || installationBusy"
        />
        <p class="helper">
          Current runtime:
          {{ localPc ? localPc.repo : "Not installed on this computer" }}. A new
          path copies the program, prepares its Python environment, then changes
          login startup. The old installation is kept.
        </p>
        <p
          v-if="installation.state && installation.state !== 'idle'"
          class="helper"
          role="status"
        >
          {{ installation.state }}:
          {{ installation.detail || installation.target }}
        </p>
        <button
          class="uk-button uk-button-default compact-button"
          type="button"
          :disabled="
            !localPc ||
              busyAction !== '' ||
              installationBusy ||
              !installationPath ||
              hardwareBusy
          "
          @click="saveInstallation"
        >
          {{
            busyAction === "installation"
              ? "Preparing…"
              : "Install at this path"
          }}
        </button>
      </div>
      <div class="form-grid">
        <div class="field">
          <label for="openmhs-pc-url">Microscope URL used by this PC</label>
          <input
            id="openmhs-pc-url"
            v-model.trim="pcMicroscopeUrl"
            class="uk-input"
            type="url"
            placeholder="http://microscope.local:5000"
            :disabled="!localPc || busyAction !== ''"
          />
          <p class="helper">
            Use a Pi address reachable from this PC. Saving restarts a running
            worker.
          </p>
          <button
            class="uk-button uk-button-default compact-button"
            type="button"
            :disabled="
              !localPc || busyAction !== '' || !pcMicroscopeUrl || hardwareBusy
            "
            @click="savePcUrl"
          >
            {{ busyAction === "pc-url" ? "Saving…" : "Save PC connection" }}
          </button>
        </div>
        <div class="field">
          <label for="openmhs-workspace">Canvas workspace path</label>
          <input
            id="openmhs-workspace"
            v-model.trim="workspacePath"
            class="uk-input"
            type="text"
            placeholder="~/Documents/MicroWorld/Workspace"
            :disabled="!localPc || busyAction !== ''"
          />
          <p class="helper">
            Creates the folder if needed. Existing records stay at the old path;
            saving restarts a running worker.
          </p>
          <button
            class="uk-button uk-button-default compact-button"
            type="button"
            :disabled="
              !localPc || busyAction !== '' || !workspacePath || hardwareBusy
            "
            @click="saveWorkspace"
          >
            {{ busyAction === "workspace" ? "Saving…" : "Use this workspace" }}
          </button>
        </div>
      </div>
    </div>

    <div class="settings-card">
      <div class="card-heading">
        <div>
          <h3>Microscope Pi</h3>
          <p>
            Visual prompts are stored on the Pi; Canvas records live on the PC.
          </p>
        </div>
      </div>
      <div class="form-grid">
        <div class="field">
          <label for="openmhs-vp-quota"
            >Visual-prompt storage limit (GiB)</label
          >
          <input
            id="openmhs-vp-quota"
            v-model.number="quotaGiB"
            class="uk-input number-input"
            type="number"
            min="0.25"
            max="64"
            step="0.25"
            :disabled="!piOnline || busyAction !== ''"
          />
          <p class="helper">{{ storageSummary }}</p>
          <button
            class="uk-button uk-button-default compact-button"
            type="button"
            :disabled="!piOnline || busyAction !== ''"
            @click="saveQuota"
          >
            {{ busyAction === "quota" ? "Saving…" : "Save Pi storage limit" }}
          </button>
        </div>
        <div class="field">
          <span class="field-label">Pi data location</span>
          <p class="read-only-path">{{ piSettings.root || "Unavailable" }}</p>
          <p class="helper">
            The Pi capture and prompt roots are managed by OpenFlexure. Stage,
            camera and exposure options remain in the microscope settings above.
          </p>
        </div>
      </div>
    </div>
  </section>
</template>

<script>
import axios from "axios";

const LOCAL_SUPERVISOR = "http://127.0.0.1:18765";
const GIB = 1024 * 1024 * 1024;

export default {
  name: "OpenMHSSettings",
  props: {
    active: { type: Boolean, default: false }
  },
  data() {
    return {
      timer: null,
      refreshing: false,
      busyAction: "",
      notice: "",
      noticeError: false,
      piOnline: false,
      piSettings: {},
      piStorage: {},
      hardware: {},
      pcAgent: null,
      localPc: null,
      installation: {},
      installationPath: "",
      quotaGiB: 2,
      workspacePath: "",
      pcMicroscopeUrl: ""
    };
  },
  computed: {
    microscopeOrigin() {
      return this.$store.getters.baseUri || window.location.origin;
    },
    vpBase() {
      const namespace = (window.__MICROWORLD__ && window.__MICROWORLD__.apiNamespace) || "org.microworld";
      return `${this.microscopeOrigin}/api/v2/extensions/${namespace}.visual-prompts`;
    },
    scanBase() {
      const namespace = (window.__MICROWORLD__ && window.__MICROWORLD__.apiNamespace) || "org.microworld";
      return `${this.microscopeOrigin}/api/v2/extensions/${namespace}.scan-mosaic`;
    },
    hardwareBusy() {
      return !!(this.hardware && this.hardware.busy);
    },
    pcConnected() {
      return !!(this.piOnline && this.pcAgent && this.pcAgent.worker_online);
    },
    installationBusy() {
      return ["copying", "dependencies", "switching"].includes(
        this.installation.state
      );
    },
    storageSummary() {
      if (!this.piOnline) return "Connect to the Pi to read its current limit.";
      const used = Number(this.piStorage.used_bytes || 0) / GIB;
      const max = Number(this.piSettings.max_bytes || 0) / GIB;
      return `${used.toFixed(2)} GiB used of ${max.toFixed(
        2
      )} GiB. Lowering the limit may remove the oldest prompts.`;
    }
  },
  watch: {
    active(value) {
      if (value) this.beginPolling();
      else this.endPolling();
    }
  },
  mounted() {
    if (this.active) this.beginPolling();
  },
  beforeDestroy() {
    this.endPolling();
  },
  methods: {
    beginPolling() {
      if (this.timer) return;
      this.refreshAll();
      this.timer = window.setInterval(() => this.refreshStatus(), 10000);
    },
    endPolling() {
      if (this.timer) window.clearInterval(this.timer);
      this.timer = null;
    },
    async getOrNull(url, timeout = 3000) {
      try {
        const response = await axios.get(url, { timeout });
        return response.data || null;
      } catch (_) {
        return null;
      }
    },
    async refreshStatus() {
      const [pi, agent, hardware, local, installation] = await Promise.all([
        this.getOrNull(`${this.vpBase}/status`),
        this.getOrNull(`${this.vpBase}/agent`),
        this.getOrNull(`${this.scanBase}/hardware`),
        this.getOrNull(`${LOCAL_SUPERVISOR}/status`, 1500),
        this.getOrNull(`${LOCAL_SUPERVISOR}/installation`, 1500)
      ]);
      this.piOnline = !!(pi && pi.ok);
      this.piStorage = pi || {};
      this.pcAgent = agent;
      this.hardware = hardware || {};
      this.localPc = local && local.ok ? local : null;
      this.installation = installation || this.installation;
      if (this.localPc && !this.busyAction) {
        if (!this.workspacePath)
          this.workspacePath = this.localPc.workspace || "";
        if (!this.pcMicroscopeUrl)
          this.pcMicroscopeUrl = this.localPc.microscope || "";
      }
    },
    async refreshAll() {
      this.refreshing = true;
      try {
        await this.refreshStatus();
        const settings = await this.getOrNull(`${this.vpBase}/settings`);
        if (settings) {
          this.piSettings = settings;
          if (settings.max_bytes) {
            this.quotaGiB =
              Math.round((Number(settings.max_bytes) / GIB) * 100) / 100;
          }
        }
      } finally {
        this.refreshing = false;
      }
    },
    showNotice(message, error = false) {
      this.notice = message;
      this.noticeError = error;
    },
    errorMessage(error) {
      const data = error && error.response && error.response.data;
      return (
        (data && (data.error || data.message)) ||
        (error && error.message) ||
        "Request failed."
      );
    },
    launchLocalSupervisor() {
      const link = document.createElement("a");
      link.href = "openmhs://start";
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    },
    async startPc() {
      this.busyAction = "start";
      try {
        let local = await this.getOrNull(`${LOCAL_SUPERVISOR}/status`, 1500);
        if (!local) {
          this.launchLocalSupervisor();
          for (let i = 0; i < 24 && !local; i++) {
            await new Promise(resolve => window.setTimeout(resolve, 500));
            local = await this.getOrNull(`${LOCAL_SUPERVISOR}/status`, 1200);
          }
        }
        if (!local)
          throw new Error(
            "Local supervisor unavailable. Run the PC setup installer once on this computer."
          );
        await axios.post(`${LOCAL_SUPERVISOR}/start`, {}, { timeout: 8000 });
        for (let i = 0; i < 10; i++) {
          local = await this.getOrNull(`${LOCAL_SUPERVISOR}/status`, 1500);
          if (local && local.worker_running) break;
          await new Promise(resolve => window.setTimeout(resolve, 600));
        }
        if (!local || !local.worker_running)
          throw new Error(
            "Supervisor started, but the worker is not running. Check the PC worker log."
          );
        this.showNotice(
          "PC worker started. Waiting for the microscope heartbeat…"
        );
      } catch (error) {
        this.showNotice(this.errorMessage(error), true);
      } finally {
        this.busyAction = "";
        await this.refreshAll();
      }
    },
    async saveWorkspace() {
      const path = this.workspacePath.trim();
      if (!(path.startsWith("/") || path.startsWith("~/"))) {
        this.showNotice(
          "Enter an absolute path or a path beginning with ~/.",
          true
        );
        return;
      }
      if (
        this.localPc &&
        path !== this.localPc.workspace &&
        !window.confirm(
          "Use this new workspace? Existing Canvas records will stay at the previous path. A running worker will restart."
        )
      )
        return;
      this.busyAction = "workspace";
      try {
        const response = await axios.post(
          `${LOCAL_SUPERVISOR}/workspace`,
          { path },
          { timeout: 15000 }
        );
        if (!response.data || !response.data.ok)
          throw new Error("Workspace update failed.");
        this.workspacePath = response.data.workspace;
        this.showNotice(
          `Canvas workspace set to ${response.data.workspace}. Existing records were not moved.`
        );
      } catch (error) {
        this.showNotice(this.errorMessage(error), true);
      } finally {
        this.busyAction = "";
        await this.refreshAll();
      }
    },
    async saveInstallation() {
      const path = this.installationPath.trim();
      if (!(path.startsWith("/") || path.startsWith("~/"))) {
        this.showNotice(
          "Enter an absolute installation path or a path beginning with ~/.",
          true
        );
        return;
      }
      if (this.localPc && path === this.localPc.repo) {
        this.showNotice("This is already the active installation path.");
        return;
      }
      if (
        !window.confirm(
          "Prepare a new PC Worker installation at this path? The current installation and Canvas workspace will remain in place. Login startup will switch only after setup succeeds."
        )
      )
        return;
      this.busyAction = "installation";
      try {
        const response = await axios.post(
          `${LOCAL_SUPERVISOR}/installation`,
          { path },
          { timeout: 10000 }
        );
        if (!response.data || !response.data.ok)
          throw new Error("Installation could not start.");
        this.showNotice(
          "Preparing the new installation. Keep this page open to follow progress."
        );
      } catch (error) {
        this.showNotice(this.errorMessage(error), true);
      } finally {
        this.busyAction = "";
        await this.refreshStatus();
      }
    },
    async savePcUrl() {
      const url = this.pcMicroscopeUrl.trim();
      if (!/^https?:\/\/[^\s/]+\/?$/.test(url)) {
        this.showNotice(
          "Enter a microscope origin such as http://microscope.local:5000.",
          true
        );
        return;
      }
      if (
        this.localPc &&
        url.replace(/\/$/, "") !== this.localPc.microscope &&
        !window.confirm(
          "Connect this PC worker to the new microscope? A running worker will restart."
        )
      )
        return;
      this.busyAction = "pc-url";
      try {
        const response = await axios.post(
          `${LOCAL_SUPERVISOR}/microscope`,
          { url },
          { timeout: 15000 }
        );
        if (!response.data || !response.data.ok)
          throw new Error("PC connection update failed.");
        this.pcMicroscopeUrl = response.data.microscope;
        this.showNotice(
          "PC microscope connection saved. Check the connection status above."
        );
      } catch (error) {
        this.showNotice(this.errorMessage(error), true);
      } finally {
        this.busyAction = "";
        await this.refreshAll();
      }
    },
    async saveQuota() {
      const amount = Number(this.quotaGiB);
      if (!Number.isFinite(amount) || amount < 0.25 || amount > 64) {
        this.showNotice("Choose a limit between 0.25 and 64 GiB.", true);
        return;
      }
      const bytes = Math.round(amount * GIB);
      if (
        bytes < Number(this.piSettings.max_bytes || 0) &&
        !window.confirm(
          "Lowering the Pi storage limit may delete the oldest saved visual prompts. Continue?"
        )
      )
        return;
      this.busyAction = "quota";
      try {
        await axios.post(
          `${this.vpBase}/settings`,
          { max_bytes: bytes },
          { timeout: 12000 }
        );
        this.showNotice("Pi visual-prompt storage limit saved.");
      } catch (error) {
        this.showNotice(this.errorMessage(error), true);
      } finally {
        this.busyAction = "";
        await this.refreshAll();
      }
    }
  }
};
</script>

<style scoped>
.openmhs-settings {
  --openmhs-text: #26353d;
  --openmhs-heading: #1e3138;
  --openmhs-muted: #64747d;
  --openmhs-subtle: #718089;
  --openmhs-border: #dce5e7;
  --openmhs-card: #fff;
  --openmhs-field: #fff;
  --openmhs-field-border: #bdcbd0;
  --openmhs-button: #fff;
  --openmhs-button-hover: #f1f6f7;
  --openmhs-primary: #176c60;
  --openmhs-primary-hover: #0d554d;
  --openmhs-primary-text: #fff;
  --openmhs-disabled: #eef2f2;
  --openmhs-disabled-text: #718089;
  --openmhs-tint: #f4f7f8;
  --openmhs-onboarding: #f1f7f6;
  --openmhs-link: #125f7b;
  --openmhs-success: #197e69;
  --openmhs-notice: #e9f6f0;
  --openmhs-notice-text: #24654f;
  --openmhs-error: #fff0ed;
  --openmhs-error-text: #a64839;
  max-width: 1080px;
  padding: 8px 12px 40px;
  color: var(--openmhs-text);
  color-scheme: light;
}
.uk-light .openmhs-settings {
  --openmhs-text: #e7f0f1;
  --openmhs-heading: #f4f8f8;
  --openmhs-muted: #b8cacc;
  --openmhs-subtle: #a9bec1;
  --openmhs-border: #3b555b;
  --openmhs-card: #1d2b30;
  --openmhs-field: #17262b;
  --openmhs-field-border: #5b7579;
  --openmhs-button: #2a4146;
  --openmhs-button-hover: #36545a;
  --openmhs-primary: #2e9683;
  --openmhs-primary-hover: #42aa96;
  --openmhs-primary-text: #071d1a;
  --openmhs-disabled: #293b40;
  --openmhs-disabled-text: #b1c2c3;
  --openmhs-tint: #24373c;
  --openmhs-onboarding: #1f3b3b;
  --openmhs-link: #88d3ee;
  --openmhs-success: #73d9b9;
  --openmhs-notice: #20493d;
  --openmhs-notice-text: #b9efcf;
  --openmhs-error: #54312f;
  --openmhs-error-text: #ffd1c7;
  color-scheme: dark;
}
.section-heading,
.card-heading,
.status-topline {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 20px;
}
.section-heading {
  margin-bottom: 24px;
}
.eyebrow {
  margin: 0 0 4px;
  color: var(--openmhs-success);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.13em;
  text-transform: uppercase;
}
h2 {
  margin: 0 0 6px;
  font-size: 26px;
  font-weight: 650;
  color: var(--openmhs-heading);
}
h3 {
  margin: 0 0 4px;
  font-size: 19px;
  font-weight: 650;
  color: var(--openmhs-heading);
}
.subtitle,
.card-heading p {
  margin: 0;
  color: var(--openmhs-muted);
  line-height: 1.45;
}
.status-grid,
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.status-grid {
  margin-bottom: 16px;
}
.status-card,
.settings-card {
  border: 1px solid var(--openmhs-border);
  border-radius: 15px;
  background: var(--openmhs-card);
  box-shadow: 0 3px 16px rgba(25, 55, 66, 0.035);
}
.status-card {
  padding: 18px 20px;
}
.status-name {
  font-weight: 650;
}
.state {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--openmhs-subtle);
  white-space: nowrap;
}
.state.online {
  color: var(--openmhs-success);
}
.state-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #a6b2b7;
}
.state.online .state-dot {
  background: #2fb58e;
}
.status-detail {
  margin: 18px 0 4px;
  overflow-wrap: anywhere;
  font-size: 14px;
}
.status-caption,
.helper {
  margin: 0;
  color: var(--openmhs-subtle);
  font-size: 12px;
  line-height: 1.55;
}
.settings-card {
  margin: 16px 0;
  padding: 22px;
}
.install-field {
  margin-bottom: 22px;
}
.onboarding {
  margin-bottom: 20px;
  padding: 16px;
  border-radius: 10px;
  background: var(--openmhs-onboarding);
}
.openmhs-settings a {
  color: var(--openmhs-link);
  text-decoration: underline;
}
.openmhs-settings a:hover {
  color: var(--openmhs-link);
  text-decoration-thickness: 2px;
}
.onboarding p {
  margin: 5px 0 0;
  font-size: 13px;
  line-height: 1.5;
}
.card-heading {
  margin-bottom: 21px;
}
.field label,
.field-label {
  display: block;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 650;
}
.field .uk-input {
  border-radius: 9px;
  height: 38px;
  font-size: 13px;
  border-color: var(--openmhs-field-border);
  background: var(--openmhs-field);
  color: var(--openmhs-text);
}
.field .uk-input::placeholder {
  color: var(--openmhs-subtle);
  opacity: 1;
}
.field .uk-input:focus {
  border-color: var(--openmhs-primary);
  background: var(--openmhs-field);
  color: var(--openmhs-text);
  outline: 2px solid var(--openmhs-primary);
  outline-offset: 1px;
}
.field .uk-input:disabled {
  border-color: var(--openmhs-border);
  background: var(--openmhs-disabled);
  color: var(--openmhs-disabled-text);
  opacity: 1;
}
.field .helper {
  margin: 8px 0 12px;
}
.number-input {
  max-width: 180px;
}
.read-only-path {
  margin: 0;
  padding: 10px 12px;
  border-radius: 9px;
  background: var(--openmhs-tint);
  color: var(--openmhs-text);
  overflow-wrap: anywhere;
  font-size: 13px;
}
.compact-button {
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  text-transform: none;
  white-space: nowrap;
}
.openmhs-settings .uk-button-default {
  border: 1px solid var(--openmhs-field-border);
  background: var(--openmhs-button);
  color: var(--openmhs-text);
}
.openmhs-settings .uk-button-default:hover,
.openmhs-settings .uk-button-default:focus-visible {
  border-color: var(--openmhs-primary);
  background: var(--openmhs-button-hover);
  color: var(--openmhs-heading);
}
.openmhs-settings .uk-button-primary {
  border: 1px solid var(--openmhs-primary);
  background: var(--openmhs-primary);
  color: var(--openmhs-primary-text);
}
.openmhs-settings .uk-button-primary:hover,
.openmhs-settings .uk-button-primary:focus-visible {
  border-color: var(--openmhs-primary-hover);
  background: var(--openmhs-primary-hover);
  color: var(--openmhs-primary-text);
}
.openmhs-settings .uk-button:disabled {
  border-color: var(--openmhs-border);
  background: var(--openmhs-disabled);
  color: var(--openmhs-disabled-text);
  opacity: 1;
  cursor: not-allowed;
}
.notice {
  margin: 0 0 16px;
  padding: 11px 14px;
  border-radius: 9px;
  background: var(--openmhs-notice);
  color: var(--openmhs-notice-text);
  font-size: 13px;
}
.notice.error {
  background: var(--openmhs-error);
  color: var(--openmhs-error-text);
}
code {
  color: inherit;
}
@media (max-width: 850px) {
  .status-grid,
  .form-grid {
    grid-template-columns: 1fr;
  }
  .section-heading,
  .card-heading {
    flex-wrap: wrap;
  }
  .openmhs-settings {
    padding: 4px 0 32px;
  }
}
</style>
