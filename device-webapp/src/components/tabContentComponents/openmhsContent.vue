<template>
  <div
    class="openmhs-root"
    :class="{
      'scan-focus': viewFocus === 'scan' && scanPipVisible,
      locked: controlsLocked,
      'agent-sidebar-open': agentSidebarOpen,
      'pc-drop-over': pcDropOver,
      'canvas-open': !!studioSession
    }"
    @dragover.prevent="onPcDragOver"
    @dragleave="onPcDragLeave"
    @drop.prevent="onPcDrop"
  >
    <header v-if="!studioSession" class="mhs-toolbar" @pointerdown.stop>
      <div class="mhs-brand">
        <strong>MicroWorld</strong><span>Camera · Canvas</span>
      </div>
      <div class="mhs-tools">
        <button title="ROI tools" @click.stop="openRoiDock">
          <i class="material-icons">crop_free</i><span>ROI</span>
        </button>
        <button
          :class="{ active: stageHudOpen }"
          title="Stage controls"
          @click.stop="stageHudOpen = !stageHudOpen"
        >
          <i class="material-icons">control_camera</i><span>XYZ</span>
        </button>
        <button title="Add canvas" @click.stop="toggleCameraTiles">
          <i class="material-icons">add_photo_alternate</i
          ><span>Add canvas</span>
        </button>
        <div v-if="cameraTilesOpen" class="camera-tiles-menu" @click.stop>
          <div class="camera-tiles-head">
            <strong>Add canvas</strong>
            <button title="Close" @click="cameraTilesOpen = false">×</button>
          </div>
          <button
            class="camera-tiles-new"
            :disabled="scanRunning"
            @click="openScanSetup"
          >
            <i class="material-icons">add</i>
            <span>New canvas</span>
          </button>
          <div class="camera-tiles-divider">Open existing</div>
          <button
            v-for="r in canvasRecords"
            :key="r.id"
            class="camera-tile-record"
            @click="addRecordToCamera(r)"
          >
            <i class="material-icons">collections</i>
            <span>{{ r.title || r.id }}</span>
            <small>{{ r.view_title || "Camera" }}</small>
          </button>
          <p v-if="!canvasRecords.length">No saved Canvas yet.</p>
          <button class="camera-tiles-import" @click="importCanvasFromOther">
            Import a Canvas copy…
          </button>
        </div>
      </div>
    </header>
    <div v-if="hardwareBusy && !studioSession" class="hardware-lock-hint" @pointerdown.stop>
      Microscope busy · {{ hardwareOperation }}
    </div>
    <div
      ref="mainSurface"
      class="openmhs-main-surface"
      :class="{ interactive: !agentDrawing }"
      @pointerdown="onSurfacePointerDown"
      @pointermove="onSurfacePointerMove"
      @pointerup="onSurfacePointerUp"
      @pointercancel="onSurfacePointerUp"
      @wheel="onWheel"
    >
      <!-- Camera layer -->
      <div
        class="camera-layer"
        :class="cameraLayerClass"
        :style="cameraLayerStyle"
        @pointerdown="onCameraLayerPointerDown"
        @click.stop="onCameraPipClick"
      >
        <streamDisplay />
        <span v-if="cameraLayerClass.includes('pip')" class="pip-label">
          {{
            cameraLayerClass.includes("moving-camera")
              ? "Camera · live"
              : "Live"
          }}
        </span>
      </div>

      <div
        v-for="(p, index) in canvasPips"
        v-show="!studioSession"
        :key="p.id"
        class="canvas-pip-card"
        :style="canvasPipStyle(p, index)"
        @pointerdown.stop="startCanvasPipDrag(p, $event)"
        @click.stop="openCanvasPip(p)"
      >
        <img :src="canvasPreviewUrl(p)" :alt="p.title || p.id" />
        <span>{{ p.title || p.id }}</span>
        <button
          title="Close canvas"
          @pointerdown.stop
          @click.stop="requestCanvasClose(p.id)"
        >
          ×
        </button>
      </div>

      <!-- Mosaic / scan layer -->
      <div
        v-if="showMosaicLayer && !canvasPipHidden"
        ref="mosaicLayer"
        class="mosaic-layer"
        :class="[mosaicLayerClass, { zoomable: scanZoomEnabled }]"
        @pointerdown.stop="onMosaicLayerPointerDown"
        @pointermove.stop="onMosaicLayerPointerMove"
        @pointerup.stop="onMosaicLayerPointerUp"
        @pointercancel.stop="onMosaicLayerPointerUp"
        @click.stop="onMosaicLayerClick"
        @dblclick.stop="onMosaicDblClick"
      >
        <div class="mosaic-zoom" :style="mosaicZoomStyle">
          <img
            v-if="mosaicUrl"
            ref="mosaicImg"
            class="mosaic-img"
            :class="{ 'mode-hidden': hideSlideImage }"
            :src="mosaicUrl"
            alt="slide mosaic"
            draggable="false"
            @load="onMosaicImgLoad"
          />
          <div v-else class="mosaic-placeholder">
            {{ scanStatus || "Capturing start FOV…" }}
          </div>
          <div
            v-if="pyramidEnabled && pyramidLayerBox && pyramidTiles.length"
            class="pyramid-layer"
            :style="pyramidLayerStyle"
          >
            <img
              v-for="t in pyramidTiles"
              :key="t.key"
              class="pyramid-tile"
              :src="t.url"
              :style="t.style"
              alt=""
              draggable="false"
            />
          </div>
          <div
            v-if="tilesLodEnabled && lodLayerBox"
            class="lod-layer"
            :style="lodLayerStyle"
          >
            <img
              v-for="t in lodTiles"
              :key="t.key"
              class="lod-tile"
              :src="t.url"
              :style="t.style"
              alt=""
              draggable="false"
            />
          </div>
          <div
            v-if="mosaicUrl && stageMarkerStyle && showStageMarker"
            class="stage-marker"
            :class="{ highlight: viewFocus === 'scan' }"
            :style="stageMarkerStyle"
          />
        </div>
        <span v-if="mosaicLayerClass.includes('pip')" class="pip-label">
          Canvas
        </span>
        <span
          v-if="scanZoomEnabled && scanView.scale > 1.02"
          class="zoom-badge"
        >
          {{ scanView.scale.toFixed(1) }}×
        </span>
        <button
          v-if="viewFocus === 'scan' && scanPipVisible && mosaicUrl"
          type="button"
          class="marker-toggle"
          :title="showStageMarker ? 'Hide FOV marker' : 'Show FOV marker'"
          @click.stop="showStageMarker = !showStageMarker"
          @pointerdown.stop
        >
          <i class="material-icons">{{
            showStageMarker ? "crop_free" : "crop_din"
          }}</i>
        </button>
        <button
          v-if="mosaicLayerClass.includes('pip')"
          type="button"
          class="pip-close"
          title="Close Canvas"
          @click.stop="closeScanSession"
          @pointerdown.stop
        >
          <i class="material-icons">close</i>
        </button>
      </div>

      <!-- Refine progress (works in background while Scan PiP still open) -->
      <div v-if="refineBusy" class="refine-progress">
        <div
          class="refine-progress-bar"
          :style="{ width: refineProgress + '%' }"
        />
        <span class="refine-progress-label"
          >Refining… {{ Math.round(refineProgress) }}%</span
        >
      </div>
    </div>

    <div v-if="videoRecordOpen" class="video-record-banner">
      <strong>Video Canvas</strong>
      <span
        >{{ videoRecordTitle || "Placeholder" }} — timeline not wired yet. Stage
        still moves.</span
      >
      <button type="button" @click.stop="closeVideoRecord">Close</button>
    </div>

    <!-- Canvas setup (top-left). Camera = chat; created via Agent "New". -->
    <div
      v-if="showScanFab"
      class="hud-float hud-scan"
      :style="floatStyle(hudScan)"
    >
      <div v-if="!scanSetupOpen" class="record-stack">
        <button
          type="button"
          class="scan-entry"
          :title="
            currentViewTitle
              ? `Canvas under ${currentViewTitle}`
              : 'Create a Canvas mosaic/video'
          "
          @click.stop="openScanSetup"
          @pointerdown.stop
        >
          <i class="material-icons">fiber_manual_record</i>
          <span>Canvas</span>
        </button>
      </div>
      <div
        v-else
        class="scan-setup"
        @pointerdown.stop="startHudDrag($event, 'hudScan')"
      >
        <div class="kind-row">
          <button
            type="button"
            :class="{ active: recordKind === 'mosaic' }"
            @click="recordKind = 'mosaic'"
          >
            Mosaic
          </button>
          <button
            type="button"
            :class="{ active: recordKind === 'video' }"
            title="Stage can still move; capture pipeline comes later"
            @click="recordKind = 'video'"
          >
            Video
          </button>
        </div>
        <template v-if="recordKind === 'mosaic'">
          <div class="grid-row">
            <button
              v-for="g in gridOptions"
              :key="g.id"
              type="button"
              :class="{ active: gridPreset === g.id }"
              :disabled="g.disabled"
              :title="g.title"
              @click="selectGridPreset(g.id)"
            >
              {{ g.label }}
            </button>
          </div>
          <div
            v-if="gridPreset === 'custom'"
            class="custom-grid"
            @pointerdown.stop
          >
            <label
              >nx
              <input v-model.number="customNx" type="number" min="1" max="32" />
            </label>
            <label
              >ny
              <input v-model.number="customNy" type="number" min="1" max="32" />
            </label>
          </div>
          <div class="setup-actions">
            <button type="button" class="ghost" @click="scanSetupOpen = false">
              Cancel
            </button>
            <button type="button" class="primary" @click="startNewScan">
              Start
            </button>
          </div>
        </template>
        <template v-else>
          <p class="video-hint">
            Placeholder: create a video Canvas under the current Camera. Stage
            still moves; timeline wiring comes later.
          </p>
          <div class="setup-actions">
            <button type="button" class="ghost" @click="scanSetupOpen = false">
              Cancel
            </button>
            <button type="button" class="primary" @click="startVideoRecord">
              Create
            </button>
          </div>
        </template>
      </div>
    </div>

    <!-- ScanFocus controls -->
    <div
      v-if="viewFocus === 'scan' && scanPipVisible"
      class="hud-float hud-scan-bar"
      :style="floatStyle(hudScanBar)"
      @pointerdown.stop="startHudDrag($event, 'hudScanBar')"
    >
      <span class="scan-label">{{ scanStatus || "Canvas" }}</span>
      <span v-if="!scanRunning" class="scan-hint">Scroll zoom · drag pan</span>
      <div
        v-if="mosaicUrl"
        class="display-mode"
        title="Open Canvas · one layout, two views"
        @pointerdown.stop
      >
        <button
          type="button"
          :class="{
            active: mosaicDisplayMode === 'slide',
            stale: slideStale
          }"
          :disabled="scanRunning"
          title="Open merged Slide"
          @click.stop="openCanvasStudio('slide')"
        >
          Slide{{ slideStale ? " ·" : "" }}
        </button>
        <button
          type="button"
          :class="{ active: mosaicDisplayMode === 'tiles' }"
          title="Working tiles (overlap)"
          @click.stop="openCanvasStudio('tiles')"
        >
          Tiles
        </button>
      </div>
      <button
        v-if="scanRunning"
        type="button"
        class="ghost"
        :disabled="!scanHref"
        @click.stop="cancelScan"
      >
        {{ scanHref ? "Cancel" : "Externally controlled" }}
      </button>
      <template v-else-if="continueSetupOpen">
        <div class="grid-row continue-grids" @pointerdown.stop>
          <button
            v-for="g in continueGridOptions"
            :key="g.id"
            type="button"
            :class="{ active: gridPreset === g.id }"
            :disabled="g.disabled"
            :title="g.title"
            @click="selectGridPreset(g.id)"
          >
            {{ g.label }}
          </button>
        </div>
        <div
          v-if="gridPreset === 'custom'"
          class="custom-grid"
          @pointerdown.stop
        >
          <label
            >nx
            <input v-model.number="customNx" type="number" min="1" max="32" />
          </label>
          <label
            >ny
            <input v-model.number="customNy" type="number" min="1" max="32" />
          </label>
        </div>
        <button
          type="button"
          class="ghost"
          @click.stop="continueSetupOpen = false"
        >
          Back
        </button>
        <button
          type="button"
          class="primary-mini"
          :disabled="!canContinueWithPreset"
          @click.stop="confirmContinueScan"
        >
          Go
        </button>
      </template>
      <template v-else>
        <button
          type="button"
          class="ghost"
          :disabled="refineBusy || !scanSession || macArchiveBusy"
          @click.stop="openCanvasStudio('slide', true)"
        >
          Enhance
        </button>
        <button
          type="button"
          class="ghost"
          :disabled="!canContinueScan"
          :title="continueDisabledReason"
          @click.stop="openContinueSetup"
        >
          Continue
        </button>
        <button
          type="button"
          class="ghost"
          :title="showStageMarker ? 'Hide FOV marker' : 'Show FOV marker'"
          @click.stop="showStageMarker = !showStageMarker"
        >
          {{ showStageMarker ? "Hide FOV" : "Show FOV" }}
        </button>
        <button type="button" class="ghost" @click.stop="focusCamera">
          Camera
        </button>
        <button type="button" class="ghost" @click.stop="closeScanSession">
          Close
        </button>
      </template>
    </div>

    <canvasStudio
      v-if="studioSession"
      ref="canvasStudio"
      :key="studioSession"
      :session="studioSession"
      :base-uri="$store.getters.baseUri"
      :initial-mode="studioMode"
      :initial-adjust="studioAdjust"
      :scanning="scanRunning || refineBusy"
      :scan-status="scanStatus"
      :stage-position="stagePos"
      :show-fov="showStageMarker"
      :roi-drawing="agentDrawing"
      @toggle-fov="showStageMarker = !showStageMarker"
      @continue="openContinueSetup"
      @dismiss="closeScanSession"
      @context="canvasContext = $event"
      @position="stagePos = $event"
      @saved="refreshCanvasContext"
      @mark-roi="markCanvasRoi"
      @close="closeCanvasStudio"
      @retake="retakeCanvasRegion"
    />

    <div
      v-if="studioSession && continueSetupOpen"
      class="canvas-dialog"
      @pointerdown.stop
    >
      <section>
        <h3>Expand current canvas</h3>
        <p>Choose the new total grid. Existing tiles stay in this Canvas.</p>
        <div class="grid-row continue-modal-options">
          <button
            v-for="g in continueGridOptions"
            :key="g.id"
            :disabled="g.disabled"
            :class="{ active: gridPreset === g.id }"
            @click="selectGridPreset(g.id)"
          >
            {{ g.label }}
          </button>
        </div>
        <div v-if="gridPreset === 'custom'">
          <label
            >Columns
            <input
              v-model.number="customNx"
              type="number"
              min="1"
              max="32"/></label
          ><label
            >Rows
            <input v-model.number="customNy" type="number" min="1" max="32"
          /></label>
        </div>
        <button @click="continueSetupOpen = false">Cancel</button
        ><button
          :disabled="!canContinueWithPreset || scanRunning"
          @click="confirmContinueScan"
        >
          Continue scan
        </button>
      </section>
    </div>
    <div v-if="canvasCloseOpen" class="canvas-dialog" @pointerdown.stop>
      <section>
        <h3>Close this canvas?</h3>
        <p>
          Keep it under this Camera to reopen later, or move the entire canvas
          and its files to recovery trash.
        </p>
        <button :disabled="canvasCloseBusy" @click="canvasCloseOpen = false">
          Cancel</button
        ><button :disabled="canvasCloseBusy" @click="finishCanvasClose(false)">
          Close and keep</button
        ><button :disabled="canvasCloseBusy" @click="finishCanvasClose(true)">
          Move to trash
        </button>
        <p>{{ canvasCloseError }}</p>
      </section>
    </div>

    <!-- Fixed bottom-left dock: ROI + XYZ (one compact pill when collapsed) -->
    <div v-if="stageHudOpen" class="stage-roi-dock expanded">
      <div class="hud-float hud-controls docked expanded" @pointerdown.stop>
        <div class="stage-panel-head" @pointerdown.stop>
          <span class="stage-panel-title">Stage</span>
          <button
            type="button"
            class="stage-collapse"
            title="Collapse"
            @click.stop="stageHudOpen = false"
          >
            <i class="material-icons">expand_more</i>
          </button>
        </div>
        <div class="axis-row" @pointerdown.stop>
          <span class="axis-name">X</span>
          <input
            v-model="stageEdit.x"
            class="axis-pos-input"
            type="number"
            step="1"
            :disabled="zControlsLocked"
            title="Edit X then Enter — absolute move"
            @focus="stageEditFocus = 'x'"
            @blur="onStageEditBlur('x')"
            @keydown.enter.prevent.stop="commitStageAxis('x')"
          />
          <span class="axis-sep">|</span>
          <input
            v-model.number="stepX"
            class="step-num"
            type="number"
            min="1"
            :disabled="zControlsLocked"
            title="X steps per move (hold the edge to repeat)"
            @input="stepPreset = 'custom'"
            @change="commitStepSize('x')"
          />
        </div>
        <div class="axis-row" @pointerdown.stop>
          <span class="axis-name">Y</span>
          <input
            v-model="stageEdit.y"
            class="axis-pos-input"
            type="number"
            step="1"
            :disabled="zControlsLocked"
            title="Edit Y then Enter — absolute move"
            @focus="stageEditFocus = 'y'"
            @blur="onStageEditBlur('y')"
            @keydown.enter.prevent.stop="commitStageAxis('y')"
          />
          <span class="axis-sep">|</span>
          <input
            v-model.number="stepY"
            class="step-num"
            type="number"
            min="1"
            :disabled="zControlsLocked"
            title="Y steps per move (hold the edge to repeat)"
            @input="stepPreset = 'custom'"
            @change="commitStepSize('y')"
          />
        </div>
        <div class="axis-row" @pointerdown.stop>
          <span class="axis-name">Z</span>
          <input
            v-model="stageEdit.z"
            class="axis-pos-input"
            type="number"
            step="1"
            :disabled="zControlsLocked"
            title="Edit Z then Enter — absolute move"
            @focus="stageEditFocus = 'z'"
            @blur="onStageEditBlur('z')"
            @keydown.enter.prevent.stop="commitStageAxis('z')"
          />
          <span class="axis-sep">|</span>
          <input
            v-model.number="stepZ"
            class="step-num"
            type="number"
            min="1"
            :disabled="zControlsLocked"
            title="Z steps per move"
            @input="stepPreset = 'custom'"
            @change="commitStepSize('z')"
          />
        </div>
        <button
          type="button"
          class="stage-move-btn"
          :disabled="zControlsLocked || stageMovingAbs"
          title="Move to the entered absolute XYZ position"
          @click.stop="commitStagePosition"
          @pointerdown.stop
        >
          <i class="material-icons">near_me</i>
          <span>{{ stageMovingAbs ? "Moving…" : "Move" }}</span>
        </button>
        <div class="z-jog" @pointerdown.stop>
          <button
            type="button"
            class="z-btn"
            :disabled="zControlsLocked"
            title="Z up (+)"
            @click.stop="moveRel(0, 0, stepFor('z'))"
          >
            +
          </button>
          <button
            type="button"
            class="z-btn"
            :disabled="zControlsLocked"
            title="Z down (−)"
            @click.stop="moveRel(0, 0, -stepFor('z'))"
          >
            −
          </button>
        </div>
        <div class="step-preset-wrap" @pointerdown.stop>
          <button
            type="button"
            class="step-preset"
            :disabled="zControlsLocked"
            title="Step preset"
            @click.stop="stagePresetOpen = !stagePresetOpen"
          >
            <span>{{ stepPresetLabel }}</span>
            <i class="material-icons">expand_more</i>
          </button>
          <div v-if="stagePresetOpen" class="step-preset-menu">
            <button
              v-for="preset in stepPresetOptions"
              :key="preset.id"
              type="button"
              :class="{ active: stepPreset === preset.id }"
              @click.stop="chooseStepPreset(preset.id)"
            >
              <span>{{ preset.label }}</span>
              <small>{{ preset.xy }} / {{ preset.z }}</small>
            </button>
          </div>
        </div>
        <button
          type="button"
          class="af-btn"
          :disabled="zControlsLocked || afBusy"
          title="Auto Focus"
          @click.stop="runAutofocus"
          @pointerdown.stop
        >
          <i class="material-icons">center_focus_strong</i>
          <span>{{ afBusy ? "AF…" : "AF" }}</span>
        </button>
        <button
          type="button"
          class="scale-calib-btn"
          :disabled="zControlsLocked || scaleCalibBusy || viewFocus === 'scan'"
          :title="scaleCalibTitle"
          @click.stop="runScaleCalibrate"
          @pointerdown.stop
        >
          <i class="material-icons">straighten</i>
          <span>{{ scaleCalibBusy ? "Cal…" : "Scale" }}</span>
        </button>
        <span v-if="afStatus" class="af-status">{{ afStatus }}</span>
        <span v-if="scaleStatus" class="scale-status">{{ scaleStatus }}</span>
      </div>
    </div>

    <div v-if="peekUrl" class="peek-modal" @click.self="peekUrl = null">
      <div class="peek-card">
        <header>
          <strong>Tile {{ peekLabel }}</strong>
          <button type="button" @click="peekUrl = null">
            <i class="material-icons">close</i>
          </button>
        </header>
        <img :src="peekUrl" alt="scan tile" />
      </div>
    </div>

    <openmhsAgentTool
      ref="agentTool"
      :surface-selector="
        studioSession ? '.studio .slide' : '.openmhs-main-surface img'
      "
      :surface-context="studioSession ? canvasContext : {}"
      :surface-kind="
        studioSession || (viewFocus === 'scan' && scanPipVisible)
          ? 'mosaic'
          : 'live'
      "
      :surface-version="
        `${studioSession || viewFocus}|${canvasContext.revision ||
          0}|${macSessionId || sessionId || ''}|${mosaicDisplayMode ||
          'slide'}|${mosaicUrl || 'live'}|sb${agentSidebarOpen ? 1 : 0}`
      "
      :mosaic-session-id="macSessionId || sessionId"
      :current-view-id="currentViewId"
      :current-view-title="currentViewTitle"
      @drawing-change="onAgentDrawingChange"
      @sidebar-change="onAgentSidebarChange"
      @load-pc-session="onLoadPcSession"
      @preview-canvas="onPreviewCanvas"
      @create-camera-view="onNewCameraChat"
      @select-camera-view="selectCameraView"
      @import-legacy="importLegacyMosaics"
      @import-canvas="importCanvasFromOther"
      @new-camera-chat="onNewCameraChat"
      @pc-status="onAgentPcStatus"
      @roi-count="n => (roiDockCount = n || 0)"
      @agent-run-mosaic-scan="agentRunMosaicScan"
      @agent-delete-comprehension="agentDeleteComprehension"
      @retake-roi="retakeRoi"
    />
  </div>
</template>

<script>
import axios from "axios";
import streamDisplay from "./streamContent.vue";
import openmhsAgentTool from "./openmhsComponents/openmhsAgentTool.vue";
import canvasStudio from "./openmhsComponents/canvasStudio.vue";

const PRESETS = {
  fine: { xy: 50, z: 20 },
  medium: { xy: 200, z: 50 },
  coarse: { xy: 800, z: 100 }
};

const STEP_PRESET_OPTIONS = [
  { id: "fine", label: "Fine", xy: 50, z: 20 },
  { id: "medium", label: "Med", xy: 200, z: 50 },
  { id: "coarse", label: "Coarse", xy: 800, z: 100 }
];

const GRID_PRESETS = {
  fov: [1, 1, 1],
  small: [2, 2, 1],
  med: [3, 3, 1],
  large: [4, 4, 1]
};

const DEFAULT_SCAN_OVERLAP = 0.25;
const VIEW_LS_KEY = "openmhs.currentViewId";

