<template>
  <div class="visual-prompt-shell">
    <button
      :class="['visual-prompt-toggle', { active: selecting }]"
      type="button"
      :aria-pressed="selecting ? 'true' : 'false'"
      @click="beginSelection"
    >
      <i class="material-icons">center_focus_strong</i>
      <span>{{ selecting ? "Drag on the live view" : "Visual Prompt" }}</span>
      <span :class="['bridge-dot', connected ? 'online' : 'offline']"></span>
    </button>

    <div v-if="selecting && !dragging" class="selection-hint">
      <i class="material-icons">crop_free</i>
      Click or drag around an object
      <button
        type="button"
        aria-label="Cancel selection"
        @click="cancelSelection"
      >
        <i class="material-icons">close</i>
      </button>
    </div>

    <div
      v-if="roi"
      class="roi-outline"
      :style="roiStyle"
      aria-label="Selected microscope region"
    >
      <span class="roi-label">ROI</span>
    </div>

    <section
      v-if="roi"
      class="prompt-popover"
      :style="popoverStyle"
      aria-label="Visual prompt editor"
    >
      <header>
        <div>
          <strong>Visual Prompt</strong>
          <small>{{
            connected ? "Saved on microscope (Pi)" : "Microscope VP API offline"
          }}</small>
        </div>
        <button
          type="button"
          aria-label="Close visual prompt"
          @click="clearRoi"
        >
          <i class="material-icons">close</i>
        </button>
      </header>

      <div class="shortcut-grid">
        <button
          v-for="shortcut in shortcuts"
          :key="shortcut.id"
          :class="{ selected: action === shortcut.id }"
          type="button"
          :title="shortcut.help"
          @click="action = shortcut.id"
        >
          <i class="material-icons">{{ shortcut.icon }}</i>
          <span>{{ shortcut.label }}</span>
        </button>
      </div>

      <textarea
        v-model="note"
        class="vp-note"
        rows="2"
        maxlength="1000"
        placeholder="Add context or a question for Codex / Cursor…"
      ></textarea>

      <p v-if="message" :class="['prompt-message', { error: messageError }]">
        {{ message }}
      </p>

      <footer>
        <button class="secondary" type="button" @click="beginSelection">
          Reselect
        </button>
        <button
          class="primary"
          type="button"
          :disabled="saving || !connected"
          @click="savePrompt"
        >
          <i class="material-icons">{{
            saving ? "hourglass_empty" : "send"
          }}</i>
          {{ saving ? "Saving…" : "Save to microscope" }}
        </button>
      </footer>
    </section>

    <transition name="toast">
      <div v-if="toast" class="visual-prompt-toast">
        <i class="material-icons">check_circle</i>
        <div>
          <strong>Visual prompt ready</strong>
          <small>{{ toast }}</small>
        </div>
      </div>
    </transition>
  </div>
</template>

<script>
import axios from "axios";

const VP_EXT = "org.openmhs.visual-prompts";

