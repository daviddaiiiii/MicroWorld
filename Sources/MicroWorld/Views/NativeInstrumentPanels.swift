import SwiftUI
import UniformTypeIdentifiers

struct NativeGallery: View {
    let instrument: InstrumentStore
    let openCanvas: (String) -> Void
    @State private var selectedImage: NSImage?
    @State private var selectedName = ""
    @State private var query = ""
    @State private var showPreview = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                HStack(spacing: 7) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Search gallery", text: $query).textFieldStyle(.plain)
                    if !query.isEmpty {
                        Button { query = "" } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Clear search")
                    }
                }
                .padding(8)
                .frame(maxWidth: 300)
                .background(.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 8))
                Spacer()
                Button("Refresh", systemImage: "arrow.clockwise") { Task { await refresh() } }
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if instrument.api.connection.openmhs {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Canvases").font(.headline)
                            if visibleCanvases.isEmpty {
                                Text(query.isEmpty ? "No canvases yet." : "No matching canvases.")
                                    .foregroundStyle(.secondary)
                            }
                            LazyVGrid(columns: [GridItem(.adaptive(minimum: 172, maximum: 300), spacing: 12)],
                                      alignment: .leading, spacing: 12) {
                                ForEach(visibleCanvases) { item in
                                    CanvasCard(item: item, preview: instrument.api.localPath("/mosaic/\(item.id)/preview.jpg")) {
                                        openCanvas(item.id)
                                    }
                                }
                            }
                        }
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("This Mac").font(.headline)
                        if instrument.localCaptures.isEmpty { Text("No saved snapshots.").foregroundStyle(.secondary) }
                        ForEach(instrument.localCaptures.filter { matches($0.lastPathComponent) }, id: \.self) { url in
                            captureRow(url.lastPathComponent) {
                                if let image = NSImage(contentsOf: url) {
                                    selectedImage = image; selectedName = url.lastPathComponent; showPreview = true
                                }
                            }
                            .contextMenu { Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([url]) } }
                        }
                    }
                    LazyVStack(alignment: .leading, spacing: 8) {
                        Text("Microscope captures").font(.headline)
                        if !instrument.galleryError.isEmpty {
                            Label(instrument.galleryError, systemImage: "exclamationmark.circle")
                                .font(.callout).foregroundStyle(.secondary)
                        } else if instrument.remoteCaptures.isEmpty {
                            Text("No captures listed.").foregroundStyle(.secondary)
                        }
                        ForEach(instrument.remoteCaptures.filter { matches($0.filename ?? $0.id) }) { item in
                            captureRow(item.filename ?? item.id) { Task { await preview(item) } }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(18)
            }
        }
        .task { await refresh() }
        .sheet(isPresented: $showPreview) {
            VStack {
                HStack {
                    Text(selectedName).font(.headline)
                    Spacer()
                    Button("Done") { showPreview = false }.keyboardShortcut(.cancelAction)
                }.padding()
                if let selectedImage { NativeImageSurface(image: selectedImage) }
            }
            .frame(width: 680, height: 530)
        }
    }

    private var visibleCanvases: [CanvasSummary] {
        instrument.canvases.filter { matches($0.id) || matches($0.displayTitle) }
            .sorted { $0.displayTitle == $1.displayTitle ? $0.id > $1.id : $0.displayTitle > $1.displayTitle }
    }

    private func captureRow(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack {
                Label(title, systemImage: "photo").lineLimit(1)
                Spacer()
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
            }
            .padding(10)
            .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 7))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func matches(_ value: String) -> Bool { query.isEmpty || value.localizedCaseInsensitiveContains(query) }

    private func refresh() async {
        await instrument.reloadCanvases()
        await instrument.loadGallery()
    }

    private func preview(_ item: GalleryCapture) async {
        guard let raw = item.links?.download?.href, let path = URLComponents(string: raw)?.percentEncodedPath,
              path.hasPrefix("/api/v2/") else { return }
        do {
            let data = try await instrument.api.bytes(instrument.api.devicePath(path))
            selectedImage = NSImage(data: data)
            selectedName = item.filename ?? item.id
            showPreview = selectedImage != nil
        } catch { instrument.galleryError = error.localizedDescription }
    }
}

private struct CanvasCard: View {
    let item: CanvasSummary
    let preview: URL
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 0) {
                GeometryReader { geometry in
                    AsyncImage(url: preview) { phase in
                        if let image = phase.image {
                            image.resizable().scaledToFill()
                        } else {
                            Image(systemName: "square.grid.2x2")
                                .font(.title2).foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, maxHeight: .infinity)
                        }
                    }
                    .frame(width: geometry.size.width, height: geometry.size.height)
                    .clipped()
                }
                .aspectRatio(1.65, contentMode: .fit)
                .background(.primary.opacity(0.06))
                .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    Text(item.displayTitle)
                        .font(.callout.weight(.semibold))
                        .lineLimit(1)
                    HStack {
                        if let cells = item.cells {
                            Text(cells == 1 ? "1 tile" : "\(cells) tiles").monospacedDigit()
                        }
                        Spacer()
                        Label(item.refined == true ? "Slide" : "Tiles",
                              systemImage: item.refined == true ? "photo" : "square.grid.2x2")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                .padding(10)
            }
            .background(Color(nsColor: .controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(hovering ? Color.microworldAccent : .primary.opacity(0.12))
            }
            .contentShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(item.id)
        .contextMenu {
            Button("Copy Canvas ID") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(item.id, forType: .string)
            }
        }
    }
}