export default {
  name: "OpenmhsContent",
  components: { streamDisplay, openmhsAgentTool, canvasStudio },
  data() {
    return {
      studioSession: null,
      startingNewScan: false,
      studioMode: "slide",
      studioAdjust: false,
      stepPreset: "medium",
      stepPresetOptions: STEP_PRESET_OPTIONS,
      stagePresetOpen: false,
      stepX: 200,
      stepY: 200,
      stepZ: 50,
      invert: { x: false, y: false, z: false },
      holdMove: null,
      holdLoop: false,
      agentDrawing: false,
      agentSidebarOpen: false,
      pcDropOver: false,
      suppressTilePeek: false,
      fastAutofocusUri: null,
      afBusy: false,
      afStatus: "",
      // Dual-view session state
      viewFocus: "camera", // camera | scan
      scanPipVisible: false,
      canvasContext: {},
      canvasPips: [],
      canvasPipDrag: null,
      canvasRecords: [],
      cameraTilesOpen: false,
      canvasPipHidden: false,
      canvasCloseOpen: false,
      canvasCloseBusy: false,
      canvasCloseError: "",
      canvasCloseSession: null,
      autoStudioSeen: null,
      observedScanRunning: false,
      scanSession: false,
      scanSetupOpen: false,
      recordKind: "mosaic",
      continueSetupOpen: false,
      gridPreset: "med",
      sessionGridPreset: "med",
      customNx: 5,
      customNy: 5,
      gridOptions: [
        { id: "fov", label: "1×1", title: "Current FOV only", disabled: false },
        { id: "small", label: "S 2×2", title: "2×2", disabled: false },
        { id: "med", label: "M 3×3", title: "3×3", disabled: false },
        { id: "large", label: "L 4×4", title: "4×4", disabled: false },
        {
          id: "full",
          label: "Full",
          title: "Explore until sample edges",
          disabled: false
        },
        {
          id: "custom",
          label: "Custom",
          title: "Custom nx×ny",
          disabled: false
        }
      ],
      scanRunning: false,
      scanHref: null,
      scanStatus: "",
      mosaicUrl: null,
      mosaicPoll: null,
      mosaicRefreshBusy: false,
      mosaicFailureStreak: 0,
      // All observer paths share this short-lived snapshot.  It coalesces
      // synchronized tabs and prevents a slow Pi request from becoming a
      // retry storm while keeping scan progress visibly current.
      liveDetailsSnapshot: null,
      liveDetailsSnapshotAt: 0,
      liveDetailsInFlight: null,
      externalScanPoll: null,
      externalScanBusy: false,
      roiRetakeBusy: false,
      externalScanReady: false,
      externalScanSeen: null,
      externalScanSession: null,
      externalScanDisposed: false,
      mosaicContentBox: null,
      scanMeta: null,
      stagePos: null,
      stageEdit: { x: "", y: "", z: "" },
      stageEditFocus: null,
      stageMovingAbs: false,
      posPoll: null,
      pxPerStep: null, // [x, y] from calibrate_scale / live_details
      scaleCalibBusy: false,
      scaleStatus: "",
      peekUrl: null,
      peekLabel: "",
      keepDatasetId: null,
      sessionId: null,
      macSupervisorUri: "http://127.0.0.1:18765",
      macArchived: false,
      macSessionId: null,
      macArchiveBusy: false,
      macPathEnabled: false, // true when Mac worker reachable → Mac refine/sync
      macPreviewRevision: 0,
      lastMacPreviewProbeAt: 0,
      lastMacPreviewUsedAt: 0,
      macSessionSnapshotAt: 0,
      macSessionSnapshotSid: null,
      macSessionSnapshotOk: false,
      piCellCount: 0, // live Pi tiles; Continue needs these
      refineBusy: false,
      refineProgress: 0,
      refineHref: null,
      activePointers: {},
      lastPinchDist: null,
      // Scan-view static zoom/pan (does not move the stage)
      scanView: { scale: 1, tx: 0, ty: 0 },
      scanPan: null,
      scanPinch: null,
      lodTiles: [],
      lodCache: {},
      lodPending: {},
      lodTimer: null,
      lodRefreshBusy: false,
      canvasStatusBusy: false,
      lastCanvasStatusAt: 0,
      // One viewport-driven multi-resolution layer for live and final
      // Mac-owned canvases.  The info document is cheap; the JPEG tiles are
      // immutable per source revision and are loaded only when visible.
      pyramidInfo: null,
      pyramidTiles: [],
      pyramidInfoCheckedAt: 0,
      pyramidRefreshSeq: 0,
      // Explicit display: zoom never auto-switches slide↔tiles
      mosaicDisplayMode: "slide", // "slide" | "tiles"
      // Continue dirties the working mosaic; Slide stays frozen until Refine
      slideStale: false,
      previewNeedsRefresh: false,
      tileCanvasBox: null,
      tileLayoutMap: {}, // "ix,iy" -> {x,y,w,h} fractions of mosaic canvas
      tileLayoutRev: 0,
      showStageMarker: true,
      hudScan: { x: null, y: null },
      hudScanBar: { x: null, y: null },
      hudControls: { x: null, y: null },
      hudDrag: null,
      stageHudOpen: false,
      xyDrag: null,
      currentViewId: "",
      currentViewTitle: "",
      videoRecordOpen: false,
      videoRecordTitle: "",
      viewSynced: false,
      roiDockCount: 0,
      hardwareState: null,
      hardwarePoll: null,
      hardwareClientId: "",
      sharedCanvasRestoring: false
    };
  },
  computed: {
    moveActionUri() {
      return `${this.$store.getters.baseUri}/api/v2/actions/stage/move`;
    },
    positionUri() {
      return `${this.$store.getters.baseUri}/api/v2/instrument/state/stage/position`;
    },
    pluginsUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions`;
    },
    mosaicTileUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/tile`;
    },
    liveDetailsUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/live_details`;
    },
    hardwareUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/hardware`;
    },
    previewBaseUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/latest_preview_stitch.jpg`;
    },
    tileAtBaseUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/tile_at`;
    },
    macMosaicBase() {
      // Prefer Pi reverse-proxy so browser never needs localhost/LAN to Mac
      const sid = this.macSessionId || this.sessionId;
      if (!sid) return null;
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac`;
    },
    macViewsUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/views`;
    },
    macCurrentViewUri() {
      return `${this.macViewsUri}/current`;
    },
    macSessionQuery() {
      const sid = this.macSessionId || this.sessionId;
      return sid ? { session_id: sid } : {};
    },
    tileLayoutUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/tile_layout`;
    },
    cleanupUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/cleanup_tiles`;
    },
    refineUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/refine`;
    },
    showScanFab() {
      // The Camera toolbar owns the only New Canvas entry. This container is
      // mounted only while its setup popover is open, so the old duplicate
      // floating Canvas button never returns.
      return this.scanSetupOpen && !this.studioSession;
    },
    showMosaicLayer() {
      // During Start, enter Scan view immediately (seed FOV or placeholder)
      return (
        this.viewFocus === "scan" &&
        this.scanPipVisible &&
        (!!this.mosaicUrl || this.scanRunning)
      );
    },
    controlsLocked() {
      // Stage XY locked while scanning or inspecting mosaic; Z/AF stay usable
      return this.scanRunning || this.viewFocus === "scan" || this.hardwareBusy;
    },
    zControlsLocked() {
      return !!this.scanRunning || this.hardwareBusy;
    },
    hardwareBusy() {
      return !!(this.hardwareState && this.hardwareState.busy);
    },
    hardwareOperation() {
      if (!this.hardwareState) return "hardware operation";
      const op = this.hardwareState.operation || "hardware operation";
      const phase = this.hardwareState.phase;
      return phase && phase !== op ? `${op} · ${phase}` : op;
    },
    stagePosLabel() {
      const p = this.stagePos;
      if (!p) return { x: "—", y: "—", z: "—" };
      const fmt = v =>
        v == null || Number.isNaN(Number(v))
          ? "—"
          : String(Math.round(Number(v)));
      return { x: fmt(p.x), y: fmt(p.y), z: fmt(p.z) };
    },
    stepPresetLabel() {
      const preset = this.stepPresetOptions.find(
        option => option.id === this.stepPreset
      );
      return preset ? preset.label : "Custom";
    },
    scaleCalibTitle() {
      if (this.scaleCalibBusy) return "Calibrating scale…";
      if (this.pxPerStep) {
        const [px, py] = this.pxPerStep;
        return `Scale ready · ${px.toFixed(3)}×${py.toFixed(
          3
        )} px/step · click to recalibrate`;
      }
      return "Click to calibrate: jog stage and measure pixel shift → px/step";
    },
    scanZoomEnabled() {
      // Pan/zoom never moves the hardware.  Keep it available during a live
      // scan so the same viewport-driven pyramid can be inspected as tiles
      // arrive, while hardware/edit controls remain locked elsewhere.
      return (
        this.viewFocus === "scan" &&
        this.scanPipVisible &&
        !!this.mosaicUrl &&
        !this.agentDrawing
      );
    },
    pyramidEnabled() {
      return !!(
        this.viewFocus === "scan" &&
        this.scanPipVisible &&
        this.macArchived &&
        this.macMosaicBase &&
        !this.scanRunning &&
        (!!this.mosaicUrl || this.scanRunning)
      );
    },
    tilesWorkingMode() {
      // Overlay FOVs while scanning, after Continue (Slide frozen), or after Refine
      return !!(
        this.scanRunning ||
        this.slideStale ||
        (this.scanMeta && this.scanMeta.refined)
      );
    },
    hideSlideImage() {
      // During acquisition the single bounded live preview is the only
      // canvas image; individual tile LODs are intentionally not fanned out
      // to the browser.  After acquisition, Tiles mode can hide the Slide and
      // load tile images on demand.
      return (
        this.mosaicDisplayMode === "tiles" &&
        this.tilesWorkingMode &&
        !this.scanRunning &&
        !this.pyramidEnabled
      );
    },
    tilesLodEnabled() {
      return (
        this.viewFocus === "scan" &&
        this.scanPipVisible &&
        this.mosaicDisplayMode === "tiles" &&
        !this.agentDrawing &&
        this.tilesWorkingMode &&
        (!!this.mosaicUrl || this.scanRunning)
      );
    },
    lodLayerBox() {
      if (!this.tilesLodEnabled) return null;
      const n = Object.keys(this.tileLayoutMap || {}).length;
      if (n > 0 && this.mosaicContentBox) return this.mosaicContentBox;
      return this.tileCanvasBox || this.mosaicContentBox;
    },
    mosaicZoomStyle() {
      const { scale, tx, ty } = this.scanView;
      return {
        transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
        transformOrigin: "center center"
      };
    },
    lodLayerStyle() {
      const box = this.lodLayerBox;
      if (!box) return { display: "none" };
      return {
        left: `${box.left}px`,
        top: `${box.top}px`,
        width: `${box.width}px`,
        height: `${box.height}px`
      };
    },
    pyramidLayerBox() {
      return this.pyramidEnabled
        ? this.mosaicContentBox || this.tileCanvasBox
        : null;
    },
    pyramidLayerStyle() {
      const box = this.pyramidLayerBox;
      if (!box) return { display: "none" };
      return {
        left: `${box.left}px`,
        top: `${box.top}px`,
        width: `${box.width}px`,
        height: `${box.height}px`
      };
    },
    cameraLayerClass() {
      if (this.viewFocus === "scan" && this.scanPipVisible) {
        return this.scanRunning && this.showStageMarker && this.stageMarker
          ? "pip moving-camera"
          : "pip top-right";
      }
      return "main";
    },
    cameraLayerStyle() {
      if (
        !this.cameraLayerClass.includes("moving-camera") ||
        !this.stageMarker
      ) {
        return {};
      }
      const marker = this.stageMarker;
      const box = this.stageMarkerStyle;
      // Use the image content box when it is known. During the first tile,
      // the preview may not exist yet, so percentages still place the live
      // Camera FOV on the planned Canvas without blocking the scan.
      if (box) {
        return {
          left: `${box.left}px`,
          top: `${box.top}px`,
          width: `${Math.max(120, box.width)}px`,
          height: `${Math.max(90, box.height)}px`,
          right: "auto",
          bottom: "auto"
        };
      }
      return {
        left: `${marker.left}%`,
        top: `${marker.top}%`,
        width: `${Math.max(12, marker.w)}%`,
        height: `${Math.max(12, marker.h)}%`,
        right: "auto",
        bottom: "auto"
      };
    },
    mosaicLayerClass() {
      if (this.viewFocus === "scan" && this.scanPipVisible) return "main";
      return "pip top-right";
    },
    grid() {
      if (this.gridPreset === "full") {
        // Seed only; backend explores until edges
        return [1, 1, 1];
      }
      if (this.gridPreset === "custom") {
        const nx = Math.max(1, Math.min(32, Number(this.customNx) || 1));
        const ny = Math.max(1, Math.min(32, Number(this.customNy) || 1));
        return [nx, ny, 1];
      }
      return GRID_PRESETS[this.gridPreset] || GRID_PRESETS.med;
    },
    isExploreMode() {
      return this.gridPreset === "full";
    },
    isFovMode() {
      return this.gridPreset === "fov";
    },
    sessionChunkGrid() {
      const lg = (this.scanMeta && this.scanMeta.last_grid) || null;
      if (lg && (lg[0] === "explore" || lg[0] === "Explore")) {
        return [1, 1, 1];
      }
      const fromPreset =
        this.sessionGridPreset === "custom"
          ? [this.customNx, this.customNy, 1]
          : this.sessionGridPreset === "full"
          ? [1, 1, 1]
          : this.sessionGridPreset === "fov"
          ? [1, 1, 1]
          : GRID_PRESETS[this.sessionGridPreset];
      const g = lg || fromPreset || GRID_PRESETS.med;
      return [Number(g[0]) || 2, Number(g[1]) || 2, Number(g[2]) || 1];
    },
    continueGridOptions() {
      const [sx, sy] = this.sessionChunkGrid;
      const labels = {
        fov: "1×1",
        small: "2×2",
        med: "3×3",
        large: "4×4",
        full: "To edges",
        custom: "Custom"
      };
      return this.gridOptions.map(g => {
        g = { ...g, label: labels[g.id] || g.label };
        if (g.id === "full") {
          return {
            ...g,
            disabled: false,
            title: "Continue exploring until sample edges"
          };
        }
        if (g.id === "custom") {
          return {
            ...g,
            disabled: false,
            title: "Custom target shape (fill gaps)"
          };
        }
        if (g.id === "fov") {
          const tooSmall = sx > 1 || sy > 1;
          return {
            ...g,
            disabled: !!tooSmall,
            title: tooSmall
              ? `Session is already ${sx}×${sy}`
              : "Current FOV only"
          };
        }
        const dims = GRID_PRESETS[g.id];
        if (!dims) return { ...g, disabled: true };
        const tooSmall = dims[0] < sx || dims[1] < sy;
        return {
          ...g,
          disabled: !!tooSmall,
          title: tooSmall ? `Smaller than current ${sx}×${sy}` : g.title
        };
      });
    },
    canContinueWithPreset() {
      if (this.gridPreset === "full") return true;
      if (this.gridPreset === "custom") {
        const [sx, sy] = this.sessionChunkGrid;
        const g = this.grid;
        return g[0] >= sx && g[1] >= sy;
      }
      const opt = this.continueGridOptions.find(g => g.id === this.gridPreset);
      return opt && !opt.disabled;
    },
    canContinueScan() {
      const sid = this.macSessionId || this.sessionId;
      return (
        !!this.scanSession &&
        !this.scanRunning &&
        !this.refineBusy &&
        !this.macArchiveBusy &&
        (this.piCellCount > 0 || !!this.macArchived || !!sid)
      );
    },
    continueDisabledReason() {
      if (this.scanRunning) return "Scan running";
      if (this.refineBusy) return "Refine in progress";
      if (this.macArchiveBusy) return "Syncing to PC…";
      if (!this.scanSession) return "No active scan";
      const sid = this.macSessionId || this.sessionId;
      if (this.piCellCount < 1 && !this.macArchived && !sid) {
        return "No tiles on Pi or PC to continue from";
      }
      if (this.piCellCount < 1) {
        return "Continue from PC path";
      }
      return "Continue scanning";
    },
    stageMarker() {
      const r = this.canvasContext.reference,
        b = this.canvasContext.bounds,
        p = this.stagePos;
      if (r && b && p) {
        const x = p.x - r.stage_origin[0],
          y = p.y - r.stage_origin[1],
          m = r.stage_to_world;
        return {
          left:
            ((r.world_origin[0] + x * m[0][0] + y * m[1][0] - b.x) / b.w) * 100,
          top:
            ((r.world_origin[1] + x * m[0][1] + y * m[1][1] - b.y) / b.h) * 100,
          w: (r.fov[0] / b.w) * 100,
          h: (r.fov[1] / b.h) * 100
        };
      }
      if (!this.scanMeta || !this.stagePos) return null;
      const stride = this.scanMeta.stride_size;
      const origin =
        this.scanMeta.origin_xy || this.scanMeta.initial_xy || null;
      if (!origin || !stride || !stride[0] || !stride[1]) return null;
      const nx = Math.max(1, this.scanMeta.nx || this.grid[0]);
      const ny = Math.max(1, this.scanMeta.ny || this.grid[1]);
      const ix = (this.stagePos.x - origin[0]) / stride[0];
      const iy = (this.stagePos.y - origin[1]) / stride[1];
      const minIx = this.scanMeta.min_ix != null ? this.scanMeta.min_ix : 0;
      const minIy = this.scanMeta.min_iy != null ? this.scanMeta.min_iy : 0;
      // Match mosaic placement (axis_sign < 0 ⇒ higher index toward image origin)
      const signs = (this.scanMeta && this.scanMeta.axis_sign) || [-1, -1];
      const ax = signs[0] < 0 ? -1 : 1;
      const ay = signs[1] < 0 ? -1 : 1;
      const g = this.fallbackTileGeometry();
      const left = ((ax < 0 ? minIx + nx - ix - 1 : ix - minIx) * g.sx / g.w) * 100;
      const top = ((ay < 0 ? minIy + ny - iy - 1 : iy - minIy) * g.sy / g.h) * 100;
      const w = 100 / g.w;
      const h = 100 / g.h;
      return {
        left: Math.max(-20, Math.min(120 - w / 2, left)),
        top: Math.max(-20, Math.min(120 - h / 2, top)),
        w,
        h
      };
    },
    stageMarkerStyle() {
      const m = this.stageMarker;
      const box = this.mosaicContentBox;
      if (!m || !box) return null;
      // Percentages relative to the object-fit:contain content box, not the layer
      return {
        left: `${box.left + (m.left / 100) * box.width}px`,
        top: `${box.top + (m.top / 100) * box.height}px`,
        width: `${(m.w / 100) * box.width}px`,
        height: `${(m.h / 100) * box.height}px`
      };
    }
  },
  watch: {
    stepPreset(v) {
      const p = PRESETS[v];
      if (p) {
        this.stepX = p.xy;
        this.stepY = p.xy;
        this.stepZ = p.z;
        this.saveStepSizes();
      }
      this.stagePresetOpen = false;
    },
    sessionId() {
      this.clearLodTiles();
    }
  },
  mounted() {
    this.resolveAutofocus();
    this.ensureHardwareClientId();
    this.restoreStepSizes();
    window.addEventListener("keydown", this.onKeyDown);
    document.addEventListener("pointerdown", this.onDocumentPointerDown, true);
    window.addEventListener("pointermove", this.onHudDragMove);
    window.addEventListener("pointerup", this.onHudDragEnd);
    window.addEventListener("resize", this.onViewportResize);
    this.posPoll = setInterval(this.refreshStagePos, 800);
    this.refreshStagePos();
    this.restoreCameraViewId();
    this.loadScaleCalibration();
    this.$nextTick(() => {
      this.tryRestoreScanSession();
      this.syncCameraViewToPc().then(() => this.restoreSharedCanvas());
    });
    this.externalScanPoll = setInterval(this.observeExternalScan, 2000);
    this.hardwarePoll = setInterval(this.refreshHardwareState, 1000);
    this.refreshHardwareState();
  },
  beforeDestroy() {
    this.externalScanDisposed = true;
    clearInterval(this.externalScanPoll);
    clearInterval(this.hardwarePoll);
    window.removeEventListener("keydown", this.onKeyDown);
    document.removeEventListener(
      "pointerdown",
      this.onDocumentPointerDown,
      true
    );
    window.removeEventListener("pointermove", this.onHudDragMove);
    window.removeEventListener("pointerup", this.onHudDragEnd);
    window.removeEventListener("resize", this.onViewportResize);
    this.stopHoldMove();
    this.stopMosaicPoll();
    this.clearLodTiles();
    if (this.posPoll) clearInterval(this.posPoll);
  },
  methods: {
    canvasPreviewUrl(p) {
      return `${
        this.$store.getters.baseUri
      }/api/v2/extensions/org.microworld.scan-mosaic/mac/preview.jpg?session_id=${encodeURIComponent(
        p.id
      )}&t=${p.refresh || 0}`;
    },
    addCanvasPip(canvas) {
      if (!canvas || !canvas.id) return;
      const found = this.canvasPips.find(p => p.id === canvas.id);
      if (found) {
        found.title = canvas.title || found.title;
        found.refresh = Date.now();
        return;
      }
      const i = this.canvasPips.length;
      this.canvasPips.push({
        id: canvas.id,
        title: canvas.title || canvas.id,
        x: 18 + i * 28,
        y: 82 + i * 28,
        refresh: Date.now()
      });
    },
    canvasPipStyle(p) {
      return { left: `${p.x}px`, top: `${p.y}px` };
    },
    startCanvasPipDrag(p, e) {
      if (e.button !== 0) return;
      this.canvasPipDrag = {
        p,
        id: e.pointerId,
        x: e.clientX,
        y: e.clientY,
        ox: p.x,
        oy: p.y,
        moved: false
      };
      e.currentTarget.setPointerCapture(e.pointerId);
      const move = ev => {
        const d = this.canvasPipDrag;
        if (!d || ev.pointerId !== d.id) return;
        const dx = ev.clientX - d.x,
          dy = ev.clientY - d.y;
        d.moved = d.moved || Math.abs(dx) + Math.abs(dy) > 4;
        this.$set(
          d.p,
          "x",
          Math.max(8, Math.min(window.innerWidth - 230, d.ox + dx))
        );
        this.$set(
          d.p,
          "y",
          Math.max(72, Math.min(window.innerHeight - 180, d.oy + dy))
        );
      };
      const up = ev => {
        if (!this.canvasPipDrag || ev.pointerId !== this.canvasPipDrag.id)
          return;
        this.$set(this.canvasPipDrag.p, "dragged", this.canvasPipDrag.moved);
        this.canvasPipDrag = null;
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    },
    async openCanvasPip(p) {
      if (p.dragged) {
        this.$set(p, "dragged", false);
        return;
      }
      await this.switchCanvas(p.id);
    },
    requestCanvasClose(sid) {
      this.canvasCloseSession = sid;
      this.canvasCloseError = "";
      this.canvasCloseOpen = true;
    },
    async refreshCanvasRecords() {
      try {
        const { data } = await axios.get(this.macViewsUri, { timeout: 8000 });
        this.canvasRecords = (data.views || []).flatMap(v =>
          (v.records || [])
            .filter(r => r.kind !== "video")
            .map(r => ({
              ...r,
              view_title: v.title || v.id,
              view_id: v.id
            }))
        );
      } catch (_) {
        /* keep current list offline */
      }
    },
    async persistActiveCanvas(recordId) {
      if (!this.currentViewId) return false;
      try {
        await axios.post(
          this.macViewsUri,
          {
            action: "active-record",
            view_id: this.currentViewId,
            record_id: recordId || null
          },
          { timeout: 8000 }
        );
        return true;
      } catch (_) {
        return false;
      }
    },
    async restoreSharedCanvas() {
      if (this.sharedCanvasRestoring || this.startingNewScan || this.scanRunning || this.studioSession)
        return;
      this.sharedCanvasRestoring = true;
      try {
        const { data } = await axios.get(this.macCurrentViewUri, {
          timeout: 5000
        });
        const view = data && data.current_view;
        const sid = view && view.active_record_id;
        if (!sid || this.startingNewScan || this.scanRunning || this.studioSession) return;
        if (view.id && view.id !== this.currentViewId) {
          this.persistCameraViewId(
            view.id,
            view.title || this.currentViewTitle
          );
        }
        await this.loadPcMosaic(sid, {
          previewOnly: true,
          title: sid
        });
        if (!this.externalScanDisposed && !this.startingNewScan && !this.scanRunning) {
          await this.openCanvasStudio("slide");
        }
      } catch (_) {
        /* Mac may be offline; the normal view sync will retry later. */
      } finally {
        this.sharedCanvasRestoring = false;
      }
    },
    async toggleCameraTiles() {
      this.cameraTilesOpen = !this.cameraTilesOpen;
      if (this.cameraTilesOpen) await this.refreshCanvasRecords();
    },
    addRecordToCamera(record) {
      if (!record || !record.id) return;
      this.addCanvasPip(record);
      this.cameraTilesOpen = false;
    },
    async switchCanvas(sid) {
      if (!sid || this.scanRunning) return;
      const active = this.studioSession || this.macSessionId || this.sessionId;
      // A PiP created from the current Canvas already has its metadata and
      // preview in memory. Reopening it should be a view switch, not another
      // archive/preview round trip.
      if (String(active || "") === String(sid)) {
        this.canvasPipHidden = false;
        await this.persistActiveCanvas(sid);
        await this.openCanvasStudio("slide");
        return;
      }
      this.studioSession = null;
      this.canvasContext = {};
      this.sessionId = sid;
      this.macSessionId = sid;
      this.macArchived = true;
      this.scanSession = true;
      this.scanPipVisible = true;
      this.viewFocus = "camera";
      this.canvasPipHidden = false;
      // Canvas Studio performs the authoritative load. Avoid loading the
      // same session once here and once again inside the Studio component.
      await this.persistActiveCanvas(sid);
      await this.openCanvasStudio("slide");
    },
    async finishCanvasClose(trash) {
      if (this.scanRunning || this.canvasCloseBusy) return;
      this.canvasCloseBusy = true;
      this.canvasCloseError = "";
      try {
        const sid = this.canvasCloseSession;
        if (!sid) throw Error("Choose a canvas to close.");
        const active =
          sid === (this.studioSession || this.macSessionId || this.sessionId);
        if (trash) {
          const uri = `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`;
          const { data: d } = await axios.post(uri, {
            operation: "canvas",
            action: "load",
            session_id: sid
          });
          const { data: r } = await axios.post(uri, {
            operation: "canvas",
            action: "trash-canvas",
            session_id: sid,
            revision: d.revision,
            confirm: true
          });
          if (r.error) throw Error(r.error);
        }
        this.canvasPips = this.canvasPips.filter(p => p.id !== sid);
        if (active) {
          await this.persistActiveCanvas(null);
          this.studioSession = null;
          this.scanPipVisible = false;
          this.scanSession = false;
          this.sessionId = null;
          this.macSessionId = null;
          this.canvasContext = {};
          this.mosaicUrl = null;
          this.viewFocus = "camera";
          this.stopMosaicPoll();
          if (this.$refs.agentTool) this.$refs.agentTool.stopDrawing();
        }
        this.canvasCloseOpen = false;
        await this.refreshCanvasRecords();
      } catch (e) {
        this.canvasCloseError = e.message || "Could not close canvas";
      } finally {
        this.canvasCloseBusy = false;
      }
    },
    markCanvasRoi() {
      const t = this.$refs.agentTool;
      if (t) {
        t.openRoiPanel();
        t.setDrawTool("bbox");
      }
    },
    async refreshCanvasContext() {
      const sid = this.studioSession || this.macSessionId || this.sessionId;
      if (!sid) return;
      try {
        const { data: d } = await axios.post(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`,
          { operation: "canvas", action: "status", session_id: sid },
          { timeout: 10000 }
        );
        if (sid !== (this.studioSession || this.macSessionId || this.sessionId))
          return;
        this.canvasContext = {
          reference: d.reference,
          bounds: d.bounds,
          revision: d.revision
        };
        if (!this.studioSession) this.useMacPreview();
      } catch (_) {
        /* archive not ready yet */
      }
    },
    async refreshCanvasStudioSlide() {
      // Camera/PiP mode and the already-mounted Canvas Studio are separate
      // views.  Updating one does not remount or reload the other, so notify
      // the child explicitly when the Mac publishes the final Slide.
      await this.$nextTick();
      const studio = this.$refs.canvasStudio;
      if (!studio || typeof studio.refreshPublishedSlide !== "function") return;
      try {
        await studio.refreshPublishedSlide();
      } catch (_) {
        // Canvas Studio keeps its own retry loop for a transient publish race.
      }
    },
    async retakeCanvasRegion(payload) {
      await this.closeCanvasStudio();
      this.setMosaicDisplayMode("slide");
      await this.retakeRoi(payload);
      await this.openCanvasStudio("tiles");
    },
    async openCanvasStudio(mode = "slide", adjust = false) {
      this.refreshCanvasRecords();
      if (this.roiRetakeBusy) return;
      if (this.$refs.agentTool) this.$refs.agentTool.stopDrawing();
      if (
        !this.scanRunning &&
        !this.refineBusy &&
        !(await this.ensureMacSession())
      ) {
        this.scanStatus = "Connect to the PC Worker that stores this Canvas to edit it";
        return;
      }
      this.studioMode = this.scanRunning || this.refineBusy ? "tiles" : mode;
      this.studioAdjust = adjust;
      this.studioSession = this.macSessionId || this.sessionId;
      await this.persistActiveCanvas(this.studioSession);
    },
    async closeCanvasStudio() {
      const sid = this.studioSession;
      await this.persistActiveCanvas(null);
      this.studioSession = null;
      if (this.$refs.agentTool) {
        this.$refs.agentTool.stopDrawing();
        this.$refs.agentTool.roiPanelOpen = false;
      }
      this.clearLodTiles();
      if (sid && !this.scanRunning) await this.loadPcMosaic(sid);
      if (sid)
        this.addCanvasPip({
          id: sid,
          title: (this.canvasRecords.find(r => r.id === sid) || {}).title || sid
        });
      this.focusCamera();
    },
    async retakeRoi(payload) {
      if (this.roiRetakeBusy || this.scanRunning || this.refineBusy) return;
      if (this.mosaicDisplayMode !== "slide") {
        window.alert("Switch to Slide before selecting a retake region.");
        return;
      }
      const sid = this.macSessionId || this.sessionId;
      if (payload.session_id !== sid) return;
      const uri = `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`;
      this.roiRetakeBusy = true;
      try {
        const { data: plan } = await axios.post(
          uri,
          { operation: "plan", session_id: sid, roi: payload.roi },
          { timeout: 15000 }
        );
        if (!plan.plan_id)
          throw new Error(plan.error || "Retake plan unavailable");
        if (
          !window.confirm(
            `Retake ${plan.tiles.length} intersecting source tile(s)?\n\nThis moves the microscope to saved XYZ positions. Confirm the specimen, objective and stage coordinate reference have NOT changed. Original tiles will be retained. No retake starts without this confirmation.`
          )
        )
          return;
        const { data: job } = await axios.post(
          uri,
          {
            operation: "start",
            session_id: sid,
            plan_id: plan.plan_id,
            confirm_same_sample: true
          },
          { timeout: 15000 }
        );
        if (!job.job_id) throw new Error(job.error || "Retake did not start");
        for (let i = 0; i < 2400; i++) {
          if (this.externalScanDisposed) return;
          const { data: status } = await axios.post(
            uri,
            { operation: "status", session_id: sid, job_id: job.job_id },
            { timeout: 15000 }
          );
          this.scanStatus = `Retake ${status.completed}/${status.total} · ${status.phase}`;
          if (status.phase === "error") throw new Error(status.error);
          if (status.phase === "completed") {
            if ((this.macSessionId || this.sessionId) === sid)
              await this.loadPcMosaic(sid);
            this.scanStatus =
              "Retake complete — Slide updated (original tiles retained)";
            return;
          }
          await new Promise(resolve => setTimeout(resolve, 2000));
        }
        throw new Error(
          "Retake still running; inspect its status before retrying."
        );
      } catch (error) {
        this.scanStatus = `Retake: ${error.message}. Inspect job status before retrying.`;
        window.alert(this.scanStatus);
      } finally {
        this.roiRetakeBusy = false;
      }
    },
    floatStyle(pos) {
      if (pos.x == null || pos.y == null) return {};
      return {
        left: `${pos.x}px`,
        top: `${pos.y}px`,
        right: "auto",
        bottom: "auto",
        transform: "none"
      };
    },
    startHudDrag(e, key) {
      if (e.button !== 0) return;
      // Don't steal taps on buttons/inputs (was forcing a second click on Scan)
      const t = e.target;
      if (
        t &&
        t.closest &&
        t.closest("button, input, select, textarea, a, label, .scan-setup")
      ) {
        return;
      }
      const sel =
        key === "hudScan"
          ? ".hud-scan"
          : key === "hudScanBar"
          ? ".hud-scan-bar"
          : ".hud-controls";
      const el = this.$el.querySelector(sel);
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const cur = this[key];
      const x = cur.x != null ? cur.x : rect.left;
      const y = cur.y != null ? cur.y : rect.top;
      this[key] = { x, y };
      this.hudDrag = {
        key,
        ox: e.clientX - x,
        oy: e.clientY - y,
        moved: false,
        x0: e.clientX,
        y0: e.clientY
      };
    },
    onHudDragMove(e) {
      if (!this.hudDrag) return;
      const { key, ox, oy, x0, y0 } = this.hudDrag;
      if (!this.hudDrag.moved) {
        const d = Math.abs(e.clientX - x0) + Math.abs(e.clientY - y0);
        if (d < 6) return;
        this.hudDrag.moved = true;
      }
      this[key] = {
        x: Math.max(8, Math.min(window.innerWidth - 80, e.clientX - ox)),
        y: Math.max(8, Math.min(window.innerHeight - 48, e.clientY - oy))
      };
    },
    onHudDragEnd() {
      this.hudDrag = null;
    },
    onAgentDrawingChange(active) {
      this.agentDrawing = !!active;
      this.suppressTilePeek = true;
      if (!active) {
        setTimeout(() => {
          this.suppressTilePeek = false;
        }, 350);
      }
    },
    onAgentSidebarChange(open, width) {
      this.agentSidebarOpen = !!open;
      const w = Number(width);
      if (w >= 280 && w <= 720 && this.$el && this.$el.style) {
        this.$el.style.setProperty("--openmhs-agent-sidebar-w", `${w}px`);
      }
      // Undock HUD if it was parked under the sidebar
      if (this.hudControls && this.hudControls.x != null) {
        const maxX = window.innerWidth - (w || 400) - 40;
        if (this.hudControls.x > maxX) {
          this.hudControls = { ...this.hudControls, x: null, y: null };
        }
      }
      this.$nextTick(() => {
        this.updateMosaicContentBox && this.updateMosaicContentBox();
        const tool = this.$refs.agentTool;
        if (tool && tool.ensureSurfaceRect) {
          tool.ensureSurfaceRect(true);
          tool.syncRoisToSurface && tool.syncRoisToSurface();
        }
        requestAnimationFrame(() => {
          if (tool && tool.ensureSurfaceRect) {
            tool.ensureSurfaceRect(true);
            tool.syncRoisToSurface && tool.syncRoisToSurface();
          }
        });
      });
    },
    focusScan({ restoreSession = true } = {}) {
      this.viewFocus = "scan";
      this.scanPipVisible = true;
      this.scanSetupOpen = false;
      // Snap Continue/Start grid back to session's last chunk shape
      if (this.sessionGridPreset === "custom") {
        this.gridPreset = "custom";
      } else if (
        this.sessionGridPreset &&
        GRID_PRESETS[this.sessionGridPreset]
      ) {
        this.gridPreset = this.sessionGridPreset;
      } else if (this.sessionGridPreset === "full") {
        this.gridPreset = "full";
      }
      this.$nextTick(async () => {
        if (restoreSession) await this.ensureMacSession();
        this.updateMosaicContentBox();
        this.scheduleLodRefresh();
      });
      this.startMosaicPoll();
    },
    focusCamera() {
      this.viewFocus = "camera";
      // keep scanPipVisible true → mosaic becomes PiP (refine may continue)
      this.scanPipVisible = true;
      this.continueSetupOpen = false;
      this.resetScanView();
      this.$nextTick(() => this.updateMosaicContentBox());
    },
    resetScanView() {
      this.scanView = { scale: 1, tx: 0, ty: 0 };
      this.scanPan = null;
      this.scanPinch = null;
      if (this.mosaicDisplayMode === "tiles") {
        this.scheduleLodRefresh();
      } else {
        this.lodTiles = [];
      }
    },
    clearLodCacheOnly() {
      if (this.lodTimer) {
        clearTimeout(this.lodTimer);
        this.lodTimer = null;
      }
      Object.values(this.lodCache).forEach(url => {
        try {
          URL.revokeObjectURL(url);
        } catch (_) {
          /* ignore */
        }
      });
      this.lodCache = {};
      this.lodPending = {};
      this.lodTiles = [];
      this.pyramidInfo = null;
      this.pyramidTiles = [];
      this.pyramidInfoCheckedAt = 0;
      this.pyramidRefreshSeq += 1;
    },
    clearLodTiles() {
      this.clearLodCacheOnly();
      this.tileLayoutMap = {};
      this.tileLayoutRev = 0;
      this.mosaicDisplayMode = "slide";
    },
    softHideScan() {
      // No silent keep-alive: leaving Scan without PiP must not revive old mosaic
      return this.closeScanSession();
    },
    async closeScanSession() {
      if (this.scanRunning || this.roiRetakeBusy) {
        window.alert(
          "Wait for the current scan or acquisition to finish before closing."
        );
        return;
      }
      this.canvasCloseError = "";
      this.canvasCloseSession =
        this.studioSession || this.macSessionId || this.sessionId;
      this.canvasCloseOpen = true;
      return;
    },
    dismissScanPip() {
      return this.closeScanSession();
    },
    openScanSetup() {
      if (this.scanRunning) return;
      this.studioSession = null;
      // Always a clean "new scan" panel — never resurrect old mosaic
      this.scanSetupOpen = true;
      this.viewFocus = "camera";
      this.scanPipVisible = false;
      this.mosaicUrl = null;
      this.stopMosaicPoll();
    },
    onCameraPipPointerDown(e) {
      if (!this.cameraLayerClass.includes("pip")) return;
      // Prevent edge-jog under PiP
      e.stopPropagation();
    },
    onCameraLayerPointerDown(e) {
      if (this.cameraLayerClass.includes("pip")) {
        e.stopPropagation();
      }
      // When camera is main, let event bubble to mainSurface for edge jog
    },
    onCameraPipClick() {
      if (this.cameraLayerClass.includes("pip")) {
        this.focusCamera();
      }
    },
    onMosaicLayerPointerDown(e) {
      // Always stop — PiP must not trigger stage hold-move
      e.stopPropagation();
      if (!this.scanZoomEnabled) return;
      if (e.button !== 0 && e.pointerType === "mouse") return;
      const pts =
        this.scanPinch && this.scanPinch.pointers
          ? { ...this.scanPinch.pointers }
          : {};
      pts[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (Object.keys(pts).length >= 2) {
        const vals = Object.values(pts);
        const dx = vals[0].x - vals[1].x;
        const dy = vals[0].y - vals[1].y;
        this.scanPinch = {
          pointers: pts,
          dist0: Math.sqrt(dx * dx + dy * dy),
          scale0: this.scanView.scale
        };
        this.scanPan = null;
        return;
      }
      this.scanPinch = {
        pointers: pts,
        dist0: null,
        scale0: this.scanView.scale
      };
      this.scanPan = {
        pointerId: e.pointerId,
        x0: e.clientX,
        y0: e.clientY,
        tx0: this.scanView.tx,
        ty0: this.scanView.ty,
        moved: false
      };
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch (_) {
        /* ignore */
      }
    },
    onMosaicLayerPointerMove(e) {
      if (!this.scanZoomEnabled) return;
      if (this.scanPinch && this.scanPinch.pointers) {
        if (e.pointerId in this.scanPinch.pointers) {
          this.$set(this.scanPinch.pointers, e.pointerId, {
            x: e.clientX,
            y: e.clientY
          });
        }
        const vals = Object.values(this.scanPinch.pointers);
        if (vals.length >= 2 && this.scanPinch.dist0) {
          const dx = vals[0].x - vals[1].x;
          const dy = vals[0].y - vals[1].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          const factor = dist / Math.max(1, this.scanPinch.dist0);
          const next = Math.max(
            1,
            Math.min(16, this.scanPinch.scale0 * factor)
          );
          this.scanView = {
            ...this.scanView,
            scale: next,
            tx: next <= 1.02 ? 0 : this.scanView.tx,
            ty: next <= 1.02 ? 0 : this.scanView.ty
          };
          if (next <= 1.02) this.scanView = { scale: 1, tx: 0, ty: 0 };
          this.scheduleLodRefresh();
          return;
        }
      }
      if (!this.scanPan || e.pointerId !== this.scanPan.pointerId) return;
      const dx = e.clientX - this.scanPan.x0;
      const dy = e.clientY - this.scanPan.y0;
      if (!this.scanPan.moved && Math.abs(dx) + Math.abs(dy) < 5) return;
      this.scanPan.moved = true;
      if (this.scanView.scale <= 1.02) return;
      this.scanView = {
        ...this.scanView,
        tx: this.scanPan.tx0 + dx,
        ty: this.scanPan.ty0 + dy
      };
      this.scheduleLodRefresh();
    },
    onMosaicLayerPointerUp(e) {
      if (this.scanPinch && this.scanPinch.pointers) {
        this.$delete(this.scanPinch.pointers, e.pointerId);
        if (Object.keys(this.scanPinch.pointers).length < 2) {
          this.scanPinch = null;
        }
      }
      if (this.scanPan && e.pointerId === this.scanPan.pointerId) {
        this.scanPan = null;
      }
    },
    onMosaicLayerClick(e) {
      if (this.mosaicLayerClass.includes("pip")) {
        this.openCanvasStudio("slide");
        return;
      }
      // Zoom/pan mode: don't open peek modal on click (dblclick resets)
      if (this.scanPan && this.scanPan.moved) return;
      if (this.scanView.scale > 1.05) return;
      this.onMosaicClick(e);
    },
    onMosaicDblClick() {
      if (!this.scanZoomEnabled) return;
      this.resetScanView();
    },
    onMosaicImgLoad() {
      this.updateMosaicContentBox();
      this.scheduleLodRefresh();
    },
    updateMosaicContentBox() {
      const img = this.$refs.mosaicImg;
      const zoom = img && img.closest && img.closest(".mosaic-zoom");
      const layer = this.$refs.mosaicLayer || (zoom && zoom.parentElement);
      const sourceWidth =
        this.pyramidEnabled && this.pyramidInfo
          ? Number(this.pyramidInfo.width)
          : img && img.naturalWidth;
      const sourceHeight =
        this.pyramidEnabled && this.pyramidInfo
          ? Number(this.pyramidInfo.height)
          : img && img.naturalHeight;
      if (layer && sourceWidth > 0 && sourceHeight > 0) {
        const lw = layer.clientWidth;
        const lh = layer.clientHeight;
        const scale = Math.min(lw / sourceWidth, lh / sourceHeight);
        const w = sourceWidth * scale;
        const h = sourceHeight * scale;
        this.mosaicContentBox = {
          left: (lw - w) / 2,
          top: (lh - h) / 2,
          width: w,
          height: h
        };
      } else if (
        layer &&
        this.pyramidInfo &&
        Number(this.pyramidInfo.width) > 0 &&
        Number(this.pyramidInfo.height) > 0
      ) {
        const lw = layer.clientWidth;
        const lh = layer.clientHeight;
        const scale = Math.min(
          lw / Number(this.pyramidInfo.width),
          lh / Number(this.pyramidInfo.height)
        );
        const w = Number(this.pyramidInfo.width) * scale;
        const h = Number(this.pyramidInfo.height) * scale;
        this.mosaicContentBox = {
          left: (lw - w) / 2,
          top: (lh - h) / 2,
          width: w,
          height: h
        };
      } else if (!this.hideSlideImage) {
        this.mosaicContentBox = null;
      }
      this.updateTileCanvasBox();
    },
    updateTileCanvasBox() {
      const layer = this.$refs.mosaicLayer;
      if (!layer) return;
      const lw = layer.clientWidth;
      const lh = layer.clientHeight;
      if (lw < 8 || lh < 8) return;
      const g = this.fallbackTileGeometry();
      const canvasAspect = (g.w * (4 / 3)) / g.h;
      let w;
      let h;
      if (lw / lh > canvasAspect) {
        h = lh * 0.96;
        w = h * canvasAspect;
      } else {
        w = lw * 0.96;
        h = w / canvasAspect;
      }
      this.tileCanvasBox = {
        left: (lw - w) / 2,
        top: (lh - h) / 2,
        width: w,
        height: h
      };
    },
    zoomScanAt(clientX, clientY, factor) {
      const layer = this.$refs.mosaicLayer;
      if (!layer) return;
      const rect = layer.getBoundingClientRect();
      const cx = clientX - rect.left - rect.width / 2;
      const cy = clientY - rect.top - rect.height / 2;
      const prev = this.scanView.scale;
      const next = Math.max(1, Math.min(16, prev * factor));
      if (next <= 1.02) {
        this.scanView = { scale: 1, tx: 0, ty: 0 };
        this.scheduleLodRefresh();
        return;
      }
      const worldX = (cx - this.scanView.tx) / prev;
      const worldY = (cy - this.scanView.ty) / prev;
      this.scanView = {
        scale: next,
        tx: cx - worldX * next,
        ty: cy - worldY * next
      };
      this.scheduleLodRefresh();
    },
    async tryRestoreScanSession() {
      return this.observeExternalScan();
    },
    invalidateLiveDetails() {
      this.liveDetailsSnapshot = null;
      this.liveDetailsSnapshotAt = 0;
    },
    fetchLiveDetails({ force = false, maxAge = 700 } = {}) {
      const now = Date.now();
      if (
        !force &&
        this.liveDetailsSnapshot &&
        now - Number(this.liveDetailsSnapshotAt || 0) <= maxAge
      ) {
        return Promise.resolve(this.liveDetailsSnapshot);
      }
      if (this.liveDetailsInFlight) return this.liveDetailsInFlight;
      const request = axios
        .get(this.liveDetailsUri, { timeout: 6000 })
        .then(({ data }) => {
          const snapshot = data || {};
          this.liveDetailsSnapshot = snapshot;
          this.liveDetailsSnapshotAt = Date.now();
          return snapshot;
        })
        .finally(() => {
          if (this.liveDetailsInFlight === request) {
            this.liveDetailsInFlight = null;
          }
        });
      this.liveDetailsInFlight = request;
      return request;
    },
    // Read-only observer: never replay run_mosaic_scan or send hardware actions.
    async observeExternalScan() {
      if (this.externalScanBusy || this.externalScanDisposed) return;
      this.externalScanBusy = true;
      // Only reconnect if a scan is actually in progress on the Pi.
      // Leftover idle tiles must not open Canvas / look like a new scan.
      try {
        const data = await this.fetchLiveDetails({ maxAge: 800 });
        const d = data || {};
        if (this.externalScanDisposed || this.startingNewScan) return;
        const sid = d.session_id;
        const phase = String(d.scan_phase || d.phase || "");
        const running = [
          "calibrating",
          "scanning",
          "stitching",
          "refining"
        ].includes(phase);
        if (
          running &&
          sid &&
          (this.autoStudioSeen !== sid || !this.observedScanRunning)
        ) {
          this.autoStudioSeen = sid;
          this.sessionId = sid;
          this.studioSession = sid;
          this.studioMode = "tiles";
          this.canvasContext = {};
          if (this.$refs.agentTool) this.$refs.agentTool.stopDrawing();
        }
        this.observedScanRunning = running;
        const first = !this.externalScanReady;
        const changed = sid && sid !== this.externalScanSeen;
        this.externalScanReady = true;
        this.externalScanSeen = sid;
        // Browser-started scans retain their existing action/progress handler.
        if (this.scanRunning && !this.externalScanSession) return;
        // Do not reopen historical results on reload or repeatedly steal focus.
        if (
          !sid ||
          (!this.externalScanSession && !(running || (!first && changed)))
        )
          return;
        if (this.externalScanSession !== sid) {
          this.externalScanSession = sid;
          this.stopMosaicPoll();
          this.clearLodCacheOnly();
          this.tileLayoutMap = {};
          this.lastCanvasStatusAt = 0;
          this.canvasStatusBusy = false;
          this.scanMeta = null;
          this.mosaicUrl = null;
          this.macArchived = false;
          this.macSessionId = null;
          this.slideStale = false;
          this.scanHref = null;
          this.scanSetupOpen = false;
          this.continueSetupOpen = false;
          this.viewFocus = "scan";
          this.setMosaicDisplayMode("tiles");
          if (d.view_id) this.persistCameraViewId(d.view_id, "External scan");
        }
        this.sessionId = sid;
        this.scanSession = true;
        this.scanPipVisible = true;
        this.scanRunning = running;
        this.piCellCount = Number(d.cell_count || 0);
        this.applyScanOutput(
          { ...d, nx: (d.grid || [])[0], ny: (d.grid || [])[1] },
          d.initial_xy
        );
        if (running) {
          this.scanStatus = `External scan · ${phase} · ${
            this.piCellCount
          }/${d.cells_expected || "?"} tiles`;
          if (!this.mosaicPoll) this.startMosaicPoll();
        } else {
          this.stopMosaicPoll();
          if (phase === "done") {
            // Mac may finish archiving after the Pi completes: retry on next tick.
            const { data: archive } = await axios.get(
              this.macViewsUri.replace(/\/views$/, "/session"),
              {
                params: { session_id: sid },
                timeout: 6000
              }
            );
            const manifest = (archive && archive.manifest) || {};
            if (!manifest.sync || !manifest.sync.complete) {
              this.scanStatus = "Scan complete — waiting for archive";
              this.refreshPreview();
              return;
            }
            const got = await this.ensureMacSession();
            if (this.externalScanDisposed) return;
            if (got) {
              this.macArchived = true;
              this.setMosaicDisplayMode("slide");
              this.useMacPreview();
              // The record may only become visible on the Mac after the Pi
              // reports done.  Persist the active Canvas now so a refreshed
              // phone/LAN browser converges on the same result.
              await this.persistActiveCanvas(sid);
              await this.refreshCanvasStudioSlide();
              this.externalScanSession = null;
            } else {
              this.refreshPreview();
            }
            this.scanStatus = got
              ? "External scan complete — Canvas ready"
              : "Scan complete — waiting for archive";
          } else {
            this.scanStatus = `External scan · ${phase || "stopped"}`;
            this.externalScanSession = null;
          }
        }
      } catch (_) {
        /* Transient disconnection: keep the displayed session and retry. */
      } finally {
        this.externalScanBusy = false;
      }
    },
    sessionIdFromRelPath(path) {
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
    onPcDragOver(e) {
      this.pcDropOver = true;
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    },
    onPcDragLeave(e) {
      if (e.target === this.$el) this.pcDropOver = false;
    },
    async onPcDrop(e) {
      this.pcDropOver = false;
      const dt = e.dataTransfer;
      let canvas = null;
      try {
        const raw = dt && dt.getData("application/x-openmhs-canvas");
        if (raw) canvas = JSON.parse(raw);
      } catch (_) {
        canvas = null;
      }
      const fromTree =
        (dt && dt.getData("application/x-openmhs-path")) ||
        (dt && dt.getData("text/plain")) ||
        "";
      let sid = (canvas && canvas.id) || this.sessionIdFromRelPath(fromTree);
      if (!sid && dt && dt.items) {
        for (let i = 0; i < dt.items.length; i++) {
          const item = dt.items[i];
          const ent = item.webkitGetAsEntry && item.webkitGetAsEntry();
          if (ent && ent.isDirectory) {
            sid =
              this.sessionIdFromRelPath(ent.fullPath || "") ||
              this.sessionIdFromRelPath(ent.name) ||
              ent.name;
            break;
          }
        }
      }
      if (!sid) return;
      const tool = this.$refs.agentTool;
      if (tool && tool.attachCanvasContext) {
        tool.attachCanvasContext({
          id: sid,
          title: (canvas && canvas.title) || sid,
          view_id: canvas && canvas.view_id,
          kind: (canvas && canvas.kind) || "mosaic"
        });
      } else {
        await this.loadPcMosaic(sid, { previewOnly: true });
      }
    },
    async onLoadPcSession(sid) {
      await this.loadPcMosaic(sid, { previewOnly: true });
      await this.openCanvasStudio("slide");
    },
    async onPreviewCanvas(payload) {
      const p = payload || {};
      const sid = String(p.record_id || "").trim();
      if (!sid) return;
      this.addCanvasPip({ id: sid, title: p.title || sid });
    },
    async agentRunMosaicScan(payload) {
      const p = payload || {};
      const nx = Math.max(1, Math.min(32, Number(p.nx) || 2));
      const ny = Math.max(1, Math.min(32, Number(p.ny) || 2));
      if (p.view_id) {
        await this.selectCameraView(p.view_id);
      } else if (!this.currentViewId) {
        await this.newCameraView();
      }
      this.recordKind = "mosaic";
      this.gridPreset = "custom";
      this.customNx = nx;
      this.customNy = ny;
      this.sessionGridPreset = "custom";
      this.scanStatus = p.title
        ? `Agent → ${p.title}`
        : `Agent → ${nx}×${ny} Canvas`;
      await this.startNewScan();
    },
    async agentDeleteComprehension(payload) {
      const rid = String((payload && payload.record_id) || "").trim();
      if (!rid) return;
      const cur = this.macSessionId || this.sessionId;
      if (cur && cur === rid) {
        await this.closeScanSession();
      }
      try {
        await axios.delete(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/session`,
          { params: { session_id: rid }, timeout: 15000 }
        );
      } catch (_) {
        /* optional — Mac may already have deleted */
      }
      this.scanStatus = `Deleted Canvas ${rid.slice(0, 12)}`;
      const tool = this.$refs.agentTool;
      if (tool && tool.loadViewsTree) tool.loadViewsTree();
    },
    restoreCameraViewId() {
      try {
        const id = window.localStorage.getItem(VIEW_LS_KEY);
        if (id) this.currentViewId = id;
      } catch (_) {
        /* ignore */
      }
    },
    persistCameraViewId(id, title) {
      this.currentViewId = id || "";
      if (title) this.currentViewTitle = title;
      try {
        if (id) window.localStorage.setItem(VIEW_LS_KEY, id);
        else window.localStorage.removeItem(VIEW_LS_KEY);
      } catch (_) {
        /* ignore */
      }
    },
    newCameraViewId() {
      const d = new Date();
      const p = n => String(n).padStart(2, "0");
      const ts = `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(
        d.getUTCDate()
      )}-${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`;
      const hex = Math.random()
        .toString(16)
        .slice(2, 8)
        .padEnd(6, "0");
      return `${ts}-${hex}`;
    },
    openRoiDock() {
      const tool = this.$refs.agentTool;
      if (tool && tool.openRoiPanel) tool.openRoiPanel();
    },
    syncRoiDockCount() {
      const tool = this.$refs.agentTool;
      this.roiDockCount =
        tool && Array.isArray(tool.rois) ? tool.rois.length : 0;
    },
    async onNewCameraChat() {
      await this.newCameraView();
      const tool = this.$refs.agentTool;
      if (tool) {
        if (tool.canvasContexts) tool.canvasContexts = [];
        if (tool.startSessionForView) {
          await tool.startSessionForView(
            this.currentViewId,
            this.currentViewTitle
          );
        }
      }
      this.syncRoiDockCount();
    },
    async importCanvasFromOther() {
      // Import leftover mosaics + offer records from other Cameras into current
      await this.importLegacyMosaics();
      const online = await this.probeMacOnline();
      if (!online || !this.currentViewId) return;
      try {
        const { data } = await axios.get(this.macViewsUri, { timeout: 8000 });
        const views = (data && data.views) || [];
        const choices = [];
        views.forEach(v => {
          if (!v || !v.id || v.id === this.currentViewId) return;
          (v.records || []).forEach(r => {
            if (!r || !r.id) return;
            choices.push({
              view: v,
              record: r,
              label: `${v.title || v.id} · ${r.title || r.id}`
            });
          });
        });
        if (!choices.length) {
          this.scanStatus =
            "No other Camera Canvas to import (legacy leftovers ingested if any)";
          const tool = this.$refs.agentTool;
          if (tool && tool.loadViewsTree) tool.loadViewsTree();
          return;
        }
        let pick = choices[0];
        if (choices.length > 1) {
          const lines = choices
            .slice(0, 12)
            .map((c, i) => `${i + 1}. ${c.label}`)
            .join("\n");
          const ans = window.prompt(
            `Import which Canvas into current Camera?\n\n${lines}\n\nEnter number (1–${Math.min(
              choices.length,
              12
            )}):`,
            "1"
          );
          if (ans == null) return;
          const idx = Math.max(1, parseInt(String(ans).trim(), 10) || 1) - 1;
          pick = choices[Math.min(idx, choices.length - 1)];
        }
        const { data: imported } = await axios.post(
          this.macViewsUri,
          {
            action: "import-record",
            source_view_id: pick.view.id,
            record_id: pick.record.id,
            view_id: this.currentViewId
          },
          { timeout: 20000 }
        );
        const rec = imported && imported.record;
        if (rec && rec.id) this.addCanvasPip(rec);
        this.cameraTilesOpen = false;
        await this.refreshCanvasRecords();
        this.scanStatus = `Imported Canvas from ${pick.view.title ||
          pick.view.id}`;
        const tool = this.$refs.agentTool;
        if (tool && tool.loadViewsTree) tool.loadViewsTree();
      } catch (e) {
        this.scanStatus =
          (e && e.response && e.response.data && e.response.data.error) ||
          "Import Canvas failed";
      }
    },
    async newCameraView() {
      const title = `Camera ${new Date().toLocaleString()}`;
      const online = await this.probeMacOnline();
      if (online) {
        try {
          const { data } = await axios.post(
            this.macViewsUri,
            { action: "create", title },
            { timeout: 15000 }
          );
          const view = (data && data.view) || {};
          const id = view.id || (data && data.current_view_id);
          if (id) {
            this.persistCameraViewId(id, view.title || title);
            this.viewSynced = true;
            return;
          }
        } catch (e) {
          this.scanStatus =
            (e && e.response && e.response.data && e.response.data.error) ||
            "PC rejected New Camera";
        }
      }
      const localId = this.newCameraViewId();
      this.persistCameraViewId(localId, title);
      this.viewSynced = false;
      this.scanStatus = online
        ? "Camera created"
        : "Camera saved in this browser — persist when PC is online";
    },
    async selectCameraView(id) {
      const vid = String(id || "").trim();
      if (!vid) return;
      this.persistCameraViewId(vid, this.currentViewTitle);
      const online = await this.probeMacOnline();
      if (!online) {
        const tool = this.$refs.agentTool;
        if (tool && tool.loadSessionForView) {
          await tool.loadSessionForView(vid);
        }
        return;
      }
      try {
        const { data } = await axios.post(
          this.macViewsUri,
          { action: "current", view_id: vid },
          { timeout: 12000 }
        );
        const view = (data && data.view) || {};
        if (view.title) this.currentViewTitle = view.title;
        this.viewSynced = true;
        const tool = this.$refs.agentTool;
        if (tool && tool.loadSessionForView) {
          await tool.loadSessionForView(vid, view.session_id || null);
        }
      } catch (_) {
        this.viewSynced = false;
        const tool = this.$refs.agentTool;
        if (tool && tool.loadSessionForView) {
          await tool.loadSessionForView(vid);
        }
      }
    },
    async syncCameraViewToPc() {
      const online = await this.probeMacOnline();
      if (!online) return;
      try {
        let data;
        let views = [];
        try {
          const state = await axios.get(this.macCurrentViewUri, {
            timeout: 5000
          });
          data = state.data || {};
        } catch (_) {
          const full = await axios.get(this.macViewsUri, { timeout: 10000 });
          data = full.data || {};
          views = data.views || [];
        }
        // The Mac view index is the shared source of truth.  A browser's
        // localStorage may be empty or stale after switching devices, so do
        // not let it create a phantom Camera and overwrite the shared view.
        const sharedId =
          (data && data.current_view_id) ||
          (views.find(v => v && v.current) || {}).id ||
          "";
        const sharedView = data && data.current_view;
        if (sharedId && sharedId !== this.currentViewId) {
          this.persistCameraViewId(
            sharedId,
            (sharedView && sharedView.title) || this.currentViewTitle
          );
        }
        if (!this.currentViewId) return;
        const found =
          (sharedView && sharedView.id === this.currentViewId
            ? sharedView
            : null) || views.find(v => v && v.id === this.currentViewId);
        if (found) {
          this.currentViewTitle = found.title || this.currentViewTitle;
        } else {
          const full = await axios.get(this.macViewsUri, { timeout: 10000 });
          const fullViews = (full.data && full.data.views) || [];
          const existing = fullViews.find(
            v => v && v.id === this.currentViewId
          );
          if (existing) {
            this.currentViewTitle = existing.title || this.currentViewTitle;
            this.viewSynced = true;
            await this.importLegacyMosaics();
            return;
          }
          const { data: created } = await axios.post(
            this.macViewsUri,
            {
              action: "create",
              title: this.currentViewTitle || "Camera",
              view_id: this.currentViewId
            },
            { timeout: 15000 }
          );
          const view = (created && created.view) || {};
          const id = view.id || this.currentViewId;
          this.persistCameraViewId(id, view.title || this.currentViewTitle);
        }
        this.viewSynced = true;
        await this.importLegacyMosaics();
      } catch (_) {
        this.viewSynced = false;
      }
    },
    async importLegacyMosaics() {
      if (!this.currentViewId) return;
      try {
        await axios.post(
          this.macViewsUri,
          { action: "ingest", view_id: this.currentViewId },
          { timeout: 20000 }
        );
      } catch (_) {
        /* PC offline or no leftovers */
      }
    },
    async onAgentPcStatus(ev) {
      const online = !!(ev && ev.online);
      if (online && !this.viewSynced) {
        await this.syncCameraViewToPc();
      }
      if (!online) this.viewSynced = false;
    },
    async startVideoRecord() {
      this.scanSetupOpen = false;
      if (!this.currentViewId) {
        await this.newCameraView();
      }
      const online = await this.probeMacOnline();
      if (!online) {
        this.scanStatus = "Video Canvas needs PC worker online";
        return;
      }
      try {
        const { data } = await axios.post(
          this.macViewsUri,
          {
            action: "video",
            kind: "video",
            view_id: this.currentViewId,
            title: `Video ${new Date().toLocaleString()}`
          },
          { timeout: 15000 }
        );
        const rec = (data && data.record) || {};
        this.videoRecordOpen = true;
        this.videoRecordTitle = rec.title || rec.id || "Video";
        if (rec.id) {
          this.sessionId = rec.id;
          this.macSessionId = rec.id;
        }
        this.scanStatus = "Video Canvas placeholder";
      } catch (e) {
        this.scanStatus =
          (e && e.response && e.response.data && e.response.data.error) ||
          "Could not create video Canvas";
      }
    },
    closeVideoRecord() {
      this.videoRecordOpen = false;
      this.videoRecordTitle = "";
      if (!this.scanPipVisible) {
        this.sessionId = null;
        this.macSessionId = null;
      }
    },
    async loadPcMosaic(sid, opts = {}) {
      const id = String(sid || "").trim();
      if (!id || id === "mosaics" || id === "captures") return;
      const previewOnly = opts.previewOnly !== false;
      this.sessionId = id;
      this.macSessionId = id;
      this.scanSession = true;
      this.scanPipVisible = true;
      this.viewFocus = "camera";
      this.macArchived = true;
      this.scanSetupOpen = false;
      this.continueSetupOpen = false;
      this.scanRunning = false;
      this.piCellCount = 0;
      this.slideStale = false;
      // Slide image only in PiP — never import foreign chat/results into current Camera
      const tool = this.$refs.agentTool;
      if (tool && tool.clearCanvasOverlayState) tool.clearCanvasOverlayState();
      const got = await this.ensureMacSession();
      // Pull Continue metadata from Mac manifest so plan/Go work after load
      try {
        const {
          data
        } = await axios.get(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/session`,
          { params: { session_id: id }, timeout: 8000 }
        );
        const man = (data && data.manifest) || data || {};
        const lastGrid = man.last_grid || [
          man.nx || this.grid[0],
          man.ny || this.grid[1],
          1
        ];
        this.scanMeta = {
          ...(this.scanMeta || {}),
          session_id: id,
          stride_size:
            man.stride_size || (this.scanMeta && this.scanMeta.stride_size),
          px_per_step:
            man.px_per_step || (this.scanMeta && this.scanMeta.px_per_step),
          axis_sign: man.axis_sign || [-1, -1],
          origin_xy: man.origin_xy || null,
          initial_xy: man.initial_xy || man.origin_xy || null,
          nx: man.nx || (lastGrid && lastGrid[0]) || 1,
          ny: man.ny || (lastGrid && lastGrid[1]) || 1,
          min_ix: man.min_ix != null ? man.min_ix : 0,
          min_iy: man.min_iy != null ? man.min_iy : 0,
          last_grid: lastGrid,
          refined: !!man.refined,
          dataset_id: man.dataset_id || null
        };
        this.sessionGridPreset = this.presetFromGrid(lastGrid);
        this.gridPreset = this.sessionGridPreset;
        const n =
          (man.cells && man.cells.length) ||
          (man.sync && man.sync.mac_cells) ||
          0;
        if (n > 0) this.piCellCount = 0; // occupancy is on PC until hydrate
      } catch (_) {
        /* optional */
      }
      this.scanStatus = got
        ? previewOnly
          ? `Canvas ${opts.title || id}`
          : `Loaded PC ${id}`
        : `No mosaic at ${id}`;
      this.$nextTick(() => this.scheduleLodRefresh());
      this.refreshCanvasContext();
      // previewOnly: do not apply agent.json chat / results into the dialog
      if (!previewOnly) {
        try {
          const {
            data
          } = await axios.get(
            `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/agent`,
            { params: { session_id: id }, timeout: 8000 }
          );
          const snap = data && data.agent;
          if (snap && tool && tool.applyPcAgentSnapshot) {
            tool.applyPcAgentSnapshot(snap);
          }
        } catch (_) {
          /* no saved agent chat */
        }
      }
    },
    async macSessionHasTiles(sid) {
      const id = sid || this.macSessionId || this.sessionId;
      if (!id) return false;
      try {
        const {
          data
        } = await axios.get(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/session`,
          { params: { session_id: id }, timeout: 6000 }
        );
        const man = (data && data.manifest) || data || {};
        const n =
          (man.cells && man.cells.length) ||
          (man.sync && man.sync.mac_cells) ||
          0;
        return Number(n) > 0;
      } catch (_) {
        return false;
      }
    },
    scheduleLodRefresh() {
      if (this.lodTimer) clearTimeout(this.lodTimer);
      this.lodTimer = setTimeout(async () => {
        if (this.lodRefreshBusy) return;
        this.lodRefreshBusy = true;
        try {
          await this.ensureMacSession();
          await this.refreshPyramidTiles();
          await this.refreshLodTiles();
        } finally {
          this.lodRefreshBusy = false;
        }
      }, 120);
    },
    async refreshPyramidTiles() {
      if (!this.pyramidEnabled || !this.macMosaicBase) {
        this.pyramidTiles = [];
        return;
      }
      const sid = this.macSessionId || this.sessionId;
      if (!sid) {
        this.pyramidTiles = [];
        return;
      }
      const requestSeq = ++this.pyramidRefreshSeq;
      try {
        const now = Date.now();
        if (
          !this.pyramidInfo ||
          now - Number(this.pyramidInfoCheckedAt || 0) > 2200 ||
          this.pyramidInfo.session_id !== sid
        ) {
          const r = await axios.get(`${this.macMosaicBase}/pyramid/info.json`, {
            params: {
              ...this.macSessionQuery,
              _: this.macPreviewRevision || now
            },
            timeout: 120000
          });
          if (requestSeq !== this.pyramidRefreshSeq) return;
          const info = r.data || {};
          if (!info.width || !info.height || !Array.isArray(info.levels)) {
            throw new Error("invalid pyramid metadata");
          }
          this.pyramidInfo = info;
          this.pyramidInfoCheckedAt = now;
        }
        this.updateMosaicContentBox();
        this.updateTileCanvasBox();
        const box = this.pyramidLayerBox;
        const layer = this.$refs.mosaicLayer;
        const info = this.pyramidInfo;
        if (!box || !layer || !info || !info.levels.length) {
          this.pyramidTiles = [];
          return;
        }

        const levels = info.levels;
        const scale = Math.max(1, Number(this.scanView.scale) || 1);
        const targetWidth = Math.max(256, box.width * scale * 1.25);
        let levelIndex = levels.length - 1;
        for (let i = 0; i < levels.length; i += 1) {
          if (Number(levels[i].width) >= targetWidth) {
            levelIndex = i;
            break;
          }
        }

        const layerWidth = layer.clientWidth;
        const layerHeight = layer.clientHeight;
        const centerX = layerWidth / 2;
        const centerY = layerHeight / 2;
        const transformedVisible = level => {
          const levelWidth = Number(level.width) || 1;
          const levelHeight = Number(level.height) || 1;
          const worldLeft =
            centerX + (0 - centerX - Number(this.scanView.tx || 0)) / scale;
          const worldTop =
            centerY + (0 - centerY - Number(this.scanView.ty || 0)) / scale;
          const worldRight =
            centerX +
            (layerWidth - centerX - Number(this.scanView.tx || 0)) / scale;
          const worldBottom =
            centerY +
            (layerHeight - centerY - Number(this.scanView.ty || 0)) / scale;
          const left = Math.max(
            box.left,
            Math.min(box.left + box.width, worldLeft)
          );
          const top = Math.max(
            box.top,
            Math.min(box.top + box.height, worldTop)
          );
          const right = Math.max(
            box.left,
            Math.min(box.left + box.width, worldRight)
          );
          const bottom = Math.max(
            box.top,
            Math.min(box.top + box.height, worldBottom)
          );
          const lx0 = Math.max(
            0,
            ((Math.min(left, right) - box.left) / Math.max(1, box.width)) *
              levelWidth
          );
          const ly0 = Math.max(
            0,
            ((Math.min(top, bottom) - box.top) / Math.max(1, box.height)) *
              levelHeight
          );
          const lx1 = Math.min(
            levelWidth,
            ((Math.max(left, right) - box.left) / Math.max(1, box.width)) *
              levelWidth
          );
          const ly1 = Math.min(
            levelHeight,
            ((Math.max(top, bottom) - box.top) / Math.max(1, box.height)) *
              levelHeight
          );
          return { levelWidth, levelHeight, lx0, ly0, lx1, ly1 };
        };

        let visible = transformedVisible(levels[levelIndex]);
        const tileSize = Math.max(16, Number(info.tile_size) || 256);
        const tileCount = value => {
          const x0 = Math.max(0, Math.floor(value.lx0 / tileSize) - 1);
          const y0 = Math.max(0, Math.floor(value.ly0 / tileSize) - 1);
          const x1 = Math.min(
            Number(levels[levelIndex].tiles_x) || 0,
            Math.ceil(value.lx1 / tileSize) + 1
          );
          const y1 = Math.min(
            Number(levels[levelIndex].tiles_y) || 0,
            Math.ceil(value.ly1 / tileSize) + 1
          );
          return {
            x0,
            y0,
            x1,
            y1,
            count: Math.max(0, x1 - x0) * Math.max(0, y1 - y0)
          };
        };
        // A pathological zoom/pan should degrade to one coarser level rather
        // than queue hundreds of requests and freeze the browser.
        while (levelIndex > 0 && tileCount(visible).count > 64) {
          levelIndex -= 1;
          visible = transformedVisible(levels[levelIndex]);
        }
        const range = tileCount(visible);
        const level = levels[levelIndex];
        const overlap = Math.max(0, Number(info.tile_overlap) || 0);
        const next = [];
        for (let y = range.y0; y < range.y1; y += 1) {
          for (let x = range.x0; x < range.x1; x += 1) {
            const sx0 = Math.max(0, x * tileSize - overlap);
            const sy0 = Math.max(0, y * tileSize - overlap);
            const sx1 = Math.min(
              Number(level.width),
              (x + 1) * tileSize + overlap
            );
            const sy1 = Math.min(
              Number(level.height),
              (y + 1) * tileSize + overlap
            );
            next.push({
              key: `${info.revision}|${levelIndex}|${x}|${y}`,
              url: `${
                this.macMosaicBase
              }/pyramid/tile?session_id=${encodeURIComponent(
                sid
              )}&level=${levelIndex}&x=${x}&y=${y}&r=${encodeURIComponent(
                info.revision || this.macPreviewRevision || "0"
              )}`,
              style: {
                left: `${box.left + (sx0 / Number(level.width)) * box.width}px`,
                top: `${box.top + (sy0 / Number(level.height)) * box.height}px`,
                width: `${((sx1 - sx0) / Number(level.width)) * box.width}px`,
                height: `${((sy1 - sy0) / Number(level.height)) * box.height}px`
              }
            });
          }
        }
        if (requestSeq === this.pyramidRefreshSeq) this.pyramidTiles = next;
      } catch (_) {
        // A live preview may briefly disappear while the Mac is rewriting it;
        // keep the last usable tiles and let the next poll retry.
      }
    },
    setMosaicDisplayMode(mode) {
      const next = mode === "tiles" ? "tiles" : "slide";
      if (this.mosaicDisplayMode === next) return;
      this.mosaicDisplayMode = next;
      if (next === "slide") {
        this.lodTiles = [];
      } else {
        this.$nextTick(() => {
          this.updateMosaicContentBox();
          this.scheduleLodRefresh();
        });
      }
    },
    async ensureMacSession() {
      const sid = this.macSessionId || this.sessionId;
      if (!sid) return false;
      const now = Date.now();
      if (
        this.macSessionSnapshotSid === sid &&
        now - Number(this.macSessionSnapshotAt || 0) <=
          (this.scanRunning ? 700 : 1200)
      ) {
        return this.macSessionSnapshotOk;
      }
      if (this._macSessionProbe && this._macSessionProbe.sid === sid) {
        return this._macSessionProbe.promise;
      }
      const probe = (async () => {
        try {
          const {
            data
          } = await axios.get(
            `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/session`,
            { params: { session_id: sid }, timeout: 6000 }
          );
          const man = (data && data.manifest) || data || {};
          const cells = (man.cells && man.cells.length) || 0;
          const syncCells = (man.sync && man.sync.mac_cells) || 0;
          if (cells < 1 && syncCells < 1) {
            this.macSessionSnapshotSid = sid;
            this.macSessionSnapshotAt = Date.now();
            this.macSessionSnapshotOk = false;
            return false;
          }
          const previewRevision =
            man.preview_revision ||
            man.live_preview_revision ||
            man.refined_at ||
            man.updated_at ||
            0;
          if (previewRevision && previewRevision !== this.macPreviewRevision) {
            this.macPreviewRevision = previewRevision;
            this.pyramidInfoCheckedAt = 0;
          }
          this.macSessionId = sid;
          this.macArchived = true;
          if (man.layout && man.layout.tiles && man.layout.tiles.length) {
            this.applyTileLayout(man.layout);
          } else if (Object.keys(this.tileLayoutMap || {}).length === 0) {
            await this.fetchTileLayout();
          }
          // Frozen Slide after Continue must not be replaced by the latest Mac stitch
          if (
            !this.scanRunning &&
            !this.slideStale &&
            (!this.mosaicUrl ||
              !String(this.mosaicUrl).includes("/mac/preview"))
          ) {
            this.useMacPreview();
          }
          this.macSessionSnapshotSid = sid;
          this.macSessionSnapshotAt = Date.now();
          this.macSessionSnapshotOk = true;
          return true;
        } catch (_) {
          this.macSessionSnapshotSid = sid;
          this.macSessionSnapshotAt = Date.now();
          this.macSessionSnapshotOk = false;
          return false;
        }
      })();
      this._macSessionProbe = { sid, promise: probe };
      return probe.then(
        value => {
          if (this._macSessionProbe && this._macSessionProbe.promise === probe)
            this._macSessionProbe = null;
          return value;
        },
        error => {
          if (this._macSessionProbe && this._macSessionProbe.promise === probe)
            this._macSessionProbe = null;
          throw error;
        }
      );
    },
    cellStyleForIndex(ix, iy) {
      // The pre-refinement layout uses the same XY geometry as the scan.
      const key = `${ix},${iy}`;
      const pose = this.tileLayoutMap && this.tileLayoutMap[key];
      if (pose && pose.w > 0.01 && pose.h > 0.01) {
        return {
          left: `${pose.x * 100}%`,
          top: `${pose.y * 100}%`,
          width: `${pose.w * 100}%`,
          height: `${pose.h * 100}%`,
          zIndex: Math.round((pose.y + pose.x) * 100) + 1
        };
      }
      const m = this.scanMeta || {};
      const nx = Math.max(1, m.nx || this.grid[0] || 1);
      const ny = Math.max(1, m.ny || this.grid[1] || 1);
      const minIx = m.min_ix != null ? m.min_ix : 0;
      const minIy = m.min_iy != null ? m.min_iy : 0;
      const signs = m.axis_sign || [-1, -1];
      const ax = signs[0] < 0 ? -1 : 1;
      const ay = signs[1] < 0 ? -1 : 1;
      const col = ax < 0 ? minIx + nx - 1 - ix : ix - minIx;
      const row = ay < 0 ? minIy + ny - 1 - iy : iy - minIy;
      const g = this.fallbackTileGeometry();
      return {
        left: ((col * g.sx / g.w) * 100) + "%",
        top: ((row * g.sy / g.h) * 100) + "%",
        width: (100 / g.w) + "%",
        height: (100 / g.h) + "%",
        zIndex: row * nx + col + 1
      };
    },
    fallbackTileGeometry() {
      const m = this.scanMeta || {};
      const stride = m.stride_size || [], scale = m.px_per_step || [];
      const fraction = (axis, span) => {
        const value = Math.abs(stride[axis] * scale[axis]) / span;
        return Number.isFinite(value) && value > 0 ? value : 1 - DEFAULT_SCAN_OVERLAP;
      };
      const sx = fraction(0, 640), sy = fraction(1, 480);
      const nx = Math.max(1, Number(m.nx) || this.grid[0] || 1);
      const ny = Math.max(1, Number(m.ny) || this.grid[1] || 1);
      return { sx, sy, w: 1 + (nx - 1) * sx, h: 1 + (ny - 1) * sy };
    },
    applyTileLayout(layout) {
      if (!layout || !Array.isArray(layout.tiles)) return;
      const map = {};
      layout.tiles.forEach(t => {
        if (t == null || t.ix == null || t.iy == null) return;
        map[`${t.ix},${t.iy}`] = {
          x: Number(t.x) || 0,
          y: Number(t.y) || 0,
          w: Number(t.w) || 0,
          h: Number(t.h) || 0
        };
      });
      const n = Object.keys(map).length;
      if (!n) return;
      this.tileLayoutMap = map;
      this.tileLayoutRev += 1;
    },
    async fetchTileLayout() {
      try {
        const uri =
          this.macArchived && this.macMosaicBase
            ? `${this.macMosaicBase}/layout`
            : this.tileLayoutUri;
        const r = await axios.get(uri, {
          timeout: 8000,
          params: this.macArchived ? this.macSessionQuery : undefined
        });
        this.applyTileLayout(r.data || {});
      } catch (_) {
        /* layout optional until first stitch */
      }
    },
    visibleCellIndices() {
      const m = this.scanMeta || {};
      const nx = Math.max(1, Number(m.nx) || this.grid[0] || 1);
      const ny = Math.max(1, Number(m.ny) || this.grid[1] || 1);
      const minIx = m.min_ix != null ? Number(m.min_ix) : 0;
      const minIy = m.min_iy != null ? Number(m.min_iy) : 0;
      let cells;
      const layoutKeys = Object.keys(this.tileLayoutMap || {});
      // While a scan is starting, scanMeta describes the planned grid but no
      // image exists yet. Do not turn that plan into a burst of guaranteed
      // 404 tile requests; wait for the first published live layout.
      if (this.scanRunning && !layoutKeys.length) return [];
      if (layoutKeys.length) {
        cells = layoutKeys.map(key => key.split(",").map(Number));
      } else {
        cells = [];
        for (let iy = minIy; iy < minIy + ny; iy++) {
          for (let ix = minIx; ix < minIx + nx; ix++) {
            cells.push([ix, iy]);
          }
        }
      }
      // At 1× the explicit Tiles view remains a complete tile sheet.  Once
      // zoomed, use the same viewport rule as the pyramid and keep one small
      // prefetch ring so a drag does not reveal empty holes.
      if (this.scanView.scale <= 1.02 || !this.$refs.mosaicLayer) return cells;
      const box = this.lodLayerBox || this.mosaicContentBox;
      const layer = this.$refs.mosaicLayer;
      if (!box || !layer || box.width < 1 || box.height < 1) return cells;
      const scale = Math.max(1, Number(this.scanView.scale) || 1);
      const cx = layer.clientWidth / 2;
      const cy = layer.clientHeight / 2;
      const worldLeft = cx + (0 - cx - Number(this.scanView.tx || 0)) / scale;
      const worldTop = cy + (0 - cy - Number(this.scanView.ty || 0)) / scale;
      const worldRight =
        cx + (layer.clientWidth - cx - Number(this.scanView.tx || 0)) / scale;
      const worldBottom =
        cy + (layer.clientHeight - cy - Number(this.scanView.ty || 0)) / scale;
      const pad = 0.06 / scale;
      const x0 = Math.max(
        0,
        Math.min(
          1,
          (Math.min(worldLeft, worldRight) - box.left) / box.width - pad
        )
      );
      const y0 = Math.max(
        0,
        Math.min(
          1,
          (Math.min(worldTop, worldBottom) - box.top) / box.height - pad
        )
      );
      const x1 = Math.max(
        0,
        Math.min(
          1,
          (Math.max(worldLeft, worldRight) - box.left) / box.width + pad
        )
      );
      const y1 = Math.max(
        0,
        Math.min(
          1,
          (Math.max(worldTop, worldBottom) - box.top) / box.height + pad
        )
      );
      return cells.filter(([ix, iy]) => {
        const style = this.cellStyleForIndex(ix, iy);
        const left = parseFloat(style.left) / 100;
        const top = parseFloat(style.top) / 100;
        const width = parseFloat(style.width) / 100;
        const height = parseFloat(style.height) / 100;
        return (
          left + width >= x0 && left <= x1 && top + height >= y0 && top <= y1
        );
      });
    },
    async refreshLodTiles() {
      if (!this.tilesLodEnabled) {
        if (this.mosaicDisplayMode !== "tiles") this.lodTiles = [];
        return;
      }
      // A new Canvas enters Tiles mode before the Pi has returned a session
      // id.  Do not fan out tile_at requests for that empty transitional
      // state; several old tabs doing so can overload the Pi with 404s.
      if (
        this.scanRunning &&
        !this.sessionId &&
        !this.macSessionId &&
        !this.mosaicUrl
      ) {
        this.lodTiles = [];
        return;
      }
      this.updateTileCanvasBox();
      if (!this.lodLayerBox) {
        this.updateMosaicContentBox();
        if (!this.lodLayerBox && this.$refs.mosaicLayer) {
          const layer = this.$refs.mosaicLayer;
          const w = layer.clientWidth;
          const h = layer.clientHeight;
          if (w > 8 && h > 8) {
            this.tileCanvasBox = { left: 0, top: 0, width: w, height: h };
          }
        }
        if (!this.lodLayerBox) return;
      }
      await this.ensureMacSession();
      // Mac-only only when Pi tiles are gone; Continue still reads Pi first
      const useMacOnly = !!(
        this.macArchived &&
        this.macMosaicBase &&
        !this.scanRunning &&
        this.piCellCount < 1
      );
      // During acquisition the Mac already owns a bounded copy of each
      // received tile. Prefer its small thumbnail when available; fall back
      // to the Pi's original tile if the sync is one tile behind. This keeps
      // the arrival view useful without pushing full-resolution JPEGs through
      // the Pi proxy or retaining them all in the browser.
      const preferMacThumb = !!(
        this.scanRunning &&
        this.macArchived &&
        this.macMosaicBase
      );
      const sid = useMacOnly
        ? this.macSessionId || this.sessionId || "mac"
        : this.sessionId || this.macSessionId || "nosession";
      if (useMacOnly && Object.keys(this.tileLayoutMap || {}).length === 0) {
        await this.fetchTileLayout();
      }
      const cells = this.visibleCellIndices();
      // During acquisition the bounded preview remains the base layer. Add
      // only a small, latest-tile window on top of it: this gives the user a
      // visible one-by-one Canvas update without opening dozens of image
      // requests or retaining a full-resolution copy of a Full scan in the
      // browser.
      const liveCells = this.scanRunning ? cells.slice(-24) : cells;
      const next = [];
      const fetchOne = async (ix, iy) => {
        const key = `${sid}|${ix},${iy}`;
        let url = this.lodCache[key];
        if (url) {
          return {
            key,
            url,
            style: this.cellStyleForIndex(ix, iy)
          };
        }
        if (this.lodPending[key]) return null;
        this.$set(this.lodPending, key, true);
        try {
          let r;
          const fetchPiTile = () =>
            axios.get(this.tileAtBaseUri, {
              params: {
                ix,
                iy,
                session_id: this.sessionId || this.macSessionId || undefined,
                _: `${sid}-${ix},${iy}`
              },
              responseType: "blob",
              headers: { "Cache-Control": "no-cache" },
              timeout: 15000
            });
          if (useMacOnly) {
            r = await axios.get(`${this.macMosaicBase}/tile`, {
              params: {
                ix,
                iy,
                session_id: this.macSessionId || this.sessionId,
                _: `${sid}-${ix},${iy}`
              },
              responseType: "blob",
              timeout: 20000,
              headers: { "Cache-Control": "no-cache" }
            });
          } else if (preferMacThumb) {
            try {
              r = await axios.get(`${this.macMosaicBase}/tile`, {
                params: {
                  ix,
                  iy,
                  session_id: this.macSessionId || this.sessionId,
                  thumb: 1,
                  width: 320,
                  _: `${sid}-${ix},${iy}`
                },
                responseType: "blob",
                timeout: 12000
              });
            } catch (_) {
              r = await fetchPiTile();
            }
          } else {
            try {
              r = await fetchPiTile();
            } catch (e1) {
              if (!this.macMosaicBase && !(await this.ensureMacSession())) {
                throw e1;
              }
              r = await axios.get(`${this.macMosaicBase}/tile`, {
                params: {
                  ix,
                  iy,
                  session_id: this.macSessionId || this.sessionId,
                  _: `${sid}-${ix},${iy}`
                },
                responseType: "blob",
                timeout: 20000
              });
            }
          }
          const blob = r && r.data;
          if (!blob || !(blob instanceof Blob) || blob.size < 64) {
            return null;
          }
          if (blob.type && /json|text|html/i.test(blob.type)) {
            return null;
          }
          url = URL.createObjectURL(blob);
          this.$set(this.lodCache, key, url);
          return {
            key,
            url,
            style: this.cellStyleForIndex(ix, iy)
          };
        } catch (_) {
          return null;
        } finally {
          this.$delete(this.lodPending, key);
        }
      };
      // Bounded concurrency keeps a Full scan responsive on both the Pi and
      // the browser. Four small tile requests are enough to make arrival
      // visible while avoiding the old request fan-out.
      const settled = [];
      let cursor = 0;
      const worker = async () => {
        while (cursor < liveCells.length) {
          const index = cursor++;
          const cell = liveCells[index];
          const item = await fetchOne(cell[0], cell[1]);
          if (item) settled.push(item);
        }
      };
      await Promise.all(
        Array.from({ length: Math.min(4, liveCells.length) }, () => worker())
      );
      settled.forEach(item => {
        if (item) next.push(item);
      });
      this.lodTiles = next;
      // Object URLs are not garbage-collected while retained in lodCache.
      // Keep only the current viewport window during a scan, and a bounded
      // recent cache after a scan. A Full scan can otherwise leave hundreds
      // of decoded tile blobs attached to one browser tab.
      const keepKeys = new Set(next.map(item => item.key));
      const cacheLimit = this.scanRunning ? 32 : 96;
      const candidates = Object.keys(this.lodCache).filter(
        key => key.startsWith(`${sid}|`) && !keepKeys.has(key)
      );
      const removeCount = Math.max(
        0,
        candidates.length - Math.max(0, cacheLimit - keepKeys.size)
      );
      candidates.slice(0, removeCount).forEach(key => {
        try {
          URL.revokeObjectURL(this.lodCache[key]);
        } catch (_) {
          /* ignore stale object URLs */
        }
        this.$delete(this.lodCache, key);
      });
      if (useMacOnly && next.length === 0 && cells.length) {
        this.scanStatus = "PC tiles unreachable — check PC worker / supervisor";
      }
    },
    presetFromGrid(grid) {
      if (!grid || grid.length < 2) return "med";
      if (grid[0] === "explore" || grid[0] === "Explore") return "full";
      const nx = Number(grid[0]);
      const ny = Number(grid[1]);
      if (!Number.isFinite(nx) || !Number.isFinite(ny)) return "full";
      if (nx === 1 && ny === 1) return "fov";
      const entry = Object.entries(GRID_PRESETS).find(
        ([, v]) => v[0] === nx && v[1] === ny
      );
      if (entry) return entry[0];
      this.customNx = nx;
      this.customNy = ny;
      return "custom";
    },
    selectGridPreset(id) {
      this.gridPreset = id;
      if (id === "custom" && (!this.customNx || !this.customNy)) {
        const cur = this.sessionChunkGrid;
        this.customNx = Math.max(cur[0], 5);
        this.customNy = Math.max(cur[1], 5);
      }
    },
    openContinueSetup() {
      // Restore session chunk shape; filter smaller presets out
      const preset = this.presetFromGrid(this.sessionChunkGrid);
      this.sessionGridPreset = preset;
      this.gridPreset = preset;
      if (preset === "custom") {
        const g = this.sessionChunkGrid;
        this.customNx = g[0];
        this.customNy = g[1];
      }
      // If current preset disabled somehow, pick smallest allowed
      if (!this.canContinueWithPreset) {
        const first = this.continueGridOptions.find(g => !g.disabled);
        if (first) this.selectGridPreset(first.id);
      }
      // Show existing cells so Continue feels like expanding the mosaic
      this.setMosaicDisplayMode("tiles");
      this.continueSetupOpen = true;
    },
    confirmContinueScan() {
      if (!this.canContinueWithPreset) return;
      this.continueSetupOpen = false;
      this.sessionGridPreset = this.gridPreset;
      this.continueScan();
    },
    resolveAutofocus() {
      // Manual AF must stay on OpenFlexure's native Fast-AF action.  The
      // slower conditional probe/repair path belongs only to scan capture,
      // where the Pi runs one native pass and classifies the resulting tile.
      this.fastAutofocusUri = `${this.$store.getters.baseUri}/api/v2/extensions/org.openflexure.autofocus/fast_autofocus`;
      axios.get(this.pluginsUri).catch(() => {});
    },
    ensureHardwareClientId() {
      if (this.hardwareClientId) return this.hardwareClientId;
      try {
        const key = "openmhs.hardwareClientId";
        let id = window.localStorage.getItem(key);
        if (!id) {
          id = `browser-${Math.random()
            .toString(36)
            .slice(2)}-${Date.now()}`;
          window.localStorage.setItem(key, id);
        }
        this.hardwareClientId = id;
      } catch (_) {
        this.hardwareClientId = `browser-${Date.now()}`;
      }
      return this.hardwareClientId;
    },
    hardwareRequestConfig(timeout = 8000) {
      const id = this.ensureHardwareClientId();
      return {
        timeout,
        headers: id ? { "X-MicroWorld-Client": id } : {}
      };
    },
    async refreshHardwareState() {
      try {
        const { data } = await axios.get(this.hardwareUri, {
          timeout: 2500
        });
        this.hardwareState = data && typeof data === "object" ? data : null;
      } catch (_) {
        // A transient Pi restart should not make the UI claim a false lease.
        this.hardwareState = null;
      }
    },
    refreshStagePos() {
      axios
        .get(this.positionUri)
        .then(r => {
          const p = r.data;
          if (p && (p.x != null || p[0] != null)) {
            this.stagePos = Array.isArray(p)
              ? { x: p[0], y: p[1], z: p[2] }
              : { x: p.x, y: p.y, z: p.z };
            this.syncStageEditFromPos();
          }
        })
        .catch(() => {});
    },
    onDocumentPointerDown(e) {
      if (!this.stagePresetOpen) return;
      const menu =
        this.$el && this.$el.querySelector
          ? this.$el.querySelector(".step-preset-wrap")
          : null;
      if (menu && !menu.contains(e.target)) this.stagePresetOpen = false;
    },
    chooseStepPreset(id) {
      if (this.zControlsLocked || !PRESETS[id]) return;
      this.stepPreset = id;
      this.stagePresetOpen = false;
    },
    validStep(value) {
      const step = Number(value);
      return Number.isInteger(step) && step >= 1 && step <= 10000;
    },
    stepFor(axis) {
      const key = { x: "stepX", y: "stepY", z: "stepZ" }[axis];
      return key && this.validStep(this[key]) ? Number(this[key]) : 1;
    },
    commitStepSize(axis) {
      const key = { x: "stepX", y: "stepY", z: "stepZ" }[axis];
      if (!key) return;
      if (!this.validStep(this[key])) this[key] = 1;
      this.stepPreset = "custom";
      this.saveStepSizes();
    },
    saveStepSizes() {
      try {
        window.localStorage.setItem(
          "openmhs.stageSteps",
          JSON.stringify({ x: this.stepX, y: this.stepY, z: this.stepZ })
        );
      } catch (_) {
        /* storage may be disabled */
      }
    },
    restoreStepSizes() {
      try {
        const saved = JSON.parse(
          window.localStorage.getItem("openmhs.stageSteps")
        );
        if (
          !saved ||
          !["x", "y", "z"].every(axis => this.validStep(saved[axis]))
        )
          return;
        this.stepX = saved.x;
        this.stepY = saved.y;
        this.stepZ = saved.z;
        const matching = Object.keys(PRESETS).find(
          id =>
            PRESETS[id].xy === saved.x &&
            PRESETS[id].xy === saved.y &&
            PRESETS[id].z === saved.z
        );
        this.stepPreset = matching || "custom";
      } catch (_) {
        /* no saved steps */
      }
    },
    syncStageEditFromPos() {
      const p = this.stagePos;
      if (!p || this.stageMovingAbs) return;
      const next = { ...this.stageEdit };
      ["x", "y", "z"].forEach(axis => {
        if (this.stageEditFocus === axis) return;
        const v = p[axis];
        next[axis] =
          v == null || Number.isNaN(Number(v))
            ? ""
            : String(Math.round(Number(v)));
      });
      this.stageEdit = next;
    },
    onStageEditBlur(axis) {
      this.stageEditFocus = null;
      this.commitStageAxis(axis);
    },
    resetStageAxis(axis) {
      if (!this.stagePos || this.stagePos[axis] == null) return;
      this.stageEdit = {
        ...this.stageEdit,
        [axis]: String(Math.round(Number(this.stagePos[axis])))
      };
    },
    async commitStageAxis(axis) {
      const raw = this.stageEdit[axis];
      const n = Number(raw);
      if (!Number.isFinite(n) || this.zControlsLocked) {
        this.syncStageEditFromPos();
        return;
      }
      const p = this.stagePos || { x: 0, y: 0, z: 0 };
      const cur = Number(p[axis]);
      if (Number.isFinite(cur) && Math.round(cur) === Math.round(n)) {
        this.stageEdit = { ...this.stageEdit, [axis]: String(Math.round(n)) };
        return;
      }
      const target = {
        x: axis === "x" ? n : p.x,
        y: axis === "y" ? n : p.y,
        z: axis === "z" ? n : p.z
      };
      this.stageEdit = {
        ...this.stageEdit,
        [axis]: String(Math.round(n))
      };
      await this.moveAbs(target.x, target.y, target.z);
    },
    async commitStagePosition() {
      if (this.zControlsLocked || this.stageMovingAbs) return;
      const values = {};
      for (const axis of ["x", "y", "z"]) {
        const value = Number(this.stageEdit[axis]);
        if (!Number.isFinite(value)) {
          this.syncStageEditFromPos();
          return;
        }
        values[axis] = Math.round(value);
      }
      this.stageEditFocus = null;
      await this.moveAbs(values.x, values.y, values.z);
    },
    async moveAbs(x, y, z) {
      if (this.scanRunning || this.hardwareBusy) return;
      const payload = {
        x: Number(x) || 0,
        y: Number(y) || 0,
        z: Number(z) || 0,
        absolute: true
      };
      this.stageMovingAbs = true;
      try {
        const res = await axios.post(
          this.moveActionUri,
          payload,
          this.hardwareRequestConfig()
        );
        await this.waitAction(res.data.href, 200);
        this.refreshStagePos();
      } catch (e) {
        if (e && e.response && e.response.status === 409) {
          await this.refreshHardwareState();
        }
      } finally {
        this.stageMovingAbs = false;
        this.syncStageEditFromPos();
      }
    },
    async moveRel(dx, dy, dz) {
      // Allow pure-Z moves even while viewing mosaic (focus); block XY then.
      const pureZ = !dx && !dy && !!dz;
      if (this.scanRunning || this.hardwareBusy) return;
      if (!pureZ && this.controlsLocked) return;
      const payload = {
        x: (dx || 0) * (this.invert.x ? -1 : 1),
        y: (dy || 0) * (this.invert.y ? -1 : 1),
        z: (dz || 0) * (this.invert.z ? -1 : 1),
        absolute: false
      };
      try {
        const res = await axios.post(
          this.moveActionUri,
          payload,
          this.hardwareRequestConfig()
        );
        await this.waitAction(res.data.href, 120);
        this.refreshStagePos();
      } catch (e) {
        if (e && e.response && e.response.status === 409) {
          await this.refreshHardwareState();
        }
      }
    },
    onViewportResize() {
      this.updateMosaicContentBox();
    },
    applyScaleCalibration(calib) {
      if (!calib) return;
      const pps = calib.px_per_step || calib.pxPerStep;
      if (Array.isArray(pps) && pps.length >= 2) {
        this.pxPerStep = [Number(pps[0]), Number(pps[1])];
        try {
          window.localStorage.setItem(
            "openmhs.pxPerStep",
            JSON.stringify(this.pxPerStep)
          );
        } catch (_) {
          /* ignore */
        }
      }
    },
    loadScaleCalibration() {
      try {
        const raw = window.localStorage.getItem("openmhs.pxPerStep");
        if (raw) {
          const arr = JSON.parse(raw);
          if (Array.isArray(arr) && arr.length >= 2) {
            this.pxPerStep = [Number(arr[0]), Number(arr[1])];
          }
        }
      } catch (_) {
        /* ignore */
      }
      // Prefer live values from Pi if available
      axios
        .get(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/live_details`,
          {
            timeout: 2500
          }
        )
        .then(r => {
          const d = r.data || {};
          // An old in-flight poll must not replace the selected Canvas.
          if (
            this.externalScanDisposed ||
            (d.session_id && this.sessionId && d.session_id !== this.sessionId)
          )
            return;
          if (Array.isArray(d.px_per_step) && d.px_per_step.length >= 2) {
            this.applyScaleCalibration(d);
          }
        })
        .catch(() => {});
    },
    async runScaleCalibrate() {
      if (this.scaleCalibBusy || this.zControlsLocked) return;
      if (this.viewFocus === "scan" && this.scanPipVisible) {
        this.scaleStatus = "Switch to live camera to calibrate";
        return;
      }
      this.scaleCalibBusy = true;
      this.scaleStatus = "Jogging & measuring…";
      try {
        const r = await axios.post(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/calibrate_scale`,
          { probe_steps: 600 },
          { timeout: 60000 }
        );
        const calib = r.data || {};
        this.applyScaleCalibration(calib);
        if (calib.ok) {
          const [px, py] = this.pxPerStep || [0, 0];
          this.scaleStatus = `OK · ${px.toFixed(3)} / ${py.toFixed(3)} px/step`;
        } else {
          this.scaleStatus = "Weak match — using best estimate";
        }
        this.refreshStagePos();
      } catch (err) {
        const msg =
          (err.response && (err.response.data || {}).message) ||
          err.message ||
          "Calibration failed";
        this.scaleStatus = String(msg).slice(0, 60);
      } finally {
        this.scaleCalibBusy = false;
        setTimeout(() => {
          if (!this.scaleCalibBusy) this.scaleStatus = "";
        }, 5000);
      }
    },
    async waitAction(href, maxTicks) {
      if (!href) return;
      const limit = maxTicks || 600;
      for (let i = 0; i < limit; i++) {
        const r = await axios.get(href);
        if (r.data.status === "completed") return r.data;
        if (["failed", "cancelled", "error"].includes(r.data.status)) {
          throw new Error(r.data.status);
        }
        await new Promise(res => setTimeout(res, 100));
      }
      throw new Error("timeout");
    },
    onKeyDown(e) {
      if (this.studioSession || this.scanRunning || this.agentDrawing) return;
      const el = e.target;
      if (
        el &&
        (el.isContentEditable ||
          (el.closest &&
            el.closest("input, textarea, select, [contenteditable=true]")))
      ) {
        return;
      }
      const tag = (el && el.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      const x = this.stepFor("x");
      const y = this.stepFor("y");
      const z = this.stepFor("z");
      // + / = (unshifted +) raise Z; - / _ lower Z
      if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        this.moveRel(0, 0, z);
        return;
      }
      if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        this.moveRel(0, 0, -z);
        return;
      }
      const map = {
        ArrowUp: [0, y, 0],
        ArrowDown: [0, -y, 0],
        ArrowLeft: [x, 0, 0],
        ArrowRight: [-x, 0, 0]
      };
      const d = map[e.key];
      if (!d) return;
      if (this.controlsLocked) return;
      e.preventDefault();
      this.moveRel(d[0], d[1], d[2]);
    },
    onWheel(e) {
      if (this.agentDrawing || !this.scanZoomEnabled || !e.deltaY) return;
      e.preventDefault();
      const factor = e.deltaY > 0 ? 0.9 : 1.12;
      this.zoomScanAt(e.clientX, e.clientY, factor);
    },
    onSurfacePointerDown(e) {
      if (this.controlsLocked || this.agentDrawing) return;
      if (this.viewFocus === "scan") return;
      this.$set(this.activePointers, e.pointerId, {
        x: e.clientX,
        y: e.clientY
      });
      if (Object.keys(this.activePointers).length >= 2) {
        this.stopHoldMove();
        this.xyDrag = null;
        this.lastPinchDist = this.pinchDistance();
        return;
      }
      const el = this.$refs.mainSurface;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      const y = (e.clientY - rect.top) / rect.height;
      let dx = 0;
      let dy = 0;
      // Edge press: hold near border to jog continuously
      if (x < 0.18) dx = 1;
      else if (x > 0.82) dx = -1;
      if (y < 0.18) dy = 1;
      else if (y > 0.82) dy = -1;
      if (dx || dy) {
        this.holdMove = { dx, dy };
        this.xyDrag = null;
        try {
          e.currentTarget.setPointerCapture(e.pointerId);
        } catch (_) {
          /* ignore */
        }
        this.runHoldLoop();
        return;
      }
      // Center drag: drag to nudge stage in that direction
      this.xyDrag = {
        pointerId: e.pointerId,
        x0: e.clientX,
        y0: e.clientY,
        lastX: e.clientX,
        lastY: e.clientY,
        moved: false
      };
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch (_) {
        /* ignore */
      }
    },
    onSurfacePointerMove(e) {
      if (this.controlsLocked || this.agentDrawing) return;
      if (
        !(e.pointerId in this.activePointers) &&
        !(this.xyDrag && this.xyDrag.pointerId === e.pointerId)
      ) {
        return;
      }
      if (e.pointerId in this.activePointers) {
        this.$set(this.activePointers, e.pointerId, {
          x: e.clientX,
          y: e.clientY
        });
      }
      if (Object.keys(this.activePointers).length >= 2) {
        const dist = this.pinchDistance();
        if (this.lastPinchDist == null) {
          this.lastPinchDist = dist;
          return;
        }
        const delta = dist - this.lastPinchDist;
        if (Math.abs(delta) > 12) {
          this.lastPinchDist = dist;
          this.moveRel(0, 0, (delta > 0 ? 1 : -1) * this.stepFor("z"));
        }
        return;
      }
      if (
        this.xyDrag &&
        this.xyDrag.pointerId === e.pointerId &&
        !this.holdMove
      ) {
        const dx = e.clientX - this.xyDrag.lastX;
        const dy = e.clientY - this.xyDrag.lastY;
        const thresh = 28;
        if (Math.abs(dx) >= thresh || Math.abs(dy) >= thresh) {
          this.xyDrag.moved = true;
          // Screen right → stage −X (matches arrow mapping); screen down → stage −Y
          let mx = 0;
          let my = 0;
          if (Math.abs(dx) >= Math.abs(dy)) {
            mx = dx > 0 ? -1 : 1;
          } else {
            my = dy > 0 ? -1 : 1;
          }
          this.xyDrag.lastX = e.clientX;
          this.xyDrag.lastY = e.clientY;
          this.moveRel(mx * this.stepFor("x"), my * this.stepFor("y"), 0);
        }
      }
    },
    onSurfacePointerUp(e) {
      if (e && e.pointerId != null)
        this.$delete(this.activePointers, e.pointerId);
      else this.activePointers = {};
      if (Object.keys(this.activePointers).length < 2)
        this.lastPinchDist = null;
      if (this.xyDrag && (!e || e.pointerId === this.xyDrag.pointerId)) {
        this.xyDrag = null;
      }
      this.stopHoldMove();
    },
    pinchDistance() {
      const pts = Object.values(this.activePointers);
      if (pts.length < 2) return 0;
      const dx = pts[0].x - pts[1].x;
      const dy = pts[0].y - pts[1].y;
      return Math.sqrt(dx * dx + dy * dy);
    },
    stopHoldMove() {
      this.holdMove = null;
    },
    async runHoldLoop() {
      if (this.holdLoop) return;
      this.holdLoop = true;
      try {
        while (this.holdMove) {
          const { dx, dy } = this.holdMove;
          await this.moveRel(dx * this.stepFor("x"), dy * this.stepFor("y"), 0);
          if (this.holdMove)
            await new Promise(resolve => setTimeout(resolve, 250));
        }
      } finally {
        this.holdLoop = false;
        if (this.holdMove) this.runHoldLoop();
      }
    },
    async runAutofocus() {
      if (this.zControlsLocked || this.afBusy || this.hardwareBusy) return;
      const stockUri = `${this.$store.getters.baseUri}/api/v2/extensions/org.openflexure.autofocus/fast_autofocus`;
      const uri = this.fastAutofocusUri || stockUri;
      this.afBusy = true;
      this.afStatus = "Focusing…";
      try {
        const res = await axios.post(
          uri,
          { dz: 2000 },
          this.hardwareRequestConfig()
        );
        const href =
          (res.data && res.data.href) ||
          (res.data &&
            res.data.links &&
            res.data.links.self &&
            res.data.links.self.href);
        await this.waitAction(href, 900);
        this.afStatus = "Done";
        setTimeout(() => {
          this.afStatus = "";
        }, 1500);
      } catch (e) {
        if (e && e.response && e.response.status === 409) {
          await this.refreshHardwareState();
          this.afStatus = "Microscope busy";
        } else {
          this.afStatus = "AF failed";
        }
        setTimeout(() => {
          this.afStatus = "";
        }, 2500);
      } finally {
        this.afBusy = false;
      }
    },
    async startNewScan() {
      if (this.startingNewScan || this.scanRunning) return;
      this.startingNewScan = true;
      this.scanSetupOpen = false;
      try {
        const live = await this.fetchLiveDetails({ force: true });
        const expected = Number(live.cell_count || 0);
        if (expected > 0) {
          const { data } = await axios.get(
            this.macViewsUri.replace(/\/views$/, "/session"),
            { params: { session_id: live.session_id }, timeout: 8000 }
          );
          const manifest = data.manifest || {};
          if (!manifest.sync || !manifest.sync.complete || (manifest.cells || []).length < expected)
            throw new Error("Previous acquisition is not fully archived.");
        }

        this.sessionGridPreset = this.gridPreset;
        this.stopMosaicPoll();
        // Release the Pi preview after the archived tiles are safe on the PC.
        await axios.post(this.cleanupUri, {
          dataset_id: live.dataset_id || this.keepDatasetId || undefined,
          save_slide: false
        });
        this.studioSession = null;
        this.canvasContext = {};
        this.scanSession = false;
        this.keepDatasetId = null;
        this.sessionId = null;
        this.piCellCount = 0;
        this.macArchived = false;
        this.macSessionId = null;
        this.macArchiveBusy = false;
        this.mosaicUrl = null;
        this.mosaicContentBox = null;
        this.slideStale = false;
        this.clearLodCacheOnly();
        this.tileLayoutMap = {};
        this.invalidateLiveDetails();
        await this.runScan({ append: false });
      } catch (e) {
        this.scanSetupOpen = true;
        window.alert("Wait before creating a new canvas: " + e.message);
      } finally {
        this.startingNewScan = false;
      }
    },
    async continueScan() {
      const sid = this.macSessionId || this.sessionId;
      if (this.piCellCount < 1 && sid) {
        this.scanStatus = "Loading Continue path from PC…";
        try {
          const { data } = await axios.post(
            `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/hydrate`,
            { session_id: sid },
            { timeout: 20000 }
          );
          if (data && data.cells) this.piCellCount = Number(data.cells) || 0;
          if (data) {
            this.scanMeta = {
              ...(this.scanMeta || {}),
              session_id: data.session_id || sid,
              origin_xy:
                data.origin_xy || (this.scanMeta && this.scanMeta.origin_xy),
              stride_size:
                data.stride_size ||
                (this.scanMeta && this.scanMeta.stride_size),
              nx: data.nx != null ? data.nx : this.scanMeta && this.scanMeta.nx,
              ny: data.ny != null ? data.ny : this.scanMeta && this.scanMeta.ny,
              min_ix:
                data.min_ix != null
                  ? data.min_ix
                  : this.scanMeta && this.scanMeta.min_ix,
              min_iy:
                data.min_iy != null
                  ? data.min_iy
                  : this.scanMeta && this.scanMeta.min_iy
            };
          }
          this.setMosaicDisplayMode("tiles");
          this.scanStatus = `PC path ready (${this.piCellCount} cells) — expanding mosaic`;
        } catch (e) {
          this.scanStatus =
            (e && e.response && e.response.data && e.response.data.message) ||
            (e && e.message) ||
            "Failed to load Continue path from PC";
          return;
        }
      }
      await this.runScan({ append: true });
    },
    async probeMacOnline() {
      // Prefer heartbeat; fall back to Pi→PC list proxy (works if supervisor URL known)
      try {
        const {
          data
        } = await axios.get(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.visual-prompts/agent`,
          { timeout: 3000 }
        );
        if (data && data.worker_online && data.supervisor_url) return true;
        // Heartbeat stale but PC supervisor URL remembered — probe proxy
        if (data && data.supervisor_url) {
          try {
            const r = await axios.get(
              `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/list`,
              { timeout: 4000 }
            );
            if (r.data && (r.data.ok || r.data.sessions)) return true;
          } catch (_) {
            /* fall through */
          }
        }
      } catch (_) {
        /* try proxy */
      }
      try {
        const {
          data
        } = await axios.get(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/list`,
          { timeout: 4000 }
        );
        return !!(data && (data.ok || data.sessions));
      } catch (_) {
        return false;
      }
    },
    async runScan({ append }) {
      if (this.roiRetakeBusy) {
        window.alert("Wait for the ROI retake to finish.");
        return;
      }
      this.scanRunning = true;
      this.continueSetupOpen = false;
      this.scanSetupOpen = false;
      if (!append) this.invalidateLiveDetails();
      // Live progress is always Tiles — Slide is disabled until scan ends
      this.setMosaicDisplayMode("tiles");
      this.macPathEnabled = await this.probeMacOnline();
      if (this.macPathEnabled) await this.syncCameraViewToPc();
      this.scanStatus = append
        ? this.isExploreMode
          ? "Exploring edges…"
          : "Filling missing tiles…"
        : this.isExploreMode
        ? "Calibrating & exploring…"
        : this.isFovMode
        ? "Capturing current FOV…"
        : this.macPathEnabled
        ? "Calibrating & scanning… (PC sync)"
        : "Calibrating & scanning… (Pi only)";
      if (!append) {
        this.mosaicUrl = null;
        this.lastCanvasStatusAt = 0;
        this.canvasStatusBusy = false;
        this.macArchived = false;
        this.macPreviewRevision = 0;
        this.lastMacPreviewProbeAt = 0;
        this.lastMacPreviewUsedAt = 0;
        this.slideStale = false;
        this.resetScanView();
        this.setMosaicDisplayMode("tiles");
      } else {
        // Continue: freeze published Slide; keep existing tile images
        this.slideStale = true;
        this.setMosaicDisplayMode("tiles");
        const g = this.grid;
        if (g && g.length >= 2) {
          const m = this.scanMeta || {};
          this.scanMeta = {
            ...m,
            nx: Math.max(Number(g[0]) || 1, Number(m.nx) || 1),
            ny: Math.max(Number(g[1]) || 1, Number(m.ny) || 1),
            min_ix: m.min_ix != null ? m.min_ix : 0,
            min_iy: m.min_iy != null ? m.min_iy : 0
          };
        }
        this.$nextTick(() => this.updateTileCanvasBox());
      }
      const initial = this.stagePos ? [this.stagePos.x, this.stagePos.y] : null;
      if (!append) {
        const g = this.grid;
        this.scanMeta = {
          nx: Math.max(1, Number(g[0]) || 1),
          ny: Math.max(1, Number(g[1]) || 1),
          min_ix: 0,
          min_iy: 0,
          axis_sign: [-1, -1],
          initial_xy: initial,
          origin_xy: initial
        };
      }
      this.focusScan({ restoreSession: append });
      this.peekUrl = null;
      try {
        const name = `openmhs_${Date.now()}`;
        const res = await axios.post(this.mosaicTileUri, {
          filename: name,
          temporary: false,
          grid: this.grid,
          auto_stride: !append,
          target_overlap: DEFAULT_SCAN_OVERLAP,
          style: this.isExploreMode ? "explore" : "snake",
          center_tile: false,
          explore: !!this.isExploreMode,
          autofocus_dz: 2000,
          fast_autofocus: true,
          scan_focus_guard: true,
          adaptive_focus_stack: true,
          // When the Mac worker is reachable, the Pi only publishes the
          // current tile; the Mac builds the bounded live preview.
          mac_preview_only: !!this.macPathEnabled,
          explore_strategy: "boundary",
          explore_max_cells: 200,
          explore_max_radius: 28,
          use_video_port: true,
          namemode: "coordinates",
          keep_tiles: true,
          auto_refine: false,
          append_session: !!append,
          fill_gaps: !!append && !this.isExploreMode,
          session_id: append
            ? this.macSessionId || this.sessionId || undefined
            : undefined,
          view_id: this.currentViewId || undefined,
          tags: ["openmhs-scan"]
        });
        this.scanHref = res.data.href;
        this.pollScanAction(initial, append);
        this.$nextTick(() => this.scheduleLodRefresh());
      } catch (e) {
        const detail =
          (e &&
            e.response &&
            e.response.data &&
            (e.response.data.message || e.response.data.name)) ||
          (e && e.message) ||
          "";
        this.scanStatus = detail
          ? `Scan failed to start (${detail})`
          : "Scan failed to start";
        this.scanRunning = false;
        // Keep Scan view on Continue failure — only new-scan start hides PiP
        if (!append) {
          this.viewFocus = "camera";
          this.scanPipVisible = false;
          this.mosaicUrl = null;
        }
      }
    },
    applyScanOutput(out, initial) {
      const nx = out.nx || this.grid[0];
      const ny = out.ny || this.grid[1];
      const lastGrid = out.last_grid || this.grid;
      this.scanMeta = {
        stride_size: out.stride_size,
        px_per_step: out.px_per_step,
        nx,
        ny,
        min_ix: out.min_ix != null ? out.min_ix : 0,
        min_iy: out.min_iy != null ? out.min_iy : 0,
        axis_sign: out.axis_sign || [-1, -1],
        initial_xy: out.initial_xy || initial,
        origin_xy: out.origin_xy || out.initial_xy || initial,
        dataset_id: out.dataset_id || null,
        mosaic_id: out.mosaic_id,
        session_id: out.session_id || this.sessionId,
        refined: !!out.refined,
        slide_saved: !!out.slide_saved,
        slide_id: out.slide_id || null,
        slide_name: out.slide_name || null,
        last_grid: lastGrid
      };
      this.keepDatasetId = out.dataset_id || this.keepDatasetId;
      this.sessionId = out.session_id || this.sessionId;
      if (out.cells != null || out.cell_count != null) {
        this.piCellCount = Number(out.cells || out.cell_count) || 0;
      }
      this.sessionGridPreset = this.presetFromGrid(lastGrid);
      this.gridPreset = this.sessionGridPreset;
      this.scanSession = true;
      this.$nextTick(() => this.updateMosaicContentBox());
    },
    async pollScanAction(initial, append) {
      if (!this.scanHref) return;
      for (let i = 0; i < 1800; i++) {
        try {
          const st = await axios.get(this.scanHref);
          const s = st.data.status;
          const prog = st.data.progress;
          // Mirror auto-refine phase while scan action is still running
          if (s === "running" && i % 2 === 0) {
            try {
              const d = (await this.fetchLiveDetails({ maxAge: 500 })) || {};
              if (d.scan_phase === "refining") {
                const rp =
                  d.refine_progress != null ? Number(d.refine_progress) : prog;
                this.refineBusy = true;
                if (rp != null) this.refineProgress = Number(rp);
                this.scanStatus = `Auto-refining… ${
                  rp != null ? Math.round(rp) + "%" : ""
                }`;
              } else if (d.focus_phase === "initializing") {
                this.scanStatus = `Initializing first tile · AF ${d.focus_attempt ||
                  1}/${d.focus_attempts || 3}`;
              }
              if (d.session_id) this.sessionId = d.session_id;
              if (d.cell_count != null) {
                this.piCellCount = Number(d.cell_count) || 0;
              }
              // Keep planned grid + live tile progress in sync
              if (d.grid || d.origin_xy || d.stride_size) {
                this.scanMeta = {
                  ...(this.scanMeta || {}),
                  stride_size:
                    d.stride_size ||
                    (this.scanMeta && this.scanMeta.stride_size),
                  px_per_step:
                    d.px_per_step ||
                    (this.scanMeta && this.scanMeta.px_per_step),
                  nx: Math.max(
                    (d.grid && d.grid[0]) || 0,
                    (this.scanMeta && this.scanMeta.nx) || 0,
                    1
                  ),
                  ny: Math.max(
                    (d.grid && d.grid[1]) || 0,
                    (this.scanMeta && this.scanMeta.ny) || 0,
                    1
                  ),
                  min_ix:
                    d.min_ix != null
                      ? d.min_ix
                      : this.scanMeta && this.scanMeta.min_ix,
                  min_iy:
                    d.min_iy != null
                      ? d.min_iy
                      : this.scanMeta && this.scanMeta.min_iy,
                  axis_sign: d.axis_sign ||
                    (this.scanMeta && this.scanMeta.axis_sign) || [-1, -1],
                  origin_xy:
                    d.origin_xy || (this.scanMeta && this.scanMeta.origin_xy),
                  initial_xy:
                    d.initial_xy || (this.scanMeta && this.scanMeta.initial_xy),
                  last_grid:
                    d.last_grid || (this.scanMeta && this.scanMeta.last_grid),
                  session_id: d.session_id || this.sessionId
                };
              }
              this.scheduleLodRefresh();
              // Live Pi→Mac tile sync progress
              if (i % 4 === 0) {
                const syncInfo = await this.pollMacSyncStatus();
                if (syncInfo && syncInfo.cells > 0 && !this.refineBusy) {
                  const pi =
                    (syncInfo.sync && syncInfo.sync.pi_cells) ||
                    d.cell_count ||
                    "?";
                  this.scanStatus = `Scan… ${
                    prog != null ? prog + "% · " : ""
                  }PC sync ${syncInfo.cells}/${pi}`;
                }
              }
            } catch (_) {
              /* ignore */
            }
          }
          if (s === "running" && !this.refineBusy) {
            this.scanStatus = `${
              append
                ? this.isExploreMode
                  ? "Explore"
                  : "Fill gaps"
                : this.isExploreMode
                ? "Explore"
                : "Scan"
            }… ${prog != null ? prog + "%" : ""}`;
          } else if (s !== "running") {
            this.scanStatus = s;
          }
          if (s === "completed") {
            this.scanRunning = false;
            this.refineBusy = false;
            const out = st.data.output || {};
            this.applyScanOutput(out, initial);
            this.setMosaicDisplayMode(append ? "tiles" : "slide");
            if (append) {
              this.slideStale = false;
              this.scanStatus = "Updating expanded canvas…";
              if (this.macPathEnabled) {
                try {
                  await this.syncTilesToMacOnly();
                  await this.runMacRefine({
                    afterArchive: true,
                    timeoutSec: 90
                  });
                  this.setMosaicDisplayMode("slide");
                  this.scanStatus = "Canvas updated";
                } catch (_) {
                  this.scanStatus =
                    "Canvas update pending — check the PC Worker connection";
                }
              }
            } else {
              this.slideStale = false;
              this.previewNeedsRefresh = true;
              if (this.macArchived && this.macMosaicBase) {
                this.useMacPreview();
              } else {
                this.refreshPreview();
              }
              if (this.macPathEnabled) {
                this.scanStatus = "PC Worker is assembling the Slide…";
                try {
                  await this.syncTilesToMacOnly();
                } catch (_) {
                  /* optional */
                }
              } else {
                this.scanStatus = "Scan complete — open Canvas to explore";
              }
              this.previewNeedsRefresh = true;
              if (this.macArchived && this.macMosaicBase) {
                this.useMacPreview();
              } else {
                this.refreshPreview();
              }
            }
            this.$nextTick(() => {
              this.updateTileCanvasBox();
              this.scheduleLodRefresh();
            });
            return;
          }
          if (["error", "cancelled", "failed"].includes(s)) {
            this.scanRunning = false;
            this.refineBusy = false;
            this.scanStatus = `Scan ${s}`;
            this.applyScanOutput(st.data.output || {}, initial);
            return;
          }
        } catch (_) {
          /* retry */
        }
        await new Promise(r => setTimeout(r, 1000));
      }
      this.scanRunning = false;
      this.refineBusy = false;
      this.scanStatus = "Scan timed out";
    },
    async cancelScan() {
      if (!this.scanHref) return;
      try {
        await axios.delete(this.scanHref);
      } catch (_) {
        try {
          await axios.post(this.scanHref + "/cancel");
        } catch (e2) {
          /* ignore */
        }
      }
      this.scanRunning = false;
      this.scanStatus = "Paused — Refine or Continue";
      this.scanSession = true;
    },
    async resolveMacSupervisor() {
      // Prefer LAN URL from Mac heartbeat (Pi page → Mac), else localhost
      const candidates = [];
      try {
        const {
          data
        } = await axios.get(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.visual-prompts/agent`,
          { timeout: 3500 }
        );
        if (data && data.supervisor_url) {
          candidates.push(String(data.supervisor_url).replace(/\/$/, ""));
        }
      } catch (_) {
        /* ignore */
      }
      candidates.push("http://127.0.0.1:18765");
      // de-dupe
      const seen = new Set();
      for (const url of candidates) {
        if (!url || seen.has(url)) continue;
        seen.add(url);
        try {
          const { data } = await axios.get(`${url}/status`, { timeout: 1500 });
          if (data && data.ok) {
            this.macSupervisorUri = (data.base_url || url).replace(/\/$/, "");
            return this.macSupervisorUri;
          }
        } catch (_) {
          /* try next */
        }
      }
      return this.macSupervisorUri;
    },
    async pollMacSyncStatus() {
      const sid = this.sessionId;
      if (!sid) return null;
      try {
        const {
          data
        } = await axios.get(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/session`,
          { params: { session_id: sid }, timeout: 5000 }
        );
        const man = (data && data.manifest) || data || {};
        const sync = man.sync || {};
        const cells = (man.cells && man.cells.length) || sync.mac_cells || 0;
        if (cells > 0) {
          this.macSessionId = sid;
          // Enable Mac LOD as soon as some tiles are synced (scan can still be running)
          if (!this.macArchived) {
            this.macArchived = true;
            await this.fetchTileLayout();
          }
        }
        return { man, sync, cells };
      } catch (_) {
        return null;
      }
    },
    async syncTilesToMacOnly() {
      // Pull tiles to PC workspace without starting refine
      if (!(await this.probeMacOnline())) return null;
      this.macArchiveBusy = true;
      const SYNC_TIMEOUT_S = 25;
      try {
        let syncInfo = await this.pollMacSyncStatus();
        for (let i = 0; i < SYNC_TIMEOUT_S; i++) {
          const mac = (syncInfo && syncInfo.cells) || 0;
          const pi = (syncInfo && syncInfo.sync && syncInfo.sync.pi_cells) || 0;
          if (mac > 0 && (pi <= 0 || mac >= pi)) break;
          this.scanStatus =
            mac > 0
              ? `Synced PC ${mac}/${pi || "?"}`
              : `Syncing to PC… ${i + 1}s`;
          await new Promise(r => setTimeout(r, 1000));
          syncInfo = await this.pollMacSyncStatus();
        }
        if (this.macSessionId) {
          this.macArchived = true;
          await this.fetchTileLayout();
          await this.refreshCanvasStudioSlide();
          this.$nextTick(() => this.scheduleLodRefresh());
        }
        return syncInfo;
      } finally {
        this.macArchiveBusy = false;
      }
    },
    async archiveAndRefineOnMac() {
      this.macArchiveBusy = true;
      const SYNC_TIMEOUT_S = 25;
      const REFINE_TIMEOUT_S = 90;
      try {
        // Re-check Mac is still alive before waiting
        if (!(await this.probeMacOnline())) {
          throw new Error("PC offline");
        }
        let syncInfo = await this.pollMacSyncStatus();
        for (let i = 0; i < SYNC_TIMEOUT_S; i++) {
          const mac = (syncInfo && syncInfo.cells) || 0;
          const pi = (syncInfo && syncInfo.sync && syncInfo.sync.pi_cells) || 0;
          if (mac > 0 && (pi <= 0 || mac >= pi)) break;
          if (i > 0 && i % 5 === 0 && !(await this.probeMacOnline())) {
            throw new Error("PC disconnected during sync");
          }
          this.scanStatus =
            mac > 0
              ? `Syncing… PC ${mac}/${pi || "?"} tiles`
              : `Syncing to PC… ${i + 1}s`;
          await new Promise(r => setTimeout(r, 1000));
          syncInfo = await this.pollMacSyncStatus();
        }
        if (!this.macSessionId) {
          throw new Error("PC sync timed out");
        }
        this.macArchived = true;
        this.scanStatus = "PC Worker is assembling the Slide…";
        try {
          await this.runMacRefine({
            afterArchive: true,
            timeoutSec: REFINE_TIMEOUT_S
          });
        } catch (e) {
          throw new Error((e && e.message) || "PC refine failed/timed out");
        }
        this.scanStatus = "Slide published — Tiles show raw FOVs";
        this.$nextTick(() => this.scheduleLodRefresh());
      } finally {
        this.macArchiveBusy = false;
      }
    },
    async runPiRefineFallback(reason) {
      this.macPathEnabled = false;
      this.macArchived = false;
      this.scanStatus = `PC Worker unavailable — assembling the Slide on the microscope…`;
      if (reason) {
        // keep reason short in status after start
      }
      try {
        await this.runPiRefine();
        this.scanStatus = "Done — Pi refine (PC offline/timeout)";
      } catch (e) {
        this.scanStatus =
          (e && e.message) || "Pi refine failed after PC timeout";
      }
      this.$nextTick(() => this.scheduleLodRefresh());
    },
    async runPiRefine() {
      this.refineBusy = true;
      this.refineProgress = 0;
      this.scanStatus = "Refining on Pi…";
      this.startMosaicPoll();
      try {
        const res = await axios.post(this.refineUri, {});
        const href = res.data && res.data.href;
        this.refineHref = href || null;
        let out = res.data;
        if (href) {
          out = await this.waitRefineAction(href);
          out = (out && out.output) || out;
        }
        this.applyScanOutput(out || {}, null);
        this.slideStale = false;
        if (this.scanMeta) {
          this.scanMeta = { ...this.scanMeta, refined: true };
        }
        this.refineProgress = 100;
        this.setMosaicDisplayMode("slide");
        this.refreshPreview();
        await this.refreshCanvasStudioSlide();
        this.$nextTick(() => this.scheduleLodRefresh());
        return out;
      } finally {
        this.refineBusy = false;
        this.refineHref = null;
        setTimeout(() => {
          if (!this.refineBusy) this.refineProgress = 0;
        }, 800);
      }
    },
    useMacPreview() {
      if (this.slideStale && !this.refineBusy) return;
      if (!this.macMosaicBase) return;
      const sid = this.macSessionId || this.sessionId;
      const revision = this.macPreviewRevision || Date.now();
      this.mosaicUrl = `${
        this.macMosaicBase
      }/preview.jpg?session_id=${encodeURIComponent(
        sid || ""
      )}&t=${encodeURIComponent(revision)}`;
      this.$nextTick(() => this.updateMosaicContentBox());
    },
    async runMacRefine(
      { afterArchive, timeoutSec } = { afterArchive: false, timeoutSec: 1800 }
    ) {
      const sid = this.macSessionId || this.sessionId;
      if (!sid) throw new Error("no PC session");
      const maxIter = Math.max(10, Math.floor((timeoutSec || 1800) * 2));
      this.refineBusy = true;
      this.refineProgress = 1;
      if (!afterArchive) this.scanStatus = "Refining on PC…";
      try {
        // Start refine via Pi→Mac proxy (never hit Mac from browser)
        try {
          await axios.post(
            `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/refine`,
            { align: "full", session_id: sid },
            { params: { session_id: sid }, timeout: 12000 }
          );
        } catch (_) {
          /* worker may already be refining */
        }

        for (let i = 0; i < maxIter; i++) {
          let data = null;
          try {
            const r = await axios.get(
              `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/refine_status`,
              { params: { session_id: sid }, timeout: 8000 }
            );
            data = r.data;
          } catch (e) {
            if (i > 6 && i % 4 === 0 && !(await this.probeMacOnline())) {
              throw new Error("PC disconnected during refine");
            }
            await new Promise(r => setTimeout(r, 500));
            continue;
          }
          if (data && data.refine_progress != null) {
            this.refineProgress = Number(data.refine_progress);
          }
          if (data && data.phase === "done" && data.refined) {
            this.macArchived = true;
            this.macSessionId = sid;
            this.slideStale = false;
            if (this.scanMeta) {
              this.scanMeta = { ...this.scanMeta, refined: true };
            }
            this.previewNeedsRefresh = true;
            this.useMacPreview();
            await this.fetchTileLayout();
            this.setMosaicDisplayMode("slide");
            this.scanStatus = "Slide published — Tiles show raw FOVs";
            await this.refreshCanvasStudioSlide();
            return data;
          }
          if (data && data.phase === "error") {
            throw new Error((data && data.error) || "refine failed");
          }
          if (i === 0 || i % 4 === 0) {
            this.scanStatus = `PC refining… ${Math.round(
              this.refineProgress || 0
            )}%`;
          }
          await new Promise(r => setTimeout(r, 500));
        }
        throw new Error("PC refine timed out");
      } finally {
        this.refineBusy = false;
        this.refineHref = null;
        setTimeout(() => {
          if (!this.refineBusy) this.refineProgress = 0;
        }, 800);
      }
    },
    async runRefine() {
      if (this.refineBusy || this.scanRunning || this.macArchiveBusy) return;
      if (this.macArchived && this.macSessionId) {
        try {
          await this.runMacRefine({ afterArchive: false, timeoutSec: 120 });
          this.$nextTick(() => this.scheduleLodRefresh());
        } catch (e) {
          await this.runPiRefineFallback(
            (e && e.message) || "PC refine failed"
          );
        }
        return;
      }
      if (this.sessionId && (await this.probeMacOnline())) {
        try {
          await this.archiveAndRefineOnMac();
          this.$nextTick(() => this.scheduleLodRefresh());
        } catch (e) {
          await this.runPiRefineFallback(
            (e && e.message) || "PC refine failed"
          );
        }
        return;
      }
      try {
        await this.runPiRefine();
        this.scanStatus = "Refined on Pi";
      } catch (e) {
        this.scanStatus = "Refine failed";
      }
    },
    async waitRefineAction(href) {
      const limit = 3600; // up to ~6 min @ 100ms
      for (let i = 0; i < limit; i++) {
        const r = await axios.get(href);
        const prog = r.data && r.data.progress;
        if (prog != null) this.refineProgress = Number(prog);
        if (r.data.status === "completed") return r.data;
        if (["failed", "cancelled", "error"].includes(r.data.status)) {
          throw new Error(r.data.status);
        }
        // Also mirror extension live_details refine_progress
        if (i % 5 === 0) {
          try {
            const d = (await axios.get(this.liveDetailsUri)).data || {};
            if (d.refine_progress != null) {
              this.refineProgress = Math.max(
                this.refineProgress,
                Number(d.refine_progress)
              );
            }
            if (d.preview_ready && d.stitch_timestamp) {
              this.mosaicUrl = `${this.previewBaseUri}?t=${d.stitch_timestamp}`;
              this.$nextTick(() => this.updateMosaicContentBox());
            }
          } catch (_) {
            /* ignore */
          }
        }
        await new Promise(res => setTimeout(res, 100));
      }
      throw new Error("timeout");
    },
    async discardSession({ saveSlide } = { saveSlide: false }) {
      this.stopMosaicPoll();
      this.resetScanView();
      this.mosaicUrl = null;
      const sid = this.macSessionId || this.sessionId;
      const synced = await this.macSessionHasTiles(sid);
      if (synced) {
        try {
          await axios.post(this.cleanupUri, {
            dataset_id: this.keepDatasetId,
            save_slide: !!saveSlide
          });
        } catch (_) {
          /* ignore */
        }
      }
      this.scanSession = false;
      this.scanPipVisible = false;
      this.viewFocus = "camera";
      this.mosaicUrl = null;
      this.scanMeta = null;
      this.mosaicContentBox = null;
      this.keepDatasetId = null;
      this.sessionId = null;
      this.piCellCount = 0;
      this.macArchived = false;
      this.macSessionId = null;
      this.macArchiveBusy = false;
      this.scanStatus = saveSlide ? "Slide saved" : "";
      this.scanSetupOpen = false;
      this.continueSetupOpen = false;
      this.refineBusy = false;
      this.refineProgress = 0;
      this.sessionGridPreset = "med";
      this.gridPreset = "med";
      if (saveSlide) {
        setTimeout(() => {
          if (this.scanStatus === "Slide saved") this.scanStatus = "";
        }, 2000);
      }
    },
    startMosaicPoll() {
      this.stopMosaicPoll();
      const token = (this._mosaicPollToken || 0) + 1;
      this._mosaicPollToken = token;
      // Use a single self-scheduling request rather than setInterval.  A slow
      // Pi/Mac hop can never overlap its successor; transient 502s back off
      // instead of creating a retry storm while the server is recovering.
      const tick = async () => {
        if (this._mosaicPollToken !== token) return;
        await this.refreshPreview();
        if (this._mosaicPollToken !== token) return;
        const failures = Math.min(4, Number(this.mosaicFailureStreak) || 0);
        const delay = failures ? Math.min(15000, 2500 * 2 ** failures) : 2500;
        this.mosaicPoll = setTimeout(tick, delay);
      };
      this.mosaicPoll = setTimeout(tick, 0);
    },
    stopMosaicPoll() {
      if (this.mosaicPoll) {
        clearTimeout(this.mosaicPoll);
        this.mosaicPoll = null;
      }
      this._mosaicPollToken = (this._mosaicPollToken || 0) + 1;
    },
    async refreshLiveCanvasContext(sid) {
      if (this.canvasStatusBusy || !sid || this.studioSession) return;
      this.canvasStatusBusy = true;
      try {
        const { data } = await axios.post(
          `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`,
          { operation: "canvas", action: "status", session_id: sid },
          { timeout: 6000 }
        );
        const active =
          this.studioSession || this.macSessionId || this.sessionId;
        if (String(active || "") !== String(sid)) return;
        if (data && (data.reference || data.bounds)) {
          this.canvasContext = {
            reference: data.reference,
            bounds: data.bounds,
            revision: data.revision
          };
        }
      } catch (_) {
        // The Pi stage/grid fallback keeps the moving Camera FOV usable until
        // the first Mac tile has been archived and a reference is available.
      } finally {
        this.canvasStatusBusy = false;
      }
    },
    refreshPreview() {
      if (this.mosaicRefreshBusy) return Promise.resolve(false);
      this.mosaicRefreshBusy = true;
      return this.fetchLiveDetails({ maxAge: 750 })
        .then(async d => {
          this.mosaicFailureStreak = 0;
          d = d || {};
          if (d.cell_count != null) {
            this.piCellCount = Number(d.cell_count) || 0;
          }
          if (d.session_id) {
            this.sessionId = d.session_id;
            if (
              !this.scanSession &&
              (this.piCellCount > 0 || d.preview_ready)
            ) {
              this.scanSession = true;
            }
          }
          // Pi tiles gone but session still known → treat as PC-backed
          if (
            this.sessionId &&
            this.piCellCount < 1 &&
            !this.macArchived &&
            !this.scanRunning
          ) {
            const got = await this.ensureMacSession();
            if (got) this.macArchived = true;
          }
          // Prefer Mac preview when archived; still keep Pi cell_count above
          if (this.macArchived && this.macMosaicBase && this.refineBusy) {
            return;
          }
          // Live scan: stay on Tiles progress — update meta + refresh LOD
          if (this.scanRunning) {
            const now = Date.now();
            if (d.origin_xy || d.stride_size || d.grid) {
              this.scanMeta = {
                ...(this.scanMeta || {}),
                stride_size:
                  d.stride_size || (this.scanMeta && this.scanMeta.stride_size),
                px_per_step:
                  d.px_per_step || (this.scanMeta && this.scanMeta.px_per_step),
                nx: Math.max(
                  (d.grid && d.grid[0]) || 0,
                  (this.scanMeta && this.scanMeta.nx) || 0,
                  1
                ),
                ny: Math.max(
                  (d.grid && d.grid[1]) || 0,
                  (this.scanMeta && this.scanMeta.ny) || 0,
                  1
                ),
                min_ix:
                  d.min_ix != null
                    ? d.min_ix
                    : this.scanMeta && this.scanMeta.min_ix,
                min_iy:
                  d.min_iy != null
                    ? d.min_iy
                    : this.scanMeta && this.scanMeta.min_iy,
                axis_sign: d.axis_sign ||
                  (this.scanMeta && this.scanMeta.axis_sign) || [-1, -1],
                origin_xy:
                  d.origin_xy || (this.scanMeta && this.scanMeta.origin_xy),
                initial_xy:
                  d.initial_xy || (this.scanMeta && this.scanMeta.initial_xy),
                last_grid:
                  d.last_grid || (this.scanMeta && this.scanMeta.last_grid),
                session_id: d.session_id || this.sessionId
              };
            }
            if (d.layout && d.layout.tiles && d.layout.tiles.length) {
              // The Pi publishes only the positions of tiles that already
              // exist. Keep this normalized map live so the bounded tile
              // layer can add each newly archived FOV without asking for a
              // full Canvas render.
              this.applyTileLayout(d.layout);
            }
            // Fetch only the image-free Canvas reference while scanning. It
            // lets the live Camera PiP follow the physical FOV without ever
            // requesting the expensive full Canvas render.
            if (
              !this.studioSession &&
              this.sessionId &&
              now - Number(this.lastCanvasStatusAt || 0) >= 1500
            ) {
              this.lastCanvasStatusAt = now;
              this.refreshLiveCanvasContext(this.sessionId);
            }
            // If the Mac worker owns live preview generation, discover its
            // current revision occasionally and display that one bounded
            // image.  This replaces the old per-tile fan-out during Full scan.
            if (
              this.macPathEnabled &&
              Number(d.cell_count || 0) > 0 &&
              now - Number(this.lastMacPreviewProbeAt || 0) >= 1500
            ) {
              this.lastMacPreviewProbeAt = now;
              try {
                const got = await this.ensureMacSession();
                if (
                  got &&
                  this.macMosaicBase &&
                  now - Number(this.lastMacPreviewUsedAt || 0) >= 1000
                ) {
                  this.useMacPreview();
                  this.lastMacPreviewUsedAt = now;
                }
              } catch (_) {
                /* Mac preview is optional during acquisition. */
              }
            }
            // First scan only: seed a hidden slide for later publish
            if (
              !this.slideStale &&
              d.preview_ready &&
              d.stitch_timestamp &&
              (!this.mosaicUrl || !String(this.mosaicUrl).includes("/mac/"))
            ) {
              this.mosaicUrl = `${this.previewBaseUri}?t=${d.stitch_timestamp}`;
              this.$nextTick(() => this.updateMosaicContentBox());
            }
            this.$nextTick(() => this.updateTileCanvasBox());
            this.scheduleLodRefresh();
            return;
          }
          if (this.slideStale && !this.refineBusy) {
            this.scheduleLodRefresh();
            return;
          }
          if (this.macArchived && this.macMosaicBase && !this.scanRunning) {
            if (
              this.previewNeedsRefresh ||
              !this.mosaicUrl ||
              !String(this.mosaicUrl).includes("/mac/preview")
            ) {
              this.useMacPreview();
              this.previewNeedsRefresh = false;
            }
            if (this.mosaicDisplayMode === "tiles") {
              this.scheduleLodRefresh();
            }
            return;
          }
          const sessionOk =
            d.session_id &&
            (!this.sessionId || d.session_id === this.sessionId);
          // Pi tiles gone → probe Mac so zoom/preview still work
          if (
            sessionOk &&
            this.scanPipVisible &&
            (d.cell_count || 0) < 1 &&
            !d.preview_ready
          ) {
            const got = await this.ensureMacSession();
            if (got) return;
          }
          const active =
            this.scanRunning ||
            this.refineBusy ||
            (this.scanPipVisible &&
              this.scanSession &&
              sessionOk &&
              (d.cell_count > 0 || d.preview_ready || this.macArchived));
          if (!active) {
            if (!this.scanRunning && !this.refineBusy && !this.scanSession) {
              this.mosaicUrl = null;
            }
            return;
          }
          // Prefer Pi live preview while available (fast, no Mac hop)
          if (this.slideStale && !this.refineBusy) {
            this.scheduleLodRefresh();
            return;
          }
          if (d.preview_ready && d.stitch_timestamp && !this.macArchived) {
            this.mosaicUrl = `${this.previewBaseUri}?t=${d.stitch_timestamp}`;
            this.scanPipVisible = true;
            this.$nextTick(() => this.updateMosaicContentBox());
          } else if (this.macArchived && this.macMosaicBase) {
            this.useMacPreview();
          }
          if (
            d.layout &&
            d.layout.tiles &&
            d.layout.tiles.length &&
            !this.macArchived
          ) {
            this.applyTileLayout(d.layout);
          }
          if (d.refine_progress != null && this.refineBusy) {
            this.refineProgress = Math.max(
              this.refineProgress,
              Number(d.refine_progress)
            );
          }
          if (d.origin_xy || d.stride_size) {
            this.scanMeta = {
              ...(this.scanMeta || {}),
              stride_size:
                d.stride_size || (this.scanMeta && this.scanMeta.stride_size),
              px_per_step:
                d.px_per_step || (this.scanMeta && this.scanMeta.px_per_step),
              nx: (d.grid && d.grid[0]) || (this.scanMeta && this.scanMeta.nx),
              ny: (d.grid && d.grid[1]) || (this.scanMeta && this.scanMeta.ny),
              min_ix:
                d.min_ix != null
                  ? d.min_ix
                  : this.scanMeta && this.scanMeta.min_ix,
              min_iy:
                d.min_iy != null
                  ? d.min_iy
                  : this.scanMeta && this.scanMeta.min_iy,
              axis_sign: d.axis_sign ||
                (this.scanMeta && this.scanMeta.axis_sign) || [-1, -1],
              origin_xy:
                d.origin_xy || (this.scanMeta && this.scanMeta.origin_xy),
              initial_xy:
                d.initial_xy || (this.scanMeta && this.scanMeta.initial_xy),
              last_grid:
                d.last_grid || (this.scanMeta && this.scanMeta.last_grid),
              refined: d.refined || (this.scanMeta && this.scanMeta.refined),
              session_id: d.session_id || this.sessionId
            };
            if (d.last_grid) {
              this.sessionGridPreset = this.presetFromGrid(d.last_grid);
            }
          }
        })
        .catch(() => {
          this.mosaicFailureStreak = Math.min(
            4,
            (Number(this.mosaicFailureStreak) || 0) + 1
          );
          return false;
        })
        .then(result => {
          this.mosaicRefreshBusy = false;
          return result;
        });
    },
    async onMosaicClick(e) {
      if (this.viewFocus !== "scan" || this.scanRunning) return;
      if (this.agentDrawing || this.suppressTilePeek) return;
      const img = this.$refs.mosaicImg;
      if (!img || !img.naturalWidth) return;
      this.updateMosaicContentBox();
      const box = this.mosaicContentBox;
      if (!box) return;
      const layer = img.parentElement.getBoundingClientRect();
      const left = layer.left + box.left;
      const top = layer.top + box.top;
      const nx = (e.clientX - left) / box.width;
      const ny = (e.clientY - top) / box.height;
      if (nx < 0 || ny < 0 || nx > 1 || ny > 1) return;
      try {
        let r;
        if (this.macArchived && this.macMosaicBase) {
          // Approximate nearest cell from layout for peek
          const keys = Object.keys(this.tileLayoutMap || {});
          let best = null;
          let bestD = 1e9;
          keys.forEach(k => {
            const cell = this.tileLayoutMap[k];
            if (!cell) return;
            const cx = cell.x + cell.w / 2;
            const cy = cell.y + cell.h / 2;
            const d = (cx - nx) * (cx - nx) + (cy - ny) * (cy - ny);
            if (d < bestD) {
              bestD = d;
              best = k.split(",").map(Number);
            }
          });
          if (!best) throw new Error("no cell");
          r = await axios.get(`${this.macMosaicBase}/tile`, {
            params: {
              ix: best[0],
              iy: best[1],
              session_id: this.macSessionId || this.sessionId
            },
            responseType: "blob"
          });
        } else {
          r = await axios.get(this.tileAtBaseUri, {
            params: { nx, ny },
            responseType: "blob"
          });
        }
        if (this.peekUrl) URL.revokeObjectURL(this.peekUrl);
        this.peekUrl = URL.createObjectURL(r.data);
        this.peekLabel = `${nx.toFixed(2)}, ${ny.toFixed(2)}`;
      } catch (err) {
        this.scanStatus = "No tile at that location";
      }
    }
  }
};
</script>

<style scoped>
.canvas-dialog {
  position: absolute;
  inset: 0;
  background: #0005;
  z-index: 150;
  display: grid;
  place-items: center;
}
.canvas-dialog section {
  background: white;
  padding: 26px;
  border-radius: 16px;
  box-sizing: border-box;
  width: min(560px, calc(100vw - 48px));
  max-width: 560px;
  color: #222;
}
.canvas-dialog input {
  width: 64px;
  margin: 8px 12px 8px 4px;
  padding: 8px;
  border: 1px solid #d8d8dd;
  border-radius: 8px;
  background: #fff;
  color: #242426;
}
.canvas-dialog button {
  margin: 0;
  padding: 0 12px;
  min-height: 34px;
  font: inherit;
  line-height: 1.25;
  border: 1px solid #ddd;
  border-radius: 8px;
  background: #fafafa;
  color: #222;
  cursor: pointer;
}
.canvas-dialog button.active {
  color: #145bd7;
  border-color: #9bbcf4;
  background: #edf4ff;
}
.canvas-dialog .grid-row button {
  color: #242426;
  border-color: #d8d8dd;
  background: #f5f5f7;
  opacity: 1;
}
.canvas-dialog .grid-row button:disabled {
  color: #99999f;
  background: #fafafa;
  opacity: 1;
}
.canvas-dialog .grid-row button.active {
  color: #145bd7;
  border-color: #9bbcf4;
  background: #edf4ff;
}
.mhs-tools select {
  max-width: 160px;
  border: 1px solid #ddd;
  border-radius: 7px;
  padding: 7px;
  background: white;
}
.openmhs-root ::v-deep .agent-tool-root {
  top: 64px;
}
.openmhs-root.canvas-open ::v-deep .agent-tool-root {
  z-index: 100;
}
.openmhs-root.canvas-open ::v-deep .codex-status-panel {
  display: none;
}
.openmhs-root .hud-scan-bar .display-mode,
.openmhs-root .hud-scan-bar .scan-hint {
  display: none;
}
.mhs-toolbar {
  position: absolute;
  inset: 0 0 auto;
  height: 56px;
  z-index: 40;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 16px;
  background: #fafafa;
  color: #242426;
  border-bottom: 1px solid #dedee3;
  font: 13px -apple-system, BlinkMacSystemFont, sans-serif;
  box-sizing: border-box;
}
.hardware-lock-hint {
  position: absolute;
  top: 62px;
  left: 50%;
  z-index: 42;
  transform: translateX(-50%);
  padding: 6px 12px;
  border: 1px solid #f0c36a;
  border-radius: 999px;
  background: #fff8e8e8;
  color: #7a4b00;
  font: 12px -apple-system, BlinkMacSystemFont, sans-serif;
  pointer-events: none;
  white-space: nowrap;
}
.mhs-brand {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 132px;
  flex: 0 0 auto;
}
.mhs-brand strong {
  font-size: 17px;
  font-weight: 600;
}
.mhs-brand span {
  font-size: 10px;
  color: #777;
}
.mhs-toolbar nav {
  display: flex;
  background: #ededf0;
  border-radius: 9px;
  padding: 3px;
}
.mhs-toolbar button {
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: inherit;
  padding: 8px 14px;
  cursor: pointer;
  font: inherit;
}
.mhs-toolbar button:hover,
.mhs-toolbar button.active {
  background: #fff;
}
.mhs-toolbar button:disabled {
  opacity: 0.35;
  cursor: default;
}
.mhs-tools {
  position: relative;
  display: flex;
  gap: 8px;
  margin-left: auto;
  align-items: center;
  flex: 0 0 auto;
}
.mhs-tools > button:not(.new-canvas-button) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 76px;
  justify-content: center;
  height: 36px;
  padding: 8px 10px;
}
.mhs-tools .material-icons {
  font-size: 18px;
  flex: 0 0 18px;
  width: 18px;
  height: 18px;
  overflow: hidden;
  text-align: center;
}
.camera-tiles-menu {
  position: absolute;
  top: 48px;
  right: 0;
  z-index: 70;
  width: min(340px, calc(100vw - 100px));
  max-height: min(520px, calc(100vh - 90px));
  overflow: auto;
  padding: 8px;
  border: 1px solid #dedee3;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.98);
  box-shadow: 0 18px 50px #0003;
}
.camera-tiles-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 6px 8px;
}
.camera-tiles-head button {
  padding: 2px 8px;
}
.camera-tile-record {
  width: 100%;
  display: grid !important;
  grid-template-columns: 22px minmax(0, 1fr) auto;
  text-align: left;
}
.camera-tile-record span,
.camera-tile-record small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.camera-tile-record small {
  max-width: 90px;
  color: #85858b;
}
.camera-tiles-import {
  width: 100%;
  margin-top: 6px;
  border-top: 1px solid #ececf0 !important;
}
.camera-tiles-new {
  width: 100%;
  display: flex !important;
  align-items: center;
  gap: 8px;
  text-align: left;
  font-weight: 500;
}
.camera-tiles-new .material-icons {
  flex: 0 0 18px;
  width: 18px;
  overflow: hidden;
}
.camera-tiles-divider {
  padding: 10px 6px 4px;
  color: #85858b;
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.canvas-pip-card {
  position: absolute;
  width: min(220px, 30vw);
  aspect-ratio: 4 / 3;
  z-index: 14;
  overflow: hidden;
  padding: 0;
  border: 1px solid rgba(255, 255, 255, 0.72);
  border-radius: 12px;
  background: #171719;
  color: white;
  box-shadow: 0 10px 32px #0007;
  cursor: grab;
  touch-action: none;
}
.canvas-pip-card:active {
  cursor: grabbing;
}
.canvas-pip-card > img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  pointer-events: none;
}
.canvas-pip-card > span {
  position: absolute;
  left: 8px;
  right: 32px;
  bottom: 7px;
  overflow: hidden;
  color: white;
  text-overflow: ellipsis;
  white-space: nowrap;
  font: 11px -apple-system, BlinkMacSystemFont, sans-serif;
  text-shadow: 0 1px 4px #000;
  pointer-events: none;
}
.canvas-pip-card > button {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 25px;
  height: 25px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: white;
  cursor: pointer;
}
.openmhs-root .openmhs-main-surface {
  top: 56px;
}
.openmhs-root .hud-scan {
  top: 72px !important;
}
.openmhs-root .hud-scan .record-stack {
  display: none;
}
.openmhs-root .hud-scan-bar {
  top: auto !important;
  bottom: 22px;
  left: 50% !important;
  transform: translateX(-50%);
  max-width: 70%;
  border-radius: 10px;
  box-shadow: none;
}
.openmhs-root ::v-deep .codex-status-panel {
  top: auto;
  bottom: 16px;
  right: 16px;
  background: rgba(250, 250, 252, 0.95);
  color: #444;
  border: 1px solid #dedee3;
  border-radius: 9px;
  font: 12px -apple-system, BlinkMacSystemFont, sans-serif;
}
.openmhs-root ::v-deep .codex-status-panel > button {
  display: none;
}
.openmhs-root ::v-deep .roi-panel {
  background: #fafafa;
  color: #242426;
  border: 1px solid #dedee3;
  border-radius: 12px;
  box-shadow: 0 8px 25px #0002;
}
.openmhs-root ::v-deep .roi-panel button {
  color: #333;
  border-color: #dedee3;
  background: #f0f0f3;
}
.openmhs-root ::v-deep .roi-panel small {
  color: #666;
}
.openmhs-root ::v-deep .vp-guidance {
  font-size: 14px;
  font-weight: 600;
}
@media (max-width: 700px) {
  .mhs-toolbar {
    gap: 8px;
    padding: 0 10px;
  }
  .mhs-brand {
    min-width: 75px;
  }
  .mhs-brand span {
    display: none;
  }
  .mhs-toolbar button {
    padding: 8px;
  }
  .mhs-tools {
    gap: 0;
  }
}
.openmhs-root {
  position: relative;
  width: 100%;
  height: 100%;
  background: #0a0a0a;
  overflow: hidden;
  --openmhs-agent-sidebar-w: 400px;
}
.openmhs-root.pc-drop-over::after {
  content: "Drop mosaic folder to load";
  position: absolute;
  inset: 12px;
  z-index: 40;
  border: 2px dashed rgba(140, 190, 255, 0.7);
  border-radius: 12px;
  background: rgba(20, 30, 50, 0.35);
  color: #dce8ff;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  font-size: 14px;
}
.openmhs-root.agent-sidebar-open .openmhs-main-surface {
  /* Shrink FOV so Agent sidebar does not cover the image */
  right: var(--openmhs-agent-sidebar-w);
}
.openmhs-root.agent-sidebar-open .hud-controls.expanded {
  max-width: min(300px, calc(100vw - var(--openmhs-agent-sidebar-w) - 28px));
}
.openmhs-root.locked .hud-controls {
  /* Still show XYZ readout while mosaic inspect locks XY */
  opacity: 1;
}
.openmhs-main-surface {
  position: absolute;
  inset: 0;
  touch-action: none;
}
.camera-layer,
.mosaic-layer {
  position: absolute;
  overflow: hidden;
  background: #000;
}
.camera-layer.main,
.mosaic-layer.main {
  inset: 0;
  z-index: 1;
}
.camera-layer.pip,
.mosaic-layer.pip {
  width: min(200px, 26vw);
  height: min(150px, 20vh);
  z-index: 5;
  border: 1px solid rgba(255, 255, 255, 0.28);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  cursor: pointer;
}
.top-right {
  top: 12px;
  right: 12px;
}
.pip-label {
  position: absolute;
  left: 6px;
  bottom: 4px;
  font-size: 10px;
  color: #fff;
  background: rgba(0, 0, 0, 0.45);
  padding: 1px 6px;
  border-radius: 4px;
  pointer-events: none;
}
.pip-close {
  position: absolute;
  top: 2px;
  right: 2px;
  z-index: 2;
  border: none;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  border-radius: 50%;
  width: 22px;
  height: 22px;
  padding: 0;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.pip-close .material-icons {
  font-size: 14px;
}
.camera-layer >>> .stream-display,
.camera-layer >>> .uk-height-1-1 {
  height: 100% !important;
  width: 100% !important;
  pointer-events: none;
}
.mosaic-layer.zoomable {
  cursor: grab;
}
.mosaic-layer.zoomable:active {
  cursor: grabbing;
}
.mosaic-zoom {
  position: absolute;
  inset: 0;
  will-change: transform;
}
.mosaic-img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
  pointer-events: none;
  user-select: none;
  -webkit-user-drag: none;
}
.mosaic-img.mode-hidden {
  opacity: 0;
}
.pyramid-layer {
  position: absolute;
  overflow: hidden;
  pointer-events: none;
  z-index: 1;
}
.pyramid-tile {
  position: absolute;
  object-fit: fill;
  image-rendering: auto;
  display: block;
  user-select: none;
  -webkit-user-drag: none;
}
.lod-layer {
  position: absolute;
  overflow: hidden;
  pointer-events: none;
  z-index: 1;
}
.lod-tile {
  position: absolute;
  object-fit: fill;
  image-rendering: auto;
  opacity: 0;
  animation: lod-in 0.18s ease forwards;
}
@keyframes lod-in {
  to {
    opacity: 1;
  }
}
.display-mode {
  display: inline-flex;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 8px;
  overflow: hidden;
  flex-shrink: 0;
}
.display-mode button {
  border: none;
  background: transparent;
  color: rgba(255, 255, 255, 0.7);
  font-size: 11px;
  padding: 4px 8px;
  cursor: pointer;
  line-height: 1.2;
}
.display-mode button.active {
  background: rgba(108, 182, 255, 0.28);
  color: #fff;
  font-weight: 600;
}
.display-mode button.stale:not(.active) {
  color: rgba(255, 204, 102, 0.9);
}
.display-mode button:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.display-mode button + button {
  border-left: 1px solid rgba(255, 255, 255, 0.15);
}
.zoom-badge {
  position: absolute;
  left: 10px;
  bottom: 10px;
  z-index: 3;
  font-size: 11px;
  color: #fff;
  background: rgba(0, 0, 0, 0.5);
  padding: 2px 8px;
  border-radius: 999px;
  pointer-events: none;
}
.marker-toggle {
  position: absolute;
  right: 10px;
  bottom: 10px;
  z-index: 4;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.55);
  color: #e8eef4;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  padding: 0;
}
.marker-toggle .material-icons {
  font-size: 20px;
}
.marker-toggle:hover {
  background: rgba(40, 60, 90, 0.85);
}
.scan-hint {
  font-size: 11px;
  opacity: 0.55;
  white-space: nowrap;
}
.mosaic-placeholder {
  color: rgba(255, 255, 255, 0.55);
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 12px;
  text-align: center;
}
.stage-marker {
  position: absolute;
  border: 2px solid rgba(108, 182, 255, 0.85);
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.4);
  pointer-events: none;
  box-sizing: border-box;
  z-index: 2;
}
.stage-marker.highlight {
  border-width: 3px;
  border-color: #ffcc66;
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.5), 0 0 12px rgba(255, 204, 102, 0.45);
}
.refine-progress {
  position: absolute;
  left: 12px;
  right: 12px;
  top: 10px;
  z-index: 8;
  height: 22px;
  background: rgba(0, 0, 0, 0.55);
  border-radius: 8px;
  overflow: hidden;
  pointer-events: none;
}
.refine-progress-bar {
  height: 100%;
  background: linear-gradient(90deg, #3d8bfd, #6cb6ff);
  transition: width 0.2s ease;
  min-width: 2%;
}
.refine-progress-label {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: #fff;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.primary-mini {
  background: #3d8bfd;
  border: none;
  color: #fff;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.continue-grids {
  max-width: 260px;
}
.custom-grid {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
}
.custom-grid label {
  display: flex;
  align-items: center;
  gap: 4px;
}
.custom-grid input {
  width: 48px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.2);
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
  padding: 4px 6px;
}

.hud-float {
  position: absolute;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: grab;
  user-select: none;
  touch-action: none;
}
.hud-float:active {
  cursor: grabbing;
}
.hud-scan {
  left: 12px;
  top: 12px;
  bottom: auto;
  right: auto;
  transform: none;
}
.hud-scan-bar {
  left: 12px;
  top: 12px;
  bottom: auto;
  right: auto;
  transform: none;
  background: rgba(12, 12, 12, 0.78);
  padding: 6px 10px;
  border-radius: 14px;
  color: #fff;
  flex-wrap: wrap;
  max-width: min(
    420px,
    calc(100vw - var(--openmhs-agent-sidebar-w, 400px) - 28px)
  );
  justify-content: flex-start;
  align-items: flex-start;
}
.hud-controls {
  left: auto;
  bottom: auto;
  right: auto;
  top: auto;
  position: relative;
  background: rgba(12, 12, 12, 0.78);
  padding: 8px;
  border-radius: 14px;
  color: #fff;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  flex-wrap: wrap;
  width: auto;
  max-width: 300px;
  gap: 6px;
  align-items: center;
}
.hud-controls.docked.expanded {
  position: relative;
  left: auto;
  bottom: auto;
  width: max-content;
  max-width: 220px;
  flex: 0 0 auto;
}
.stage-roi-dock {
  position: absolute;
  z-index: 12;
  left: 12px;
  bottom: 18px;
  display: inline-flex;
  flex-direction: row;
  align-items: flex-end;
  gap: 8px;
  width: max-content;
  max-width: none;
  pointer-events: none;
}
.stage-roi-dock > * {
  pointer-events: auto;
}
.dock-pill {
  display: inline-flex;
  flex-direction: row;
  align-items: center;
  gap: 2px;
  padding: 4px;
  border-radius: 999px;
  background: rgba(12, 12, 12, 0.82);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  width: max-content;
  flex: 0 0 auto;
}
.dock-pill .dock-fab {
  background: transparent;
  box-shadow: none;
  padding: 6px 10px;
}
.dock-pill .dock-fab:hover {
  background: rgba(255, 255, 255, 0.1);
}
.dock-fab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: none;
  border-radius: 999px;
  padding: 8px 12px;
  background: rgba(12, 12, 12, 0.82);
  color: #f5f5f5;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  width: auto;
  flex: 0 0 auto;
  white-space: nowrap;
}
.dock-fab .material-icons {
  font-size: 18px;
}
.dock-fab-count {
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
.hud-controls:not(.expanded):not(.docked) {
  padding: 0 !important;
  margin: 0;
  background: transparent !important;
  box-shadow: none !important;
  max-width: none !important;
  width: auto !important;
  min-width: 0 !important;
  min-height: 0 !important;
  gap: 0 !important;
  border-radius: 0;
  display: inline-flex;
  flex-wrap: nowrap;
}
.hud-controls.expanded {
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}
.stage-panel-head {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 2px;
}
.stage-panel-title {
  flex: 1;
  font-size: 11px;
  font-weight: 600;
  opacity: 0.8;
  letter-spacing: 0.02em;
}
.stage-fab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: none;
  border-radius: 999px;
  padding: 8px 12px;
  background: rgba(12, 12, 12, 0.82);
  color: #f5f5f5;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  width: auto;
  height: auto;
  margin: 0;
}
.stage-fab .material-icons {
  font-size: 18px;
}
.stage-collapse {
  border: none;
  background: transparent;
  color: inherit;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  opacity: 0.7;
}
.stage-collapse:hover {
  background: rgba(255, 255, 255, 0.1);
  opacity: 1;
}
.axis-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 6px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.08);
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
.axis-name {
  font-weight: 700;
  opacity: 0.85;
  min-width: 10px;
}
.axis-pos,
.axis-pos-input {
  min-width: 64px;
  width: 72px;
  text-align: right;
}
.axis-pos-input {
  border: 1px solid transparent;
  background: rgba(0, 0, 0, 0.25);
  color: inherit;
  border-radius: 6px;
  padding: 2px 4px;
  font: inherit;
  -moz-appearance: textfield;
}
.axis-pos-input::-webkit-outer-spin-button,
.axis-pos-input::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}
.axis-pos-input:focus {
  outline: none;
  border-color: rgba(108, 182, 255, 0.55);
  background: rgba(0, 0, 0, 0.4);
}
.axis-sep {
  opacity: 0.35;
  margin: 0 2px;
}
.xyz-readout {
  display: none;
}
.z-jog {
  display: inline-flex;
  gap: 4px;
}
.scale-calib-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: none;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.1);
  color: inherit;
  padding: 5px 8px;
  cursor: pointer;
  font-size: 11px;
}
.scale-calib-btn .material-icons {
  font-size: 15px;
}
.scale-calib-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.scale-status {
  font-size: 10px;
  opacity: 0.75;
  line-height: 1.3;
}
.z-btn {
  border: none;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
  font-size: 14px;
  font-weight: 700;
  padding: 4px 10px;
  cursor: pointer;
  min-width: 36px;
}
.z-btn:disabled {
  opacity: 0.35;
  cursor: default;
}
.z-btn:not(:disabled):hover {
  background: rgba(255, 255, 255, 0.2);
}
.stage-move-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  width: 100%;
  border: none;
  border-radius: 8px;
  background: #3d8bfd;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  padding: 5px 8px;
  cursor: pointer;
}
.stage-move-btn .material-icons {
  font-size: 15px;
}
.stage-move-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
.step-preset {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 118px;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 8px;
  background: #242424;
  color: #fff;
  font-size: 12px;
  padding: 5px 7px;
  cursor: pointer;
  color-scheme: dark;
}
.step-preset .material-icons {
  font-size: 16px;
  opacity: 0.78;
}
.step-preset:disabled {
  opacity: 0.45;
  cursor: default;
}
.step-preset-wrap {
  position: relative;
  display: inline-flex;
}
.step-preset-menu {
  position: absolute;
  z-index: 50;
  left: 0;
  bottom: calc(100% + 6px);
  min-width: 160px;
  padding: 4px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 10px;
  background: #242424;
  color: #fff;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
}
.step-preset-menu button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: #fff;
  padding: 7px 8px;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.step-preset-menu button:hover,
.step-preset-menu button.active {
  background: #3d8bfd;
}
.step-preset-menu small {
  color: rgba(255, 255, 255, 0.64);
  font-size: 10px;
}
.step-preset-menu button.active small,
.step-preset-menu button:hover small {
  color: rgba(255, 255, 255, 0.88);
}
.hud-controls .sens {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  opacity: 0.95;
}
.hud-grip {
  display: flex;
  opacity: 0.55;
}
.hud-grip .material-icons {
  font-size: 18px;
}
.scan-entry {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  border: none;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.92);
  color: #111;
  font-weight: 600;
  font-size: 13px;
  cursor: pointer;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.35);
}
.scan-entry .material-icons {
  font-size: 18px;
}
.record-stack {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.view-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: none;
  border-radius: 999px;
  background: rgba(12, 12, 12, 0.78);
  color: #f5f5f5;
  font-size: 12px;
  cursor: pointer;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.3);
  max-width: min(280px, calc(100vw - 40px));
}
.view-chip .material-icons {
  font-size: 16px;
}
.view-chip span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 160px;
}
.view-chip em {
  font-style: normal;
  opacity: 0.7;
  font-size: 11px;
  margin-left: 2px;
}
.kind-row {
  display: flex;
  gap: 6px;
}
.kind-row button {
  flex: 1;
  border: 1px solid rgba(255, 255, 255, 0.16);
  background: transparent;
  color: inherit;
  border-radius: 8px;
  padding: 6px 8px;
  cursor: pointer;
  font-size: 12px;
}
.kind-row button.active {
  background: rgba(255, 255, 255, 0.16);
}
.video-hint {
  margin: 0;
  font-size: 12px;
  opacity: 0.8;
  line-height: 1.4;
}
.video-record-banner {
  position: absolute;
  left: 12px;
  bottom: 18px;
  z-index: 8;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 12px;
  background: rgba(12, 12, 12, 0.82);
  color: #fff;
  font-size: 12px;
  max-width: min(520px, calc(100vw - 40px));
  pointer-events: auto;
}
.video-record-banner button {
  border: none;
  background: rgba(255, 255, 255, 0.16);
  color: inherit;
  border-radius: 8px;
  padding: 4px 8px;
  cursor: pointer;
}
.scan-setup {
  background: rgba(12, 12, 12, 0.9);
  color: #fff;
  padding: 10px 12px;
  border-radius: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 220px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.4);
}
.grid-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.grid-row button {
  border: 1px solid rgba(255, 255, 255, 0.15);
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
  border-radius: 8px;
  min-height: 34px;
  padding: 0 10px;
  font: inherit;
  font-size: 12px;
  line-height: 1.25;
  cursor: pointer;
}
.grid-row button.active {
  border-color: #6cb6ff;
  background: rgba(108, 182, 255, 0.22);
}
.grid-row button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.setup-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
  align-items: center;
  flex-wrap: wrap;
}
.setup-actions button {
  min-height: 34px;
  border-radius: 8px;
  padding: 0 12px;
  line-height: 1.25;
}
.setup-actions .primary {
  background: #3d8bfd;
  border: none;
  color: #fff;
  border-radius: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.scan-label {
  font-size: 11px;
  opacity: 0.9;
  max-width: 160px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ghost {
  background: rgba(255, 255, 255, 0.12);
  border: none;
  color: #fff;
  border-radius: 999px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 11px;
}
.ghost:disabled {
  opacity: 0.45;
}
.sens {
  font-size: 11px;
  display: flex;
  gap: 4px;
  align-items: center;
}
.sens select,
.step-num {
  background: rgba(255, 255, 255, 0.1);
  border: 1px solid rgba(255, 255, 255, 0.15);
  color: #fff;
  border-radius: 6px;
  padding: 3px 5px;
  width: 64px;
  font-size: 12px;
}
.step-num {
  width: 56px;
}
.af-btn {
  display: flex;
  align-items: center;
  gap: 2px;
  border: none;
  border-radius: 8px;
  padding: 5px 8px;
  background: #3d8bfd;
  color: #fff;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.af-btn .material-icons {
  font-size: 16px;
}
.af-btn:disabled {
  opacity: 0.45;
}
.af-status {
  font-size: 11px;
  opacity: 0.85;
  min-width: 48px;
}
.peek-modal {
  position: absolute;
  inset: 0;
  z-index: 30;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.peek-card {
  background: #111;
  border-radius: 12px;
  max-width: min(900px, 92vw);
  max-height: 90vh;
  overflow: auto;
  color: #fff;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
}
.peek-card header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.peek-card header button {
  background: transparent;
  border: none;
  color: inherit;
  cursor: pointer;
}
.peek-card img {
  display: block;
  max-width: 100%;
  height: auto;
}
.locked .openmhs-main-surface.interactive {
  cursor: default;
}
</style>