export default {
  name: "VisualPrompt",
  data: function() {
    return {
      connected: false,
      selecting: false,
      dragging: false,
      dragStart: null,
      dragEnd: null,
      imageElement: null,
      imageRect: null,
      roi: null,
      action: "reference",
      note: "",
      saving: false,
      message: "",
      messageError: false,
      toast: "",
      toastTimer: null,
      healthTimer: null,
      shortcuts: [
        {
          id: "reference",
          label: "Reference",
          icon: "search",
          help: "Identify or explain this structure"
        },
        {
          id: "compare",
          label: "Compare",
          icon: "compare",
          help: "Compare with prior references"
        },
        {
          id: "focus",
          label: "Focus",
          icon: "filter_center_focus",
          help: "Assess focus quality in this region"
        },
        {
          id: "measure",
          label: "Measure",
          icon: "straighten",
          help: "Estimate size when calibration is available"
        }
      ]
    };
  },
  computed: {
    vpBase: function() {
      const base = (this.$store.getters.baseUri || "").replace(/\/$/, "");
      return `${base}/api/v2/extensions/${VP_EXT}`;
    },
    roiStyle: function() {
      if (!this.roi) return {};
      return {
        left: `${this.roi.left}px`,
        top: `${this.roi.top}px`,
        width: `${this.roi.width}px`,
        height: `${this.roi.height}px`
      };
    },
    popoverStyle: function() {
      if (!this.roi) return {};
      const width = Math.min(350, window.innerWidth - 24);
      let left = this.roi.left + this.roi.width + 12;
      if (left + width > window.innerWidth - 12)
        left = this.roi.left - width - 12;
      left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
      const top = Math.max(
        12,
        Math.min(this.roi.top, window.innerHeight - 470)
      );
      return { left: `${left}px`, top: `${top}px`, width: `${width}px` };
    }
  },
  mounted() {
    document.addEventListener("pointerdown", this.pointerDown, true);
    document.addEventListener("pointermove", this.pointerMove, true);
    document.addEventListener("pointerup", this.pointerUp, true);
    document.addEventListener("dblclick", this.blockDoubleClick, true);
    window.addEventListener("resize", this.clearRoi);
    this.checkHealth();
    this.healthTimer = setInterval(this.checkHealth, 15000);
  },
  beforeDestroy() {
    document.removeEventListener("pointerdown", this.pointerDown, true);
    document.removeEventListener("pointermove", this.pointerMove, true);
    document.removeEventListener("pointerup", this.pointerUp, true);
    document.removeEventListener("dblclick", this.blockDoubleClick, true);
    window.removeEventListener("resize", this.clearRoi);
    clearInterval(this.healthTimer);
    clearTimeout(this.toastTimer);
  },
  methods: {
    checkHealth: async function() {
      try {
        const response = await axios.get(`${this.vpBase}/status`, {
          timeout: 3500
        });
        this.connected = !!(response.data && response.data.ok);
      } catch (_) {
        this.connected = false;
      }
    },
    beginSelection: function() {
      this.clearRoi();
      this.selecting = true;
      this.messageError = false;
    },
    cancelSelection: function() {
      this.selecting = false;
      this.dragging = false;
    },
    clearRoi: function() {
      this.selecting = false;
      this.dragging = false;
      this.dragStart = null;
      this.dragEnd = null;
      this.imageElement = null;
      this.imageRect = null;
      this.roi = null;
      this.message = "";
    },
    streamImageFromEvent: function(event) {
      const target = event.target;
      if (!target || target.tagName !== "IMG") return null;
      if (!target.closest || !target.closest(".stream-display")) return null;
      if (!target.naturalWidth || !target.naturalHeight) return null;
      return target;
    },
    displayedImageRect: function(image) {
      const box = image.getBoundingClientRect();
      const scale = Math.min(
        box.width / image.naturalWidth,
        box.height / image.naturalHeight
      );
      const width = image.naturalWidth * scale;
      const height = image.naturalHeight * scale;
      return {
        left: box.left + (box.width - width) / 2,
        top: box.top + (box.height - height) / 2,
        width: width,
        height: height
      };
    },
    clampPoint: function(event) {
      const rect = this.imageRect;
      return {
        x: Math.max(rect.left, Math.min(event.clientX, rect.left + rect.width)),
        y: Math.max(rect.top, Math.min(event.clientY, rect.top + rect.height))
      };
    },
    pointerDown: function(event) {
      if (!this.selecting || event.button !== 0) return;
      const image = this.streamImageFromEvent(event);
      if (!image) return;
      this.imageElement = image;
      this.imageRect = this.displayedImageRect(image);
      const point = this.clampPoint(event);
      this.dragStart = point;
      this.dragEnd = point;
      this.dragging = true;
      this.roi = { left: point.x, top: point.y, width: 1, height: 1 };
      event.preventDefault();
      event.stopPropagation();
    },
    pointerMove: function(event) {
      if (!this.dragging) return;
      this.dragEnd = this.clampPoint(event);
      this.updateRoi();
      event.preventDefault();
      event.stopPropagation();
    },
    pointerUp: function(event) {
      if (!this.dragging) return;
      this.dragEnd = this.clampPoint(event);
      this.updateRoi(true);
      this.dragging = false;
      this.selecting = false;
      event.preventDefault();
      event.stopPropagation();
    },
    blockDoubleClick: function(event) {
      if (
        (!this.selecting && !this.dragging) ||
        !this.streamImageFromEvent(event)
      )
        return;
      event.preventDefault();
      event.stopPropagation();
    },
    updateRoi: function(finish) {
      let left = Math.min(this.dragStart.x, this.dragEnd.x);
      let top = Math.min(this.dragStart.y, this.dragEnd.y);
      let width = Math.abs(this.dragEnd.x - this.dragStart.x);
      let height = Math.abs(this.dragEnd.y - this.dragStart.y);
      if (finish && width < 10 && height < 10) {
        const size = Math.min(96, this.imageRect.width, this.imageRect.height);
        left = Math.max(
          this.imageRect.left,
          Math.min(
            this.dragStart.x - size / 2,
            this.imageRect.left + this.imageRect.width - size
          )
        );
        top = Math.max(
          this.imageRect.top,
          Math.min(
            this.dragStart.y - size / 2,
            this.imageRect.top + this.imageRect.height - size
          )
        );
        width = size;
        height = size;
      }
      this.roi = {
        left: left,
        top: top,
        width: Math.max(1, width),
        height: Math.max(1, height)
      };
    },
    imageData: function() {
      const image = this.imageElement;
      if (!image || !this.roi || !this.imageRect)
        throw new Error("Select a region on the live microscope view first.");
      const normalized = {
        x: (this.roi.left - this.imageRect.left) / this.imageRect.width,
        y: (this.roi.top - this.imageRect.top) / this.imageRect.height,
        width: this.roi.width / this.imageRect.width,
        height: this.roi.height / this.imageRect.height
      };
      Object.keys(normalized).forEach(key => {
        normalized[key] = Math.max(0, Math.min(1, normalized[key]));
      });
      normalized.width = Math.min(normalized.width, 1 - normalized.x);
      normalized.height = Math.min(normalized.height, 1 - normalized.y);
      const frame = document.createElement("canvas");
      frame.width = image.naturalWidth;
      frame.height = image.naturalHeight;
      frame.getContext("2d").drawImage(image, 0, 0, frame.width, frame.height);
      const sx = Math.round(normalized.x * frame.width);
      const sy = Math.round(normalized.y * frame.height);
      const sw = Math.max(1, Math.round(normalized.width * frame.width));
      const sh = Math.max(1, Math.round(normalized.height * frame.height));
      const crop = document.createElement("canvas");
      crop.width = sw;
      crop.height = sh;
      crop.getContext("2d").drawImage(frame, sx, sy, sw, sh, 0, 0, sw, sh);
      return {
        roi: normalized,
        frame: frame.toDataURL("image/jpeg", 0.9),
        crop: crop.toDataURL("image/jpeg", 0.94)
      };
    },
    copyText: async function(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
      const element = document.createElement("textarea");
      element.value = text;
      element.style.position = "fixed";
      element.style.opacity = "0";
      document.body.appendChild(element);
      element.select();
      const copied = document.execCommand("copy");
      document.body.removeChild(element);
      return copied;
    },
    savePrompt: async function() {
      this.saving = true;
      this.message = "";
      this.messageError = false;
      try {
        const image = this.imageData();
        const response = await axios.post(
          `${this.vpBase}/prompts`,
          {
            action: this.action,
            note: this.note,
            roi: image.roi,
            frame_data_url: image.frame,
            roi_data_url: image.crop
          },
          { timeout: 30000 }
        );
        let copied = false;
        try {
          copied = await this.copyText(response.data.prompt);
        } catch (_) {
          copied = false;
        }
        const promptId = response.data.id || "latest";
        this.message = copied
          ? `Saved on Pi (${promptId}). Analyze via MCP get_latest_visual_prompt.`
          : `Saved on Pi (${promptId}). In Cursor/Codex use MCP get_latest_visual_prompt.`;
        this.toast = `id=${promptId}`;
        clearTimeout(this.toastTimer);
        this.toastTimer = setTimeout(() => {
          this.toast = "";
        }, 7000);
      } catch (error) {
        this.messageError = true;
        this.message =
          (error.response &&
            error.response.data &&
            error.response.data.error) ||
          error.message ||
          "Unable to save the visual prompt on the microscope.";
      } finally {
        this.saving = false;
      }
    }
  }
};
</script>

