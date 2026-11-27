import AppKit
import Foundation
import Observation

@MainActor @Observable
final class InstrumentStore {
    let api: InstrumentAPI
    let camera = CameraFeed()
    var position: StagePosition?
    var hardware: AcquisitionStatus?
    var busy = false
    var error = ""
    var notice = ""
    var canvases: [CanvasSummary] = []
    var activeScan: String?
    var localCaptures: [URL] = []
    var remoteCaptures: [GalleryCapture] = []
    var galleryError = ""
    private var observedScan: String?
    private var continuingScan: String?
    private var pendingKey: String { "pendingCanvasDecisions." + api.connection.id }
    var pendingCanvasDecisions: Set<String> = []

    func markNewCanvas(_ id: String) {
        guard id != continuingScan else { return }
        pendingCanvasDecisions.insert(id)
        UserDefaults.standard.set(Array(pendingCanvasDecisions), forKey: pendingKey)
    }

    func resolveNewCanvas(_ id: String) {
        pendingCanvasDecisions.remove(id)
        UserDefaults.standard.set(Array(pendingCanvasDecisions), forKey: pendingKey)
    }

    init(connection: Connection) {
        api = InstrumentAPI(connection)
        pendingCanvasDecisions = Set(UserDefaults.standard.stringArray(forKey: pendingKey) ?? [])
    }

    var canControl: Bool {
        !busy && (!api.connection.openmhs || hardware?.busy == false)
    }

    func watch() async {
        var lastGalleryRefresh = Date.distantPast
        while !Task.isCancelled {
            if Date().timeIntervalSince(lastGalleryRefresh) >= 4 {
                await reloadCanvases()
                lastGalleryRefresh = Date()
            }
            await refreshState()
            try? await Task.sleep(for: .seconds(1))
        }
    }

    func refreshState() async {
        async let current: StagePosition? = try? api.read(InstrumentAPI.position)
        if api.connection.openmhs {
            let state: AcquisitionStatus? = try? await api.read(InstrumentAPI.scan + "/hardware")
            hardware = state
            if let state, let id = state.scan_session_id {
                if state.busy, state.operation == "scan" {
                    if observedScan != id && !canvases.contains(where: { $0.id == id }) {
                        markNewCanvas(id)
                    }
                    observedScan = id
                    activeScan = id
                } else if id == observedScan {
                    observedScan = nil
                    await reloadCanvases()
                }
            }
        }
        position = await current
    }

