import SwiftUI
import UniformTypeIdentifiers

struct NativeCapture: View {
    enum Mode: String, CaseIterable { case image = "Single Image", scan = "Scan", smart = "Smart Stack" }
    let instrument: InstrumentStore
    @State private var capture = AdvancedCaptureOptions()
    @State private var scan = AdvancedScanOptions()
    @State private var mode = Mode.image
    @State private var confirm = false
    @AppStorage("advancedCaptureOptions") private var savedCapture = Data()
    @AppStorage("advancedScanOptions") private var savedScan = Data()

    var body: some View {
        HSplitView {
            CameraPreview(instrument: instrument, label: "Live capture preview")
                .frame(minWidth: 330)
            Form {
                Section("Acquisition") {
                    Picker("Mode", selection: $mode) {
                        Text(Mode.image.rawValue).tag(Mode.image)
                        if availableScan { Text(Mode.scan.rawValue).tag(Mode.scan) }
                        if instrument.acquisitionCapabilities?.smartStack != nil { Text(Mode.smart.rawValue).tag(Mode.smart) }
                    }
                    TextField("File name", text: $capture.filename)
                    Toggle("Temporary", isOn: $capture.temporary)
                    if mode == .image && !capture.bayer {
                        Toggle("Full resolution", isOn: $capture.fullResolution)
                    }
                    Toggle("Store Bayer data", isOn: $capture.bayer)
                    if capture.bayer {
                        Text("Bayer captures use the full sensor resolution.").foregroundStyle(.secondary)
                    } else {
                        Toggle("Resize capture", isOn: $capture.resize)
                        if capture.resize {
                            TextField("Width (px)", value: $capture.width, format: .number)
                            TextField("Height (px)", value: $capture.height, format: .number)
                        }
                    }
                }
                .disabled(instrument.busy)
                Section("Metadata") {
                    TextField("Tags", text: $capture.tags, prompt: Text("Comma separated"))
                    TextField("Notes", text: $capture.notes)
                    ForEach($capture.annotations) { $row in
                        HStack {
                            TextField("Key", text: $row.key)
                            TextField("Value", text: $row.value)
                            Button("Remove annotation", systemImage: "minus.circle") {
                                let id = row.id
                                capture.annotations.removeAll { $0.id == id }
                            }
                            .labelStyle(.iconOnly)
                            .buttonStyle(.borderless)
                        }
                    }
                    Button("Add Annotation", systemImage: "plus") { capture.annotations.append(CaptureAnnotation()) }
                }
                .disabled(instrument.busy)
                if mode != .image { scanFields.disabled(instrument.busy) }
                Section {
                    if let progress = instrument.actionProgress {
                        ProgressView(value: progress, total: 100)
                    } else if instrument.busy {
                        ProgressView()
                    }
                    if instrument.actionRoute != nil {
                        Button(instrument.stoppingAction ? "Stopping…" : "Stop Scan", role: .destructive) {
                            Task { await instrument.stopAdvancedScan() }
                        }
                        .disabled(instrument.stoppingAction)
                    } else {
                        Button(mode == .image ? "Capture to Microscope" : "Start \(mode.rawValue)") {
                            if mode == .image {
                                let options = capture
                                Task { await instrument.capture(options) }
                            } else { confirm = true }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(!instrument.canControl)
                        if mode == .image {
                            Button("Save Live Frame to Mac") { Task { await instrument.takeSnapshot() } }
                                .disabled(instrument.camera.frame == nil || instrument.busy)
                        }
                    }
                }
            }
            .formStyle(.grouped)
            .frame(minWidth: 320, idealWidth: 380, maxWidth: 470)
        }
        .onAppear {
            if let value = try? JSONDecoder().decode(AdvancedCaptureOptions.self, from: savedCapture) { capture = value }
            if let value = try? JSONDecoder().decode(AdvancedScanOptions.self, from: savedScan) { scan = value }
        }
        .onChange(of: capture) { _, value in
            if let data = try? JSONEncoder().encode(value) { savedCapture = data }
        }
        .onChange(of: scan) { _, value in
            if let data = try? JSONEncoder().encode(value) { savedScan = data }
        }
        .confirmationDialog("Start \(mode.rawValue)?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Start") {
                let options = scan, settings = capture, smart = mode == .smart
                Task { await instrument.advancedScan(options, capture: settings, smart: smart) }
            }
        } message: {
            Text("The stage will move using the selected X, Y and Z grid.")
        }
    }

