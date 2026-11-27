import SwiftUI

struct InstrumentView: View {
    @Binding var tab: ToolTab
    @Binding var instrumentBusy: Bool
    let slideLink: SlideLink?
    @State private var instrument: InstrumentStore
    @State private var canvas: CanvasStore?
    @State private var scanSetup = false
    @State private var continuing: String?
    @State private var jogPending = false
    @State private var lastExternalScan: String?
    @State private var askToKeepCanvas = false
    @State private var recentCanvasID: String?

    init(tab: Binding<ToolTab>, instrumentBusy: Binding<Bool>, connection: Connection, slideLink: SlideLink?) {
        _tab = tab
        _instrumentBusy = instrumentBusy
        self.slideLink = slideLink
        _instrument = State(initialValue: InstrumentStore(connection: connection))
    }

    var body: some View {
        VStack(spacing: 0) {
            switch tab {
            case .observe:
                if let canvas {
                    NativeCanvasView(canvas: canvas, instrument: instrument,
                                     close: { returnToCamera(canvas) },
                                     trashed: { finishClosing(canvas, keep: false) },
                                     extend: { setupScan(continuing: canvas.id) })
                        .id(canvas.id)
                } else {
                    observe
                }
            case .gallery:
                NativeGallery(instrument: instrument, openCanvas: openCanvas)
            case .navigate:
                HSplitView {
                    NativeCamera(instrument: instrument, enableMappedMovement: true)
                        .frame(minWidth: 400)
                    StagePanel(instrument: instrument)
                        .frame(minWidth: 250, idealWidth: 270, maxWidth: 320)
                }
            case .capture:
                NativeCapture(instrument: instrument)
            case .settings:
                NativeSettings(instrument: instrument)
            }
            if !instrument.error.isEmpty || !instrument.notice.isEmpty {
                HStack(spacing: 8) {
                    if instrument.busy { ProgressView().controlSize(.small) }
                    if !instrument.error.isEmpty {
                        Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
                    }
                    Text(instrument.error.isEmpty ? instrument.notice : instrument.error)
                        .lineLimit(2)
                        .textSelection(.enabled)
                    Spacer()
                    if !instrument.error.isEmpty {
                        Button { instrument.error = "" } label: { Image(systemName: "xmark") }
                            .buttonStyle(.borderless)
                            .accessibilityLabel("Dismiss message")
                    }
                }
                .padding(9)
                .background(.regularMaterial)
            }
        }
        .background {
            StageKeyboardControl(enabled: (tab == .observe && canvas == nil || tab == .navigate)
                                 && !scanSetup && !askToKeepCanvas) { x, y, z in
                guard instrument.canControl, !jogPending else { return }
                let xyStep = max(1, min(20_000, UserDefaults.standard.object(forKey: "stageStepXY") as? Int ?? 100))
                let zStep = max(1, min(5_000, UserDefaults.standard.object(forKey: "stageStepZ") as? Int ?? 25))
                jogPending = true
                Task {
                    await instrument.move(StagePosition(x: x * xyStep, y: y * xyStep, z: z * zStep))
                    jogPending = false
                }
            }
        }
        .task { await instrument.watch() }
        .task {
            await instrument.refreshCapabilities()
            do { try await instrument.refreshCalibration() }
            catch { instrument.calibrationError = error.localizedDescription }
        }
        .onChange(of: instrument.busy) { _, busy in instrumentBusy = busy }
        .task(id: tab) {
            if tab == .observe || tab == .navigate || tab == .capture || tab == .settings {
                await instrument.camera.watch(instrument.api)
            }
        }
        .onChange(of: slideLink?.requestID, initial: true) { _, _ in
            if let slideLink { openCanvas(slideLink.sessionID) }
        }
        .onChange(of: instrument.activeScan) { _, id in
            if let id, id != lastExternalScan {
                lastExternalScan = id
                if tab != .capture || !instrument.busy {
                    openCanvas(id, acquiring: instrument.hardware?.busy == true)
                }
            }
        }
        .confirmationDialog("Keep this scan?", isPresented: $askToKeepCanvas, titleVisibility: .visible) {
            Button("Save Scan") {
                if let canvas { finishClosing(canvas, keep: true) }
            }
            Button("Move to Recovery Trash", role: .destructive) {
                guard let canvas else { return }
                Task {
                    if await canvas.trash() { finishClosing(canvas, keep: false) }
                }
            }
            .disabled(canvas?.editable != true)
            Button("Cancel", role: .cancel) {}
        } message: {
            if canvas?.editable != true { Text("You can move the scan to Trash after processing finishes.") }
        }
        .sheet(isPresented: $scanSetup) {
            ScanSetup(continuing: continuing) { columns, rows, explore in
                let previous = continuing
                scanSetup = false
                Task { await instrument.scan(columns: columns, rows: rows, explore: explore,
                                             continuing: previous) }
            }
        }
    }