<style scoped>
.visual-prompt-toggle {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 990;
  min-height: 48px;
  padding: 0 17px;
  border: 0;
  border-radius: 25px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: #fff;
  background: #1e87f0;
  box-shadow: 0 7px 24px rgba(0, 0, 0, 0.3);
  cursor: pointer;
  font-weight: 600;
}
.visual-prompt-toggle.active {
  background: #e67e22;
}
.bridge-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}
.bridge-dot.online {
  background: #62dc91;
}
.bridge-dot.offline {
  background: #ff7373;
}
.selection-hint {
  position: fixed;
  z-index: 1102;
  top: 18px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-radius: 22px;
  color: #fff;
  background: rgba(23, 29, 36, 0.92);
  box-shadow: 0 5px 20px rgba(0, 0, 0, 0.25);
}
.selection-hint button,
.prompt-popover header button {
  border: 0;
  padding: 2px;
  color: inherit;
  background: transparent;
  cursor: pointer;
}
.selection-hint i {
  font-size: 19px;
}
.roi-outline {
  position: fixed;
  z-index: 1098;
  box-sizing: border-box;
  border: 2px solid #ffc107;
  background: rgba(255, 193, 7, 0.08);
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.45), inset 0 0 0 1px rgba(0, 0, 0, 0.35);
  pointer-events: none;
}
.roi-label {
  position: absolute;
  left: -2px;
  top: -23px;
  padding: 2px 7px;
  color: #151515;
  background: #ffc107;
  border-radius: 4px 4px 0 0;
  font-size: 11px;
  font-weight: 700;
}
.prompt-popover {
  position: fixed;
  z-index: 1100;
  box-sizing: border-box;
  padding: 13px;
  border-radius: 14px;
  color: #1f2933;
  background: rgba(255, 255, 255, 0.98);
  box-shadow: 0 12px 38px rgba(0, 0, 0, 0.34);
}
.prompt-popover header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 11px;
}
.prompt-popover header strong,
.prompt-popover header small {
  display: block;
}
.prompt-popover header small {
  margin-top: 2px;
  color: #697586;
  font-size: 11px;
}
.shortcut-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  margin-bottom: 9px;
}
.shortcut-grid button {
  min-width: 0;
  padding: 8px 4px 6px;
  border: 1px solid #d8dee7;
  border-radius: 9px;
  color: #44505f;
  background: #f7f9fb;
  cursor: pointer;
}
.shortcut-grid button i,
.shortcut-grid button span {
  display: block;
  margin: auto;
}
.shortcut-grid button i {
  font-size: 20px;
}
.shortcut-grid button span {
  margin-top: 2px;
  font-size: 11px;
}
.shortcut-grid button.selected {
  border-color: #1e87f0;
  color: #0f6fce;
  background: #eaf4ff;
  box-shadow: inset 0 0 0 1px #1e87f0;
}
.prompt-popover textarea {
  box-sizing: border-box;
  width: 100%;
  min-height: 60px;
  padding: 8px 9px;
  resize: vertical;
  border: 1px solid #ccd4df;
  border-radius: 8px;
  font: inherit;
  font-size: 13px;
}
.prompt-message {
  margin: 8px 0 0;
  color: #23824f;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.prompt-message.error {
  color: #d4385f;
}
.prompt-popover footer {
  display: flex;
  justify-content: flex-end;
  gap: 7px;
  margin-top: 10px;
}
.prompt-popover footer button {
  min-height: 36px;
  border-radius: 8px;
  padding: 0 11px;
  cursor: pointer;
}
.prompt-popover footer .secondary {
  border: 1px solid #ccd4df;
  color: #44505f;
  background: #fff;
}
.prompt-popover footer .primary {
  border: 0;
  display: flex;
  align-items: center;
  gap: 5px;
  color: #fff;
  background: #1e87f0;
}
.prompt-popover footer .primary:disabled {
  opacity: 0.5;
  cursor: default;
}
.prompt-popover footer i {
  font-size: 18px;
}
.visual-prompt-toast {
  position: fixed;
  z-index: 1110;
  right: 18px;
  bottom: 78px;
  max-width: min(440px, calc(100vw - 36px));
  display: flex;
  gap: 9px;
  align-items: flex-start;
  padding: 11px 13px;
  border-radius: 10px;
  color: #fff;
  background: rgba(27, 43, 35, 0.95);
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.28);
}
.visual-prompt-toast strong,
.visual-prompt-toast small {
  display: block;
}
.visual-prompt-toast small {
  margin-top: 2px;
  opacity: 0.8;
  overflow-wrap: anywhere;
}
.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.2s, transform 0.2s;
}
.toast-enter,
.toast-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
@media (max-width: 640px) {
  .visual-prompt-toggle span:not(.bridge-dot) {
    display: none;
  }
  .visual-prompt-toggle {
    padding: 0 14px;
  }
  .prompt-popover {
    max-height: calc(100vh - 24px);
    overflow-y: auto;
  }
}
</style>
