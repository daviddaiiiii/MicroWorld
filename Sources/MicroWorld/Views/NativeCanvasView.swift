import SwiftUI
import UniformTypeIdentifiers

struct NativeCanvasView: View {
    @State var canvas: CanvasStore
    let instrument: InstrumentStore
    let close: () -> Void
    let trashed: () -> Void
    let extend: () -> Void

    @State private var viewportReset = 0
    @State private var drawing = false
    @State private var roiTool = ROITool.rectangle
    @State private var sourceROI = false
    @State private var importImage = false
    @State private var inspector = true
    @State private var showDelete = false
    @State private var showTrash = false
    @State private var dragOffset = CGSize.zero
    @State private var fovOffset = CGSize.zero
    @State private var sampleConfirmed = false
    @State private var pendingPlacement: Placement?
    @State private var showPlacement = false

    struct Placement {
        let intent: String
        var x: Double?
        var y: Double?
    }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            if drawing {
                HStack(spacing: 12) {
                    Picker("Shape", selection: $roiTool) { ForEach(ROITool.allCases) { Text($0.rawValue).tag($0) } }
                        .fixedSize()
                    Picker("Role", selection: $sourceROI) {
                        Text("Target").tag(false)
                        Text("Source").tag(true)
                    }
                    .pickerStyle(.segmented)
                    .fixedSize()
                    Text(roiTool == .polygon ? "Click corners, then double-click to close." : "Drag on the Slide to mark a region.")
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Clear ROI") { Task { await canvas.share(nil) } }
                        .disabled(canvas.target == nil || canvas.syncingROI)
                    Button("Done") { drawing = false }
                }
                .padding(10)
                Divider()
            }
            HStack(spacing: 0) {
                surface
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .layoutPriority(1)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .padding(12)
                if inspector {
                    Divider()
                    inspectorView.frame(width: 230)
                }
            }
            Divider()
            HStack {
                if canvas.live || canvas.busy { ProgressView().controlSize(.small) }
                Text(status)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .help(status)
                    .foregroundStyle(canvas.error.isEmpty ? Color.secondary : Color.orange)
                Spacer()
                if let doc = canvas.document {
                    Text("\(doc.tiles.count) tiles").monospacedDigit().foregroundStyle(.secondary)
                }
            }
            .font(.callout)
            .padding(.horizontal, 12)
            .padding(.vertical, 4)
            .frame(minHeight: 34)
        }
        .font(.body)
        .task { await canvas.watch() }
        .task(id: canvas.zoom > 1 ? canvas.document?.revision : nil) { await canvas.loadPyramid() }
        .fileImporter(isPresented: $importImage, allowedContentTypes: [.image]) { result in
            Task { await importFile(result) }
        }
        .confirmationDialog("Remove selected tile?", isPresented: $showDelete, titleVisibility: .visible) {
            Button("Remove from Canvas") { delete(storage: "keep") }
            Button("Move tile to Recovery Trash", role: .destructive) { delete(storage: "trash") }
        }
        .confirmationDialog("Move this Canvas to Recovery Trash?", isPresented: $showTrash, titleVisibility: .visible) {
            Button("Move Canvas to Recovery Trash", role: .destructive) {
                Task {
                    if await canvas.trash() { trashed() }
                }
            }
        }
        .confirmationDialog("Use the current specimen for this Canvas?", isPresented: $showPlacement, titleVisibility: .visible) {
            Button("Confirm and Continue") {
                sampleConfirmed = true
                if let pendingPlacement { runPlacement(pendingPlacement) }
                pendingPlacement = nil
            }
            Button("Cancel", role: .cancel) { pendingPlacement = nil }
        } message: {
            Text("Confirm the specimen, objective and stage zero match this Canvas before using its saved positions.")
        }
    }

    private var toolbar: some View {
        HStack(spacing: 9) {
            Button(action: close) { Label("Camera", systemImage: "chevron.left") }
                .help("Return to the live camera")
            Divider().frame(height: 20)
            Picker("Canvas view", selection: Binding(get: { canvas.showTiles }, set: { tiles in
                drawing = false
                if tiles { canvas.showTiles = true }
                else { Task { await canvas.selectSlide() } }
            })) {
                Text("Slide").tag(false)
                Text("Tiles").tag(true)
            }
            .pickerStyle(.segmented)
            .fixedSize()
            .disabled(canvas.live || canvas.busy)
            Button {
                drawing.toggle()
            } label: { Label("Mark ROI", systemImage: "viewfinder") }
                .disabled(canvas.showTiles || canvas.image == nil || canvas.live)
            Spacer()
            Button { canvas.zoom = max(0.1, canvas.zoom / 1.35) } label: { Image(systemName: "minus.magnifyingglass") }
                .help("Zoom out")
            Button { canvas.zoom = 1; viewportReset += 1 } label: { Text("Fit") }
            Button { canvas.zoom = min(64, canvas.zoom * 1.35) } label: { Image(systemName: "plus.magnifyingglass") }
                .help("Zoom in")
            Menu {
                Button("Continue Scan…", action: extend).disabled(!canvas.editable || !instrument.canControl)
                Button("Refine Slide") { Task { await canvas.refine() } }.disabled(!canvas.editable)
                Button("Add Image…") { importImage = true }.disabled(!canvas.editable)
                Button("Retake Target ROI…") { place(Placement(intent: "roi")) }
                    .disabled(canvas.target == nil || !canvas.editable || !instrument.canControl)
                Button("Capture Current FOV…") { place(Placement(intent: "capture-current")) }
                    .disabled(!canvas.editable || !instrument.canControl || instrument.position == nil)
                Toggle("Live FOV", isOn: Binding(get: { canvas.showFOV }, set: { canvas.showFOV = $0 }))
                Divider()
                Button("Undo Tile Edit") { Task { await canvas.undo() } }.disabled(!canvas.canUndo)
                Button("Redo Tile Edit") { Task { await canvas.redo() } }.disabled(!canvas.canRedo)
                Button("Move Canvas to Recovery Trash…", role: .destructive) { showTrash = true }
                    .disabled(!canvas.editable)
            } label: { Image(systemName: "ellipsis.circle").accessibilityLabel("Canvas actions") }
            Button { inspector.toggle() } label: { Image(systemName: "sidebar.right") }.help("Toggle Layers")
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 16)
        .frame(height: 46)
    }

    @ViewBuilder private var surface: some View {
        if canvas.showTiles, let doc = canvas.document, let bounds = doc.bounds {
            tileSurface(doc, bounds: bounds)
        } else if let image = canvas.image {
            NativeImageSurface(image: image, zoom: canvas.zoom, interactiveZoom: $canvas.zoom, resetViewport: viewportReset, drawing: drawing && !canvas.syncingROI, tool: roiTool,
                               target: canvas.target, source: canvas.source, pyramid: canvas.pyramid) { selected in
                Task { await canvas.share(selected, asSource: sourceROI) }
            }
            .overlay(alignment: .bottomTrailing) {
                if canvas.showFOV, let frame = instrument.camera.frame {
                    Image(nsImage: frame).resizable().scaledToFit().frame(width: 132)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.mint.opacity(0.6)))
                        .padding(12).allowsHitTesting(false)
                }
            }
        } else {
            ZStack {
                Color.black
                VStack(spacing: 12) {
                    ProgressView().tint(.white)
                    Text(canvas.error.isEmpty ? "Opening Canvas…" : canvas.error)
                        .foregroundStyle(.white).multilineTextAlignment(.center)
                }
                .padding()
            }
        }
    }

    private func tileSurface(_ doc: CanvasDocument, bounds: CanvasBounds) -> some View {
        CanvasViewport(zoom: $canvas.zoom, reset: viewportReset) { viewport in
            let scale = max(0.01, min(viewport.width / max(1, bounds.w), viewport.height / max(1, bounds.h)) * canvas.zoom)
            Group {
                ZStack(alignment: .topLeading) {
                    Color.black
                    ForEach(doc.tiles.filter { $0.hidden != true }) { tile in
                        let chosen = tile.id == canvas.selected
                        let rect = CGRect(x: (tile.x - bounds.x) * scale, y: (tile.y - bounds.y) * scale,
                                          width: tile.w * scale, height: tile.h * scale)
                        AsyncImage(url: canvas.tileURL(tile, sharp: chosen || canvas.zoom > 2)) { picture in
                            picture.resizable().interpolation(.medium)
                        } placeholder: { Color.white.opacity(0.06) }
                        .opacity(tile.opacity ?? 1)
                        .overlay { if chosen { Rectangle().strokeBorder(Color.mint, lineWidth: 2) } }
                        .frame(width: max(1, rect.width), height: max(1, rect.height))
                        .offset(x: rect.minX + (chosen ? dragOffset.width : 0),
                                y: rect.minY + (chosen ? dragOffset.height : 0))
                        .onTapGesture { canvas.selected = tile.id }
                        .gesture(DragGesture(minimumDistance: 4)
                            .onChanged { change in
                                if canvas.editable { canvas.selected = tile.id; dragOffset = change.translation }
                            }
                            .onEnded { change in
                                dragOffset = .zero
                                if canvas.editable {
                                    Task {
                                        await canvas.setLayer(tile.id) { layer in
                                            layer.x += change.translation.width / scale
                                            layer.y += change.translation.height / scale
                                            layer.manual = true
                                        }
                                    }
                                }
                            })
                    }
                    if canvas.showFOV, let position = instrument.position,
                       let field = doc.reference?.field(at: position),
                       let frame = instrument.camera.frame {
                        Image(nsImage: frame).resizable()
                            .frame(width: field.width * scale, height: field.height * scale)
                            .overlay { Rectangle().strokeBorder(.mint, lineWidth: 2) }
                            .overlay(alignment: .topTrailing) {
                                Button { place(Placement(intent: "capture-current")) } label: { Image(systemName: "plus") }
                                    .buttonStyle(.borderedProminent)
                                    .disabled(!canvas.editable || !instrument.canControl)
                                    .help("Capture tile at current FOV")
                            }
                            .offset(x: (field.minX - bounds.x) * scale + fovOffset.width,
                                    y: (field.minY - bounds.y) * scale + fovOffset.height)
                            .gesture(DragGesture(minimumDistance: 8)
                                .onChanged { if canvas.editable && instrument.canControl { fovOffset = $0.translation } }
                                .onEnded { delta in
                                    fovOffset = .zero
                                    if canvas.editable && instrument.canControl {
                                        place(Placement(intent: "fov-move",
                                                        x: field.minX + delta.translation.width / scale,
                                                        y: field.minY + delta.translation.height / scale))
                                    }
                                })
                    }
                }
                .frame(width: max(1, bounds.w * scale), height: max(1, bounds.h * scale))
                .frame(minWidth: viewport.width, minHeight: viewport.height)
                .clipped()
            }
        }
        .background(.black)
    }

    private var inspectorView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Layers").font(.headline)
                    Spacer()
                    if canvas.selected != nil { Button("Deselect") { canvas.selected = nil }.buttonStyle(.link) }
                }
                if let doc = canvas.document {
                    ForEach(doc.tiles.reversed()) { tile in
                        HStack(spacing: 8) {
                            Button {
                                Task { await canvas.setLayer(tile.id) { $0.hidden = !($0.hidden ?? false) } }
                            } label: { Image(systemName: tile.hidden == true ? "eye.slash" : "eye") }
                                .help(tile.hidden == true ? "Show tile" : "Hide tile")
                                .disabled(!canvas.editable)
                            Button(tile.title) { canvas.selected = tile.id }
                                .foregroundStyle(canvas.selected == tile.id ? Color.accentColor : .primary)
                                .lineLimit(1)
                                .help(tile.title)
                            Spacer(minLength: 0)
                            Menu {
                                Button("Move Up") { move(tile, by: 1) }
                                Button("Move Down") { move(tile, by: -1) }
                                Button("Remove…", role: .destructive) { canvas.selected = tile.id; showDelete = true }
                            } label: { Image(systemName: "ellipsis").accessibilityLabel("Tile actions") }
                                .menuStyle(.borderlessButton)
                                .fixedSize()
                                .disabled(!canvas.editable)
                        }
                        .buttonStyle(.borderless)
                        .padding(5)
                        .background(canvas.selected == tile.id ? Color.accentColor.opacity(0.12) : .clear,
                                    in: RoundedRectangle(cornerRadius: 5))
                        .onDrag { NSItemProvider(object: tile.id as NSString) }
                        .dropDestination(for: String.self) { ids, _ in
                            guard canvas.editable, let source = ids.first, source != tile.id,
                                  var order = canvas.document?.tiles.map(\.id),
                                  let from = order.firstIndex(of: source),
                                  let to = order.firstIndex(of: tile.id) else { return false }
                            order.remove(at: from)
                            order.insert(source, at: to)
                            Task { await canvas.reorder(order) }
                            return true
                        }
                    }
                    Divider()
                    AdjustmentView(canvas: canvas)
                }
            }
            .padding(12)
        }
        .background(.regularMaterial)
    }

    private var status: String {
        if instrument.hardware?.busy == true, instrument.hardware?.scan_session_id == canvas.id {
            switch instrument.hardware?.focus_phase {
            case "video_scout": return "Surveying the sample with live video…"
            case "scout_verify": return "Checking uncertain fields…"
            case "focus_map": return "Building the focus map…"
            case "scout_capture": return "Capturing high-resolution tiles…"
            case "scout_retake": return "Checking focus and retaking soft tiles…"
            default: break
            }
        }
        return [canvas.error, canvas.notice, canvas.roiStatus].first { !$0.isEmpty } ?? "Canvas saved"
    }

    private func delete(storage: String) {
        guard let selected = canvas.selected else { return }
        Task { await canvas.mutate("delete", fields: ["tile_ids": [selected], "storage": storage]) }
    }

    private func move(_ tile: CanvasLayer, by delta: Int) {
        guard var order = canvas.document?.tiles.map(\.id), let index = order.firstIndex(of: tile.id),
              order.indices.contains(index + delta) else { return }
        order.swapAt(index, index + delta)
        Task { await canvas.reorder(order) }
    }

    private func place(_ placement: Placement) {
        guard instrument.canControl else { return }
        if sampleConfirmed { runPlacement(placement) }
        else { pendingPlacement = placement; showPlacement = true }
    }

    private func runPlacement(_ placement: Placement) {
        Task {
            await canvas.recapture(intent: placement.intent, x: placement.x, y: placement.y,
                                   position: instrument.position, confirmed: sampleConfirmed)
        }
    }

    private func importFile(_ result: Result<URL, Error>) async {
        do {
            let url = try result.get()
            let access = url.startAccessingSecurityScopedResource()
            defer { if access { url.stopAccessingSecurityScopedResource() } }
            let data = try Data(contentsOf: url)
            guard data.count <= 8 * 1024 * 1024 else { throw RuntimeError.message("Use an image smaller than 8 MB.") }
            await canvas.mutate("import", fields: ["image": "data:image/" + url.pathExtension + ";base64," + data.base64EncodedString()])
        } catch { canvas.error = error.localizedDescription }
    }
}