struct NativeSettings: View {
    let instrument: InstrumentStore
    @State private var quality = 90
    @State private var mjpegQuality = 75
    @State private var width = 640
    @State private var height = 480
    @State private var exposure = 0.0
    @State private var analogGain = 1.0
    @State private var digitalGain = 1.0
    @State private var framerate = 30.0
    @State private var redGain = 1.0
    @State private var blueGain = 1.0
    @State private var cameraLoaded = false
    @State private var piCamera = false
    @State private var settingsError = ""
    @State private var confirm = false

    var body: some View {
        Form {
            Section("Connection and Storage") {
                LabeledContent("Microscope", value: instrument.api.connection.address.display)
                LabeledContent("Device ID", value: instrument.api.connection.id)
                LabeledContent("PC Worker", value: instrument.api.connection.openmhs ? "Bundled · port 18766" : "MicroWorld scan extension unavailable")
                LabeledContent("Workspace") {
                    Button("Show in Finder") { NSWorkspace.shared.open(instrument.api.connection.workspace) }
                }
            }
            CameraMappingSettings(instrument: instrument)
            Section("Camera") {
                TextField("JPEG quality", value: $quality, format: .number)
                TextField("MJPEG quality", value: $mjpegQuality, format: .number)
                TextField("Stream width", value: $width, format: .number)
                TextField("Stream height", value: $height, format: .number)
                if piCamera {
                    TextField("Shutter (µs)", value: $exposure, format: .number)
                    TextField("Analog gain", value: $analogGain, format: .number)
                    TextField("Digital gain", value: $digitalGain, format: .number)
                    TextField("Frames per second", value: $framerate, format: .number)
                    TextField("Red gain", value: $redGain, format: .number)
                    TextField("Blue gain", value: $blueGain, format: .number)
                }
                HStack {
                    Button("Reload Settings") { Task { await load() } }
                    Button("Apply Camera Settings") { Task { await apply() } }
                        .disabled(!cameraLoaded || !instrument.canControl)
                }
                if !settingsError.isEmpty { Text(settingsError).foregroundStyle(.secondary) }
            }
            Section("Stage") {
                LabeledContent("Position units", value: "Motor steps")
                if instrument.api.connection.openmhs {
                    Button("Estimate Camera–Stage Scale") { Task { await instrument.calibrateScale() } }
                        .disabled(!instrument.canControl)
                    if !instrument.scaleDescription.isEmpty { Text(instrument.scaleDescription) }
                }
                Button("Zero Stage…", role: .destructive) { confirm = true }
                    .disabled(!instrument.canControl)
            }
        }
        .formStyle(.grouped)
        .task { await load() }
        .confirmationDialog("Reset the stage coordinates to zero?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Zero Stage", role: .destructive) {
                Task {
                    await instrument.perform("Zeroing stage…") {
                        try await instrument.api.hardwareIdle()
                        _ = try await instrument.api.run("/api/v2/actions/stage/zero", body: [:])
                        await instrument.refreshState()
                    }
                }
            }
        } message: {
            Text("Saved Canvases refer to the current stage origin.")
        }
    }

    private func load() async {
        do {
            let camera: CameraSettings = try await instrument.api.read("/api/v2/instrument/settings/camera")
            quality = camera.jpeg_quality ?? quality
            mjpegQuality = camera.mjpeg_quality ?? mjpegQuality
            if let size = camera.stream_resolution, size.count == 2 { width = size[0]; height = size[1] }
            piCamera = camera.picamera != nil
            if let sensor = camera.picamera {
                exposure = sensor.shutter_speed ?? exposure
                analogGain = sensor.analog_gain ?? analogGain
                digitalGain = sensor.digital_gain ?? digitalGain
                framerate = sensor.framerate ?? framerate
                if let gains = sensor.awb_gains, gains.count == 2 { redGain = gains[0]; blueGain = gains[1] }
            }
            cameraLoaded = true
            settingsError = ""
        } catch { settingsError = error.localizedDescription }
    }

    private func apply() async {
        await instrument.perform("Applying camera settings…") {
            try await instrument.api.hardwareIdle()
            var camera: [String: Any] = [
                "jpeg_quality": min(100, max(1, quality)), "mjpeg_quality": min(100, max(1, mjpegQuality)),
                "stream_resolution": [max(1, width), max(1, height)],
            ]
            if piCamera {
                camera["picamera"] = ["shutter_speed": exposure, "analog_gain": analogGain, "digital_gain": digitalGain,
                                      "framerate": framerate, "awb_gains": [redGain, blueGain]]
            }
            _ = try await instrument.api.writeDevice("/api/v2/instrument/settings", body: ["camera": camera], method: "PUT")
            await load()
        }
    }
}
