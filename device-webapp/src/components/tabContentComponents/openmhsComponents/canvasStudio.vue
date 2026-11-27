<template>
  <section
    class="studio"
    :class="{ readonly: locked }"
    tabindex="-1"
    @keydown.stop="key"
    @keyup.stop
    @pointerdown.stop
    @wheel.stop
    @click.stop
    @dragover.prevent.stop
    @drop.prevent.stop
  >
    <header>
      <div class="identity">
        <span class="eyebrow">MicroWorld</span><strong>Canvas</strong>
      </div>
      <nav aria-label="Canvas view">
        <button
          :disabled="locked"
          :class="{ active: mode === 'slide' }"
          @click="setMode('slide')"
        >
          Slide</button
        ><button
          :disabled="locked"
          :class="{ active: mode === 'tiles' }"
          @click="setMode('tiles')"
        >
          Tiles
        </button>
      </nav>
      <fieldset class="actions" :disabled="locked || busy || !!activeFovJob">
        <button
          class="icon-action"
          title="Continue canvas"
          aria-label="Continue canvas"
          @click="$emit('continue')"
        >
          <i class="material-icons">add_to_photos</i>
        </button>
        <button
          :disabled="busy || vpBusy"
          :class="{ active: marking }"
          @click="markRoi"
        >
          Mark ROI
        </button>
        <button
          :disabled="busy || !undoStack.length"
          title="Undo"
          @click="undo"
        >
          ↶
        </button>
        <button
          :disabled="busy || !redoStack.length"
          title="Redo"
          @click="redo"
        >
          ↷
        </button>
        <button :disabled="busy || !doc" @click="$refs.file.click()">
          ＋ Add tiles
        </button>
        <button :class="{ active: inspector }" @click="inspector = !inspector">
          Adjust
        </button>
      </fieldset>
      <button
        class="icon-action"
        :title="showFov ? 'Hide live FOV' : 'Show live FOV'"
        :aria-label="showFov ? 'Hide live FOV' : 'Show live FOV'"
        @click="$emit('toggle-fov')"
      >
        <i class="material-icons">{{
          showFov ? "visibility" : "visibility_off"
        }}</i>
      </button>
      <button
        class="icon-action"
        title="Camera"
        aria-label="Camera"
        @click="close"
      >
        <i class="material-icons">arrow_back</i>
      </button>
      <button
        class="icon-action"
        title="Close canvas"
        aria-label="Close canvas"
        :disabled="busy || locked || !!activeFovJob"
        @click="$emit('dismiss')"
      >
        ×
      </button>
      <input
        ref="file"
        type="file"
        accept="image/*"
        hidden
        @change="importImage"
      />
    </header>
    <div class="body">
      <div
        ref="viewport"
        class="viewport"
        @pointerdown="down"
        @pointermove="move"
        @pointerup="up"
        @pointercancel="up"
        @wheel.prevent="wheel"
      >
        <div
          class="world"
          :style="{
            transform: `translate(${view.x}px,${view.y}px) scale(${view.scale})`
          }"
        >
          <template v-if="doc">
            <div
              v-if="showFov && stageBox"
              class="stage-fov"
              :style="box(pendingTile || stageBox)"
            >
              <streamDisplay class="fov-live" />
              <div
                class="fov-tools"
                :style="{ transform: `scale(${1 / view.scale})` }"
              >
                <button
                  class="fov-capture"
                  :disabled="busy || locked || !!activeFovJob"
                  title="Capture tile"
                  aria-label="Capture tile"
                  @pointerdown.stop
                  @click.stop="captureAtFov"
                >
                  {{ activeFovJob ? "…" : "+" }}
                </button>
              </div>
            </div>
            <div
              v-if="selectionRect"
              class="vp-selection"
              :style="box(selectionRect)"
            />
            <div v-if="vpRect" class="vp-selection" :style="box(vpRect)" />
            <img
              v-if="mode === 'slide' && slide"
              :src="slide"
              class="slide"
              :style="box(bounds)"
              draggable="false"
              alt="Merged canvas"
              @click.stop="slideToTile"
            />
            <template v-else>
              <div
                v-for="t in visible"
                :key="t.id"
                class="tile"
                :class="{ selected: selectedIds.includes(t.id) }"
                :style="{
                  ...box(t),
                  opacity: t.opacity == null ? 1 : t.opacity
                }"
                :data-tile="t.id"
              >
                <img
                  v-if="t.url"
                  :src="t.url"
                  draggable="false"
                  alt="Adjusted tile (original preserved)"
                />
                <div v-else class="tile-placeholder">Tile available</div>
                <span
                  >{{
                    t.asset
                      ? t.stage_position
                        ? "Captured"
                        : "Imported"
                      : `${t.ix}, ${t.iy}`
                  }}{{ t.manual ? " · Manual" : "" }}</span
                >
              </div>
            </template>
          </template>
        </div>
        <div v-if="!doc" class="empty">{{ error || "Opening canvas…" }}</div>
        <div v-if="locked" class="scan-lock">
          {{ scanStatus || "Scanning — edits locked" }}
        </div>
        <div class="view-controls" @pointerdown.stop>
          <button @click="zoom(0.8)">−</button
          ><span>{{ Math.round(view.scale * 100) }}%</span
          ><button @click="zoom(1.25)">＋</button
          ><button @click="fit">Fit</button>
        </div>
        <div class="hint">
          {{
            mode === "tiles"
              ? "Drag a tile to align · Drag background to pan"
              : "Click a region to open its tile · Scroll to zoom"
          }}
        </div>
      </div>
      <aside v-if="inspector && doc">
        <h3>Layers · {{ doc.tiles.length }}</h3>
        <p class="caption">Drag layers to reorder</p>
        <div class="layer-list">
          <div
            v-for="t in [...doc.tiles].reverse()"
            :key="t.id"
            class="layer-row"
            :class="{ active: selectedIds.includes(t.id) }"
            :draggable="!locked"
            @dragstart="startLayerDrag(t, $event)"
            @dragover.prevent
            @drop.prevent="dropLayer(t)"
            @click="!locked && selectTile(t, $event)"
          >
            <button
              class="layer-check"
              :disabled="locked"
              :title="t.hidden ? 'Show tile' : 'Hide tile'"
              @click.stop="toggleLayer(t)"
            >
              <i class="material-icons">{{
                t.hidden ? "check_box_outline_blank" : "check_box"
              }}</i>
            </button>
            <span class="layer-label">{{ tileLabel(t) }}</span>
            <button
              class="layer-delete"
              :disabled="locked"
              title="Delete tile"
              @click.stop="prepareDelete(t)"
            >
              <i class="material-icons">delete_outline</i>
            </button>
          </div>
        </div>
        <fieldset :disabled="locked || busy">
          <template v-if="selectedIds.length">
            <label
              >Opacity
              <output
                >{{
                  Math.round(
                    (chosen.opacity == null ? 1 : chosen.opacity) * 100
                  )
                }}%</output
              >
              <input
                type="range"
                min="0"
                max="1"
                step="0.01"
                :value="chosen.opacity == null ? 1 : chosen.opacity"
                @pointerdown="checkpoint"
                @change="setOpacity($event.target.value)"
              />
            </label>
          </template>
          <div class="section-head">
            <h3>
              {{
                selectedIds.length
                  ? `Selected · ${selectedIds.length}`
                  : "All tiles"
              }}
            </h3>
            <button :disabled="busy" @click="resetAdjustments">Reset</button>
          </div>
          <p class="caption">
            Non-destructive adjustments. Source images stay unchanged.
            <span v-if="adjustmentPending" class="pending">Updating…</span>
          </p>
          <label v-for="s in sliders" :key="s.key"
            >{{ s.label
            }}<output>{{ Number(activeAdjustments[s.key]).toFixed(2) }}</output
            ><input
              :value="activeAdjustments[s.key]"
              :disabled="busy"
              type="range"
              :min="s.min"
              :max="s.max"
              :step="s.step"
              @pointerdown="checkpoint"
              @keydown="sliderKey"
              @input="changeAdjustment(s.key, Number($event.target.value))"
          /></label>
          <label
            >Color map<select
              :value="activeAdjustments.colormap"
              :disabled="busy"
              @focus="checkpoint"
              @change="changeAdjustment('colormap', $event.target.value)"
              ><option value="natural">Original color</option
              ><option value="gray">Grayscale</option
              ><option value="viridis">Viridis · false color</option
              ><option value="magma">Magma · false color</option></select
            ></label
          >
          <button
            class="enhance"
            :class="{ active: activeAdjustments.enhance }"
            :disabled="busy"
            @click="toggleEnhance"
          >
            {{ activeAdjustments.enhance ? "✓ Enhanced" : "Enhance" }}
          </button>
          <p class="caption">
            Edge-preserving denoise + gentle sharpening. No generated detail.
            Slide and Tiles show the same adjustments. Source files stay
            unchanged.
          </p>
          <template v-if="chosen">
            <hr />
            <h3>Selected tile</h3>
            <p class="caption">
              {{
                chosen.asset && !chosen.stage_position
                  ? "Imported · no stage coordinates"
                  : chosen.asset
                  ? "Captured tile · stage coordinates saved"
                  : `Source ${chosen.ix}, ${chosen.iy}`
              }}
            </p>
            <button :disabled="busy" @click="restorePosition">
              Restore automatic position
            </button>
            <button
              :disabled="busy || (chosen.asset && !chosen.stage_position)"
              @click="retakeSelected"
            >
              Retake region…
            </button>
          </template>
        </fieldset>
      </aside>
    </div>
    <footer>
      <button v-if="activeFovJob && !busy" @click="checkFovJob">
        Check move / capture status
      </button>
      <span :class="{ error: !!error }">{{
        error ||
          (busy
            ? "Saving and blending…"
            : vpStatus ||
              (marking
                ? "Drag a region, then ask Codex in the conversation"
                : "All changes saved"))
      }}</span
      ><button v-if="error && doc" :disabled="busy" @click="save">
        Retry save</button
      ><span
        >{{ visible.length }} tiles · Layout edits never move the stage</span
      >
    </footer>
    <div v-if="deleteDialog" class="delete-dialog" @pointerdown.stop>
      <div>
        <h3>Delete {{ selectedIds.length }} tile(s)?</h3>
        <p>
          Remove from this Canvas only, or also move the current image files
          into the disk recovery trash? Originals in recovery remain
          recoverable; this is not secure erasure.
        </p>
        <button @click="deleteDialog = false">Cancel</button
        ><button :disabled="busy || locked" @click="deleteTiles('keep')">
          Remove from Canvas</button
        ><button :disabled="busy || locked" @click="deleteTiles('trash')">
          Also move files to trash
        </button>
      </div>
    </div>
  </section>