    private var observe: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Label(instrument.camera.status, systemImage: "circle.fill")
                    .font(.caption.weight(.medium))
                    .lineLimit(1)
                    .foregroundStyle(instrument.camera.status == "Live" ? Color.microworldAccent : .secondary)
                Spacer()
                Button {
                    tab = .navigate
                } label: { Label("Stage", systemImage: "move.3d") }
                Button {
                    Task { await instrument.focus(2000) }
                } label: { Label("Fast AF", systemImage: "scope") }
                .disabled(!instrument.canControl)
                Button {
                    Task { await instrument.takeSnapshot() }
                } label: { Label("Snapshot", systemImage: "camera") }
                .disabled(instrument.camera.frame == nil || instrument.busy)
                if instrument.api.connection.openmhs {
                    Menu {
                        ForEach(instrument.canvases.prefix(8)) { item in
                            Button(item.displayTitle) { openCanvas(item.id) }
                                .help(item.id)
                        }
                        if !instrument.canvases.isEmpty { Divider() }
                        Button("Browse Gallery") { tab = .gallery }
                    } label: { Label("Recent", systemImage: "clock") }
                    Button {
                        setupScan()
                    } label: { Label("New Scan", systemImage: "square.grid.2x2") }
                    .buttonStyle(.borderedProminent)
                    .disabled(!instrument.canControl)
                }
            }
            .padding(.horizontal, 18)
            .frame(height: 48)
            NativeCamera(instrument: instrument)
                .overlay(alignment: .bottomTrailing) {
                    if let item = recentCanvas {
                        Button { openCanvas(item.id) } label: {
                            AsyncImage(url: instrument.api.localPath(
                                "/mosaic/\(item.id)/preview.jpg?refined=\(item.refined == true)"
                            )) { phase in
                                if let image = phase.image {
                                    image.resizable().scaledToFit()
                                } else {
                                    Label("Latest Scan", systemImage: "square.grid.2x2")
                                        .font(.caption)
                                }
                            }
                            .frame(width: 168, height: 110)
                            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                            .overlay { RoundedRectangle(cornerRadius: 10).strokeBorder(.white.opacity(0.35)) }
                        }
                        .buttonStyle(.plain)
                        .help("Open latest scan")
                        .accessibilityLabel("Open latest scan")
                        .padding(25)
                    }
                }
        }
    }

    private var recentCanvas: CanvasSummary? {
        let scans = instrument.canvases.filter { ($0.cells ?? 0) > 0 }
        return scans.first(where: { $0.id == recentCanvasID }) ?? scans.first
    }

    private func returnToCamera(_ canvas: CanvasStore) {
        if instrument.pendingCanvasDecisions.contains(canvas.id),
           !(instrument.hardware?.busy == true && instrument.hardware?.scan_session_id == canvas.id) {
            askToKeepCanvas = true
        } else {
            self.canvas = nil
            Task { await instrument.reloadCanvases() }
        }
    }

    private func finishClosing(_ canvas: CanvasStore, keep: Bool) {
        instrument.resolveNewCanvas(canvas.id)
        if keep {
            recentCanvasID = canvas.id
        } else {
            if recentCanvasID == canvas.id { recentCanvasID = nil }
            instrument.canvases.removeAll { $0.id == canvas.id }
        }
        self.canvas = nil
        Task { await instrument.reloadCanvases() }
    }

    private func openCanvas(_ id: String) { openCanvas(id, acquiring: false) }

    private func openCanvas(_ id: String, acquiring: Bool) {
        guard id.wholeMatch(of: #/[a-zA-Z0-9_-]{1,120}/#) != nil else { return }
        if canvas?.id != id { canvas = CanvasStore(id: id, api: instrument.api, acquiring: acquiring) }
        tab = .observe
    }

    private func setupScan(continuing id: String? = nil) {
        continuing = id
        scanSetup = true
    }
}

