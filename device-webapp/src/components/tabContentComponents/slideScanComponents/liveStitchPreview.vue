<template>
  <div class="live-stitch-preview uk-margin-small">
    <h4 class="uk-margin-remove-bottom">Live stitching preview</h4>
    <p class="uk-text-meta uk-margin-remove-top">
      <span v-if="details">
        Phase: {{ details.scan_phase || "—" }} · tiles
        {{ details.image_count || 0 }}
        <span v-if="details.cell_count">
          · cells {{ details.cell_count
          }}<span v-if="details.cells_expected"
            >/{{ details.cells_expected }}</span
          >
        </span>
        <span v-if="details.partial" style="color:#f0ad4e"> · partial</span>
        <span v-if="details.name"> · {{ details.name }}</span>
      </span>
      <span v-else>Waiting for scan…</span>
    </p>
    <div class="preview-frame">
      <img
        v-if="previewUrl"
        :key="previewUrl"
        class="uk-width-1-1"
        :src="previewUrl"
        alt="live stitch preview"
      />
      <div v-else class="preview-placeholder">
        Preview appears after a few tiles (like OpenFlexure v3).
      </div>
    </div>
  </div>
</template>

<script>
import axios from "axios";

export default {
  name: "LiveStitchPreview",
  props: {
    active: {
      type: Boolean,
      default: false
    },
    pollIntervalMs: {
      type: Number,
      default: 1000
    }
  },
  data() {
    return {
      details: null,
      previewUrl: null,
      timer: null
    };
  },
  computed: {
    liveDetailsUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/live_details`;
    },
    previewBaseUri() {
      return `${this.$store.getters.baseUri}/api/v2/extensions/org.microworld.scan-mosaic/latest_preview_stitch.jpg`;
    }
  },
  watch: {
    active: {
      immediate: true,
      handler(val) {
        if (val) {
          this.startPolling();
        } else {
          this.stopPolling();
          // one final fetch so the finished mosaic stays visible
          this.pollOnce();
        }
      }
    }
  },
  beforeDestroy() {
    this.stopPolling();
  },
  methods: {
    startPolling() {
      this.stopPolling();
      this.pollOnce();
      this.timer = setInterval(this.pollOnce, this.pollIntervalMs);
    },
    stopPolling() {
      if (this.timer) {
        clearInterval(this.timer);
        this.timer = null;
      }
    },
    pollOnce() {
      axios
        .get(this.liveDetailsUri)
        .then(response => {
          this.details = response.data || {};
          if (this.details.preview_ready && this.details.stitch_timestamp) {
            this.previewUrl = `${this.previewBaseUri}?t=${this.details.stitch_timestamp}`;
          }
        })
        .catch(() => {
          // Extension may be restarting; keep last preview
        });
    }
  }
};
</script>

<style scoped>
.preview-frame {
  background: #111;
  border: 1px solid rgba(0, 0, 0, 0.15);
  min-height: 160px;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.preview-frame img {
  display: block;
  max-height: 420px;
  width: auto;
  object-fit: contain;
}
.preview-placeholder {
  color: #aaa;
  padding: 1.5rem;
  text-align: center;
  font-size: 0.9rem;
}
</style>