    private var availableScan: Bool {
        instrument.acquisitionCapabilities?.mosaicScan != nil || instrument.acquisitionCapabilities?.stockScan != nil
    }

    private var scanFields: some View {
        Section("Scan and Z stack") {
            axisFields("Images per axis", value: $scan.grid)
            if mode == .scan && instrument.acquisitionCapabilities?.mosaicScan != nil {
                Toggle("Calculate XY stride automatically", isOn: $scan.automaticStride)
            }
            axisFields("Stride in motor steps", value: $scan.stride)
            Picker("Autofocus", selection: $scan.autofocus) {
                ForEach(AdvancedScanOptions.Focus.allCases, id: \.self) { Text($0.rawValue.capitalized).tag($0) }
            }
            if instrument.acquisitionCapabilities?.skipsEmptyFields == true {
                Toggle("Skip autofocus in empty fields", isOn: $scan.skipEmpty)
            }
            Picker("Traversal", selection: $scan.pattern) {
                ForEach(AdvancedScanOptions.Pattern.allCases, id: \.self) { Text($0.rawValue.capitalized).tag($0) }
            }
            Picker("Naming", selection: $scan.naming) {
                ForEach(AdvancedScanOptions.Naming.allCases, id: \.self) { Text($0.rawValue.capitalized).tag($0) }
            }
            if mode == .smart {
                TextField("Autofocus range (steps)", value: $scan.smartAutofocusRange, format: .number)
                TextField("Alignment range (steps)", value: $scan.smartAlignmentRange, format: .number)
                Text("Smart Stack needs Fast autofocus and multiple Z images.").foregroundStyle(.secondary)
            }
        }
    }

    private func axisFields(_ title: String, value: Binding<StagePosition>) -> some View {
        LabeledContent(title) {
            HStack {
                TextField("X", value: value.x, format: .number).accessibilityLabel("\(title), X")
                TextField("Y", value: value.y, format: .number).accessibilityLabel("\(title), Y")
                TextField("Z", value: value.z, format: .number).accessibilityLabel("\(title), Z")
            }
            .labelsHidden()
        }
    }
}

struct CameraMappingSettings: View {
    let instrument: InstrumentStore
    @State private var confirm = false

    var body: some View {
        Section("Camera–Stage Mapping") {
            if instrument.acquisitionCapabilities?.calibrateXY != nil ||
                instrument.acquisitionCapabilities?.calibrationData != nil {
                CameraPreview(instrument: instrument, label: "Live calibration preview")
                    .frame(height: 130)
                LabeledContent("Calibration", value: instrument.calibrationJSON == nil ? "No data loaded" : "Available")
                if !instrument.calibrationError.isEmpty { Text(instrument.calibrationError).foregroundStyle(.secondary) }
                if instrument.acquisitionCapabilities?.calibrateXY != nil {
                    Button("Auto-Calibrate Using Camera") { confirm = true }
                        .disabled(!instrument.canControl)
                }
                Button("Export Calibration JSON…", action: export)
                    .disabled(instrument.calibrationJSON == nil || instrument.busy)
            } else {
                Text(instrument.acquisitionCapabilities == nil ? "Checking device support…" : "Mapping extension unavailable")
                    .foregroundStyle(.secondary)
            }
        }
        .confirmationDialog("Recalibrate the camera and stage?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Start Calibration") { Task { await instrument.calibrateMapping() } }
        } message: {
            Text("The microscope will move the stage to measure the relationship between camera pixels and motor steps.")
        }
    }

    private func export() {
        guard let data = instrument.calibrationJSON else { return }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.json]
        panel.nameFieldStringValue = "csm_calibration.json"
        panel.begin { result in
            guard result == .OK, let url = panel.url else { return }
            do { try data.write(to: url, options: .atomic) }
            catch { instrument.error = error.localizedDescription }
        }
    }
}

struct CameraPreview: View {
    let instrument: InstrumentStore
    let label: String

    var body: some View {
        ZStack {
            Color.black
            if let frame = instrument.camera.frame {
                Image(nsImage: frame).resizable().scaledToFit().accessibilityLabel(label)
            } else {
                Text(instrument.camera.status).foregroundStyle(.white)
            }
        }
    }
}