private struct ScanSetup: View {
    let continuing: String?
    let start: (Int, Int, Bool) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var shape = "2×2"
    @State private var columns = 2
    @State private var rows = 2

    private var modes: [String] {
        ["1×1", "2×2", "3×3", "4×4", "Full", "Custom"]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(continuing == nil ? "New Canvas" : "Continue Scan").font(.title2.bold())
            if let continuing { LabeledContent("Canvas", value: continuing) }
            Picker("Area", selection: $shape) {
                ForEach(modes, id: \.self) { Text($0).tag($0) }
            }
            .pickerStyle(.segmented)
            if shape == "Custom" {
                Stepper("Columns: \(columns)", value: $columns, in: 1...20)
                Stepper("Rows: \(rows)", value: $rows, in: 1...20)
            }
            if shape == "Full" {
                Text("Search outward for sample boundaries. The scan can collect up to 200 tiles.")
                    .foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Start Scan") {
                    let fixed = Int(shape.prefix(1))
                    start(shape == "Full" ? 6 : fixed ?? columns,
                          shape == "Full" ? 6 : fixed ?? rows, shape == "Full")
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 460)
    }
}

private struct NativeCamera: View {
    let instrument: InstrumentStore
    var enableMappedMovement = false
    @State private var clickToCenter = false
    @State private var frozen: NSImage?
    @State private var drawing = false
    @State private var tool = ROITool.rectangle
    @State private var asSource = false
    @State private var target: ROISelection?
    @State private var source: ROISelection?
    @State private var saving = false
    @State private var message = ""

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                if instrument.api.connection.openmhs {
                    Button(drawing ? "Done" : "Mark ROI", systemImage: "viewfinder") {
                        if drawing { drawing = false }
                        else { frozen = instrument.camera.frame; drawing = frozen != nil }
                    }
                    .disabled(instrument.camera.frame == nil || saving)
                }
                if enableMappedMovement, instrument.acquisitionCapabilities?.moveInPixels != nil {
                    Toggle("Click to Center", isOn: $clickToCenter)
                        .disabled(instrument.calibrationJSON == nil)
                }
                if drawing {
                    Picker("Shape", selection: $tool) {
                        ForEach(ROITool.allCases) { Text($0.rawValue).tag($0) }
                    }.fixedSize()
                    Picker("Role", selection: $asSource) { Text("Target").tag(false); Text("Source").tag(true) }
                        .pickerStyle(.segmented).fixedSize()
                }
                if frozen != nil {
                    Button("Live") { drawing = false; frozen = nil; target = nil; source = nil }
                        .disabled(saving)
                    Button("Clear ROI") { Task { await save(nil) } }.disabled(saving || target == nil)
                }
                if saving { ProgressView().controlSize(.small) }
                if !message.isEmpty { Text(message).lineLimit(1).foregroundStyle(.secondary) }
                Spacer()
            }
            .buttonStyle(.borderless)
            .padding(.horizontal, 18)
            .frame(minHeight: 36)
            Group {
              if let image = frozen ?? instrument.camera.frame {
                if frozen != nil {
                    NativeImageSurface(image: image, drawing: drawing, tool: tool, target: target, source: source) { roi in
                        Task { await save(roi, asSource: asSource) }
                    }
                } else {
                    GeometryReader { geometry in
                        let scale = min(geometry.size.width / max(1, image.size.width),
                                        geometry.size.height / max(1, image.size.height))
                        let width = image.size.width * scale
                        let height = image.size.height * scale
                        Image(nsImage: image).resizable()
                            .frame(width: width, height: height)
                            .contentShape(Rectangle())
                            .onTapGesture(coordinateSpace: .local) { location in
                                guard clickToCenter, instrument.canControl, width > 0, height > 0 else { return }
                                let pixels = image.representations.first
                                let dx = (location.x / width - 0.5) * Double(pixels?.pixelsWide ?? Int(image.size.width))
                                let dy = (location.y / height - 0.5) * Double(pixels?.pixelsHigh ?? Int(image.size.height))
                                Task { await instrument.moveInCameraPixels(x: dx, y: dy) }
                            }
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .accessibilityLabel("Live camera image")
                    }
                    .background(.black)
                }
              } else {
                ZStack {
                    Color.black
                    VStack(spacing: 12) {
                        ProgressView().tint(.white)
                        Text(instrument.camera.status).foregroundStyle(.white)
                    }
                }
              }
            }
            .clipShape(RoundedRectangle(cornerRadius: 11))
            .overlay { RoundedRectangle(cornerRadius: 11).strokeBorder(.primary.opacity(0.12)).allowsHitTesting(false) }
            .padding(.horizontal, 16)
            .padding(.bottom, 16)
        }
    }

    private func save(_ roi: ROISelection?, asSource: Bool = false) async {
        guard let frozen, !saving else { return }
        let nextTarget = asSource ? target : roi
        let nextSource = asSource ? roi : (roi == nil ? nil : source)
        saving = true
        defer { saving = false }
        do {
            try await instrument.api.shareROI(frame: frozen, target: nextTarget, source: nextSource)
            target = nextTarget
            source = nextSource
            message = nextTarget == nil ? "Selection cleared" : "Selection available in Codex"
        } catch { message = error.localizedDescription }
    }
}