</template>

<script>
import axios from "axios";
import streamDisplay from "../streamContent.vue";
const defaults = () => ({
  exposure: 0,
  contrast: 1,
  gamma: 1,
  colormap: "natural",
  enhance: false
});
const LIVE_TILE_LIMIT = 24;
export default {
  components: { streamDisplay },
  props: {
    session: { type: String, required: true },
    baseUri: { type: String, required: true },
    initialMode: { type: String, default: "slide" },
    initialAdjust: { type: Boolean, default: false },
    scanning: { type: Boolean, default: false },
    scanStatus: { type: String, default: "" },
    stagePosition: { type: Object, default: () => ({}) },
    showFov: { type: Boolean, default: true },
    roiDrawing: { type: Boolean, default: false }
  },
  data() {
    return {
      doc: null,
      mode: "slide",
      inspector: false,
      busy: false,
      error: "",
      selected: null,
      selectedIds: [],
      layerDragId: null,
      selectBox: false,
      selectionRect: null,
      deleteDialog: false,
      slide: "",
      bounds: { x: 0, y: 0, w: 1, h: 1 },
      view: { x: 0, y: 0, scale: 1 },
      drag: null,
      pendingTile: null,
      sampleConfirmed: false,
      activeFovJob: null,
      fovIntent: "",
      marking: false,
      vpRect: null,
      vpBusy: false,
      vpStatus: "",
      slideStale: false,
      adjustmentPending: false,
      deferredSaveTimer: null,
      deferredSaveQueued: false,
      deferredSaveRunning: false,
      deferredSavePromise: null,
      deferredPreviewIds: [],
      undoStack: [],
      redoStack: [],
      sliders: [
        {
          key: "exposure",
          label: "Exposure compensation",
          min: -3,
          max: 3,
          step: 0.05
        },
        { key: "contrast", label: "Contrast", min: 0.25, max: 3, step: 0.05 },
        { key: "gamma", label: "Gamma", min: 0.25, max: 3, step: 0.05 }
      ]
    };
  },
  computed: {
    locked() {
      return this.scanning || !!(this.doc && this.doc.locked);
    },
    activeAdjustments() {
      return (this.chosen && this.chosen.adjustments) || this.doc.adjustments;
    },
    stageBox() {
      const r = this.doc && this.doc.reference,
        p = this.stagePosition;
      if (!r || p.x == null) return null;
      const x = p.x - r.stage_origin[0],
        y = p.y - r.stage_origin[1],
        m = r.stage_to_world;
      return {
        x: r.world_origin[0] + x * m[0][0] + y * m[1][0],
        y: r.world_origin[1] + x * m[0][1] + y * m[1][1],
        w: r.fov[0],
        h: r.fov[1]
      };
    },
    visible() {
      return this.doc ? this.doc.tiles.filter(t => !t.hidden) : [];
    },
    hidden() {
      return this.doc ? this.doc.tiles.filter(t => t.hidden) : [];
    },
    chosen() {
      return (
        this.doc && this.doc.tiles.find(t => this.selectedIds.includes(t.id))
      );
    }
  },
  watch: {
    scanning(v) {
      if (v) {
        // A new scan/Continue invalidates the previously published Slide.
        // The next refined status must trigger one fresh full Canvas load.
        this._finalSlideLoaded = false;
        this._finalSlideLoadInFlight = false;
        this._awaitingFinalSlide = true;
        this.mode = "tiles";
        this.pendingTile = null;
        this.drag = null;
        this.selectedIds = [];
      }
      this.refreshScan();
    }
  },
  async mounted() {
    this.$el.focus();
    this._apiFlights = Object.create(null);
    this._fullTileIds = new Set();
    this._finalSlideLoaded = false;
    this._finalSlideLoadInFlight = false;
    this._awaitingFinalSlide = this.scanning;
    this._resizeObserver = new ResizeObserver(() => {
      if (!this.drag) this.fit();
    });
    this._resizeObserver.observe(this.$refs.viewport);
    this.mode = this.initialMode;
    this.inspector = true;
    try {
      // A scan is a live tile stream, not a finished Slide. Loading the full
      // document here used to decode every tile while the Mac worker was
      // still ingesting them, which made the Pi proxy return 502. Start with
      // image-free status and let refreshScan add raw tile URLs incrementally.
      const live = this.scanning;
      const loaded = await this.api({ action: live ? "status" : "load" });
      // Keep the fused Slide in the response, but let tile images arrive as
      // independent cached thumbnails. This makes the Slide visible before
      // the raw tile set has finished downloading.
      this.doc = this.withLiveTileUrls(loaded, !live && !loaded.deferred_load);
      this.slide = this.doc.slide || "";
      this.bounds = this.doc.bounds;
      this.slideStale = false;
      // A normal reopen already received the complete Slide. During a live
      // scan, status is deliberately image-free and the final load is still
      // pending until the Mac publishes its refined result.
      this._finalSlideLoaded = !live && !loaded.deferred_load && !!this.slide;
      this._awaitingFinalSlide = live || (loaded.deferred_load && !loaded.refined);
      if (live || loaded.deferred_load) this.mode = "tiles";
      await this.$nextTick();
      this.fit();
      this.$emit("context", {
        reference: this.doc.reference,
        bounds: this.bounds,
        revision: this.doc.revision
      });
    } catch (e) {
      this.error = this.scanning ? "Waiting for the PC Worker to receive scan tiles…" : this.message(e);
    }
    this._refresh = setInterval(() => {
      if (
        this.scanning ||
        this._awaitingFinalSlide ||
        !this.doc ||
        this.doc.locked ||
        (!this._finalSlideLoaded && this.doc.refined === true) ||
        (this.doc.deferred_load && this.doc.refined !== true) ||
        this.doc.refine_phase === "refining"
      )
        this.refreshScan();
    }, 1500);
  },
  beforeDestroy() {
    clearInterval(this._refresh);
    if (this.deferredSaveTimer) clearTimeout(this.deferredSaveTimer);
    if (this._resizeObserver) this._resizeObserver.disconnect();
  },
  methods: {
    withLiveTileUrls(d, preserveSlide = false) {
      const previous = this.doc
        ? new Map(this.doc.tiles.map(t => [t.id, t]))
        : new Map();
      const tiles = d.tiles || [];
      // Keep metadata for every tile, but only decode a bounded working set
      // during acquisition. Newest fields and selected tiles remain visible;
      // the completed Canvas load restores every tile URL.
      const keep = new Set(
        tiles.slice(Math.max(0, tiles.length - LIVE_TILE_LIMIT)).map(t => t.id)
      );
      (this.selectedIds || []).forEach(id => keep.add(id));
      (this._fullTileIds || []).forEach(id => keep.add(id));
      return {
        ...d,
        slide: preserveSlide ? d.slide || "" : "",
        tiles: tiles.map(t => ({
          ...t,
          url: keep.has(t.id)
            ? (previous.get(t.id) && previous.get(t.id).url) ||
              this.rawTileUrl(
                t,
                d.revision,
                this._fullTileIds && this._fullTileIds.has(t.id)
              )
            : null
        }))
      };
    },
    async loadPublishedSlide(status) {
      // `status` is intentionally cheap and never contains the Slide image.
      // Once refine flips to done, perform exactly one full load so an open
      // Canvas sees the same result as a freshly reopened Canvas.
      const hasPublishedSlide =
        !!status &&
        status.deferred_load !== true &&
        typeof status.slide === "string" &&
        status.slide.length > 0 &&
        (!this._awaitingFinalSlide || status.refined === true);
      if (
        !status ||
        (!hasPublishedSlide && status.refined !== true) ||
        status.locked ||
        status.refine_phase === "refining" ||
        this._finalSlideLoaded ||
        this._finalSlideLoadInFlight
      ) {
        return false;
      }
      this._finalSlideLoadInFlight = true;
      try {
        const loaded = hasPublishedSlide
          ? status
          : await this.api({ action: "load" });
        if (!loaded || loaded.deferred_load || !loaded.slide) return false;
        this.doc = this.withLiveTileUrls(loaded, true);
        this.slide = loaded.slide;
        this.bounds = loaded.bounds || this.bounds;
        this.slideStale = false;
        this._finalSlideLoaded = true;
        this._awaitingFinalSlide = false;
        this.mode = "slide";
        this.error = "";
        await this.$nextTick();
        this.fit();
        this.$emit("context", {
          reference: this.doc.reference,
          bounds: this.bounds,
          revision: this.doc.revision
        });
        return true;
      } catch (_) {
        // Refine can publish the manifest a fraction before the final render
        // cache is visible through the Pi proxy. Keep polling and retry once
        // the next status update arrives.
        return false;
      } finally {
        this._finalSlideLoadInFlight = false;
      }
    },
    async refreshPublishedSlide() {
      // The parent can learn that the Mac has finished refinement before the
      // Studio's own scan watcher sees the transition. Explicitly arm the
      // final-slide wait here so an already-open Tiles view is upgraded in
      // place, without closing/reopening the Canvas.
      if (this._finalSlideLoaded) return;
      this._awaitingFinalSlide = true;
      return this.refreshScan();
    },
    captureAtFov() {
      if (this.busy || this.locked || this.activeFovJob || !this.stageBox)
        return;
      this.pendingTile = { ...this.stageBox };
      this.capturePending("capture-current");
    },
    async checkFovJob() {
      if (!this.activeFovJob) return;
      this.busy = true;
      try {
        const { data: s } = await axios.post(
          `${this.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`,
          {
            operation: "status",
            session_id: this.session,
            job_id: this.activeFovJob
          }
        );
        if (s.phase === "completed") {
          if (s.stage_position)
            this.$emit("position", {
              x: s.stage_position[0],
              y: s.stage_position[1],
              z: s.stage_position[2]
            });
          this.activeFovJob = null;
          this.pendingTile = null;
          this.error = "";
          this.busy = false;
          await this.refreshScan();
        } else if (s.phase === "error") {
          this.error = s.error + " — check physical position before retrying";
          this.activeFovJob = null;
          this.pendingTile = null;
        } else this.error = "Still running; wait and check again.";
      } catch (e) {
        this.error = this.message(e);
      } finally {
        this.busy = false;
      }
    },
    async refreshScan() {
      if (this._refreshing || this.busy) return;
      this._refreshing = true;
      try {
        // Stay on image-free status while acquisition/refinement is active.
        // The first full Slide load is deferred until the Mac has published a
        // refined result, so a browser refresh cannot pile expensive renders
        // onto the scan-sync worker.
        const lightweight =
          this.scanning ||
          this._awaitingFinalSlide ||
          (this.doc &&
            (this.doc.locked ||
              (this.doc.deferred_load && this.doc.refined !== true) ||
              this.doc.refine_phase === "refining"));
        const d = await this.api({ action: lightweight ? "status" : "load" });
        if (await this.loadPublishedSlide(d)) return;
        const previous = this.doc ? this.doc.tiles.length : 0;
        const statusLike =
          lightweight ||
          d.deferred_load ||
          !Object.prototype.hasOwnProperty.call(d, "slide");
        if (statusLike) {
          const tiles = d.tiles || [];
          const last = tiles.length ? tiles[tiles.length - 1] : null;
          const signature = [
            d.revision || 0,
            d.phase || d.scan_phase || "",
            tiles.length,
            last && last.id,
            d.refined,
            d.refine_phase,
            d.refine_progress || 0
          ].join("|");
          if (signature === this._liveStatusSignature && this.doc) return;
          this._liveStatusSignature = signature;
          const live = this.withLiveTileUrls(d);
          this.doc = this.doc ? { ...this.doc, ...live } : live;
          this.mode = "tiles";
        } else {
          this.doc = this.withLiveTileUrls(d, true);
          this.slide = d.slide || "";
          this.slideStale = false;
        }
        this.bounds = d.bounds || this.bounds;
        this.error = "";
        if (this.locked) this.mode = "tiles";
        if (previous !== (d.tiles || []).length) this.fit();
        this.$emit("context", {
          reference: d.reference,
          bounds: d.bounds,
          revision: d.revision
        });
      } catch (e) {
        this.error = this.scanning
          ? "Waiting for the PC Worker to receive scan tiles…"
          : this.message(e);
      } finally {
        this._refreshing = false;
      }
    },
    tileLabel(t) {
      return t.asset
        ? t.stage_position
          ? "Captured tile"
          : "Imported image"
        : `Tile ${t.ix}, ${t.iy}`;
    },
    selectTile(t, e) {
      if (e.shiftKey || e.metaKey || e.ctrlKey) {
        this.selectedIds = this.selectedIds.includes(t.id)
          ? this.selectedIds.filter(id => id !== t.id)
          : [...this.selectedIds, t.id];
      } else this.selectedIds = [t.id];
      this.selected = this.selectedIds[0] || null;
      if (t && this.doc) {
        // A selected tile is the explicit request for detail.  Replace the
        // lightweight thumbnail with the original JPEG only then.
        if (this._fullTileIds) this._fullTileIds.add(t.id);
        this.$set(t, "url", this.rawTileUrl(t, this.doc.revision, true));
      }
      this.inspector = true;
    },
    slideToTile(e) {
      if (this.locked || this.roiDrawing || !this.doc) return;
      const p = this.point(e);
      const tile = [...this.visible]
        .reverse()
        .find(
          t => p.x >= t.x && p.x <= t.x + t.w && p.y >= t.y && p.y <= t.y + t.h
        );
      if (!tile) return;
      this.selectedIds = [tile.id];
      this.selected = tile.id;
      this.inspector = true;
      this.mode = "tiles";
    },
    startLayerDrag(t, e) {
      if (this.locked || this.busy) return;
      this.layerDragId = t.id;
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", t.id);
      }
    },
    dropLayer(target) {
      const sourceId = this.layerDragId;
      this.layerDragId = null;
      if (!sourceId || sourceId === target.id || this.locked || this.busy)
        return;
      const display = [...this.doc.tiles].reverse();
      const from = display.findIndex(t => t.id === sourceId);
      const to = display.findIndex(t => t.id === target.id);
      if (from < 0 || to < 0) return;
      this.checkpoint();
      const [source] = display.splice(from, 1);
      display.splice(to, 0, source);
      this.doc.tiles = display.reverse();
      this.doc.layered = true;
      this.selectedIds = [source.id];
      this.selected = source.id;
      // Layer order is immediately useful in Tiles view; defer the expensive
      // full-image composite until the user returns to Slide.
      this.queueDeferredSave();
    },
    toggleLayer(t) {
      if (this.locked || this.busy) return;
      if (!t.hidden && this.visible.length < 2) return;
      this.checkpoint();
      t.hidden = !t.hidden;
      // Visibility is a canvas-state change, not an image-processing change.
      this.queueDeferredSave();
    },
    prepareDelete(t) {
      if (this.locked || this.busy) return;
      this.selectedIds = [t.id];
      this.selected = t.id;
      this.deleteDialog = true;
    },
    markRoi() {
      this.mode = "slide";
      this.selectBox = false;
      this.$nextTick(() => this.$emit("mark-roi"));
    },
    changeAdjustment(key, value) {
      if (this.locked) return;
      if (this.selectedIds.length) {
        this.doc.tiles
          .filter(t => this.selectedIds.includes(t.id))
          .forEach(t =>
            this.$set(t, "adjustments", {
              ...(t.adjustments || this.doc.adjustments),
              [key]: value
            })
          );
      } else {
        this.$set(this.doc.adjustments, key, value);
        this.doc.tiles.forEach(t => {
          if (t.adjustments) this.$set(t.adjustments, key, value);
        });
      }
      const ids = this.selectedIds.length
        ? this.selectedIds
        : this.doc.tiles.map(t => t.id);
      this.adjustmentPending = true;
      this.queueDeferredSave(ids);
    },
    setOpacity(value) {
      this.doc.tiles
        .filter(t => this.selectedIds.includes(t.id))
        .forEach(t => this.$set(t, "opacity", Number(value)));
      this.doc.layered = true;
      // Opacity is rendered locally in Tiles view and can wait for Slide.
      this.queueDeferredSave();
    },
    reorder(direction) {
      this.checkpoint();
      const a = this.doc.tiles,
        has = t => this.selectedIds.includes(t.id);
      if (direction === "top")
        this.doc.tiles = [...a.filter(t => !has(t)), ...a.filter(has)];
      if (direction === "bottom")
        this.doc.tiles = [...a.filter(has), ...a.filter(t => !has(t))];
      if (direction === "up")
        for (let i = a.length - 2; i >= 0; i--) {
          if (has(a[i]) && !has(a[i + 1])) a.splice(i, 2, a[i + 1], a[i]);
        }
      if (direction === "down")
        for (let i = 1; i < a.length; i++) {
          if (has(a[i]) && !has(a[i - 1])) a.splice(i - 1, 2, a[i], a[i - 1]);
        }
      this.doc.layered = true;
      this.queueDeferredSave();
    },
    async deleteTiles(storage) {
      if (this.locked || this.busy) return;
      this.busy = true;
      try {
        const r = await this.api({
          action: "delete",
          revision: this.doc.revision,
          tile_ids: this.selectedIds,
          storage
        });
        this.doc = this.withLiveTileUrls(r.document, true);
        this.slide = r.slide;
        this.bounds = r.bounds;
        this.selectedIds = [];
        this.selected = null;
        this.undoStack = [];
        this.redoStack = [];
        this.deleteDialog = false;
        this.error = "";
        this.$emit("saved");
      } catch (e) {
        this.error = this.message(e);
      } finally {
        this.busy = false;
      }
    },
    newCaptureTile() {
      const t = this.doc.tiles.find(t => !t.asset);
      if (!t) {
        this.error = "No acquired reference tile available";
        return;
      }
      const el = this.$refs.viewport;
      this.pendingTile = {
        x: (el.clientWidth / 2 - this.view.x) / this.view.scale - t.w / 2,
        y: (el.clientHeight / 2 - this.view.y) / this.view.scale - t.h / 2,
        w: t.w,
        h: t.h
      };
      this.mode = "tiles";
      this.marking = false;
      this.vpRect = null;
      this.vpStatus =
        "Drag the dashed tile, then choose Capture here. Dragging never moves the stage.";
    },
    async capturePending(intent = "fov-move") {
      if (this.busy || this.locked || this.activeFovJob || !this.pendingTile)
        return;
      this.busy = true;
      this.error = "";
      try {
        const p = await this.api({
          action: "placement-plan",
          revision: this.doc.revision,
          x: this.pendingTile.x,
          y: this.pendingTile.y,
          intent,
          stage_position:
            intent === "capture-current"
              ? [
                  this.stagePosition.x,
                  this.stagePosition.y,
                  this.stagePosition.z
                ]
              : undefined
        });
        const xyz = p.tiles[0].stage_position;
        if (
          !this.sampleConfirmed &&
          !window.confirm(
            `Enable FOV control for this canvas? Confirm the specimen, objective and stage zero are unchanged.\n\nDragging will move to the corresponding XYZ and autofocus, without saving a tile. Capture only saves the current view.\n\nTarget XYZ: ${xyz.join(
              ", "
            )}`
          )
        )
          return;
        this.sampleConfirmed = true;
        this.fovIntent = intent;
        this.activeFovJob = p.plan_id;
        const uri = `${this.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`;
        const { data: job } = await axios.post(
          uri,
          {
            operation: "start",
            session_id: this.session,
            plan_id: p.plan_id,
            confirm_same_sample: true
          },
          { timeout: 20000 }
        );
        if (!job.job_id)
          throw new Error(job.error || "Could not start acquisition");
        for (let n = 0; n < 600; n++) {
          const { data: s } = await axios.post(
            uri,
            {
              operation: "status",
              session_id: this.session,
              job_id: job.job_id
            },
            { timeout: 20000 }
          );
          if (s.phase === "error") throw new Error(s.error);
          if (s.phase === "completed") {
            this.activeFovJob = null;
            if (intent === "fov-move") {
              this.pendingTile = null;
              this.$emit("position", {
                x: s.stage_position[0],
                y: s.stage_position[1],
                z: s.stage_position[2]
              });
              this.vpStatus =
                s.focus && s.focus.warning
                  ? `Moved · autofocus warning: ${s.focus.warning}`
                  : "Positioned and autofocused — Capture adds this FOV to Canvas";
              return;
            }
            const d = await this.api({ action: "load" });
            this.doc = this.withLiveTileUrls(d, true);
            this.slide = d.slide;
            this.bounds = d.bounds;
            this.pendingTile = null;
            this.undoStack = [];
            this.redoStack = [];
            this.fit();
            this.vpStatus = "New tile captured and added to Canvas";
            this.$emit("saved");
            return;
          }
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
        throw new Error(
          "Acquisition still pending. Inspect the job before retrying."
        );
      } catch (e) {
        this.error =
          this.message(e) + " — inspect acquisition status before retrying";
      } finally {
        this.busy = false;
        if (!this.activeFovJob) this.pendingTile = null;
      }
    },
    point(e) {
      const r = this.$refs.viewport.getBoundingClientRect(),
        b = this.bounds;
      return {
        x: Math.max(
          b.x,
          Math.min(
            b.x + b.w,
            (e.clientX - r.left - this.view.x) / this.view.scale
          )
        ),
        y: Math.max(
          b.y,
          Math.min(
            b.y + b.h,
            (e.clientY - r.top - this.view.y) / this.view.scale
          )
        )
      };
    },
    key(e) {
      if (this.busy || this.locked) return;
      if (
        (e.metaKey || e.ctrlKey) &&
        e.key.toLowerCase() === "a" &&
        !["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)
      ) {
        e.preventDefault();
        this.selectedIds = this.visible.map(t => t.id);
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey && this.redoStack.length) this.redo();
        else if (!e.shiftKey && this.undoStack.length) this.undo();
      }
      if (e.key === "Escape") this.close();
    },
    retakeSelected() {
      const t = this.chosen,
        b = this.bounds;
      this.$emit("retake", {
        session_id: this.session,
        roi: {
          x: (t.x - b.x) / b.w,
          y: (t.y - b.y) / b.h,
          width: t.w / b.w,
          height: t.h / b.h
        }
      });
    },
    async api(body) {
      const action = body.action || "load";
      const flightKey =
        action === "load" || action === "status" ? action : null;
      if (flightKey && this._apiFlights && this._apiFlights[flightKey]) {
        return this._apiFlights[flightKey];
      }
      let request = axios
        .post(
          `${this.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/mac/recapture`,
          { ...body, operation: "canvas", session_id: this.session },
          {
            timeout:
              action === "status" ? 10000 : action === "load" ? 30000 : 60000
          }
        )
        .then(({ data }) => {
          if (data.error) throw new Error(data.error);
          return data;
        });
      if (!flightKey || !this._apiFlights) return request;
      const wrapped = request.then(
        value => {
          if (this._apiFlights[flightKey] === wrapped)
            delete this._apiFlights[flightKey];
          return value;
        },
        error => {
          if (this._apiFlights[flightKey] === wrapped)
            delete this._apiFlights[flightKey];
          throw error;
        }
      );
      this._apiFlights[flightKey] = wrapped;
      return wrapped;
    },
    rawTileUrl(t, revision, full = false) {
      const suffix = full ? "" : "&thumb=1&width=320";
      return `${
        this.baseUri
      }/api/v2/extensions/org.microworld.scan-mosaic/mac/tile?session_id=${encodeURIComponent(
        this.session
      )}&ix=${encodeURIComponent(t.ix)}&iy=${encodeURIComponent(
        t.iy
      )}&r=${encodeURIComponent(revision || 0)}${suffix}`;
    },
    message(e) {
      return (
        (e.response && e.response.data && e.response.data.error) ||
        e.message ||
        "Could not save canvas"
      );
    },
    box(t) {
      return {
        left: t.x + "px",
        top: t.y + "px",
        width: t.w + "px",
        height: t.h + "px"
      };
    },
    snapshot() {
      return {
        tiles: this.doc.tiles.map(t => {
          const copy = { ...t };
          delete copy.url;
          return copy;
        }),
        adjustments: { ...this.doc.adjustments },
        layered: !!this.doc.layered
      };
    },
    checkpoint() {
      if (!this.doc || this.busy) return;
      this.undoStack.push(JSON.parse(JSON.stringify(this.snapshot())));
      if (this.undoStack.length > 50) this.undoStack.shift();
      this.redoStack = [];
    },
    sliderKey(e) {
      if (!e.repeat) this.checkpoint();
    },
    queueDeferredSave(previewIds = []) {
      if (this.locked || !this.doc) return;
      this.slideStale = true;
      this.deferredSaveQueued = true;
      this.deferredPreviewIds = [
        ...new Set([...this.deferredPreviewIds, ...previewIds])
      ];
      if (this.deferredSaveTimer) clearTimeout(this.deferredSaveTimer);
      this.deferredSaveTimer = setTimeout(() => {
        this.deferredSaveTimer = null;
        this.flushDeferredSave();
      }, 180);
    },
    async flushDeferredSave() {
      if (this.deferredSaveTimer) {
        clearTimeout(this.deferredSaveTimer);
        this.deferredSaveTimer = null;
      }
      if (!this.deferredSaveQueued) return this.deferredSavePromise;
      if (this.deferredSaveRunning) return this.deferredSavePromise;
      if (this.busy) {
        await new Promise(resolve => setTimeout(resolve, 50));
        return this.flushDeferredSave();
      }
      this.deferredSaveQueued = false;
      const previewIds = this.deferredPreviewIds.slice();
      this.deferredPreviewIds = [];
      this.deferredSaveRunning = true;
      this.deferredSavePromise = this.save(true, {
        preview_tile_ids: previewIds
      }).finally(() => {
        this.deferredSaveRunning = false;
        this.deferredSavePromise = null;
        if (this.deferredSaveQueued) this.flushDeferredSave();
      });
      return this.deferredSavePromise;
    },
    async save(deferRender = false, options = {}) {
      if (this.locked || !this.doc) return;
      if (!deferRender && this.busy) return;
      if (!deferRender) this.busy = true;
      this.error = "";
      try {
        const r = await this.api({
          action: "save",
          revision: this.doc.revision,
          document: this.snapshot(),
          defer_render: !!deferRender,
          ...options
        });
        if (deferRender) {
          if (r.document && r.document.revision != null)
            this.doc.revision = r.document.revision;
          (r.tiles || []).forEach(preview => {
            const tile = this.doc.tiles.find(t => t.id === preview.id);
            if (tile && preview.url) this.$set(tile, "url", preview.url);
          });
          this.slideStale = true;
          this.adjustmentPending = false;
          return;
        }
        this.doc = this.withLiveTileUrls(r.document, true);
        if (this.vpRect) {
          this.vpRect = null;
          this.vpStatus = "Canvas changed — mark a new region for Codex";
        }
        this.slide = r.slide;
        this.bounds = r.bounds;
        this.$emit("context", {
          reference: this.doc.reference,
          bounds: this.bounds,
          revision: this.doc.revision
        });
        this.$emit("saved");
      } catch (e) {
        this.error = this.message(e);
        if (deferRender) this.adjustmentPending = false;
      } finally {
        if (!deferRender) this.busy = false;
      }
    },
    async setMode(next) {
      next = next === "tiles" ? "tiles" : "slide";
      if (next === "tiles" || !this.slideStale || this.busy) {
        this.mode = next;
        return;
      }
      await this.flushDeferredSave();
      this.busy = true;
      this.error = "";
      try {
        const d = await this.api({ action: "load" });
        this.doc = this.withLiveTileUrls(d, true);
        this.slide = d.slide;
        this.bounds = d.bounds;
        this.slideStale = false;
        this.mode = "slide";
        await this.$nextTick();
        this.fit();
      } catch (e) {
        this.error = this.message(e);
      } finally {
        this.busy = false;
      }
    },
    applySnapshot(s) {
      const map = new Map(s.tiles.map(t => [t.id, t]));
      const current = new Map(this.doc.tiles.map(t => [t.id, t]));
      this.doc.tiles = [
        ...s.tiles
          .filter(t => current.has(t.id))
          .map(t => ({ ...current.get(t.id), ...t })),
        ...this.doc.tiles
          .filter(t => !map.has(t.id))
          .map(t => ({ ...t, hidden: true }))
      ];
      this.doc.adjustments = { ...s.adjustments };
      this.doc.layered = !!s.layered;
      if (this.mode === "tiles") this.queueDeferredSave();
      else this.save();
    },
    undo() {
      this.redoStack.push(this.snapshot());
      this.applySnapshot(this.undoStack.pop());
    },
    redo() {
      this.undoStack.push(this.snapshot());
      this.applySnapshot(this.redoStack.pop());
    },
    fit() {
      if (!this.visible.length) return;
      const el = this.$refs.viewport;
      const x = Math.min(...this.visible.map(t => t.x)),
        y = Math.min(...this.visible.map(t => t.y));
      const w = Math.max(...this.visible.map(t => t.x + t.w)) - x,
        h = Math.max(...this.visible.map(t => t.y + t.h)) - y;
      const scale = Math.min(
        (el.clientWidth - 100) / w,
        (el.clientHeight - 100) / h,
        2
      );
      this.view = {
        scale: Math.max(0.02, scale),
        x: (el.clientWidth - w * scale) / 2 - x * scale,
        y: (el.clientHeight - h * scale) / 2 - y * scale
      };
    },
    zoom(f, cx, cy) {
      const el = this.$refs.viewport;
      if (cx == null) {
        cx = el.clientWidth / 2;
        cy = el.clientHeight / 2;
      }
      const old = this.view.scale,
        s = Math.max(0.02, Math.min(12, old * f));
      this.view = {
        scale: s,
        x: cx - ((cx - this.view.x) * s) / old,
        y: cy - ((cy - this.view.y) * s) / old
      };
    },
    wheel(e) {
      const r = this.$refs.viewport.getBoundingClientRect();
      this.zoom(
        Math.exp(-e.deltaY * 0.001),
        e.clientX - r.left,
        e.clientY - r.top
      );
    },
    down(e) {
      if (this.busy || this.vpBusy || !this.doc || e.button > 0) return;
      if (this.selectBox && !this.locked && !e.target.closest(".stage-fov")) {
        const p = this.point(e);
        this.drag = {
          id: e.pointerId,
          box: p,
          additive: e.shiftKey,
          previous: this.selectedIds.slice()
        };
        this.selectionRect = { ...p, w: 0, h: 0 };
        this.$refs.viewport.setPointerCapture(e.pointerId);
        return;
      }
      const ghost = e.target.closest(".stage-fov");
      if (ghost && (this.locked || this.activeFovJob)) return;
      if (ghost) this.pendingTile = { ...this.stageBox };
      const tile = e.target.closest("[data-tile]");
      const t = this.locked
        ? null
        : ghost
        ? this.pendingTile
        : this.mode === "tiles" && tile
        ? this.doc.tiles.find(t => t.id === tile.dataset.tile)
        : null;
      if (t && !ghost) {
        this.checkpoint();
        if (
          e.shiftKey ||
          e.metaKey ||
          e.ctrlKey ||
          !this.selectedIds.includes(t.id)
        )
          this.selectTile(t, e);
        this.selected = t.id;
        this.inspector = true;
      }
      this.drag = {
        group:
          t && !ghost
            ? this.doc.tiles
                .filter(t => this.selectedIds.includes(t.id))
                .map(t => ({ t, x: t.x, y: t.y }))
            : null,
        pending: !!ghost,
        slideClick:
          this.mode === "slide" && !!e.target.closest(".slide") && !ghost,
        id: e.pointerId,
        x: e.clientX,
        y: e.clientY,
        tile: t,
        ox: t ? t.x : this.view.x,
        oy: t ? t.y : this.view.y,
        moved: false
      };
      if (!t && !this.locked) {
        this.selectedIds = [];
        this.selected = null;
      }
      this.$refs.viewport.setPointerCapture(e.pointerId);
    },
    move(e) {
      const d = this.drag;
      if (!d || d.id !== e.pointerId) return;
      if (d.box) {
        const p = this.point(e),
          a = d.box;
        this.selectionRect = {
          x: Math.min(a.x, p.x),
          y: Math.min(a.y, p.y),
          w: Math.abs(a.x - p.x),
          h: Math.abs(a.y - p.y)
        };
        return;
      }
      if (d.selection) {
        const p = this.point(e),
          a = d.selection;
        this.vpRect = {
          x: Math.min(a.x, p.x),
          y: Math.min(a.y, p.y),
          w: Math.abs(a.x - p.x),
          h: Math.abs(a.y - p.y)
        };
        return;
      }
      const dx = e.clientX - d.x,
        dy = e.clientY - d.y;
      d.moved = d.moved || Math.abs(dx) + Math.abs(dy) > 2;
      if (d.tile) {
        d.tile.x = d.ox + dx / this.view.scale;
        d.tile.y = d.oy + dy / this.view.scale;
        d.tile.manual = true;
        if (d.group)
          d.group.forEach(({ t, x, y }) => {
            t.x = x + dx / this.view.scale;
            t.y = y + dy / this.view.scale;
            t.manual = true;
          });
      } else {
        this.view.x = d.ox + dx;
        this.view.y = d.oy + dy;
      }
    },
    up(e) {
      if (!this.drag || this.drag.id !== e.pointerId) return;
      const d = this.drag;
      this.drag = null;
      if (d.box) {
        const r = this.selectionRect,
          ids = this.visible
            .filter(
              t =>
                t.x < r.x + r.w &&
                t.x + t.w > r.x &&
                t.y < r.y + r.h &&
                t.y + t.h > r.y
            )
            .map(t => t.id);
        this.selectedIds = [
          ...new Set([...(d.additive ? d.previous : []), ...ids])
        ];
        this.selected = this.selectedIds[0] || null;
        this.selectionRect = null;
        this.inspector = true;
        return;
      }
      if (d.pending) {
        if (d.moved && e.type !== "pointercancel")
          this.capturePending("fov-move");
        else this.pendingTile = null;
        return;
      }
      if (d.slideClick && !d.moved && e.type !== "pointercancel") {
        this.slideToTile(e);
        return;
      }
      if (d.tile && d.moved) this.queueDeferredSave();
    },
    hideTile() {
      this.checkpoint();
      this.doc.tiles
        .filter(t => this.selectedIds.includes(t.id))
        .forEach(t => {
          t.hidden = true;
        });
      this.selectedIds = [];
      this.selected = null;
      this.queueDeferredSave();
    },
    restoreTile(t) {
      this.checkpoint();
      t.hidden = false;
      this.queueDeferredSave();
    },
    restorePosition() {
      this.checkpoint();
      Object.assign(this.chosen, {
        x: this.chosen.auto_x,
        y: this.chosen.auto_y,
        manual: false
      });
      this.queueDeferredSave();
    },
    resetAdjustments() {
      this.checkpoint();
      if (this.selectedIds.length)
        this.doc.tiles
          .filter(t => this.selectedIds.includes(t.id))
          .forEach(t => this.$set(t, "adjustments", defaults()));
      else {
        this.doc.adjustments = defaults();
        this.doc.tiles.forEach(t => this.$set(t, "adjustments", null));
      }
      const ids = this.selectedIds.length
        ? this.selectedIds
        : this.doc.tiles.map(t => t.id);
      this.queueDeferredSave(ids);
    },
    toggleEnhance() {
      this.checkpoint();
      this.changeAdjustment("enhance", !this.activeAdjustments.enhance);
    },
    async importImage(e) {
      const file = e.target.files[0];
      e.target.value = "";
      if (!file) return;
      if (file.size > 8 * 1024 * 1024) {
        this.error = "Choose an image smaller than 8 MB";
        return;
      }
      this.checkpoint();
      this.busy = true;
      try {
        const image = await new Promise((resolve, reject) => {
          const r = new FileReader();
          r.onload = () => resolve(r.result);
          r.onerror = reject;
          r.readAsDataURL(file);
        });
        const r = await this.api({
          action: "import",
          revision: this.doc.revision,
          image
        });
        this.doc = this.withLiveTileUrls(r.document, true);
        this.slide = r.slide;
        this.bounds = r.bounds;
        this.mode = "tiles";
        this.error = "";
        this.fit();
        this.$emit("saved");
      } catch (err) {
        this.error = this.message(err);
      } finally {
        this.busy = false;
      }
    },
    close() {
      if (this.busy || this.activeFovJob) return;
      if (
        this.error &&
        !window.confirm("Some changes are not saved. Close this editor?")
      )
        return;
      this.$emit("close");
    }
  }
};
</script>