    func reloadCanvases() async {
        guard api.connection.openmhs else { return }
        struct Listing: Decodable { let sessions: [CanvasSummary] }
        do {
            let result: Listing = try await api.read("/mosaic/list", local: true)
            canvases = result.sessions.sorted { ($0.mtime ?? $0.id) > ($1.mtime ?? $1.id) }
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }

    func perform(_ name: String, _ operation: () async throws -> Void) async {
        guard !busy else { return }
        busy = true
        error = ""
        notice = name
        defer { busy = false }
        do {
            try await operation()
            notice = ""
        } catch is CancellationError {
            notice = ""
        } catch {
            notice = ""
            self.error = error.localizedDescription
        }
    }

    func move(_ target: StagePosition, absolute: Bool = false) async {
        await perform("Moving stage…") {
            try await api.hardwareIdle()
            var body = target.values as [String: Any]
            body["absolute"] = absolute
            _ = try await api.run("/api/v2/actions/stage/move", body: body)
            await refreshState()
        }
    }

    func focus(_ span: Int) async {
        await perform("Focusing…") {
            try await api.hardwareIdle()
            _ = try await api.run("/api/v2/extensions/org.openflexure.autofocus/fast_autofocus", body: ["dz": span])
            await refreshState()
        }
    }

    func calibrateScale() async {
        await perform("Estimating scale…") {
            try await api.hardwareIdle()
            let data = try await api.writeDevice(InstrumentAPI.scan + "/calibrate_scale",
                                                 body: ["probe_steps": 600], timeout: 65)
            let info = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            if let scale = info?["px_per_step"] as? [Double], scale.count == 2 {
                scaleDescription = String(format: "%.3f × %.3f px/step", scale[0], scale[1])
            } else {
                scaleDescription = "Scale estimated."
            }
            await refreshState()
        }
    }

    var scaleDescription = ""

    func scan(columns: Int, rows: Int, explore: Bool, continuing id: String? = nil) async {
        await perform("Acquiring scan…") {
            try await api.hardwareIdle()
            var body: [String: Any] = [
                "filename": "microworld_\(Int(Date().timeIntervalSince1970))",
                "temporary": false, "grid": [columns, rows, 1], "stride_size": [2000, 1500, 100],
                "auto_stride": id == nil, "target_overlap": 0.25, "style": explore ? "explore" : "snake",
                "explore": explore, "explore_strategy": "boundary", "explore_max_cells": 200,
                "explore_max_radius": 28, "center_tile": false, "fast_autofocus": true,
                "autofocus_dz": 2000, "scan_focus_guard": true, "adaptive_focus_stack": true,
                "mac_preview_only": true, "use_video_port": true, "namemode": "coordinates",
                "keep_tiles": true, "auto_refine": false, "tags": ["openmhs-scan", "microworld-native"],
            ]
            try await prepareMosaicScan(&body)
            if id == nil && hardware?.native_resolution_scan != true && hardware?.continuous_scan == true {
                body["continuous_scan"] = true
                body["resize"] = ["width": 1280, "height": 960]
            }
            if let id {
                body["append_session"] = true
                body["fill_gaps"] = !explore
                body["session_id"] = id
            }
            continuingScan = id
            defer { continuingScan = nil }
            let action = try await api.run(InstrumentAPI.scan + "/tile", body: body, timeout: 3600)
            if let sessionID = action.output?.session_id {
                if id == nil && observedScan != sessionID && activeScan != sessionID {
                    markNewCanvas(sessionID)
                }
                activeScan = sessionID
            }
            await reloadCanvases()
        }
    }

    private func prepareMosaicScan(_ body: inout [String: Any]) async throws {
        struct Live: Decodable { let session_id: String?; let cell_count: Int? }
        struct Archive: Decodable {
            let manifest: Manifest
            struct Manifest: Decodable {
                let cells: [Cell]
                let sync: Sync?
                struct Cell: Decodable { let ix: Int; let iy: Int }
                struct Sync: Decodable { let complete: Bool? }
            }
        }
        let previous: Live = try await api.read(InstrumentAPI.scan + "/live_details")
        if let prior = previous.session_id, let count = previous.cell_count, count > 0 {
            let archive: Archive? = try? await api.read("/mosaic/\(prior)", local: true)
            guard let archive, archive.manifest.sync?.complete == true,
                  archive.manifest.cells.count >= count else {
                throw RuntimeError.message("The PC Worker is still archiving the previous scan.")
            }
        }
        struct CurrentView: Decodable { let current_view_id: String? }
        let current: CurrentView? = try? await api.read("/views/current", local: true)
        if let viewID = current?.current_view_id { body["view_id"] = viewID }
    }

    func takeSnapshot() async {
        await perform("Saving camera frame…") {
            let data = try await api.bytes(api.devicePath("/api/v2/streams/snapshot"))
            guard NSImage(data: data) != nil else { throw RuntimeError.message("The camera did not return an image.") }
            let folder = api.connection.workspace.appendingPathComponent("captures", isDirectory: true)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let name = "Camera-\(Date().formatted(.iso8601.year().month().day().time(includingFractionalSeconds: true)))"
            let destination = folder.appendingPathComponent(name + ".jpg")
            try data.write(to: destination, options: .atomic)
            loadLocalCaptures()
        }
    }

    var acquisitionCapabilities: AcquisitionCapabilities?
    var calibrationJSON: Data?
    var calibrationError = ""
    var actionRoute: String?
    var actionProgress: Double?
    var stoppingAction = false

    func refreshCapabilities() async {
        do {
            acquisitionCapabilities = try await api.acquisitionCapabilities()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func refreshCalibration() async throws {
        guard let path = acquisitionCapabilities?.calibrationData else { return }
        let data = try await api.bytes(api.devicePath(path))
        let value = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        calibrationJSON = value?.isEmpty == false ? data : nil
        calibrationError = ""
    }

    func calibrateMapping() async {
        guard let path = acquisitionCapabilities?.calibrateXY else { return }
        await perform("Calibrating camera and stage…") {
            try await api.hardwareIdle()
            _ = try await api.run(path, body: [:], timeout: 1800)
            try await refreshCalibration()
            await refreshState()
        }
    }

    func moveInCameraPixels(x: Double, y: Double) async {
        guard let path = acquisitionCapabilities?.moveInPixels, calibrationJSON != nil else { return }
        await perform("Centering camera view…") {
            try await api.hardwareIdle()
            _ = try await api.run(path, body: ["x": y, "y": x])
            await refreshState()
        }
    }

    func capture(_ options: AdvancedCaptureOptions) async {
        await perform("Capturing…") {
            try await api.hardwareIdle()
            _ = try await api.run("/api/v2/actions/camera/capture", body: options.payload())
            await loadGallery()
        }
    }

    func advancedScan(_ options: AdvancedScanOptions, capture: AdvancedCaptureOptions, smart: Bool) async {
        guard let acquisitionCapabilities else { return }
        await perform(smart ? "Acquiring Smart Stack…" : "Acquiring scan…") {
            try await api.hardwareIdle()
            var request = try options.request(capture, capabilities: acquisitionCapabilities, smart: smart)
            if request.mosaic {
                let needsUpdatedFirmware = options.autofocus == .off || options.naming == .number ||
                    (options.pattern == .spiral && options.grid.z > 1)
                if needsUpdatedFirmware || capture.bayer {
                    let status: AcquisitionStatus = try await api.read(InstrumentAPI.scan + "/hardware")
                    guard !needsUpdatedFirmware || status.advanced_capture == true,
                          !capture.bayer || status.bayer_scan == true else {
                        throw RuntimeError.message("This option needs the updated MicroWorld scan extension on the Pi.")
                    }
                }
                try await prepareMosaicScan(&request.body)
            }
            defer { actionRoute = nil; actionProgress = nil; stoppingAction = false }
            _ = try await api.run(request.path, body: request.body, timeout: 3600) { route, action in
                self.actionRoute = route
                self.actionProgress = action.progress.map { min(100, max(0, $0)) }
            }
            await reloadCanvases()
            await loadGallery()
        }
    }

    func stopAdvancedScan() async {
        guard let route = actionRoute, !stoppingAction else { return }
        stoppingAction = true
        do {
            try await api.verifyDevice()
            _ = try await api.bytes(api.devicePath(route), method: "DELETE")
        } catch {
            stoppingAction = false
            self.error = error.localizedDescription
        }
    }

    func loadLocalCaptures() {
        let folder = api.connection.workspace.appendingPathComponent("captures", isDirectory: true)
        let paths = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        localCaptures = paths.filter { ["jpg", "jpeg", "png", "tif", "tiff"].contains($0.pathExtension.lowercased()) }
            .sorted { $0.lastPathComponent > $1.lastPathComponent }
    }

    func loadGallery() async {
        loadLocalCaptures()
        do {
            let data: Data
            do {
                data = try await api.bytes(api.devicePath("/api/v2/captures"))
            } catch {
                guard api.connection.openmhs, !Task.isCancelled else { throw error }
                data = try await api.bytes(api.devicePath(InstrumentAPI.scan + "/captures"))
            }
            let decoder = JSONDecoder()
            if let rows = try? decoder.decode([String: GalleryCapture].self, from: data) {
                remoteCaptures = rows.values.sorted { ($0.time ?? "") > ($1.time ?? "") }
            } else {
                remoteCaptures = try decoder.decode([GalleryCapture].self, from: data)
            }
            galleryError = ""
        } catch {
            galleryError = "Pi gallery: \(error.localizedDescription)"
        }
    }
}

struct GalleryCapture: Decodable, Identifiable {
    let id: String
    private let name: String?
    private let legacyFilename: String?
    let time: String?
    let tags: [String]?
    let links: Links?
    struct Links: Decodable {
        let download: Link?
        struct Link: Decodable { let href: String }
    }
    var filename: String? { name ?? legacyFilename }

    private enum CodingKeys: String, CodingKey {
        case id, name, time, tags, links
        case legacyFilename = "filename"
    }
}