struct StagePanel: View {
    let instrument: InstrumentStore
    @AppStorage("stageStepXY") private var stepXY = 100
    @AppStorage("stageStepZ") private var stepZ = 25
    @AppStorage("autofocusSpan") private var focusSpan = 2000
    @State private var target = StagePosition.zero
    @State private var targetReady = false

    var body: some View {
        Form {
            Section("Current position") {
                if let p = instrument.position {
                    LabeledContent("X") { Text(p.x, format: .number).monospacedDigit() }
                    LabeledContent("Y") { Text(p.y, format: .number).monospacedDigit() }
                    LabeledContent("Z") { Text(p.z, format: .number).monospacedDigit() }
                } else { Text("Waiting for position…").foregroundStyle(.secondary) }
            }
            Section("Jog") {
                Text("Arrow keys move XY. Page Up / Page Down move Z.").font(.caption).foregroundStyle(.secondary)
                TextField("XY step", value: $stepXY, format: .number)
                TextField("Z step", value: $stepZ, format: .number)
                HStack {
                    jog("X−", x: -xy); jog("X+", x: xy)
                    jog("Y−", y: -xy); jog("Y+", y: xy)
                }
                HStack { jog("Z−", z: -z); jog("Z+", z: z) }
            }
            Section("Go to") {
                TextField("X", value: $target.x, format: .number)
                TextField("Y", value: $target.y, format: .number)
                TextField("Z", value: $target.z, format: .number)
                HStack {
                    Button("Use Current") { if let p = instrument.position { target = p; targetReady = true } }
                        .disabled(instrument.position == nil)
                    Button("Move") { let next = target; Task { await instrument.move(next, absolute: true) } }
                        .disabled(!instrument.canControl || !targetReady)
                }
            }
            Section("Focus and calibration") {
                TextField("AF span", value: $focusSpan, format: .number)
                Button("Fast Autofocus") { Task { await instrument.focus(max(1, min(focusSpan, 10_000))) } }
                    .disabled(!instrument.canControl)
                if instrument.api.connection.openmhs {
                    Button("Estimate Scale") { Task { await instrument.calibrateScale() } }
                        .disabled(!instrument.canControl)
                    if !instrument.scaleDescription.isEmpty {
                        Text(instrument.scaleDescription).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .onChange(of: instrument.position, initial: true) { _, current in
            if !targetReady, let current { target = current; targetReady = true }
        }
    }

    private var xy: Int { min(20_000, max(1, stepXY)) }
    private var z: Int { min(5_000, max(1, stepZ)) }

    private func jog(_ label: String, x: Int = 0, y: Int = 0, z: Int = 0) -> some View {
        Button { Task { await instrument.move(StagePosition(x: x, y: y, z: z)) } } label: {
            Text(label).frame(maxWidth: .infinity)
        }
            .disabled(!instrument.canControl)
    }
}