<style scoped>
.fov-tools {
  position: absolute;
  right: 0;
  top: 0;
  transform-origin: top right;
  display: flex;
  align-items: center;
  white-space: nowrap;
  pointer-events: auto;
  background: #fff;
  border: 1px solid #16866d;
  border-radius: 0 0 0 8px;
  overflow: hidden;
}
.fov-capture {
  width: 34px;
  height: 34px;
  min-height: 34px;
  background: white;
  color: #147c67;
  padding: 0;
  font-size: 24px;
  font-weight: 300;
  line-height: 1;
}
fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.layer-list {
  max-height: 210px;
  overflow: auto;
  border: 1px solid #eee;
  border-radius: 8px;
}
.layer-row {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) 34px;
  align-items: center;
  min-height: 42px;
  cursor: grab;
  border-bottom: 1px solid #eeeef1;
}
.layer-row:last-child {
  border-bottom: 0;
}
.layer-row:active {
  cursor: grabbing;
}
.layer-row.active {
  background: #edf3ff;
}
.layer-row button {
  width: 34px;
  min-height: 34px;
  padding: 4px;
  display: grid;
  place-items: center;
}
.layer-row .material-icons {
  font-size: 18px;
}
.layer-check {
  color: #2465d8;
}
.layer-delete {
  color: #a23b3b;
}
.layer-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.stage-fov {
  position: absolute;
  border: 2px solid #16a085;
  z-index: 9;
  pointer-events: auto;
  cursor: grab;
  box-sizing: border-box;
  overflow: hidden;
  background: #111;
  box-shadow: 0 3px 18px #0003;
}
.stage-fov:active {
  cursor: grabbing;
}
.fov-live {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.fov-live ::v-deep .stream-display,
.fov-live ::v-deep img,
.fov-live ::v-deep video {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.stage-fov span {
  background: #147c67;
  color: white;
  font-size: 12px;
  padding: 3px;
}
.scan-lock {
  position: absolute;
  top: 16px;
  left: 16px;
  max-width: 60%;
  padding: 10px 16px;
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 2px 10px #0001;
}
.studio.readonly .layer-row {
  cursor: default;
}
.studio.readonly .layer-row:hover {
  background: transparent;
}
.delete-dialog {
  position: absolute;
  inset: 0;
  background: #0005;
  z-index: 120;
  display: grid;
  place-items: center;
}
.delete-dialog > div {
  max-width: 480px;
  background: white;
  padding: 24px;
  border-radius: 16px;
}
.studio {
  position: absolute;
  inset: 0;
  z-index: 90;
  background: #f5f5f7;
  color: #242426;
  display: flex;
  flex-direction: column;
  font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  text-align: left;
}
header {
  min-height: 54px;
  flex-wrap: wrap;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: #fff;
  border-bottom: 1px solid #e5e5e7;
}
.identity {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.identity strong {
  font-size: 18px;
  font-weight: 600;
}
.eyebrow {
  font-size: 9px;
  letter-spacing: 1.6px;
  color: #808086;
}
button {
  border: 0;
  background: transparent;
  color: inherit;
  border-radius: 7px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 11px;
  font: inherit;
  line-height: 1.25;
  cursor: pointer;
  white-space: nowrap;
  min-height: 36px;
}
button:hover {
  background: #ececef;
}
button:focus-visible,
input:focus-visible,
select:focus-visible {
  outline: 2px solid #386af4;
  outline-offset: 2px;
}
button:disabled {
  opacity: 0.35;
  cursor: default;
}
.icon-action {
  width: 36px;
  padding: 6px;
  display: inline-grid;
  place-items: center;
}
.icon-action .material-icons {
  font-size: 19px;
}
nav {
  display: flex;
  background: #eeeef0;
  border-radius: 9px;
  padding: 3px;
}
nav button {
  min-width: 70px;
}
.active,
nav button.active {
  background: #fff;
  color: #1959c7;
}
.actions {
  margin-left: auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.actions > button:last-child {
  background: #252527;
  color: #fff;
  margin-left: 8px;
}
.body {
  display: flex;
  flex: 1;
  min-height: 0;
}
.viewport {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  position: relative;
  touch-action: none;
  background-color: #f0f0f2;
  background-image: radial-gradient(#d7d7dc 0.7px, transparent 0.7px);
  background-size: 20px 20px;
}
.world {
  position: absolute;
  transform-origin: 0 0;
}
.tile,
.slide {
  position: absolute;
  max-width: none;
  user-select: none;
}
.slide {
  cursor: pointer;
}
.tile img {
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.tile-placeholder {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  background: #dfe3ea;
  color: #6d7480;
  font: 12px -apple-system, BlinkMacSystemFont, sans-serif;
}
.tile {
  outline: 1px solid #ffffff90;
  cursor: grab;
  will-change: left, top;
}
.tile.selected {
  outline: 3px solid #3678ed;
}
.tile span {
  position: absolute;
  left: 6px;
  top: 6px;
  background: #ffffffdf;
  border-radius: 4px;
  padding: 3px 6px;
  font-size: 11px;
  pointer-events: none;
}
.view-controls {
  position: absolute;
  bottom: 25px;
  left: 24px;
  display: flex;
  align-items: center;
  background: #fff;
  border: 1px solid #e5e5e7;
  border-radius: 10px;
}
.view-controls span {
  min-width: 45px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.hint {
  position: absolute;
  bottom: 35px;
  right: 24px;
  font-size: 11px;
  color: #74747b;
  pointer-events: none;
}
.empty {
  position: absolute;
  inset: 40%;
  white-space: nowrap;
  color: #777;
}
aside {
  width: 270px;
  flex-shrink: 0;
  padding: 24px;
  background: #fff;
  border-left: 1px solid #e5e5e7;
  overflow-y: auto;
}
h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 12px;
}
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.section-head h3 {
  margin: 0;
}
.section-head button {
  color: #6a6a73;
  font-size: 12px;
}
.caption {
  font-size: 11px;
  line-height: 1.6;
  color: #777780;
  margin: 8px 0 24px;
}
label {
  display: block;
  margin: 22px 0;
  font-size: 12px;
}
output {
  float: right;
  color: #73737a;
  font-variant-numeric: tabular-nums;
}
input[type="range"] {
  display: block;
  width: 100%;
  margin-top: 14px;
  accent-color: #3c3c43;
}
select {
  display: block;
  width: 100%;
  margin-top: 12px;
  padding: 9px;
  border: 1px solid #e5e5e7;
  border-radius: 7px;
  background: #fafafa;
  font: inherit;
}
.enhance {
  width: 100%;
  border: 1px solid #ddd;
}
.enhance.active {
  background: #eef3fc;
}
hr {
  border: 0;
  border-top: 1px solid #eee;
  margin: 24px 0;
}
aside > button {
  display: block;
}
footer {
  min-height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  flex-wrap: wrap;
  padding: 5px 16px;
  box-sizing: border-box;
  background: #fff;
  border-top: 1px solid #e5e5e7;
  font-size: 11px;
  color: #797981;
}
.error {
  color: #ac3939;
}
.vp-selection {
  position: absolute;
  border: 2px solid #2563eb;
  background: #2563eb15;
  pointer-events: none;
  z-index: 5;
  box-sizing: border-box;
}
.capture-ghost {
  position: absolute;
  z-index: 8;
  border: 3px dashed #3478ed;
  background: #3478ed18;
  box-sizing: border-box;
  cursor: grab;
  display: flex;
  align-items: center;
  justify-content: center;
}
.capture-ghost span {
  background: #fff;
  padding: 10px;
  border-radius: 6px;
  color: #2453a1;
  pointer-events: none;
}
.studio h3 {
  color: #242426;
}
@media (max-width: 800px) {
  header {
    padding: 6px 12px;
    gap: 6px;
    height: auto;
  }
  .eyebrow {
    display: none;
  }
  .identity strong {
    font-size: 15px;
  }
  .actions {
    gap: 0;
  }
  .actions button {
    padding: 7px;
  }
  aside {
    width: 210px;
    padding: 16px;
  }
  .hint {
    display: none;
  }
  footer {
    padding: 0 12px;
  }
  footer span:last-child {
    display: none;
  }
}
@media (max-width: 650px) {
  header {
    flex-wrap: wrap;
    height: auto;
    padding: 10px 12px;
    gap: 8px;
  }
  .actions {
    width: 100%;
    justify-content: space-between;
  }
  .body {
    flex-direction: column;
  }
  .viewport {
    min-height: 180px;
  }
  aside {
    width: auto;
    max-height: 230px;
    border-left: 0;
    border-top: 1px solid #e5e5e7;
  }
}
</style>