private struct AdjustmentView: View {
    let canvas: CanvasStore
    @State private var draft = CanvasAdjustments()
    @State private var opacity = 1.0

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(canvas.selectedLayer?.title ?? "All tiles").font(.headline)
            slider("Exposure", value: $draft.exposure, range: -3...3)
            slider("Contrast", value: $draft.contrast, range: 0.25...3)
            slider("Gamma", value: $draft.gamma, range: 0.25...3)
            Picker("Color map", selection: $draft.colormap) {
                Text("Original").tag("natural")
                Text("Grayscale").tag("gray")
                Text("Viridis").tag("viridis")
                Text("Magma").tag("magma")
            }
            Toggle("Enhance", isOn: $draft.enhance)
            if canvas.selectedLayer != nil { slider("Opacity", value: $opacity, range: 0...1) }
            HStack {
                Button("Reset") { draft = CanvasAdjustments(); opacity = 1 }
                Spacer()
                Button("Apply") {
                    let value = draft, alpha = opacity
                    Task {
                        if let selected = canvas.selected {
                            await canvas.setLayer(selected) { $0.adjustments = value; $0.opacity = alpha }
                        } else { await canvas.adjust(value) }
                    }
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .disabled(!canvas.editable)
        .onAppear(perform: reload)
        .onChange(of: canvas.selected) { _, _ in reload() }
        .onChange(of: canvas.document?.revision) { _, _ in reload() }
    }

    private func reload() {
        draft = canvas.selectedLayer?.adjustments ?? canvas.document?.adjustments ?? CanvasAdjustments()
        opacity = canvas.selectedLayer?.opacity ?? 1
    }

    private func slider(_ label: String, value: Binding<Double>, range: ClosedRange<Double>) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(label)
                Spacer()
                Text(value.wrappedValue, format: .number.precision(.fractionLength(2))).monospacedDigit()
            }
            .font(.callout)
            Slider(value: value, in: range, step: 0.01).accessibilityLabel(label)
        }
    }
}
