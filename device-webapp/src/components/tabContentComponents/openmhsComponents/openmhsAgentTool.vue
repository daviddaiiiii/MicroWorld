<template>
  <div class="agent-tool-root">
    <!-- ROI panel only; FAB lives in Content stage-roi-dock -->
    <div
      v-if="roiPanelOpen"
      class="roi-panel"
      :style="roiPanelStyle"
      @pointerdown.stop
    >
      <header class="roi-panel-head" @pointerdown="startRoiPanelDrag">
        <strong>ROI</strong>
        <button
          type="button"
          class="icon-btn"
          title="Close"
          @click="roiPanelOpen = false"
        >
          <i class="material-icons">close</i>
        </button>
      </header>
      <div class="agent-tools" :title="toolHint">
        <div class="tool-row">
          <button
            v-for="t in drawTools"
            :key="t.id"
            type="button"
            :class="{ active: drawing && drawTool === t.id }"
            :title="t.label"
            @click="setDrawTool(t.id)"
          >
            <i class="material-icons">{{ t.icon }}</i>
          </button>
          <span class="tool-sep" />
          <button
            type="button"
            :class="{ active: roiRole === 'target' }"
            title="Target ROI"
            @click="setRole('target')"
          >
            Target
          </button>
          <button
            type="button"
            :class="{ active: roiRole === 'source' }"
            title="Source exemplar"
            @click="setRole('source')"
          >
            Source
          </button>
          <button
            v-if="drawing"
            type="button"
            class="stop-draw"
            title="Stop drawing"
            @click="stopDrawing"
          >
            Done
          </button>
          <span class="tool-sep" />
          <button
            type="button"
            class="clear-btn"
            title="Clear all ROIs"
            :disabled="!rois.length"
            @click="clearRois"
          >
            Clear ROI
          </button>
          <button
            type="button"
            class="clear-btn"
            title="Clear result overlay / mask"
            :disabled="!hasResultOverlay"
            @click="clearResults"
          >
            Clear Result
          </button>
        </div>
        <p class="vp-guidance">Draw here. Ask in Codex.</p>
        <button
          v-if="messageError"
          type="button"
          :disabled="saving || !connected"
          @click="syncSelection"
        >
          Retry sync
        </button>
        <p v-if="message">{{ message }}</p>
        <button
          v-if="surfaceKind === 'mosaic'"
          type="button"
          :disabled="!rois.length"
          @click="requestRoiRetake"
        >
          Retake ROI…
        </button>
        <small
          >Selections sync automatically. In Codex, say “look at my current
          selection”. No question or manual save is needed here.</small
        >
        <div v-if="rois.length" class="roi-chips">
          <span v-for="(r, i) in rois" :key="i" class="chip" :class="r.role">
            {{ r.role }} · {{ r.shape }}
            <button type="button" @click="removeRoi(i)">×</button>
          </span>
        </div>
      </div>
    </div>

    <div class="codex-status-panel" @pointerdown.stop>
      <span
        :title="
          codexLastRead
            ? 'Last tool read: ' + codexLastRead
            : 'No tool read yet'
        "
      >
        {{ codexConnected ? "● PC connect" : "○ PC offline" }}
      </span>
      <button type="button" @click="openRoiPanel">Visual prompt</button>
      <details v-if="false" class="codex-materials" @toggle="onMaterialsToggle">
        <summary>Files</summary>
        <div class="workspace-bar" :title="agentWorkspace || 'PC workspace'">
          <i class="material-icons">folder_open</i>
          <input
            v-model="workspaceDraft"
            class="workspace-input"
            type="text"
            spellcheck="false"
            placeholder="PC workspace path…"
            :disabled="workspaceSaving"
            @keydown.enter.prevent="applyWorkspace"
          />
          <button
            type="button"
            class="workspace-apply"
            :disabled="workspaceSaving || !workspaceDraftTrimmed"
            title="Set workspace on this PC (restarts worker)"
            @click="applyWorkspace"
          >
            {{ workspaceSaving ? "…" : "Set" }}
          </button>
        </div>
        <div class="view-tree-wrap">
          <div class="ws-tree-head">
            <span>Cameras</span>
            <div class="head-actions">
              <button
                type="button"
                class="icon-btn"
                title="New Camera (= new chat)"
                :disabled="!agentWorkerOnline"
                @click="$emit('new-camera-chat')"
              >
                <i class="material-icons">add</i>
              </button>
              <button
                v-if="legacyMosaics.length || cameraViews.length > 1"
                type="button"
                class="icon-btn"
                title="Import Canvas from leftovers / other Cameras"
                :disabled="!agentWorkerOnline || !currentViewId"
                @click="$emit('import-canvas')"
              >
                <i class="material-icons">unarchive</i>
              </button>
              <button
                type="button"
                class="icon-btn"
                title="Refresh"
                @click="loadViewsTree"
              >
                <i class="material-icons">refresh</i>
              </button>
            </div>
          </div>
          <div v-if="!agentWorkerOnline" class="ws-tree-empty">
            Agent greyed until PC worker is online
          </div>
          <div v-else-if="!cameraViews.length" class="ws-tree-empty">
            Click + to start a Camera (chat)
          </div>
          <div v-else class="ws-tree view-tree">
            <div v-for="v in cameraViews" :key="v.id" class="view-group">
              <div
                class="ws-row view-row"
                :class="{ active: v.id === currentViewId }"
                :title="v.id"
                @click="$emit('select-camera-view', v.id)"
              >
                <i class="material-icons">videocam</i>
                <span>{{ v.title || v.id }}</span>
              </div>
              <div
                v-for="r in v.records || []"
                :key="r.id"
                class="ws-row record-row"
                :class="{
                  active: mosaicSessionId && r.id === mosaicSessionId,
                  video: r.kind === 'video'
                }"
                :title="(r.relpath || r.id) + ' — drag into chat as context'"
                draggable="true"
                @dragstart="onCanvasDragStart(r, $event)"
                @click="onRecordClick(r)"
              >
                <i class="material-icons">{{
                  r.kind === "video" ? "movie" : "collections"
                }}</i>
                <span>{{ r.title || r.id }}</span>
              </div>
            </div>
            <div v-if="legacyMosaics.length" class="legacy-label">
              Legacy mosaics
            </div>
            <div
              v-for="r in legacyMosaics"
              :key="'legacy-' + r.id"
              class="ws-row record-row legacy"
              :title="'Drag into chat as context'"
              draggable="true"
              @dragstart="onCanvasDragStart(r, $event)"
              @click="onRecordClick(r)"
            >
              <i class="material-icons">collections</i>
              <span>{{ r.title || r.id }}</span>
            </div>
          </div>
        </div>
        <div class="ws-tree-wrap">
          <div class="ws-tree-head">
            <span>Workspace</span>
            <button
              type="button"
              class="icon-btn"
              title="Refresh"
              @click="loadWorkspaceTree"
            >
              <i class="material-icons">refresh</i>
            </button>
          </div>
          <div v-if="treeError" class="ws-tree-err">{{ treeError }}</div>
          <div
            v-else-if="treeLoading && !visibleTreeRows.length"
            class="ws-tree-empty"
          >
            Loading…
          </div>
          <div v-else-if="!agentWorkerOnline" class="ws-tree-empty">
            Connect PC worker to browse workspace
          </div>
          <div v-else class="ws-tree">
            <div
              v-for="row in visibleTreeRows"
              :key="row.path"
              class="ws-row"
              :class="{
                dir: row.type === 'dir',
                mosaic: isMosaicDir(row.path),
                active:
                  mosaicSessionId &&
                  sessionIdFromPath(row.path) === mosaicSessionId
              }"
              :style="{ paddingLeft: 6 + row.depth * 12 + 'px' }"
              :title="row.path"
              draggable="true"
              @dragstart="onTreeDragStart(row, $event)"
              @click="onTreeClick(row)"
            >
              <i class="material-icons">{{ treeIcon(row) }}</i>
              <span>{{ row.name }}</span>
            </div>
          </div>
        </div>
      </details>
      <small v-if="codexLastRead">Last read: {{ codexLastRead }}</small>
    </div>

    <div class="roi-layer" :style="overlayStyle">
      <img
        v-if="maskOverlayVisible"
        class="mask-overlay-img"
        :class="{ 'is-ready': overlayReady }"
        :src="maskOverlayUrl"
        :style="maskOverlayStyle"
        alt=""
      />

      <!-- Interactive ROI chrome: drag / delete / copy crop -->
      <template v-for="(r, i) in rois">
        <div
          v-if="open && overlayReady && r.px"
          :key="'rw' + i"
          class="roi-widget"
          :class="[
            r.role,
            { active: selectedRoi === i, dragging: draggingRoi === i }
          ]"
          :style="roiWidgetStyle(r)"
          @pointerdown.stop="onRoiPointerDown(i, $event)"
        >
          <span class="roi-tag">{{ r.role }}</span>
          <button
            type="button"
            class="roi-btn"
            title="Copy ROI crop"
            @click.stop.prevent="copyRoiCrop(i)"
          >
            <i class="material-icons">content_copy</i>
          </button>
          <button
            type="button"
            class="roi-btn"
            title="Delete ROI"
            @click.stop.prevent="removeRoi(i)"
          >
            <i class="material-icons">close</i>
          </button>
        </div>
      </template>

      <!-- ROI + result annotation overlays -->
      <svg
        v-if="overlayVisible"
        class="roi-overlay"
        :class="{ 'is-ready': overlayReady }"
      >
        <template v-for="(r, i) in rois">
          <rect
            v-if="r.shape === 'bbox' && r.px"
            :key="'b' + i"
            :x="localPx(r.px).x"
            :y="localPx(r.px).y"
            :width="r.px.w"
            :height="r.px.h"
            :class="r.role"
          />
          <polyline
            v-else-if="r.px || r.pointsPx"
            :key="'p' + i"
            :points="polyPoints(r)"
            :class="[r.role, r.shape]"
            fill="none"
          />
        </template>
        <template v-for="(a, i) in resultAnnotationsLocal">
          <polygon
            v-if="
              (a.type === 'mask' || a.type === 'polygon') &&
                a.pointsPx &&
                a.pointsPx.length >= 3
            "
            :key="'rm' + i"
            :points="a.pointsPx.map(p => p.x + ',' + p.y).join(' ')"
            class="result-ann mask"
            :style="maskStyle(a)"
          />
          <rect
            v-else-if="a.type === 'bbox' && a.px"
            :key="'rb' + i"
            :x="a.px.x"
            :y="a.px.y"
            :width="a.px.w"
            :height="a.px.h"
            class="result-ann"
            :style="a.color ? { stroke: a.color } : null"
          />
          <circle
            v-else-if="a.type === 'point' && a.px"
            :key="'rp' + i"
            :cx="a.px.x"
            :cy="a.px.y"
            r="5"
            class="result-ann point"
            :style="a.color ? { stroke: a.color, fill: a.color } : null"
          />
          <polyline
            v-else-if="
              (a.type === 'polyline' || a.type === 'draw') && a.pointsPx
            "
            :key="'rl' + i"
            :points="a.pointsPx.map(p => p.x + ',' + p.y).join(' ')"
            class="result-ann"
            fill="none"
            :style="a.color ? { stroke: a.color } : null"
          />
          <text
            v-if="a.label && a.px && a.type !== 'mask'"
            :key="'rt' + i"
            :x="a.px.x"
            :y="Math.max(12, a.px.y - 4)"
            class="result-label"
          >
            {{ a.label }}
          </text>
        </template>
        <rect
          v-if="draft && draft.shape === 'bbox' && draft.px"
          :x="localPx(draft.px).x"
          :y="localPx(draft.px).y"
          :width="draft.px.w"
          :height="draft.px.h"
          class="draft"
        />
        <polyline
          v-if="draft && draft.shape !== 'bbox' && draft.pointsPx"
          :points="
            localPoints(draft.pointsPx)
              .map(p => p.x + ',' + p.y)
              .join(' ')
          "
          :class="['draft', draft.shape]"
          fill="none"
        />
        <g v-if="draft && draft.shape === 'pen' && draft.pointsPx">
          <circle
            v-for="(p, i) in localPoints(draft.pointsPx)"
            :key="'v' + i"
            :cx="p.x"
            :cy="p.y"
            r="3.5"
            class="pen-vertex"
          />
        </g>
      </svg>
    </div>
  </div>
</template>

<script>
import axios from "axios";

const VP_EXT = "org.openmhs.visual-prompts";

export default {
  name: "OpenmhsAgentTool",
  props: {
    surfaceContext: { type: Object, default: () => ({}) },
    surfaceSelector: {
      type: String,
      default: ".openmhs-main-surface img"
    },
    surfaceKind: {
      type: String,
      default: "live"
    },
    /** Changes when mosaic preview URL / layout surface changes */
    surfaceVersion: {
      type: String,
      default: ""
    },
    mosaicSessionId: {
      type: String,
      default: ""
    },
    currentViewId: {
      type: String,
      default: ""
    },
    currentViewTitle: {
      type: String,
      default: ""
    }
  },
  data() {
    return {
      codexMode: true,
      codexConnected: false,
      codexLastRead: null,
      open: true,
      roiPanelOpen: false,
      roiFabPos: { x: 12, y: null },
      roiPanelPos: { x: null, y: null },
      draggingRoiFab: false,
      draggingRoiPanel: false,
      roiFabMoved: false,
      sidebarWidthPx: 400,
      resizingSidebar: false,
      resizeStartX: 0,
      resizeStartW: 400,
      connected: false,
      agentWorkerOnline: false,
      agentSupervisorOk: false,
      agentHealthy: false,
      agentErrorType: "",
      agentWorkspace: "",
      workspaceDraft: "",
      workspaceSaving: false,
      treeKids: {},
      treeOpen: {},
      treeLoading: false,
      treeError: "",
      cameraViews: [],
      legacyMosaics: [],
      viewsLoading: false,
      agentError: "",
      agentAgeS: null,
      localStarting: false,
      localRepairing: false,
      localStartHint: "",
      localStartError: false,
      localSupervisorOk: false,
      cancellingJob: false,
      uiEventAfter: 0,
      uiEventsCaughtUp: false,
      uiEventTimer: null,
      healthTimer: null,
      syncTimer: null,
      note: "",
      sessionId: null,
      chatMessages: [],
      canvasContexts: [],
      canvasDropOver: false,
      savingAnnotation: false,
      saving: false,
      message: "",
      messageError: false,
      jobId: null,
      jobStatus: "",
      jobMarkdown: "",
      jobError: "",
      jobProgress: [],
      jobTraceOpen: false,
      resultAnnotations: [],
      maskOverlayUrl: "",
      /** Per-surface bags so live ↔ scan overlays don't bleed across. */
      overlayBags: {},
      boundOverlayKey: "",
      overlayReady: true,
      overlaySwapTimer: null,
      jobPollTimer: null,
      lastAction: "agent",
      drawTool: "bbox",
      roiRole: "target",
      rois: [],
      selectionSyncTimer: null,
      selectionPending: false,
      selectionSignature: "[]",
      drawing: false,
      draft: null,
      imageEl: null,
      imageRect: null,
      boxPos: { x: 16, y: null },
      boxSize: { w: 320, h: 0 },
      resizingBox: false,
      resizeStart: null,
      fabPos: { x: 72, y: null },
      draggingBox: false,
      draggingFab: false,
      fabMoved: false,
      dragOffset: { x: 0, y: 0 },
      selectedRoi: null,
      draggingRoi: null,
      roiDragStart: null,
      lastPenClickAt: 0,
      drawTools: [
        { id: "bbox", label: "Box", icon: "crop_free" },
        { id: "draw", label: "Brush (freehand)", icon: "brush" },
        { id: "pen", label: "Pen (polyline)", icon: "timeline" }
      ]
    };
  },
  computed: {
    vpBase() {
      const base = (this.$store.getters.baseUri || "").replace(/\/$/, "");
      return `${base}/api/v2/extensions/${VP_EXT}`;
    },
    boxStyle() {
      return {};
    },
    fabStyle() {
      const y =
        this.fabPos.y == null
          ? Math.max(20, window.innerHeight - 64)
          : this.fabPos.y;
      return {
        left: `${this.fabPos.x}px`,
        top: `${y}px`,
        bottom: "auto"
      };
    },
    roiFabStyle() {
      // Bottom-left, stacked above the XYZ fab
      const y =
        this.roiFabPos.y == null
          ? Math.max(20, window.innerHeight - 168)
          : this.roiFabPos.y;
      return {
        left: `${this.roiFabPos.x != null ? this.roiFabPos.x : 12}px`,
        top: `${y}px`,
        bottom: "auto"
      };
    },
    roiPanelStyle() {
      const y =
        this.roiPanelPos.y == null
          ? Math.max(20, window.innerHeight - 280)
          : this.roiPanelPos.y;
      const x =
        this.roiPanelPos.x == null
          ? this.roiFabPos.x != null
            ? this.roiFabPos.x
            : 12
          : this.roiPanelPos.x;
      return {
        left: `${x}px`,
        top: `${y}px`,
        bottom: "auto"
      };
    },
    /** Viewport origin of the overlay layer (main surface, not the sidebar). */
    localOrigin() {
      const surface = document.querySelector(".openmhs-main-surface");
      if (surface && surface.getBoundingClientRect) {
        const r = surface.getBoundingClientRect();
        return { left: r.left, top: r.top };
      }
      const el = this.$el;
      if (!el || !el.getBoundingClientRect) return { left: 0, top: 0 };
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top };
    },
    toolHint() {
      if (!this.drawing) {
        return "Pick a draw tool, or drag existing ROI bars to move · × delete · copy crop. No ROI → whole frame.";
      }
      if (this.drawTool === "bbox") {
        return `Drag a box as ${this.roiRole}.`;
      }
      if (this.drawTool === "draw") {
        return `Brush: drag freehand as ${this.roiRole}.`;
      }
      return `Pen: click corners as ${this.roiRole}. Double-click to finish.`;
    },
    agentConnClass() {
      if (!this.connected) return "off";
      if (this.agentHealthy) return "on";
      if (this.agentWorkerOnline || this.agentSupervisorOk) return "warn";
      return "off";
    },
    agentConnText() {
      if (!this.connected) return "Pi offline";
      if (this.agentHealthy) return "PC worker online";
      const t = this.agentErrorType;
      if (t === "supervisor_down") return "Supervisor failed";
      if (t === "worker_down") return "Worker offline";
      if (this.agentError) return String(this.agentError).slice(0, 28);
      return "PC worker not ready";
    },
    agentConnDetail() {
      const bits = [];
      bits.push(
        `supervisor:${this.agentSupervisorOk ? "ok" : "fail"}`,
        `worker:${this.agentWorkerOnline ? "ok" : "fail"}`
      );
      if (this.agentErrorType) bits.push(this.agentErrorType);
      if (this.agentWorkspace) bits.push(this.agentWorkspace);
      if (this.agentAgeS != null) bits.push(`heartbeat ${this.agentAgeS}s ago`);
      if (this.agentError) bits.push(this.agentError);
      return bits.join(" · ") || "Agent connection";
    },
    shortWorkspace() {
      const p = this.agentWorkspace || "";
      if (p.length <= 42) return p;
      return "…" + p.slice(-40);
    },
    workspaceDraftTrimmed() {
      return (this.workspaceDraft || "").trim();
    },
    visibleTreeRows() {
      const rows = [];
      const walk = (path, depth) => {
        const entries = this.treeKids[path];
        if (!entries) return;
        entries.forEach(ent => {
          rows.push({
            ...ent,
            depth,
            expanded: !!this.treeOpen[ent.path]
          });
          if (ent.type === "dir" && this.treeOpen[ent.path]) {
            walk(ent.path, depth + 1);
          }
        });
      };
      walk(".", 0);
      return rows;
    },
    hasResultOverlay() {
      return !!(
        this.maskOverlayUrl ||
        (this.resultAnnotations && this.resultAnnotations.length)
      );
    },
    liveStreamText() {
      const steps = this.jobProgress || [];
      for (let i = steps.length - 1; i >= 0; i--) {
        const s = steps[i];
        if (s && s.kind === "stream" && s.message) return String(s.message);
      }
      return "";
    },
    thinkingSteps() {
      return (this.jobProgress || []).filter(
        s => s && s.kind !== "stream" && s.message
      );
    },
    threadItems() {
      const items = [];
      const think = this.thinkingSteps;
      const msgs = this.chatMessages || [];
      // Find last user message index — process trace sits after that turn
      let lastUser = -1;
      msgs.forEach((m, i) => {
        if (m && m.role === "user") lastUser = i;
      });

      msgs.forEach((m, i) => {
        if (!m || !m.content) return;
        items.push({
          key: "c" + i,
          kind: m.role === "user" ? "user" : "assistant",
          content: this.cleanAssistantText(m.content)
        });
        // After the latest user turn, show live/finished process before assistant
        if (i === lastUser && (this.jobThinking || think.length)) {
          items.push({
            key: "thinking",
            kind: "thinking",
            steps: think.length
              ? think
              : [{ message: "Waiting for PC worker…", kind: "worker" }],
            open: !!this.jobThinking
          });
          if (this.jobThinking && this.liveStreamText) {
            items.push({
              key: "stream",
              kind: "stream",
              content: this.liveStreamText
            });
          }
        }
      });

      // No messages yet but job running
      if (!msgs.length && this.jobThinking) {
        items.push({
          key: "thinking",
          kind: "thinking",
          steps: think.length
            ? think
            : [{ message: "Waiting for PC worker…", kind: "worker" }],
          open: true
        });
        if (this.liveStreamText) {
          items.push({
            key: "stream",
            kind: "stream",
            content: this.liveStreamText
          });
        }
      }

      if (
        this.jobStatus === "done" &&
        this.jobMarkdown &&
        !msgs.some(
          m =>
            m.role === "assistant" &&
            this.cleanAssistantText(m.content) ===
              this.cleanAssistantText(this.jobMarkdown)
        )
      ) {
        items.push({
          key: "md",
          kind: "assistant",
          content: this.cleanAssistantText(this.jobMarkdown)
        });
      }
      if (this.jobStatus === "failed") {
        items.push({
          key: "err",
          kind: "error",
          content: this.jobError || "Job failed"
        });
      }
      if (this.message && this.messageError && !this.jobThinking) {
        items.push({
          key: "syserr",
          kind: "error",
          content: this.message
        });
      }
      return items;
    },
    /** Stable key for the image the user is actually looking at. */
    overlayKey() {
      if (this.surfaceKind === "mosaic" && this.mosaicSessionId) {
        return `mosaic:${this.mosaicSessionId}:${
          this.surfaceContext.revision == null
            ? "preview"
            : this.surfaceContext.revision
        }`;
      }
      return `live:${this.currentViewId || "default"}`;
    },
    overlayVisible() {
      return (
        this.overlayReady &&
        (this.rois.length > 0 ||
          this.draft ||
          this.resultAnnotationsPx.length > 0)
      );
    },
    resultAnnotationsLocal() {
      const o = this.localOrigin;
      return this.resultAnnotationsPx.map(a => {
        const out = { ...a };
        if (a.px) {
          out.px = {
            x: a.px.x - o.left,
            y: a.px.y - o.top,
            w: a.px.w,
            h: a.px.h
          };
        }
        if (a.pointsPx) {
          out.pointsPx = a.pointsPx.map(p => ({
            x: p.x - o.left,
            y: p.y - o.top
          }));
        }
        return out;
      });
    },
    overlayStyle() {
      const surface = document.querySelector(".openmhs-main-surface");
      const root = this.$el;
      if (!surface || !root || !root.getBoundingClientRect) {
        return { left: 0, top: 0, width: "100%", height: "100%" };
      }
      const s = surface.getBoundingClientRect();
      const o = root.getBoundingClientRect();
      return {
        left: `${Math.round(s.left - o.left)}px`,
        top: `${Math.round(s.top - o.top)}px`,
        width: `${Math.round(s.width)}px`,
        height: `${Math.round(s.height)}px`
      };
    },
    maskOverlayVisible() {
      return !!(this.overlayReady && this.maskOverlayUrl && this.imageRect);
    },
    maskOverlayStyle() {
      const r = this.imageRect;
      if (!r) return { left: 0, top: 0, width: "100%", height: "100%" };
      const o = this.localOrigin;
      return {
        left: `${r.left - o.left}px`,
        top: `${r.top - o.top}px`,
        width: `${r.width}px`,
        height: `${r.height}px`
      };
    },
    jobThinking() {
      return (
        this.jobStatus === "queued" ||
        this.jobStatus === "claimed" ||
        this.jobStatus === "running"
      );
    },
    jobStatusLabel() {
      if (this.jobStatus === "queued") return "QUEUED";
      if (this.jobStatus === "claimed" || this.jobStatus === "running") {
        return "THINKING";
      }
      if (this.jobStatus === "done") return "DONE";
      if (this.jobStatus === "failed") return "FAILED";
      if (this.jobStatus === "cancelled") return "CANCELLED";
      return (this.jobStatus || "…").toUpperCase();
    },
    resultAnnotationsPx() {
      if (!this.imageRect || !this.resultAnnotations.length) return [];
      const rect = this.imageRect;
      return this.resultAnnotations.map(a => {
        const out = {
          type: a.type || "bbox",
          label: a.label || a.text || "",
          color: a.color || null
        };
        const norm = a.norm || null;
        const isBoxLike =
          a.type === "bbox" ||
          a.type === "mask" ||
          a.type === "polygon" ||
          !a.type ||
          a.type === "text";
        if (norm && isBoxLike) {
          out.px = {
            x: rect.left + norm.x * rect.width,
            y: rect.top + norm.y * rect.height,
            w: Math.max(1, norm.width * rect.width),
            h: Math.max(1, norm.height * rect.height)
          };
        } else if (a.type === "point" && norm) {
          out.px = {
            x: rect.left + (norm.x + (norm.width || 0) / 2) * rect.width,
            y: rect.top + (norm.y + (norm.height || 0) / 2) * rect.height,
            w: 0,
            h: 0
          };
        } else if (a.type === "point" && a.x != null) {
          out.px = {
            x: rect.left + Number(a.x) * rect.width,
            y: rect.top + Number(a.y) * rect.height,
            w: 0,
            h: 0
          };
        }
        if (Array.isArray(a.points) && a.points.length) {
          out.pointsPx = a.points.map(p => ({
            x: rect.left + Number(p.x != null ? p.x : p[0]) * rect.width,
            y: rect.top + Number(p.y != null ? p.y : p[1]) * rect.height
          }));
          if (!out.px && out.pointsPx[0]) {
            out.px = { x: out.pointsPx[0].x, y: out.pointsPx[0].y, w: 0, h: 0 };
          }
        }
        return out;
      });
    }
  },
  watch: {
    sidebarWidthPx() {
      this.emitSidebarLayout();
    },
    open() {
      // Agent sidebar is always open; keep layout in sync.
      this.emitSidebarLayout();
      this.$nextTick(() => this.syncRoisToSurface());
    },
    agentWorkerOnline(val) {
      this.$emit("pc-status", {
        online: !!val,
        workspace: this.agentWorkspace
      });
      if (val) {
        this.loadViewsTree();
        if (this.open) this.loadWorkspaceTree();
      }
    },
    overlayKey(next, prev) {
      this.swapOverlaySurface(next, prev);
    },
    surfaceVersion() {
      // Same overlay bag, but the underlying image asset/layout changed.
      this.$nextTick(() => {
        this.ensureSurfaceRect(true);
        this.syncRoisToSurface();
        if (!this.overlaySwapTimer) this.overlayReady = true;
      });
    },
    rois: {
      deep: true,
      handler(list) {
        this.$emit("roi-count", Array.isArray(list) ? list.length : 0);
        const signature = JSON.stringify(
          (list || []).map(r => ({
            role: r.role,
            shape: r.shape,
            norm: r.norm,
            points: r.points
          }))
        );
        if (signature !== this.selectionSignature) {
          this.selectionSignature = signature;
          if (this.overlayReady && this.codexMode) {
            clearTimeout(this.selectionSyncTimer);
            this.selectionSyncTimer = setTimeout(
              () => this.syncSelection(),
              450
            );
          }
        }
      }
    }
  },
  mounted() {
    this.boundOverlayKey = this.overlayKey;
    this.clearRequestRender();
    try {
      const w = Number(window.localStorage.getItem("openmhs.agentSidebarW"));
      if (w >= 280 && w <= 720) this.sidebarWidthPx = w;
    } catch (_) {
      /* ignore */
    }
    this.open = true;
    this.emitSidebarLayout();
    try {
      const n = Number(window.localStorage.getItem("openmhs.uiEventAfter"));
      if (Number.isFinite(n) && n > 0) this.uiEventAfter = n;
    } catch (_) {
      /* ignore */
    }
    if (!this.codexMode) {
      this.loadWorkspaceTree();
      this.loadViewsTree();
    }
    this.checkHealth();
    this.healthTimer = setInterval(this.checkHealth, 5000);
    if (!this.codexMode) {
      this.pollUiEvents();
      this.uiEventTimer = setInterval(this.pollUiEvents, 2500);
    }
    // Keep ROI boxes locked to the image content (not the viewport)
    this.syncTimer = setInterval(this.syncRoisToSurface, 200);
    document.addEventListener("pointerdown", this.onPointerDown, true);
    document.addEventListener("pointermove", this.onPointerMove, true);
    document.addEventListener("pointerup", this.onPointerUp, true);
    document.addEventListener("dblclick", this.onDblClick, true);
    window.addEventListener("pointermove", this.onBoxDragMove);
    window.addEventListener("pointerup", this.onBoxDragEnd);
    window.addEventListener("keydown", this.onKeyDown);
    window.addEventListener("resize", this.syncRoisToSurface);
    window.addEventListener("beforeunload", this.clearRequestRender);
    window.addEventListener("pagehide", this.clearRequestRender);
  },
  beforeDestroy() {
    clearTimeout(this.selectionSyncTimer);
    if (this.overlaySwapTimer) clearTimeout(this.overlaySwapTimer);
    this.clearRequestRender();
    clearInterval(this.healthTimer);
    clearInterval(this.uiEventTimer);
    clearInterval(this.syncTimer);
    this.stopJobPoll();
    document.removeEventListener("pointerdown", this.onPointerDown, true);
    document.removeEventListener("pointermove", this.onPointerMove, true);
    document.removeEventListener("pointerup", this.onPointerUp, true);
    document.removeEventListener("dblclick", this.onDblClick, true);
    window.removeEventListener("pointermove", this.onBoxDragMove);
    window.removeEventListener("pointerup", this.onBoxDragEnd);
    window.removeEventListener("keydown", this.onKeyDown);
    window.removeEventListener("resize", this.syncRoisToSurface);
    window.removeEventListener("beforeunload", this.clearRequestRender);
    window.removeEventListener("pagehide", this.clearRequestRender);
  },
  methods: {
    requestRoiRetake() {
      const roi = [...this.rois]
        .reverse()
        .find(r => r.norm && r.role !== "result");
      if (!roi) {
        this.message = "Draw a target ROI on the Slide first.";
        return;
      }
      this.$emit("retake-roi", {
        session_id: this.mosaicSessionId,
        roi: { ...roi.norm }
      });
    },
    onMaterialsToggle(e) {
      if (e.target.open) {
        this.loadWorkspaceTree();
        this.loadViewsTree();
      }
    },
    emitSidebarLayout() {
      if (this.codexMode) {
        this.$emit("sidebar-change", false, 0);
        return;
      }
      const w = Math.max(
        280,
        Math.min(720, Number(this.sidebarWidthPx) || 400)
      );
      this.sidebarWidthPx = w;
      try {
        window.localStorage.setItem("openmhs.agentSidebarW", String(w));
      } catch (_) {
        /* ignore */
      }
      this.$emit("sidebar-change", true, w);
    },
    startSidebarResize(e) {
      if (e.button !== 0) return;
      this.resizingSidebar = true;
      this.resizeStartX = e.clientX;
      this.resizeStartW = this.sidebarWidthPx;
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch (_) {
        /* ignore */
      }
    },
    openRoiPanel() {
      this.roiPanelOpen = true;
      if (this.roiPanelPos.x == null) {
        const root = document.querySelector(".openmhs-root");
        const rect = root ? root.getBoundingClientRect() : { left: 0, top: 0 };
        this.roiPanelPos = {
          x: rect.left + 16,
          y: rect.top + 80
        };
      }
      this.$emit("roi-count", this.rois.length);
    },
    onRoiFabClick() {
      this.openRoiPanel();
    },
    startRoiFabDrag(e) {
      // Docked FAB is owned by parent; keep no-op for safety
      if (e && e.button !== 0) return;
    },
    startRoiPanelDrag(e) {
      if (e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest("button")) return;
      this.draggingRoiPanel = true;
      const y =
        this.roiPanelPos.y == null
          ? Math.max(20, window.innerHeight - 320)
          : this.roiPanelPos.y;
      const x = this.roiPanelPos.x == null ? 12 : this.roiPanelPos.x;
      this.roiPanelPos = { x, y };
      this.dragOffset = { x: e.clientX - x, y: e.clientY - y };
    },
    close() {
      // Agent stays open; closing only stops drawing chrome.
      this.stopDrawing();
      this.draft = null;
      this.selectedRoi = null;
      this.draggingRoi = null;
      this.roiPanelOpen = false;
    },
    clearRequestRender() {
      this.stopJobPoll();
      this.jobId = null;
      this.jobStatus = "";
      this.jobMarkdown = "";
      this.jobError = "";
      this.jobProgress = [];
      this.jobTraceOpen = false;
      this.resultAnnotations = [];
      this.maskOverlayUrl = "";
      this.message = "";
      this.messageError = false;
      this.rememberCurrentOverlayBag();
    },
    captureOverlayBag() {
      return {
        rois: (this.rois || []).map(r => ({
          role: r.role,
          shape: r.shape,
          norm: r.norm ? { ...r.norm } : null,
          points: r.points ? r.points.map(p => ({ x: p.x, y: p.y })) : null
        })),
        resultAnnotations: Array.isArray(this.resultAnnotations)
          ? JSON.parse(JSON.stringify(this.resultAnnotations))
          : [],
        maskOverlayUrl: this.maskOverlayUrl || ""
      };
    },
    applyOverlayBag(bag) {
      const b = bag || {};
      this.rois = Array.isArray(b.rois) ? b.rois : [];
      this.resultAnnotations = Array.isArray(b.resultAnnotations)
        ? b.resultAnnotations
        : [];
      this.maskOverlayUrl = b.maskOverlayUrl || "";
      this.selectedRoi = null;
      this.draggingRoi = null;
      this.draft = null;
    },
    rememberCurrentOverlayBag() {
      const key = this.boundOverlayKey || this.overlayKey;
      if (!key) return;
      this.$set(this.overlayBags, key, this.captureOverlayBag());
    },
    /**
     * Live ↔ scan (and mosaic A ↔ B) each keep their own ROI/result overlays.
     * Fade out briefly so leftover polygons don't flash on the wrong image.
     */
    swapOverlaySurface(next, prev) {
      const from = prev || this.boundOverlayKey;
      if (!next || from === next) {
        this.$nextTick(() => {
          this.ensureSurfaceRect(true);
          this.syncRoisToSurface();
          this.overlayReady = true;
        });
        return;
      }
      this.overlayReady = false;
      if (this.overlaySwapTimer) {
        clearTimeout(this.overlaySwapTimer);
        this.overlaySwapTimer = null;
      }
      if (from) {
        this.$set(this.overlayBags, from, this.captureOverlayBag());
      }
      this.stopDrawing();
      this.draft = null;
      this.selectedRoi = null;
      this.draggingRoi = null;
      this.imageEl = null;
      this.imageRect = null;
      const bag = this.overlayBags[next] || null;
      this.applyOverlayBag(bag);
      this.boundOverlayKey = next;
      this.overlaySwapTimer = setTimeout(() => {
        this.overlaySwapTimer = null;
        this.$nextTick(() => {
          this.ensureSurfaceRect(true);
          this.syncRoisToSurface();
          this.overlayReady = true;
        });
      }, 60);
    },
    roiWidgetStyle(r) {
      if (!r || !r.px) return { display: "none" };
      const o = this.localOrigin;
      return {
        left: `${r.px.x - o.left}px`,
        top: `${Math.max(0, r.px.y - o.top - 26)}px`,
        width: `${Math.max(72, r.px.w)}px`
      };
    },
    /** Convert viewport px → local for SVG overlays inside agent-tool-root. */
    localPx(p) {
      if (!p) return p;
      const o = this.localOrigin;
      if (p.w != null || p.h != null) {
        return {
          x: p.x - o.left,
          y: p.y - o.top,
          w: p.w,
          h: p.h
        };
      }
      return { x: p.x - o.left, y: p.y - o.top };
    },
    localPoints(points) {
      if (!points || !points.length) return points;
      const o = this.localOrigin;
      return points.map(p => ({ x: p.x - o.left, y: p.y - o.top }));
    },
    /**
     * Reproject stored normalized ROIs into current on-screen image pixels.
     * Call whenever the camera frame or mosaic preview changes size/position.
     */
    syncRoisToSurface() {
      if (this.draft) return; // don't fight an in-progress stroke
      const img = this.findSurfaceImage();
      if (!img || !img.naturalWidth) return;
      const rect = this.displayedRect(img);
      const sameRect =
        this.imageRect &&
        this.imageEl === img &&
        Math.abs(this.imageRect.left - rect.left) < 0.5 &&
        Math.abs(this.imageRect.top - rect.top) < 0.5 &&
        Math.abs(this.imageRect.width - rect.width) < 0.5 &&
        Math.abs(this.imageRect.height - rect.height) < 0.5;
      this.imageEl = img;
      this.imageRect = rect;
      // Always keep imageRect fresh so result polygons can project even with 0 user ROIs
      if (!this.rois.length) return;
      if (sameRect) return;
      this.rois = this.rois.map(r => this.reprojectRoi(r, rect));
    },
    reprojectRoi(r, rect) {
      const n = r.norm;
      if (!n) return r;
      const px = {
        x: rect.left + n.x * rect.width,
        y: rect.top + n.y * rect.height,
        w: n.width * rect.width,
        h: n.height * rect.height
      };
      const out = { ...r, px };
      if (r.points && r.points.length) {
        out.pointsPx = r.points.map(p => ({
          x: rect.left + p.x * rect.width,
          y: rect.top + p.y * rect.height
        }));
      }
      return out;
    },
    reprojectDraft(d, rect) {
      if (d.shape === "bbox" && d.norm) {
        const n = d.norm;
        return {
          ...d,
          px: {
            x: rect.left + n.x * rect.width,
            y: rect.top + n.y * rect.height,
            w: n.width * rect.width,
            h: n.height * rect.height
          },
          start: {
            x: rect.left + n.x * rect.width,
            y: rect.top + n.y * rect.height
          }
        };
      }
      if (d.points && d.points.length) {
        const pointsPx = d.points.map(p => ({
          x: rect.left + p.x * rect.width,
          y: rect.top + p.y * rect.height
        }));
        return { ...d, pointsPx };
      }
      return d;
    },
    onFabClick() {
      // Agent FAB removed — sidebar is always open.
      if (this.jobId) this.pollJobOnce();
      else this.loadLatestJob();
    },
    async loadLatestJob() {
      try {
        const { data } = await axios.get(`${this.vpBase}/jobs/latest`, {
          timeout: 10000
        });
        const job = (data && data.job) || null;
        if (!job || !job.id) return;
        const live =
          job.status === "queued" ||
          job.status === "claimed" ||
          job.status === "running";
        // Finished jobs stay on Pi disk; do not resurrect ROI / overlay / chat
        // after a refresh. Only resume work that is still in flight.
        if (!live) return;
        this.jobId = job.id;
        this.jobStatus = job.status || "";
        this.startJobPoll(job.id);
      } catch (_) {
        /* ignore */
      }
    },
    startFabDrag(e) {
      if (e.button !== 0) return;
      this.draggingFab = true;
      this.fabMoved = false;
      const y =
        this.fabPos.y == null
          ? Math.max(20, window.innerHeight - 64)
          : this.fabPos.y;
      this.fabPos = { x: this.fabPos.x, y };
      this.dragOffset = { x: e.clientX - this.fabPos.x, y: e.clientY - y };
      e.preventDefault();
    },
    setDrawTool(id) {
      if (this.drawing && this.drawTool === id) {
        this.stopDrawing();
        return;
      }
      this.drawTool = id;
      this.draft = null;
      this.drawing = true;
      this.$emit("drawing-change", true);
      this.message = this.toolHint;
      this.messageError = false;
    },
    setRole(role) {
      this.roiRole = role;
      // Keep drawing armed so role switch doesn't require re-picking a tool
      if (!this.drawing) {
        this.drawing = true;
        this.$emit("drawing-change", true);
      } else {
        this.$emit("drawing-change", true);
      }
      this.message = this.toolHint;
      this.messageError = false;
    },
    stopDrawing() {
      this.drawing = false;
      this.draft = null;
      this.$emit("drawing-change", false);
    },
    clearRois() {
      this.rois = [];
      this.draft = null;
      this.rememberCurrentOverlayBag();
    },
    removeRoi(i) {
      this.rois.splice(i, 1);
      if (this.selectedRoi === i) this.selectedRoi = null;
      else if (this.selectedRoi > i) this.selectedRoi -= 1;
    },
    onRoiPointerDown(i, e) {
      if (e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest(".roi-btn")) return;
      this.selectedRoi = i;
      const r = this.rois[i];
      if (!r || !r.px || !this.imageRect) return;
      this.draggingRoi = i;
      this.roiDragStart = {
        x: e.clientX,
        y: e.clientY,
        px: { ...r.px },
        norm: r.norm ? { ...r.norm } : null,
        points: r.points ? r.points.map(p => ({ ...p })) : null,
        pointsPx: r.pointsPx ? r.pointsPx.map(p => ({ ...p })) : null
      };
      e.preventDefault();
    },
    moveRoiByDelta(i, dx, dy) {
      const r = this.rois[i];
      const rect = this.imageRect;
      if (!r || !r.px || !rect) return;
      let nx = r.px.x + dx;
      let ny = r.px.y + dy;
      nx = Math.max(rect.left, Math.min(nx, rect.left + rect.width - r.px.w));
      ny = Math.max(rect.top, Math.min(ny, rect.top + rect.height - r.px.h));
      const adx = nx - r.px.x;
      const ady = ny - r.px.y;
      r.px = { ...r.px, x: nx, y: ny };
      if (r.pointsPx && r.pointsPx.length) {
        r.pointsPx = r.pointsPx.map(p => ({ x: p.x + adx, y: p.y + ady }));
        r.points = r.pointsPx.map(p => this.toNorm(p));
      }
      const n0 = this.toNorm({ x: r.px.x, y: r.px.y });
      const n1 = this.toNorm({
        x: r.px.x + r.px.w,
        y: r.px.y + r.px.h
      });
      r.norm = {
        x: n0.x,
        y: n0.y,
        width: n1.x - n0.x,
        height: n1.y - n0.y
      };
      this.$set(this.rois, i, { ...r });
    },
    async copyRoiCrop(i) {
      const r = this.rois[i];
      if (!r || !r.norm) return;
      const img =
        this.imageEl ||
        this.findSurfaceImage() ||
        document.querySelector(".openmhs-main-surface .main img");
      if (!img || !img.naturalWidth) {
        this.message = "No image to copy";
        this.messageError = true;
        return;
      }
      const sx = Math.round(r.norm.x * img.naturalWidth);
      const sy = Math.round(r.norm.y * img.naturalHeight);
      const sw = Math.max(1, Math.round(r.norm.width * img.naturalWidth));
      const sh = Math.max(1, Math.round(r.norm.height * img.naturalHeight));
      const canvas = document.createElement("canvas");
      canvas.width = sw;
      canvas.height = sh;
      canvas.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
      try {
        const blob = await new Promise(resolve =>
          canvas.toBlob(resolve, "image/png")
        );
        if (blob && navigator.clipboard && window.ClipboardItem) {
          await navigator.clipboard.write([
            new window.ClipboardItem({ "image/png": blob })
          ]);
          this.message = `Copied ${r.role} ROI crop`;
          this.messageError = false;
          return;
        }
      } catch (_) {
        /* fall through to download */
      }
      const a = document.createElement("a");
      a.href = canvas.toDataURL("image/png");
      a.download = `roi-${r.role}-${i + 1}.png`;
      a.click();
      this.message = `Downloaded ${r.role} ROI crop`;
      this.messageError = false;
    },
    polyPoints(r) {
      if (!r.pointsPx) return "";
      return this.localPoints(r.pointsPx)
        .map(p => `${p.x},${p.y}`)
        .join(" ");
    },
    checkHealth: async function() {
      try {
        const { data } = await axios.get(`${this.vpBase}/codex`, {
          timeout: 3500
        });
        this.codexConnected = !!data.connected;
        this.codexLastRead = data.last_read || null;
        if (this.codexMode) {
          this.connected = true;
          this.agentWorkerOnline = !!data.worker_online;
          this.agentSupervisorOk = this.agentWorkerOnline;
          this.agentHealthy = this.agentWorkerOnline;
          this.agentWorkspace = data.workspace || "";
          return;
        }
      } catch (_) {
        this.codexConnected = false;
        if (this.codexMode) {
          this.connected = false;
          this.agentWorkerOnline = false;
          this.agentSupervisorOk = false;
          this.agentHealthy = false;
          return;
        }
      }
      try {
        const r = await axios.get(`${this.vpBase}/status`, { timeout: 3500 });
        this.connected = !!(r.data && r.data.ok);
      } catch (_) {
        this.connected = false;
      }
      // Local supervisor probe (browser on this Mac)
      const localSup = await this.probeLocalSupervisor();
      try {
        const { data } = await axios.get(`${this.vpBase}/agent`, {
          timeout: 3500
        });
        const supervisorOk = !!(
          (data && data.supervisor_ok) ||
          (localSup && localSup.ok)
        );
        const workerOnline = !!(
          (data && data.worker_online) ||
          (localSup && localSup.worker_running)
        );
        this.agentSupervisorOk = supervisorOk;
        this.agentWorkerOnline = workerOnline;
        this.agentHealthy = !!(this.connected && supervisorOk && workerOnline);
        this.agentErrorType = this.agentHealthy
          ? ""
          : (data && data.error_type) ||
            (!this.connected
              ? "pi_offline"
              : !supervisorOk
              ? "supervisor_down"
              : "worker_down");
        this.agentWorkspace = data.workspace || "";
        if (
          this.agentWorkspace &&
          !this.workspaceSaving &&
          (!this.workspaceDraft ||
            this.workspaceDraft === this._syncedWorkspace)
        ) {
          this.workspaceDraft = this.agentWorkspace;
          this._syncedWorkspace = this.agentWorkspace;
        }
        this.agentError = (data && data.error) || "";
        this.agentAgeS = data.age_s != null ? data.age_s : null;
        if (this.agentHealthy && this.localStartHint) {
          // Clear sticky repair banner once the chain is green again
          this.localStartHint = "";
          this.localStartError = false;
        }
        if (
          this.agentWorkerOnline &&
          this.open &&
          (!this._lastViewsLoadAt || Date.now() - this._lastViewsLoadAt > 15000)
        ) {
          this._lastViewsLoadAt = Date.now();
          this.loadViewsTree();
        }
      } catch (_) {
        this.agentWorkerOnline = !!(localSup && localSup.worker_running);
        this.agentSupervisorOk = !!(localSup && localSup.ok);
        this.agentHealthy = !!(
          this.connected &&
          this.agentSupervisorOk &&
          this.agentWorkerOnline
        );
        this.agentErrorType = this.connected ? "worker_down" : "pi_offline";
      }
    },
    async applyWorkspace() {
      const path = this.workspaceDraftTrimmed;
      if (!path) return;
      this.workspaceSaving = true;
      this.localStartHint = "";
      this.localStartError = false;
      try {
        const st = await this.probeLocalSupervisor();
        if (!st) {
          throw new Error(
            "Local supervisor is down. Click Repair (requires login autostart), or use the desktop OpenMHS PC Worker launcher."
          );
        }
        const { data } = await axios.post(
          "http://127.0.0.1:18765/workspace",
          { path },
          { timeout: 15000 }
        );
        if (!data || data.ok === false) {
          throw new Error((data && data.error) || "set workspace failed");
        }
        this.agentWorkspace = data.workspace || path;
        this.workspaceDraft = this.agentWorkspace;
        this._syncedWorkspace = this.agentWorkspace;
        this.message = `Workspace → ${this.agentWorkspace}`;
        this.messageError = false;
        this.loadWorkspaceTree();
        setTimeout(() => this.checkHealth(), 1500);
      } catch (err) {
        this.localStartError = true;
        this.localStartHint =
          (err &&
            err.response &&
            err.response.data &&
            err.response.data.error) ||
          (err && err.message) ||
          "Failed to set workspace";
      } finally {
        this.workspaceSaving = false;
      }
    },
    persistUiEventCursor(n) {
      const id = Math.max(0, Number(n) || 0);
      this.uiEventAfter = id;
      try {
        window.localStorage.setItem("openmhs.uiEventAfter", String(id));
      } catch (_) {
        /* ignore */
      }
    },
    uiEventIsFresh(ev, maxAgeMs) {
      const at = ev && ev.at;
      if (!at) return false;
      const t = Date.parse(at);
      if (!Number.isFinite(t)) return false;
      return Date.now() - t < maxAgeMs;
    },
    async pollUiEvents() {
      if (!this.agentWorkerOnline && !this.localSupervisorOk) return;
      try {
        const localUrl = this.localSupervisorOk
          ? "http://127.0.0.1:18765/ui/events"
          : `${this.$store.getters.baseUri}/api/v2/extensions/org.openmhs.scan-mosaic/mac/ui/events`;
        const { data } = await axios.get(localUrl, {
          params: { after: this.uiEventAfter || 0, limit: 50 },
          timeout: 2500
        });
        const events = (data && data.events) || [];
        const latest = Number((data && data.latest_id) || 0) || 0;
        if (!this.uiEventsCaughtUp) {
          this.uiEventsCaughtUp = true;
          // Refresh must not replay history (especially run_mosaic_scan).
          // Only apply events that arrived in the last few seconds.
          for (let i = 0; i < events.length; i++) {
            const ev = events[i];
            if (
              !ev ||
              (ev.id != null && Number(ev.id) <= (this.uiEventAfter || 0))
            ) {
              continue;
            }
            if (!this.uiEventIsFresh(ev, 12000)) continue;
            await this.applyUiEvent(ev);
          }
          this.persistUiEventCursor(Math.max(this.uiEventAfter || 0, latest));
          return;
        }
        for (let i = 0; i < events.length; i++) {
          await this.applyUiEvent(events[i]);
        }
        if (latest) {
          this.persistUiEventCursor(Math.max(this.uiEventAfter || 0, latest));
        }
      } catch (_) {
        /* PC offline or route not deployed yet */
      }
    },
    async applyUiEvent(ev) {
      if (!ev || !ev.type) return;
      const p = ev.payload || {};
      const type = String(ev.type);
      if (type === "select_observation") {
        if (p.view_id) {
          this.$emit("select-camera-view", p.view_id);
        }
        this.open = true;
        this.loadViewsTree();
        return;
      }
      if (type === "run_mosaic_scan") {
        // Stale agent commands must never start a scan on page load
        if (!this.uiEventIsFresh(ev, 20000)) return;
        this.open = true;
        this.$emit("agent-run-mosaic-scan", {
          view_id: p.view_id,
          nx: p.nx || 2,
          ny: p.ny || 2,
          title: p.title || ""
        });
        this.loadViewsTree();
        return;
      }
      if (type === "open_comprehension") {
        if (p.view_id) this.$emit("select-camera-view", p.view_id);
        if (p.kind === "video") {
          this.message = "Video Comprehension placeholder";
          this.messageError = false;
          this.loadViewsTree();
          return;
        }
        if (p.record_id) this.$emit("load-pc-session", p.record_id);
        this.loadViewsTree();
        return;
      }
      if (type === "delete_comprehension") {
        this.$emit("agent-delete-comprehension", {
          record_id: p.record_id
        });
        this.loadViewsTree();
        return;
      }
      if (type === "set_rois") {
        this.applyAgentRois(p.rois || []);
        this.open = true;
        return;
      }
      if (type === "set_result_annotations") {
        this.resultAnnotations = Array.isArray(p.annotations)
          ? p.annotations
          : [];
        if (p.markdown) {
          this.jobMarkdown = this.cleanAssistantText(p.markdown);
        }
        this.open = true;
        this.rememberCurrentOverlayBag();
        this.$nextTick(() => {
          this.ensureSurfaceRect(true);
          this.syncRoisToSurface();
        });
        this.persistAgentSnapshot();
        return;
      }
      if (type === "clear_rois") {
        this.clearRois();
        return;
      }
      if (type === "set_draw_tool") {
        const tool = String(p.tool || "bbox").toLowerCase();
        const role = String(p.role || "target").toLowerCase();
        if (role === "source" || role === "target") this.roiRole = role;
        if (tool === "none" || tool === "off" || tool === "stop") {
          this.stopDrawing();
        } else {
          this.setDrawTool(tool === "pen" || tool === "draw" ? tool : "bbox");
        }
        this.open = true;
      }
    },
    applyAgentRois(list) {
      const rois = [];
      (list || []).forEach(item => {
        if (!item) return;
        const norm = item.norm || {
          x: item.x,
          y: item.y,
          width: item.width,
          height: item.height
        };
        if (
          norm == null ||
          norm.x == null ||
          norm.y == null ||
          norm.width == null ||
          norm.height == null
        ) {
          return;
        }
        rois.push({
          role: item.role === "source" ? "source" : "target",
          shape: item.shape || "bbox",
          points: null,
          norm: {
            x: Number(norm.x),
            y: Number(norm.y),
            width: Number(norm.width),
            height: Number(norm.height)
          }
        });
      });
      this.rois = rois;
      // Force reproject even if imageRect unchanged
      const img = this.findSurfaceImage();
      if (img && img.naturalWidth) {
        const rect = this.displayedRect(img);
        this.imageEl = img;
        this.imageRect = rect;
        this.rois = this.rois.map(r => this.reprojectRoi(r, rect));
      } else {
        this.$nextTick(() => this.syncRoisToSurface());
      }
    },
    sessionIdFromPath(path) {
      const parts = String(path || "")
        .replace(/\\/g, "/")
        .split("/")
        .filter(p => p && p !== ".");
      const r = parts.indexOf("records");
      if (r >= 0 && parts[r + 1]) return parts[r + 1];
      const i = parts.indexOf("mosaics");
      if (i >= 0 && parts[i + 1]) return parts[i + 1];
      return null;
    },
    isMosaicDir(path) {
      const p = String(path || "");
      return (
        /(^|\/)views\/[^/]+\/records\/[^/]+$/.test(p) ||
        /(^|\/)captures\/mosaics\/[^/]+$/.test(p)
      );
    },
    treeIcon(row) {
      if (row.type !== "dir") {
        if (row.name === "agent.json") return "chat";
        if (/\.(jpg|jpeg|png)$/i.test(row.name)) return "image";
        return "insert_drive_file";
      }
      const p = row.path || "";
      if (/(^|\/)views\/[^/]+$/.test(p)) return "videocam";
      if (this.isMosaicDir(p)) return "collections";
      if (p === "lib" || /(^|\/)lib$/.test(p)) return "menu_book";
      return row.expanded ? "folder_open" : "folder";
    },
    onTreeDragStart(row, e) {
      const path = row.path || "";
      e.dataTransfer.setData("application/x-openmhs-path", path);
      const sid = this.sessionIdFromPath(path);
      if (sid || this.isMosaicDir(path)) {
        const id = sid || this.sessionIdFromPath(path);
        const payload = JSON.stringify({
          id: id || row.name,
          title: row.name,
          path,
          kind: "mosaic"
        });
        e.dataTransfer.setData("application/x-openmhs-canvas", payload);
      }
      e.dataTransfer.setData("text/plain", path);
      e.dataTransfer.effectAllowed = "copy";
    },
    onCanvasDragStart(rec, e) {
      if (!rec || !rec.id) return;
      const payload = JSON.stringify({
        id: rec.id,
        title: rec.title || rec.id,
        view_id: rec.view_id || "",
        kind: rec.kind || "mosaic",
        path: rec.relpath || ""
      });
      e.dataTransfer.setData("application/x-openmhs-canvas", payload);
      if (rec.relpath) {
        e.dataTransfer.setData("application/x-openmhs-path", rec.relpath);
      }
      e.dataTransfer.setData("text/plain", rec.relpath || rec.id);
      e.dataTransfer.effectAllowed = "copy";
    },
    onCanvasDragOver(e) {
      this.canvasDropOver = true;
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    },
    onCanvasDragLeave(e) {
      const el = e.currentTarget;
      if (el && e.relatedTarget && el.contains(e.relatedTarget)) return;
      this.canvasDropOver = false;
    },
    onCanvasDrop(e) {
      this.canvasDropOver = false;
      const dt = e.dataTransfer;
      let canvas = null;
      try {
        const raw = dt && dt.getData("application/x-openmhs-canvas");
        if (raw) canvas = JSON.parse(raw);
      } catch (_) {
        canvas = null;
      }
      if (!canvas || !canvas.id) {
        const path =
          (dt && dt.getData("application/x-openmhs-path")) ||
          (dt && dt.getData("text/plain")) ||
          "";
        const sid = this.sessionIdFromPath(path);
        if (!sid) return;
        canvas = { id: sid, title: sid, kind: "mosaic", path };
      }
      this.attachCanvasContext(canvas);
    },
    attachCanvasContext(canvas) {
      if (!canvas || !canvas.id) return;
      if (canvas.kind === "video") {
        this.message = "Video Canvas not attachable yet";
        this.messageError = false;
        return;
      }
      const id = String(canvas.id);
      const existing = this.canvasContexts.findIndex(c => c.id === id);
      const entry = {
        id,
        title: canvas.title || id,
        view_id: canvas.view_id || "",
        kind: canvas.kind || "mosaic"
      };
      if (existing >= 0) {
        this.$set(this.canvasContexts, existing, entry);
      } else {
        this.canvasContexts.push(entry);
      }
      // Preview slide in PiP only — never load old chat/results into this Camera
      this.$emit("preview-canvas", {
        record_id: id,
        title: entry.title,
        view_id: entry.view_id || undefined
      });
      this.message = `Canvas context: ${entry.title}`;
      this.messageError = false;
    },
    removeCanvasContext(id) {
      this.canvasContexts = this.canvasContexts.filter(c => c.id !== id);
    },
    previewCanvasContext(c) {
      if (!c || !c.id) return;
      this.$emit("preview-canvas", {
        record_id: c.id,
        title: c.title,
        view_id: c.view_id || undefined
      });
    },
    clearCanvasOverlayState() {
      // Keep current Camera chat; only wipe result overlays from another Canvas
      this.resultAnnotations = [];
      this.maskOverlayUrl = "";
      this.jobMarkdown = "";
      this.jobError = "";
      this.jobProgress = [];
      this.rois = [];
      this.$emit("roi-count", 0);
      this.rememberCurrentOverlayBag();
    },
    async onTreeClick(row) {
      if (row.type === "dir") {
        const open = !this.treeOpen[row.path];
        this.$set(this.treeOpen, row.path, open);
        if (open) await this.fetchWsDir(row.path);
        if (this.isMosaicDir(row.path)) {
          // Preview only — drag into chat to attach as context
          this.$emit("preview-canvas", {
            record_id: this.sessionIdFromPath(row.path),
            title: row.name
          });
        }
        return;
      }
      const sid = this.sessionIdFromPath(row.path);
      if (sid) {
        this.$emit("preview-canvas", { record_id: sid, title: row.name });
      }
    },
    async fetchWsDir(path) {
      const rel = path || ".";
      const {
        data
      } = await axios.get(
        `${this.$store.getters.baseUri}/api/v2/extensions/org.openmhs.scan-mosaic/mac/workspace`,
        { params: { path: rel }, timeout: 8000 }
      );
      const entries = (data && data.entries) || [];
      this.$set(this.treeKids, rel, entries);
      return entries;
    },
    async loadWorkspaceTree() {
      this.treeLoading = true;
      this.treeError = "";
      try {
        await this.fetchWsDir(".");
        this.$set(this.treeOpen, "views", true);
        await this.fetchWsDir("views");
        this.$set(this.treeOpen, "lib", true);
        this.$set(this.treeOpen, "captures", true);
        await this.fetchWsDir("captures");
        this.$set(this.treeOpen, "captures/mosaics", true);
        await this.fetchWsDir("captures/mosaics");
      } catch (e) {
        this.treeError =
          e && e.response && e.response.status === 503
            ? "PC workspace offline"
            : (e && e.message) || "Cannot list PC files";
      } finally {
        this.treeLoading = false;
      }
    },
    async loadViewsTree() {
      if (this.viewsLoading) return;
      this.viewsLoading = true;
      try {
        const {
          data
        } = await axios.get(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.openmhs.scan-mosaic/mac/views`,
          { timeout: 8000 }
        );
        this.cameraViews = (data && data.views) || [];
        this.legacyMosaics = (data && data.legacy_mosaics) || [];
      } catch (_) {
        this.cameraViews = [];
        this.legacyMosaics = [];
      } finally {
        this.viewsLoading = false;
      }
    },
    onRecordClick(rec) {
      if (!rec || !rec.id) return;
      if (rec.kind === "video") {
        this.message = "Video Canvas placeholder — timeline not wired yet";
        this.messageError = false;
        return;
      }
      // Click = preview slide in PiP only. Drag into chat to attach as context.
      this.$emit("preview-canvas", {
        record_id: rec.id,
        title: rec.title || rec.id,
        view_id: rec.view_id || undefined
      });
      this.message =
        "Preview only — drag Canvas into chat to attach as context";
      this.messageError = false;
    },
    applyPcAgentSnapshot(snap) {
      // Intentionally unused for Canvas load: history stays with its own Camera chat.
      // Kept for rare explicit restore paths.
      if (!snap) return;
      const key = this.overlayKey;
      if (this.boundOverlayKey && this.boundOverlayKey !== key) {
        this.swapOverlaySurface(key, this.boundOverlayKey);
      } else {
        this.boundOverlayKey = key;
      }
      if (Array.isArray(snap.annotations)) {
        this.resultAnnotations = snap.annotations;
      } else {
        this.resultAnnotations = [];
      }
      this.maskOverlayUrl = "";
      if (Array.isArray(snap.rois) && snap.rois.length) {
        this.rois = snap.rois;
      } else {
        this.rois = [];
      }
      this.rememberCurrentOverlayBag();
      this.open = true;
      this.overlayReady = false;
      this.$nextTick(() => {
        this.ensureSurfaceRect(true);
        this.syncRoisToSurface();
        this.overlayReady = true;
      });
    },
    async persistAgentSnapshot() {
      if (this.codexMode) return;
      const sid = this.mosaicSessionId;
      const payload = {
        markdown: this.jobMarkdown || "",
        annotations: this.resultAnnotations || [],
        chatMessages: (this.chatMessages || []).map(m => ({
          role: m.role,
          content: String(m.content || "").slice(0, 8000)
        })),
        rois: (this.rois || []).map(r => ({
          role: r.role,
          shape: r.shape,
          norm: r.norm || null,
          points: r.points || null
        }))
      };
      // Mosaic Comprehension: keep agent.json next to the scan
      if (sid) {
        try {
          await axios.post(
            `${this.$store.getters.baseUri}/api/v2/extensions/org.openmhs.scan-mosaic/mac/agent`,
            { session_id: sid, ...payload },
            { params: { session_id: sid }, timeout: 8000 }
          );
        } catch (_) {
          /* optional */
        }
      }
      // Always also push a durable copy into the current Observation live folder
      if (this.currentViewId) {
        try {
          await axios.post(
            `${this.$store.getters.baseUri}/api/v2/extensions/org.openmhs.scan-mosaic/mac/views`,
            {
              action: "save_analysis",
              view_id: this.currentViewId,
              record_id: sid || undefined,
              ...payload
            },
            { timeout: 8000 }
          );
        } catch (_) {
          /* optional until Pi/Mac deploy */
        }
      }
    },
    async probeLocalSupervisor() {
      try {
        const { data } = await axios.get("http://127.0.0.1:18765/status", {
          timeout: 1200
        });
        this.localSupervisorOk = !!(data && data.ok);
        return data;
      } catch (_) {
        this.localSupervisorOk = false;
        return null;
      }
    },
    async startLocalWorker() {
      this.localStarting = true;
      this.localStartHint = "";
      this.localStartError = false;
      try {
        let st = await this.probeLocalSupervisor();
        if (!st) {
          // Browser cannot spawn processes; openmhs://start wakes local Starter.app
          this.localStartHint =
            "Starting local supervisor… (allow the openmhs:// prompt if shown)";
          try {
            const a = document.createElement("a");
            a.href = "openmhs://start";
            a.style.display = "none";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
          } catch (_) {
            window.location.href = "openmhs://start";
          }
          for (let i = 0; i < 25; i++) {
            await new Promise(r => setTimeout(r, 400));
            st = await this.probeLocalSupervisor();
            if (st) break;
          }
          if (!st) {
            this.localStartError = true;
            this.localStartHint =
              "Could not start local services. Run mac-agent autostart installer or the desktop OpenMHS PC Worker launcher.";
            return;
          }
        }
        await axios.post("http://127.0.0.1:18765/start", {}, { timeout: 8000 });
        this.localStartHint = "Starting…";
        for (let i = 0; i < 10; i++) {
          await new Promise(r => setTimeout(r, 800));
          await this.checkHealth();
          if (this.agentHealthy || this.agentWorkerOnline) {
            this.localStartHint = "PC worker online";
            this.localStartError = !this.agentHealthy;
            if (this.agentHealthy) return;
          }
        }
        this.localStartHint =
          this.agentConnText +
          " — try Repair; log: /tmp/openmhs-mac-worker.log";
        this.localStartError = !this.agentHealthy;
      } catch (_) {
        this.localStartError = true;
        this.localStartHint =
          "Start failed. Run the login autostart installer or the desktop launcher.";
      } finally {
        this.localStarting = false;
      }
    },
    async repairAgentConnection() {
      this.localRepairing = true;
      this.localStartHint = "Restarting supervisor → worker…";
      this.localStartError = false;
      try {
        let st = await this.probeLocalSupervisor();
        if (!st) {
          await this.startLocalWorker();
          return;
        }
        try {
          await axios.post(
            "http://127.0.0.1:18765/restart",
            {},
            { timeout: 12000 }
          );
        } catch (_) {
          await axios.post(
            "http://127.0.0.1:18765/stop",
            {},
            { timeout: 5000 }
          );
          await new Promise(r => setTimeout(r, 400));
          await axios.post(
            "http://127.0.0.1:18765/start",
            {},
            { timeout: 8000 }
          );
        }
        st = await this.probeLocalSupervisor();
        const macOk = !!(st && st.ok && st.worker_running);
        // Quick Pi probe — don't spin forever if microscope API is wedged
        let piOk = false;
        try {
          const r = await axios.get(`${this.vpBase}/status`, { timeout: 2500 });
          piOk = !!(r.data && r.data.ok);
          this.connected = piOk;
        } catch (_) {
          this.connected = false;
          piOk = false;
        }
        if (macOk && !piOk) {
          this.localStartError = true;
          this.localStartHint =
            "Local worker restarted, but the microscope API is not responding (Pi may be wedged). Refresh; if still offline, restart openflexure-microscope-server on the Pi.";
          return;
        }
        for (let i = 0; i < 10; i++) {
          await new Promise(r => setTimeout(r, 600));
          await this.checkHealth();
          if (this.agentHealthy) {
            this.localStartHint = "Connection repaired · PC worker online";
            this.localStartError = false;
            return;
          }
          if (!this.connected && macOk) {
            this.localStartError = true;
            this.localStartHint =
              "Mac is fine, microscope still offline — hard-refresh; if it persists, restart the Pi microscope service.";
            return;
          }
        }
        this.localStartError = true;
        this.localStartHint =
          "Still not fully green: " +
          this.agentConnText +
          (this.agentError ? " — " + this.agentError : "");
      } catch (err) {
        this.localStartError = true;
        this.localStartHint =
          (err && err.message) || "Repair failed — check VEEE proxy and .env";
      } finally {
        this.localRepairing = false;
      }
    },
    async interruptJob() {
      if (!this.jobId || this.cancellingJob) return;
      this.cancellingJob = true;
      try {
        await axios.post(
          `${this.vpBase}/jobs/${this.jobId}/cancel`,
          {},
          { timeout: 8000 }
        );
        this.jobStatus = "cancelled";
        this.jobProgress = (this.jobProgress || []).concat([
          {
            message: "Cancelled by user",
            kind: "cancel",
            op: "cancel"
          }
        ]);
        this.message = "Agent call interrupted";
        this.messageError = false;
        this.stopJobPoll();
      } catch (err) {
        this.messageError = true;
        this.message =
          (err.response && err.response.data && err.response.data.error) ||
          err.message ||
          "Interrupt failed";
      } finally {
        this.cancellingJob = false;
      }
    },
    startDrag(e) {
      if (e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest("button")) return;
      this.draggingBox = true;
      const y =
        this.boxPos.y == null
          ? Math.max(80, window.innerHeight - 420)
          : this.boxPos.y;
      this.dragOffset = { x: e.clientX - this.boxPos.x, y: e.clientY - y };
    },
    startBoxResize(e) {
      if (e.button !== 0) return;
      this.resizingBox = true;
      const el = e.currentTarget && e.currentTarget.parentElement;
      const rect = el ? el.getBoundingClientRect() : null;
      this.resizeStart = {
        x: e.clientX,
        y: e.clientY,
        w: (rect && rect.width) || this.boxSize.w || 320,
        h: (rect && rect.height) || this.boxSize.h || 360
      };
    },
    onBoxDragMove(e) {
      if (this.draggingRoi != null && this.roiDragStart) {
        const dx = e.clientX - this.roiDragStart.x;
        const dy = e.clientY - this.roiDragStart.y;
        // Reset to start then apply total delta for stability
        const i = this.draggingRoi;
        const start = this.roiDragStart;
        const r = this.rois[i];
        if (r && start.px) {
          r.px = { ...start.px };
          if (start.pointsPx) {
            r.pointsPx = start.pointsPx.map(p => ({ ...p }));
          }
          this.moveRoiByDelta(i, dx, dy);
        }
        return;
      }
      if (this.resizingBox && this.resizeStart) {
        const dw = e.clientX - this.resizeStart.x;
        const dh = e.clientY - this.resizeStart.y;
        this.boxSize = {
          w: Math.max(
            280,
            Math.min(window.innerWidth - 24, this.resizeStart.w + dw)
          ),
          h: Math.max(
            220,
            Math.min(window.innerHeight - 24, this.resizeStart.h + dh)
          )
        };
        return;
      }
      if (this.resizingSidebar) {
        const dx = this.resizeStartX - e.clientX; // drag left edge → wider
        const nw = Math.max(280, Math.min(720, this.resizeStartW + dx));
        this.sidebarWidthPx = nw;
        return;
      }
      if (this.draggingRoiFab) {
        const nx = Math.max(8, e.clientX - this.dragOffset.x);
        const ny = Math.max(8, e.clientY - this.dragOffset.y);
        if (
          Math.abs(nx - this.roiFabPos.x) > 3 ||
          Math.abs(ny - this.roiFabPos.y) > 3
        ) {
          this.roiFabMoved = true;
        }
        this.roiFabPos = { x: nx, y: ny };
        return;
      }
      if (this.draggingRoiPanel) {
        const nx = Math.max(8, e.clientX - this.dragOffset.x);
        const ny = Math.max(8, e.clientY - this.dragOffset.y);
        this.roiPanelPos = { x: nx, y: ny };
        return;
      }
      if (this.draggingFab) {
        const nx = Math.max(8, e.clientX - this.dragOffset.x);
        const ny = Math.max(8, e.clientY - this.dragOffset.y);
        if (
          Math.abs(nx - this.fabPos.x) > 3 ||
          Math.abs(ny - this.fabPos.y) > 3
        ) {
          this.fabMoved = true;
        }
        this.fabPos = { x: nx, y: ny };
        return;
      }
      if (!this.draggingBox) return;
      this.boxPos = {
        x: Math.max(8, e.clientX - this.dragOffset.x),
        y: Math.max(8, e.clientY - this.dragOffset.y)
      };
    },
    onBoxDragEnd() {
      if (this.resizingSidebar) this.emitSidebarLayout();
      this.draggingBox = false;
      this.draggingFab = false;
      this.draggingRoiFab = false;
      this.draggingRoiPanel = false;
      this.resizingSidebar = false;
      this.resizingBox = false;
      this.resizeStart = null;
      this.draggingRoi = null;
      this.roiDragStart = null;
    },
    findSurfaceImage(event) {
      if (this.surfaceSelector !== ".openmhs-main-surface img")
        return document.querySelector(this.surfaceSelector);
      const root = document.querySelector(".openmhs-main-surface");
      if (!root) return null;
      const mainImg =
        root.querySelector(".mosaic-layer.main img.mosaic-img") ||
        root.querySelector(".camera-layer.main .stream-display img") ||
        root.querySelector(".camera-layer.main img");
      if (mainImg && mainImg.naturalWidth) return mainImg;
      if (event && event.target) {
        const surface =
          event.target.closest && event.target.closest(".openmhs-main-surface");
        if (surface) {
          const img = surface.querySelector(
            ".mosaic-layer.main img, .camera-layer.main img, img"
          );
          if (img && img.naturalWidth) return img;
        }
      }
      return null;
    },
    displayedRect(image) {
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
        width,
        height
      };
    },
    clamp(event) {
      const rect = this.imageRect;
      return {
        x: Math.max(rect.left, Math.min(event.clientX, rect.left + rect.width)),
        y: Math.max(rect.top, Math.min(event.clientY, rect.top + rect.height))
      };
    },
    toNorm(px) {
      return {
        x: (px.x - this.imageRect.left) / this.imageRect.width,
        y: (px.y - this.imageRect.top) / this.imageRect.height
      };
    },
    pointInImage(e) {
      const r = this.imageRect;
      return (
        e.clientX >= r.left &&
        e.clientX <= r.left + r.width &&
        e.clientY >= r.top &&
        e.clientY <= r.top + r.height
      );
    },
    commitPathRoi(d) {
      if (!d.pointsPx || d.pointsPx.length < 2) return;
      const xs = d.pointsPx.map(p => p.x);
      const ys = d.pointsPx.map(p => p.y);
      const minX = Math.min(...xs);
      const minY = Math.min(...ys);
      const maxX = Math.max(...xs);
      const maxY = Math.max(...ys);
      if (maxX - minX < 4 && maxY - minY < 4) return;
      const n0 = this.toNorm({ x: minX, y: minY });
      const n1 = this.toNorm({ x: maxX, y: maxY });
      this.rois.push({
        role: d.role,
        shape: d.shape,
        pointsPx: d.pointsPx.slice(),
        points: d.pointsPx.map(p => this.toNorm(p)),
        px: { x: minX, y: minY, w: maxX - minX, h: maxY - minY },
        norm: {
          x: n0.x,
          y: n0.y,
          width: n1.x - n0.x,
          height: n1.y - n0.y
        }
      });
    },
    onPointerDown(e) {
      if (!this.open || e.button !== 0) return;
      if (
        e.target.closest &&
        e.target.closest(
          ".mhs-toolbar, .codex-status-panel, .camera-pip, .view-controls, .stage-fov, .canvas-dialog"
        )
      )
        return;
      if (
        e.target.closest &&
        e.target.closest(".studio") &&
        (this.surfaceSelector === ".openmhs-main-surface img" ||
          !e.target.closest(".viewport"))
      )
        return;
      if (e.target.closest && e.target.closest(".agent-box")) return;
      if (e.target.closest && e.target.closest(".agent-fab")) return;
      if (e.target.closest && e.target.closest(".roi-fab")) return;
      if (e.target.closest && e.target.closest(".roi-panel")) return;
      if (e.target.closest && e.target.closest(".roi-widget")) return;
      if (e.target.closest && e.target.closest(".hud-float")) return;
      if (e.target.closest && e.target.closest(".peek-modal")) return;
      if (!this.drawing) return;
      const img = this.findSurfaceImage(e);
      if (!img) return;
      this.imageEl = img;
      this.imageRect = this.displayedRect(img);
      if (!this.pointInImage(e)) return;
      const p = this.clamp(e);

      // Pen: click-to-add vertices (not drag)
      if (this.drawTool === "pen") {
        const now = Date.now();
        if (
          this.draft &&
          this.draft.shape === "pen" &&
          now - this.lastPenClickAt < 350
        ) {
          // treat as finish (dblclick companion)
          this.commitPathRoi(this.draft);
          this.draft = null;
          this.lastPenClickAt = 0;
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          return;
        }
        this.lastPenClickAt = now;
        if (!this.draft || this.draft.shape !== "pen") {
          this.draft = {
            shape: "pen",
            role: this.roiRole,
            pointsPx: [p]
          };
        } else {
          this.draft.pointsPx.push(p);
        }
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        return;
      }

      if (this.drawTool === "bbox") {
        this.draft = {
          shape: "bbox",
          role: this.roiRole,
          start: p,
          px: { x: p.x, y: p.y, w: 1, h: 1 }
        };
      } else {
        // Brush freehand
        this.draft = {
          shape: "draw",
          role: this.roiRole,
          pointsPx: [p],
          last: p
        };
      }
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
    },
    onPointerMove(e) {
      if (!this.draft) return;
      if (this.draft.shape === "pen") return; // vertices only on click
      const p = this.clamp(e);
      if (this.draft.shape === "bbox") {
        const x0 = Math.min(this.draft.start.x, p.x);
        const y0 = Math.min(this.draft.start.y, p.y);
        this.draft.px = {
          x: x0,
          y: y0,
          w: Math.abs(p.x - this.draft.start.x),
          h: Math.abs(p.y - this.draft.start.y)
        };
      } else {
        const last =
          this.draft.last ||
          this.draft.pointsPx[this.draft.pointsPx.length - 1];
        const dx = p.x - last.x;
        const dy = p.y - last.y;
        if (dx * dx + dy * dy >= 9) {
          this.draft.pointsPx.push(p);
          this.draft.last = p;
        }
      }
      e.preventDefault();
      e.stopPropagation();
    },
    onPointerUp(e) {
      if (!this.draft) return;
      if (this.draft.shape === "pen") {
        // pen finishes on double-click / Enter, not pointerup
        return;
      }
      const d = this.draft;
      this.draft = null;
      // Keep drawing armed for next ROI / role
      this.$emit("drawing-change", true);
      if (d.shape === "bbox") {
        if (d.px.w < 8 || d.px.h < 8) return;
        const n0 = this.toNorm({ x: d.px.x, y: d.px.y });
        const n1 = this.toNorm({
          x: d.px.x + d.px.w,
          y: d.px.y + d.px.h
        });
        this.rois.push({
          role: d.role,
          shape: "bbox",
          px: d.px,
          norm: {
            x: n0.x,
            y: n0.y,
            width: n1.x - n0.x,
            height: n1.y - n0.y
          }
        });
      } else {
        this.commitPathRoi(d);
      }
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
    },
    onDblClick(e) {
      if (!this.open || !this.drawing || this.drawTool !== "pen") return;
      if (!this.draft || this.draft.shape !== "pen") return;
      this.commitPathRoi(this.draft);
      this.draft = null;
      this.$emit("drawing-change", true);
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
    },
    onKeyDown(e) {
      if (!this.open || !this.drawing) return;
      if (e.key === "Enter" && this.draft && this.draft.shape === "pen") {
        this.commitPathRoi(this.draft);
        this.draft = null;
        e.preventDefault();
      } else if (e.key === "Escape") {
        if (this.draft) {
          this.draft = null;
        } else {
          this.stopDrawing();
        }
        e.preventDefault();
      }
    },
    inferActionFromNote() {
      // Let the connected agent host choose its analysis workflow.
      return "agent";
    },
    renderVpOverlay(img, rois) {
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      const list =
        rois && rois.length
          ? rois
          : [
              {
                role: "target",
                norm: { x: 0, y: 0, width: 1, height: 1 }
              }
            ];
      list.forEach((r, i) => {
        if (!r.norm) return;
        const color = r.role === "source" ? "#508cdc" : "#50b478";
        const x = r.norm.x * canvas.width;
        const y = r.norm.y * canvas.height;
        const w = r.norm.width * canvas.width;
        const h = r.norm.height * canvas.height;
        ctx.strokeStyle = color;
        ctx.lineWidth = Math.max(2, Math.round(canvas.width / 400));
        ctx.strokeRect(x, y, w, h);
        if (r.points && r.points.length >= 2) {
          ctx.beginPath();
          r.points.forEach((p, pi) => {
            const px = p.x * canvas.width;
            const py = p.y * canvas.height;
            if (pi === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
          });
          ctx.stroke();
        }
        ctx.fillStyle = color;
        ctx.font = `${Math.max(
          14,
          Math.round(canvas.width / 60)
        )}px sans-serif`;
        ctx.fillText(`${r.role || "roi"}#${i}`, x + 6, Math.max(18, y + 20));
      });
      return canvas.toDataURL("image/jpeg", 0.9);
    },
    buildPayload() {
      const img =
        this.imageEl ||
        this.findSurfaceImage() ||
        document.querySelector(".openmhs-main-surface .main img");
      if (!img || !img.naturalWidth) {
        throw new Error("No live/preview image available");
      }
      const rect = this.displayedRect(img);
      this.imageEl = img;
      this.imageRect = rect;

      const frame = document.createElement("canvas");
      frame.width = img.naturalWidth;
      frame.height = img.naturalHeight;
      frame.getContext("2d").drawImage(img, 0, 0, frame.width, frame.height);
      const frameUrl = frame.toDataURL("image/jpeg", 0.9);
      const vpUrl = this.renderVpOverlay(img, this.rois);

      const target =
        this.rois.find(r => r.role === "target") ||
        (this.rois.length === 0
          ? {
              role: "target",
              shape: "bbox",
              norm: { x: 0, y: 0, width: 1, height: 1 }
            }
          : null);
      const source = this.rois.find(r => r.role === "source");

      let cropUrl = frameUrl;
      let primaryRoi = { x: 0, y: 0, width: 1, height: 1 };
      if (target && target.norm) {
        primaryRoi = { ...target.norm };
        const sx = Math.round(primaryRoi.x * frame.width);
        const sy = Math.round(primaryRoi.y * frame.height);
        const sw = Math.max(1, Math.round(primaryRoi.width * frame.width));
        const sh = Math.max(1, Math.round(primaryRoi.height * frame.height));
        const crop = document.createElement("canvas");
        crop.width = sw;
        crop.height = sh;
        crop.getContext("2d").drawImage(frame, sx, sy, sw, sh, 0, 0, sw, sh);
        cropUrl = crop.toDataURL("image/jpeg", 0.94);
      }

      const roisPayload = this.rois.map(r => ({
        role: r.role,
        shape: r.shape,
        ...(r.norm || {}),
        points: r.points || undefined
      }));
      if (!roisPayload.length) {
        roisPayload.push({
          role: "target",
          shape: "bbox",
          x: 0,
          y: 0,
          width: 1,
          height: 1
        });
      }

      return {
        action: "agent",
        canvas_revision: this.surfaceContext.revision,
        canvas_bounds: this.surfaceContext.bounds,
        canvas_reference: this.surfaceContext.reference,
        note: this.note,
        roi: primaryRoi,
        rois: roisPayload,
        surface: this.surfaceKind,
        has_source: !!source,
        frame_data_url: frameUrl,
        roi_data_url: cropUrl,
        vp_overlay_data_url: vpUrl,
        view_id: this.currentViewId || undefined,
        record_id:
          this.mosaicSessionId ||
          (this.canvasContexts[0] && this.canvasContexts[0].id) ||
          undefined,
        context_records: (this.canvasContexts || []).map(c => ({
          id: c.id,
          title: c.title,
          kind: c.kind || "mosaic"
        }))
      };
    },
    maskStyle(a) {
      const c = a.color || "#50b478";
      return {
        fill: c.length === 7 ? `${c}55` : "rgba(80,180,120,0.35)",
        stroke: c,
        strokeWidth: 1.5
      };
    },
    applyJobResult(result) {
      if (!result) return;
      this.jobMarkdown = this.cleanAssistantText(
        result.markdown || result.summary || this.jobMarkdown || ""
      );
      this.jobError = result.error || "";
      if (Array.isArray(result.annotations)) {
        this.resultAnnotations = result.annotations;
      }
      if (
        (!this.jobProgress || !this.jobProgress.length) &&
        Array.isArray(result.actions_log) &&
        result.actions_log.length
      ) {
        this.jobProgress = result.actions_log.map(a => ({
          message: a.detail
            ? `${a.op || "step"} — ${a.detail}`
            : a.op || "step",
          op: a.op || "",
          kind: a.ok === false ? "error" : "tool"
        }));
      }
      this.maskOverlayUrl =
        result.mask_overlay_data_url || this.maskOverlayUrl || "";
      const assistantText = this.jobMarkdown;
      if (assistantText) {
        const last = this.chatMessages[this.chatMessages.length - 1];
        if (
          !(last && last.role === "assistant" && last.content === assistantText)
        ) {
          this.chatMessages.push({
            role: "assistant",
            content: assistantText
          });
        }
      }
      this.open = true;
      this.rememberCurrentOverlayBag();
      this.$nextTick(() => {
        this.ensureSurfaceRect(true);
        this.syncRoisToSurface();
        this.scrollThreadToBottom();
      });
      this.persistAgentSnapshot();
    },
    ensureSurfaceRect(force) {
      const img = this.findSurfaceImage();
      if (!img || !img.naturalWidth) return null;
      const rect = this.displayedRect(img);
      if (
        !force &&
        this.imageRect &&
        this.imageEl === img &&
        Math.abs(this.imageRect.left - rect.left) < 0.5 &&
        Math.abs(this.imageRect.top - rect.top) < 0.5 &&
        Math.abs(this.imageRect.width - rect.width) < 0.5 &&
        Math.abs(this.imageRect.height - rect.height) < 0.5
      ) {
        return this.imageRect;
      }
      this.imageEl = img;
      this.imageRect = rect;
      return rect;
    },
    cleanAssistantText(text) {
      let t = String(text || "");
      // Strip legacy debug dumps from older workers
      t = t.replace(/\n*###\s*Actions[\s\S]*$/i, "");
      t = t.replace(/\n*###\s*Attempts[\s\S]*?(?=\n###|\n_host:|$)/gi, "");
      t = t.replace(/\n*_host:\s*[^\n]*\n?/gi, "");
      return t.trim();
    },
    scrollThreadToBottom() {
      const el = this.$refs.threadEl;
      if (el) el.scrollTop = el.scrollHeight;
    },
    onComposerKeydown(e) {
      if (e.key !== "Enter" || e.shiftKey) return;
      // During IME composition, Enter confirms a candidate instead of sending.
      if (e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      if (this.saving || this.jobThinking || !(this.note || "").trim()) return;
      this.submit();
    },
    onJobTraceToggle(ev) {
      const el = ev && ev.target;
      if (el && typeof el.open === "boolean") {
        this.jobTraceOpen = el.open;
      }
    },
    clearResults() {
      this.clearRequestRender();
    },
    stopJobPoll() {
      if (this.jobPollTimer) {
        clearInterval(this.jobPollTimer);
        this.jobPollTimer = null;
      }
    },
    startJobPoll(jobId) {
      if (this.codexMode) return;
      this.stopJobPoll();
      this.jobId = jobId;
      this.pollJobOnce();
      // Faster while running so stream tokens show up promptly
      this.jobPollTimer = setInterval(this.pollJobOnce, 500);
    },
    async pollJobOnce() {
      if (!this.jobId) return;
      try {
        const { data } = await axios.get(`${this.vpBase}/jobs/${this.jobId}`, {
          timeout: 10000
        });
        const job = (data && data.job) || data;
        if (!job) return;
        this.jobStatus = job.status || "";
        if (Array.isArray(job.progress) && job.progress.length) {
          this.jobProgress = job.progress;
          this.$nextTick(() => this.scrollThreadToBottom());
        } else if (this.jobStatus === "queued" && !this.jobProgress.length) {
          this.jobProgress = [
            {
              message: "Queued — waiting for PC worker…",
              kind: "llm",
              op: "queued"
            }
          ];
        }
        const result = job.result || null;
        if (result) {
          this.applyJobResult(result);
        }
        if (
          this.jobStatus === "done" ||
          this.jobStatus === "failed" ||
          this.jobStatus === "cancelled"
        ) {
          this.stopJobPoll();
          if (this.jobStatus === "done") {
            this.message = "";
            this.messageError = false;
          } else if (this.jobStatus === "failed") {
            this.message = this.jobError || "Job failed";
            this.messageError = true;
          } else if (this.jobStatus === "cancelled") {
            this.message = "Interrupted";
            this.messageError = false;
          }
        }
      } catch (err) {
        // Keep polling through transient errors
      }
    },
    async syncSelection() {
      if (this.saving) {
        this.selectionPending = true;
        return;
      }
      this.saving = true;
      this.message = "Syncing selection…";
      try {
        const payload = this.buildPayload();
        payload.note = "";
        payload.selection_cleared = !this.rois.length;
        payload.context_only = true;
        await axios.post(`${this.vpBase}/codex/prompts`, payload, {
          timeout: 60000
        });
        this.messageError = false;
        this.message = this.rois.length
          ? "Selection available to Codex · ask in the conversation"
          : "Selection cleared · available to Codex";
      } catch (e) {
        this.messageError = true;
        this.message =
          "Selection not synced. " + (e.message || "Check connection");
      } finally {
        this.saving = false;
        if (this.selectionPending) {
          this.selectionPending = false;
          this.syncSelection();
        }
      }
    },
    submit: async function() {
      this.saving = true;
      this.message = "";
      this.messageError = false;
      try {
        const payload = this.buildPayload();
        if (this.codexMode) {
          const { data } = await axios.post(
            `${this.vpBase}/codex/prompts`,
            payload,
            { timeout: 60000 }
          );
          this.message = `Saved ${data.id}. Ask Codex to read the latest visual prompt.`;
          return;
        }
        this.lastAction = payload.action || "agent";
        if (!this.sessionId) {
          const created = await axios.post(
            `${this.vpBase}/sessions`,
            {
              note: payload.note,
              view_id: this.currentViewId || undefined
            },
            { timeout: 15000 }
          );
          this.sessionId =
            (created.data && created.data.session && created.data.session.id) ||
            null;
          if (this.sessionId && this.currentViewId) {
            try {
              await axios.post(
                `${this.$store.getters.baseUri}/api/v2/extensions/org.openmhs.scan-mosaic/mac/views`,
                {
                  action: "bind-session",
                  view_id: this.currentViewId,
                  session_id: this.sessionId
                },
                { timeout: 10000 }
              );
            } catch (_) {
              /* optional Mac bind */
            }
          }
        }
        let response;
        if (this.sessionId) {
          this.chatMessages.push({
            role: "user",
            content: payload.note || "(empty)"
          });
          this.$nextTick(() => this.scrollThreadToBottom());
          response = await axios.post(
            `${this.vpBase}/sessions/${this.sessionId}/messages`,
            payload,
            { timeout: 60000 }
          );
        } else {
          response = await axios.post(`${this.vpBase}/prompts`, payload, {
            timeout: 60000
          });
        }
        const jobId =
          (response.data && (response.data.job_id || response.data.id)) || null;
        this.note = "";
        if (jobId) {
          this.jobMarkdown = "";
          this.jobError = "";
          this.jobProgress = [];
          this.jobTraceOpen = true;
          this.jobStatus = "queued";
          this.startJobPoll(jobId);
        }
      } catch (err) {
        this.messageError = true;
        this.message =
          (err.response && err.response.data && err.response.data.message) ||
          (err.response && err.response.data && err.response.data.error) ||
          err.message ||
          "Failed to save prompt";
      } finally {
        this.saving = false;
      }
    },
    async newSession() {
      // New chat ≡ new Camera
      this.$emit("new-camera-chat");
    },
    async startSessionForView(viewId, title) {
      if (this.codexMode) return;
      const vid = String(viewId || "").trim();
      try {
        const { data } = await axios.post(
          `${this.vpBase}/sessions`,
          { view_id: vid || undefined, note: title || "" },
          { timeout: 15000 }
        );
        this.sessionId = (data && data.session && data.session.id) || null;
        this.chatMessages = [];
        this.jobMarkdown = "";
        this.jobError = "";
        this.jobProgress = [];
        this.resultAnnotations = [];
        this.maskOverlayUrl = "";
        this.rois = [];
        this.canvasContexts = [];
        this.rememberCurrentOverlayBag();
        this.jobId = null;
        this.jobStatus = "";
        this.message = "";
        this.messageError = false;
        this.stopJobPoll();
        this.$emit("roi-count", 0);
        if (vid && this.sessionId) {
          try {
            await axios.post(
              `${this.$store.getters.baseUri}/api/v2/extensions/org.openmhs.scan-mosaic/mac/views`,
              {
                action: "bind-session",
                view_id: vid,
                session_id: this.sessionId
              },
              { timeout: 10000 }
            );
          } catch (_) {
            /* optional Mac bind */
          }
        }
        this.loadViewsTree();
      } catch (err) {
        this.messageError = true;
        this.message = err.message || "Failed to create session";
      }
    },
    async loadSessionForView(viewId, sessionIdHint) {
      if (this.codexMode) return;
      const vid = String(viewId || "").trim();
      this.clearRequestRender();
      this.chatMessages = [];
      this.jobMarkdown = "";
      this.jobError = "";
      this.jobProgress = [];
      this.rois = [];
      this.$emit("roi-count", 0);
      let sid = sessionIdHint || null;
      if (!sid && vid) {
        try {
          const { data } = await axios.get(`${this.vpBase}/sessions`, {
            params: { view_id: vid },
            timeout: 8000
          });
          const sess = (data && data.session) || (data && data.latest) || null;
          if (sess && sess.id && (!sess.view_id || sess.view_id === vid)) {
            sid = sess.id;
          }
        } catch (_) {
          /* fall through */
        }
      }
      if (!sid) {
        this.sessionId = null;
        this.message = "No chat yet for this Camera — send a message to start";
        this.messageError = false;
        this.loadViewsTree();
        return;
      }
      try {
        const { data } = await axios.get(`${this.vpBase}/sessions/${sid}`, {
          timeout: 10000
        });
        const session = (data && data.session) || {};
        this.sessionId = session.id || sid;
        const msgs = session.messages || [];
        this.chatMessages = msgs
          .filter(m => m && (m.role === "user" || m.role === "assistant"))
          .map(m => ({
            role: m.role,
            content: m.content || ""
          }));
        if (Array.isArray(session.rois) && session.rois.length) {
          // ROIs are normalized; surface sync will map px
          this.rois = session.rois.map(r => ({ ...r }));
          this.$emit("roi-count", this.rois.length);
        }
        if (session.draft_annotations) {
          this.resultAnnotations = session.draft_annotations;
        }
        if (session.draft_mask_data_url) {
          this.maskOverlayUrl = session.draft_mask_data_url;
        }
        this.message = "";
        this.messageError = false;
        this.$nextTick(
          () => this.syncRoisToSurface && this.syncRoisToSurface()
        );
      } catch (err) {
        this.sessionId = sid;
        this.messageError = true;
        this.message = err.message || "Failed to load Camera chat";
      }
      this.loadViewsTree();
    },
    async addToTraining() {
      this.savingAnnotation = true;
      try {
        const payload = this.buildPayload();
        await axios.post(
          `${this.vpBase}/annotations`,
          {
            session_id: this.sessionId,
            note: this.jobMarkdown || payload.note || "",
            image_data_url: payload.roi_data_url || payload.frame_data_url,
            mask_data_url: this.maskOverlayUrl || undefined,
            annotations: this.resultAnnotations,
            split: "train"
          },
          { timeout: 60000 }
        );
        this.message = "Saved to training set on Pi";
        this.messageError = false;
      } catch (err) {
        this.messageError = true;
        this.message =
          (err.response && err.response.data && err.response.data.error) ||
          err.message ||
          "Failed to save annotation";
      } finally {
        this.savingAnnotation = false;
      }
    }
  }
};
</script>

<style scoped>
.codex-status-panel {
  position: fixed;
  top: 82px;
  right: 16px;
  z-index: 100;
  background: #18231fed;
  color: #e0eee6;
  padding: 10px;
  border-radius: 9px;
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  max-width: 360px;
}
.codex-status-panel small {
  width: 100%;
  font-size: 10px;
}
.codex-materials[open] {
  width: 330px;
  max-height: 65vh;
  overflow: auto;
}
.codex-materials summary {
  cursor: pointer;
}
.roi-panel textarea {
  width: 100%;
  color: #17271e;
}
.agent-tool-root {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: hidden;
}
.roi-fab,
.codex-status-panel,
.roi-panel,
.agent-box,
.roi-widget {
  pointer-events: auto;
}
.roi-fab {
  position: fixed;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border: none;
  border-radius: 999px;
  background: #1a1a1a;
  color: #f5f5f5;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  cursor: grab;
  font-size: 12px;
  touch-action: none;
}
.roi-fab:active {
  cursor: grabbing;
}
.roi-fab .material-icons {
  font-size: 18px;
}
.roi-fab-count {
  font-style: normal;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: #3d8bfd;
  color: #fff;
  font-size: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.roi-panel {
  position: fixed;
  z-index: 31;
  width: min(340px, calc(100vw - 24px));
  background: rgba(14, 14, 16, 0.96);
  color: #ececec;
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
  border: 1px solid rgba(255, 255, 255, 0.08);
  overflow: hidden;
}
.roi-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  cursor: grab;
}
.sidebar-resize {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 6px;
  cursor: ew-resize;
  z-index: 5;
}
.sidebar-resize:hover,
.sidebar-resize:active {
  background: rgba(61, 139, 253, 0.35);
}
.agent-box {
  position: absolute;
  z-index: 30;
  background: rgba(14, 14, 16, 0.98);
  color: #ececec;
  box-shadow: -8px 0 28px rgba(0, 0, 0, 0.35);
  padding: 0;
  backdrop-filter: blur(10px);
  display: flex;
  flex-direction: column;
}
.agent-box.agent-sidebar {
  top: 0;
  right: 0;
  bottom: 0;
  width: var(--openmhs-agent-sidebar-w, 400px);
  min-width: 280px;
  max-width: min(720px, 92vw);
  height: auto;
  max-height: none;
  overflow: hidden;
  border-radius: 0;
  border-left: 1px solid rgba(255, 255, 255, 0.08);
}
.roi-widget {
  position: absolute;
  z-index: 6;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 4px;
  border-radius: 6px 6px 0 0;
  background: rgba(18, 18, 18, 0.82);
  color: #f2f2f2;
  cursor: grab;
  user-select: none;
  box-sizing: border-box;
}
.roi-widget.dragging {
  cursor: grabbing;
  opacity: 0.92;
}
.roi-widget.target {
  box-shadow: inset 0 0 0 1px rgba(80, 180, 120, 0.7);
}
.roi-widget.source {
  box-shadow: inset 0 0 0 1px rgba(80, 140, 220, 0.7);
}
.roi-tag {
  font-size: 11px;
  opacity: 0.9;
  margin-right: 2px;
}
.roi-btn {
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: 0;
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
}
.roi-btn:hover {
  background: rgba(255, 255, 255, 0.12);
}
.roi-btn .material-icons {
  font-size: 14px;
}
.agent-box-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  cursor: default;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  flex: 0 0 auto;
  background: rgba(14, 14, 16, 0.98);
  z-index: 1;
}
.head-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.head-left strong {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.01em;
}
.head-right {
  display: flex;
  align-items: center;
  gap: 2px;
  min-width: 0;
}
.icon-btn {
  background: transparent;
  border: none;
  color: inherit;
  cursor: pointer;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  opacity: 0.75;
}
.icon-btn:hover {
  background: rgba(255, 255, 255, 0.08);
  opacity: 1;
}
.icon-btn .material-icons {
  font-size: 18px;
}
.icon-btn:disabled {
  opacity: 0.35;
  cursor: default;
}
.model-select.head-model {
  max-width: 120px;
  font-size: 10px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(0, 0, 0, 0.35);
  color: inherit;
  padding: 3px 4px;
  margin-right: 4px;
}
.workspace-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  font-size: 10px;
  opacity: 0.85;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  flex: 0 0 auto;
  min-width: 0;
}
.view-tree-wrap {
  flex: 0 0 auto;
  max-height: 32%;
  overflow: auto;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.view-row {
  font-weight: 600;
}
.record-row {
  padding-left: 22px !important;
  opacity: 0.92;
}
.record-row.video span::after {
  content: " · video";
  opacity: 0.55;
  font-size: 10px;
}
.legacy-label {
  font-size: 10px;
  opacity: 0.55;
  padding: 6px 10px 2px;
}
.agent-sidebar.pc-offline .composer,
.agent-sidebar.pc-offline .agent-tools {
  opacity: 0.45;
}
.workspace-bar .material-icons {
  font-size: 14px;
  flex: 0 0 auto;
  opacity: 0.7;
}
.workspace-input {
  flex: 1 1 auto;
  min-width: 0;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.28);
  color: inherit;
  font-size: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  padding: 3px 6px;
}
.workspace-input:disabled {
  opacity: 0.5;
}
.workspace-apply {
  flex: 0 0 auto;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 6px;
  padding: 2px 8px;
  cursor: pointer;
  font-size: 10px;
  background: rgba(255, 255, 255, 0.08);
  color: inherit;
}
.workspace-apply:disabled {
  opacity: 0.4;
  cursor: default;
}
.ws-tree-wrap {
  flex: 0 0 auto;
  max-height: 28%;
  min-height: 88px;
  display: flex;
  flex-direction: column;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.ws-tree-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px 0 10px;
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  opacity: 0.65;
}
.ws-tree-head .icon-btn .material-icons {
  font-size: 16px;
}
.ws-tree-head .head-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}
.ws-tree {
  overflow: auto;
  padding: 4px 4px 8px;
}
.ws-tree-empty,
.ws-tree-err {
  padding: 8px 12px;
  font-size: 12px;
  opacity: 0.7;
}
.ws-tree-err {
  color: #f0b0b0;
}
.ws-row {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  line-height: 1.4;
  padding: 2px 6px 2px 0;
  border-radius: 4px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ws-row:hover {
  background: rgba(255, 255, 255, 0.08);
}
.ws-row.active {
  background: rgba(90, 140, 220, 0.22);
}
.ws-row.mosaic {
  color: #c5d8ff;
}
.ws-row .material-icons {
  font-size: 15px;
  opacity: 0.85;
  flex: 0 0 auto;
}
.workspace-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.conn-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
  background: #777;
}
.conn-dot.on {
  background: #50b478;
  box-shadow: 0 0 0 2px rgba(80, 180, 120, 0.25);
}
.conn-dot.warn {
  background: #ffc850;
}
.conn-dot.off {
  background: #ff6b6b;
}
.conn-label {
  font-size: 11px;
  opacity: 0.75;
  max-width: 148px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.composer-stop {
  background: #b33 !important;
}
.start-worker-btn {
  flex: 0 0 auto;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 6px;
  padding: 2px 8px;
  cursor: pointer;
  font-size: 10px;
  line-height: 1.3;
  background: rgba(61, 139, 253, 0.2);
  color: #cfe3ff;
}
.start-worker-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.local-hint {
  margin: 0;
  padding: 0 12px 6px;
  font-size: 11px;
  opacity: 0.7;
  line-height: 1.35;
  flex: 0 0 auto;
}
.local-hint.err {
  color: #ff8f8f;
  opacity: 1;
}
.agent-tools {
  padding: 8px 10px 6px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  flex: 0 0 auto;
}
.tool-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}
.tool-sep {
  width: 1px;
  height: 16px;
  background: rgba(255, 255, 255, 0.12);
  margin: 0 2px;
}
.tool-row button {
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid transparent;
  color: inherit;
  border-radius: 7px;
  padding: 5px 7px;
  cursor: pointer;
  font-size: 11px;
}
.tool-row button.active {
  border-color: rgba(108, 182, 255, 0.55);
  background: rgba(108, 182, 255, 0.16);
}
.tool-row .material-icons {
  font-size: 16px;
  vertical-align: middle;
}
.stop-draw {
  background: rgba(255, 160, 80, 0.2) !important;
}
.clear-btn {
  opacity: 0.85;
  font-size: 10px !important;
  padding: 5px 8px !important;
}
.clear-btn:disabled {
  opacity: 0.35;
  cursor: default;
}
.chip-clear {
  border: none;
  background: transparent;
  color: inherit;
  opacity: 0.55;
  font-size: 10px;
  cursor: pointer;
  text-decoration: underline;
  padding: 0;
}
.roi-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 6px;
  align-items: center;
}
.chip {
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.1);
}
.chip.target {
  background: rgba(80, 180, 120, 0.35);
}
.chip.source {
  background: rgba(80, 140, 220, 0.35);
}
.chip button {
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  margin-left: 2px;
}
.chat-thread {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 10px 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.thread-empty {
  margin: auto 0;
  text-align: center;
  font-size: 12px;
  opacity: 0.45;
  line-height: 1.45;
  padding: 24px 8px;
}
.thread-item {
  max-width: 100%;
}
.msg-user .msg-body,
.msg-assistant .msg-body {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font: inherit;
  font-size: 13px;
  line-height: 1.45;
}
.msg-user {
  align-self: stretch;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 10px;
  padding: 8px 10px;
}
.msg-assistant {
  padding: 2px 2px 4px;
}
.msg-assistant.streaming .msg-body {
  opacity: 0.92;
}
.caret {
  display: inline-block;
  width: 6px;
  margin-left: 1px;
  animation: blink 1s step-end infinite;
  color: #8ab4ff;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.msg-thinking {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.22);
  font-size: 11px;
  overflow: hidden;
}
.msg-thinking summary {
  padding: 6px 8px;
  cursor: pointer;
  list-style: none;
  opacity: 0.8;
  user-select: none;
}
.msg-thinking summary::-webkit-details-marker {
  display: none;
}
.think-meta {
  float: right;
  opacity: 0.45;
}
.msg-thinking ol {
  margin: 0;
  padding: 0 10px 8px 22px;
  max-height: 120px;
  overflow: auto;
  line-height: 1.4;
  opacity: 0.8;
}
.msg-thinking li.tool {
  color: #9ecbff;
}
.msg-thinking li.compact {
  color: #c9b6ff;
  font-weight: 600;
}
.msg-thinking li.llm {
  color: #b8d4c8;
}
.msg-thinking li.error {
  color: #ffb0b0;
}
.msg-error {
  color: #ff9a9a;
  font-size: 12px;
  line-height: 1.4;
}
.msg-system {
  font-size: 11px;
  opacity: 0.55;
}
.msg-action {
  margin-top: 6px;
  border: none;
  background: transparent;
  color: inherit;
  opacity: 0.5;
  font-size: 11px;
  cursor: pointer;
  text-decoration: underline;
  padding: 0;
}
.composer-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 12px;
  font-size: 11px;
  background: rgba(255, 100, 100, 0.1);
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  flex: 0 0 auto;
}
.context-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 6px 10px 0;
  flex: 0 0 auto;
}
.context-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  opacity: 0.55;
}
.context-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  border: 1px solid rgba(108, 182, 255, 0.35);
  background: rgba(108, 182, 255, 0.12);
  color: #d7ebff;
  border-radius: 999px;
  padding: 3px 8px 3px 6px;
  font-size: 11px;
  cursor: pointer;
}
.context-chip .material-icons {
  font-size: 14px;
}
.context-chip span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 140px;
}
.context-chip .chip-x {
  font-size: 12px;
  opacity: 0.65;
}
.context-chip .chip-x:hover {
  opacity: 1;
}
.chat-thread.drop-target,
.composer.drop-target {
  outline: 1px dashed rgba(108, 182, 255, 0.7);
  outline-offset: -2px;
  background: rgba(108, 182, 255, 0.06);
}
.composer {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 8px 10px 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.07);
  background: rgba(10, 10, 12, 0.95);
}
.composer-input {
  flex: 1;
  min-width: 0;
  box-sizing: border-box;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(0, 0, 0, 0.35);
  color: inherit;
  padding: 8px 10px;
  resize: none;
  font: inherit;
  font-size: 13px;
  line-height: 1.4;
  max-height: 120px;
}
.composer-input:disabled {
  opacity: 0.55;
}
.composer-send {
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: 50%;
  background: #3d8bfd;
  color: #fff;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.composer-send:disabled {
  opacity: 0.35;
  cursor: default;
}
.composer-send .material-icons {
  font-size: 18px;
}
.roi-layer {
  position: absolute;
  z-index: 2;
  pointer-events: none;
  overflow: hidden;
}
.roi-overlay {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  z-index: 2;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.16s ease;
}
.roi-overlay.is-ready {
  opacity: 1;
}
.mask-overlay-img {
  position: absolute;
  z-index: 1;
  pointer-events: none;
  object-fit: fill;
  opacity: 0;
  transition: opacity 0.16s ease;
}
.mask-overlay-img.is-ready {
  opacity: 0.85;
}
.roi-overlay rect,
.roi-overlay polyline,
.roi-overlay polygon {
  stroke-width: 2;
  fill: rgba(80, 180, 120, 0.12);
  stroke: #50b478;
}
.roi-overlay polyline.draw {
  stroke-width: 3;
  fill: none;
}
.roi-overlay polyline.pen {
  stroke-width: 2;
  stroke-linejoin: round;
  fill: none;
}
.roi-overlay .source {
  fill: rgba(80, 140, 220, 0.12);
  stroke: #508cdc;
}
.roi-overlay .draft {
  fill: rgba(255, 255, 255, 0.08);
  stroke: #fff;
  stroke-dasharray: 4 3;
}
.pen-vertex {
  fill: #fff;
  stroke: #508cdc;
  stroke-width: 1;
}
.roi-overlay .result-ann {
  fill: rgba(255, 200, 80, 0.1);
  stroke: #ffc850;
  stroke-width: 2;
}
.roi-overlay .result-ann.mask {
  stroke-width: 1.5;
}
.roi-overlay .result-ann.point {
  fill: #ffc850;
  stroke: #fff;
}
.roi-overlay .result-label {
  fill: #ffe9a8;
  font-size: 12px;
  font-family: system-ui, sans-serif;
  paint-order: stroke;
  stroke: rgba(0, 0, 0, 0.65);
  stroke-width: 3px;
}
</style>
