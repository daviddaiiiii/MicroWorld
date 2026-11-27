import AppKit
import Foundation
import Observation

struct ImageRegion: Codable, Equatable {
    var x: Double
    var y: Double
    var width: Double
    var height: Double
    var rect: CGRect { CGRect(x: x, y: y, width: width, height: height) }
    var dictionary: [String: Double] { ["x": x, "y": y, "width": width, "height": height] }

    init(_ rect: CGRect) {
        x = rect.minX; y = rect.minY; width = rect.width; height = rect.height
    }
}

struct ROISelection: Equatable {
    let region: ImageRegion
    var points: [CGPoint] = []

    func payload(role: String) -> [String: Any] {
        var value: [String: Any] = region.dictionary
        value["role"] = role
        value["shape"] = points.isEmpty ? "bbox" : "polygon"
        if !points.isEmpty { value["points"] = points.map { ["x": $0.x, "y": $0.y] } }
        return value
    }
}

struct CanvasBounds: Codable, Equatable {
    let x: Double
    let y: Double
    let w: Double
    let h: Double
    var dictionary: [String: Double] { ["x": x, "y": y, "w": w, "h": h] }
}

struct CanvasReference: Codable {
    let id: String?
    let stage_origin: [Double]?
    let world_origin: [Double]?
    let stage_to_world: [[Double]]?
    let fov: [Double]?

    func field(at p: StagePosition) -> CGRect? {
        guard let stage_origin, stage_origin.count >= 2, let world_origin, world_origin.count >= 2,
              let stage_to_world, stage_to_world.count >= 2, stage_to_world[0].count >= 2,
              stage_to_world[1].count >= 2, let fov, fov.count >= 2 else { return nil }
        let dx = Double(p.x) - stage_origin[0], dy = Double(p.y) - stage_origin[1]
        let x = world_origin[0] + stage_to_world[0][0] * dx + stage_to_world[1][0] * dy
        let y = world_origin[1] + stage_to_world[0][1] * dx + stage_to_world[1][1] * dy
        return CGRect(x: x, y: y, width: fov[0], height: fov[1])
    }
}

struct CanvasAdjustments: Codable, Equatable {
    var exposure: Double = 0
    var contrast: Double = 1
    var gamma: Double = 1
    var colormap: String = "natural"
    var enhance: Bool = false
}

struct CanvasLayer: Codable, Identifiable, Equatable {
    let id: String
    let ix: Int
    let iy: Int
    var x: Double
    var y: Double
    let w: Double
    let h: Double
    var hidden: Bool?
    var manual: Bool?
    var opacity: Double?
    var adjustments: CanvasAdjustments?
    var url: String?
    var capture_relpath: String?
    var content_revision: String?

    var title: String { ix < -999_999 ? "Imported image" : "Tile \(ix), \(iy)" }
}

struct CanvasDocument: Codable {
    var revision: Int
    var adjustments: CanvasAdjustments
    var tiles: [CanvasLayer]
    var reference: CanvasReference?
    var bounds: CanvasBounds?
    var layered: Bool?
    var locked: Bool?
    var phase: String?
    var refine_phase: String?
    var refined: Bool?
    var deferred_load: Bool?
    var pyramid_revision: String?
    var calibration_fallback: Bool?
    var slide: String?
}

extension InstrumentAPI {
    func shareROI(frame: NSImage, target: ROISelection?, source: ROISelection? = nil,
                  record: String? = nil, revision: Int? = nil, bounds: CanvasBounds? = nil,
                  reference: CanvasReference? = nil) async throws {
        guard let pixels = frame.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            throw RuntimeError.message("No image is ready for ROI selection.")
        }
        let frameData = NSBitmapImageRep(cgImage: pixels).representation(using: .jpeg, properties: [.compressionFactor: 0.92])
        let region = target?.region ?? ImageRegion(CGRect(x: 0, y: 0, width: 1, height: 1))
        let pixelBounds = CGRect(x: 0, y: 0, width: pixels.width, height: pixels.height)
        let crop = CGRect(x: region.x * pixelBounds.width, y: region.y * pixelBounds.height,
                          width: region.width * pixelBounds.width, height: region.height * pixelBounds.height)
            .integral.intersection(pixelBounds)
        guard let frameData, let cut = pixels.cropping(to: crop),
              let cropData = NSBitmapImageRep(cgImage: cut).representation(using: .jpeg, properties: [.compressionFactor: 0.96]) else {
            throw RuntimeError.message("Could not encode the selected area.")
        }
        var rois: [[String: Any]] = []
        if let target { rois.append(target.payload(role: "target")) }
        if let source { rois.append(source.payload(role: "source")) }
        var payload: [String: Any] = [
            "action": "agent", "surface": record == nil ? "live" : "mosaic",
            "context_only": true, "selection_cleared": target == nil, "roi": region.dictionary, "rois": rois,
            "frame_data_url": "data:image/jpeg;base64," + frameData.base64EncodedString(),
            "roi_data_url": "data:image/jpeg;base64," + cropData.base64EncodedString(),
        ]
        if let record { payload["record_id"] = record }
        if let revision { payload["canvas_revision"] = revision }
        if let bounds { payload["canvas_bounds"] = bounds.dictionary }
        if let reference {
            payload["canvas_reference"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(reference))
        }
        _ = try await writeDevice(Self.prompts + "/codex/prompts", body: payload, timeout: 45)
    }
}

@MainActor @Observable
final class CanvasStore {
    let id: String
    let api: InstrumentAPI
    var document: CanvasDocument?
    var image: NSImage?
    var error = ""
    var notice = ""
    var busy = false
    var live = false
    var showTiles = false
    var selected: String?
    var target: ROISelection?
    var source: ROISelection?
    var roiStatus = ""
    var syncingROI = false
    var zoom: Double = 1
    var showFOV = true
    var pyramid: SlidePyramid?
    private var undoStack: [CanvasDocument] = []
    private var redoStack: [CanvasDocument] = []
    private var loaded = false

    init(id: String, api: InstrumentAPI, acquiring: Bool = false) {
        self.id = id
        self.api = api
        live = acquiring
        showTiles = acquiring
    }

    var editable: Bool { document != nil && !live && !busy && document?.locked != true }
    var canUndo: Bool { !undoStack.isEmpty && editable }
    var canRedo: Bool { !redoStack.isEmpty && editable }
    var selectedLayer: CanvasLayer? { document?.tiles.first { $0.id == selected } }

    private func request(_ action: String, fields: [String: Any] = [:]) async throws -> Data {
        var body = fields
        body["action"] = action
        return try await api.writeLocal("/mosaic/\(id)/canvas", body: body)
    }

    func watch() async {
        while !Task.isCancelled {
            if !busy {
                do {
                    let data = try await request("status")
                    let status = try JSONDecoder().decode(CanvasDocument.self, from: data)
                    guard !Task.isCancelled, !busy else { continue }
                    let scanning = status.locked == true || status.refine_phase == "refining"
                    let wasLive = live
                    let needsLoad = !loaded || status.revision != document?.revision
                        || status.refined != document?.refined || (wasLive && !scanning)
                    live = scanning
                    if scanning {
                        if !wasLive { showTiles = true }
                        document = status
                        if status.refine_phase == "refining" {
                            notice = "Registering and fusing tiles…"
                        } else {
                            notice = status.calibration_fallback == true
                                ? "Acquiring with fixed XY steps · \(status.tiles.count) tiles"
                                : "Acquiring · \(status.tiles.count) tiles"
                        }
                    } else if needsLoad {
                        if wasLive { showTiles = false }
                        try await load()
                    }
                    error = ""
                } catch {
                    if !Task.isCancelled {
                        self.error = live ? "Waiting for scan tiles…" : error.localizedDescription
                    }
                }
            }
            try? await Task.sleep(for: .seconds(live ? 1.5 : 5))
        }
    }

    func load() async throws {
        let data = try await request("load")
        let doc = try JSONDecoder().decode(CanvasDocument.self, from: data)
        try Task.checkCancellation()
        document = doc
        guard doc.deferred_load != true else { return }
        image = doc.slide.flatMap(Self.decodeImage)
        loaded = true
        notice = doc.calibration_fallback == true ? "Scan used fixed XY steps" : ""
    }

    func tileURL(_ tile: CanvasLayer, width: Int = 320) -> URL? {
        if let raw = tile.url, let url = URL(string: raw, relativeTo: Runtime.origin),
           url.host == Runtime.origin.host && url.port == Runtime.origin.port {
            return url
        }
        let settings = tile.adjustments ?? document?.adjustments ?? CanvasAdjustments()
        if settings != CanvasAdjustments() || tile.ix < -999_999 || tile.capture_relpath != nil {
            var route = URLComponents(url: api.localPath("/desktop/canvas/\(id)/layer"), resolvingAgainstBaseURL: false)!
            route.queryItems = [
                URLQueryItem(name: "id", value: tile.id),
                URLQueryItem(name: "revision", value: String(document?.revision ?? 0)),
                URLQueryItem(name: "capture", value: tile.content_revision),
                URLQueryItem(name: "width", value: String(min(width, 2048))),
            ]
            return route.url
        }
        let detail = width > 1280 ? "" : "&thumb=1&width=\(width)"
        let capture = tile.content_revision ?? "original"
        return api.localPath("/mosaic/\(id)/tile?ix=\(tile.ix)&iy=\(tile.iy)&r=\(document?.revision ?? 0)&capture=\(capture)\(detail)")
    }

    static func decodeImage(_ raw: String) -> NSImage? {
        guard let comma = raw.firstIndex(of: ","), let data = Data(base64Encoded: String(raw[raw.index(after: comma)...])) else { return nil }
        return NSImage(data: data)
    }

    func setLayer(_ id: String, edit: (inout CanvasLayer) -> Void) async {
        guard var doc = document, let i = doc.tiles.firstIndex(where: { $0.id == id }) else { return }
        edit(&doc.tiles[i])
        doc.layered = true
        await save(doc)
    }

    func reorder(_ ids: [String]) async {
        guard var doc = document, Set(ids) == Set(doc.tiles.map(\.id)), ids.count == doc.tiles.count else { return }
        let layers = Dictionary(uniqueKeysWithValues: doc.tiles.map { ($0.id, $0) })
        doc.tiles = ids.compactMap { layers[$0] }
        doc.layered = true
        await save(doc)
    }

    func adjust(_ value: CanvasAdjustments) async {
        if let selected {
            await setLayer(selected) { $0.adjustments = value }
        } else if var doc = document {
            doc.adjustments = value
            await save(doc)
        }
    }

    private func save(_ candidate: CanvasDocument, recordHistory: Bool = true) async {
        guard editable, let before = document else { return }
        busy = true
        error = ""
        defer { busy = false }
        var outbound = candidate
        outbound.slide = nil
        outbound.tiles = candidate.tiles.map { layer in
            var tile = layer
            tile.url = nil
            return tile
        }
        // Keep tile edits interactive. The worker renders when returning to Slide.
        do {
            let encoded = try JSONEncoder().encode(outbound)
            let value = try JSONSerialization.jsonObject(with: encoded)
            let result = try await request("save", fields: [
                "revision": before.revision, "document": value, "defer_render": true,
            ])
            struct SaveResult: Decodable { let document: CanvasDocument }
            let resultDoc = try JSONDecoder().decode(SaveResult.self, from: result).document
            var updated = resultDoc
            updated.bounds = before.bounds
            updated.reference = before.reference
            document = updated
            if recordHistory { undoStack.append(before); redoStack.removeAll() }
            if !showTiles { try await load() }
        } catch {
            self.error = error.localizedDescription
        }
    }

    func undo() async {
        guard let previous = undoStack.popLast(), let current = document else { return }
        await save(previous, recordHistory: false)
        if error.isEmpty { redoStack.append(current) } else { undoStack.append(previous) }
    }

    func redo() async {
        guard let next = redoStack.popLast(), let current = document else { return }
        await save(next, recordHistory: false)
        if error.isEmpty { undoStack.append(current) } else { redoStack.append(next) }
    }

    func mutate(_ action: String, fields: [String: Any]) async {
        guard editable, let revision = document?.revision else { return }
        busy = true
        error = ""
        defer { busy = false }
        do {
            var payload = fields
            payload["revision"] = revision
            _ = try await request(action, fields: payload)
            undoStack.removeAll()
            redoStack.removeAll()
            try await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func trash() async -> Bool {
        guard editable, let revision = document?.revision else { return false }
        busy = true
        error = ""
        defer { busy = false }
        do {
            _ = try await request("trash-canvas", fields: ["revision": revision, "confirm": true])
            return true
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }

    func selectSlide() async {
        showTiles = false
        if !live && !busy {
            do { try await load() } catch { self.error = error.localizedDescription }
        }
    }

    func loadPyramid() async {
        pyramid = nil
        guard let document, document.pyramid_revision != nil,
              document.layered != true, document.adjustments == CanvasAdjustments(),
              document.tiles.allSatisfy({ $0.adjustments == nil && $0.hidden != true }),
              image != nil, !showTiles, !live else { return }
        do {
            let info: SlidePyramid.Info = try await api.read("/mosaic/\(id)/pyramid/info.json", local: true)
            try Task.checkCancellation()
            pyramid = SlidePyramid(info: info, route: api.localPath("/mosaic/\(id)/pyramid/tile"))
        } catch { }
    }

    func share(_ selection: ROISelection?, asSource: Bool = false) async {
        guard let image, let document, !showTiles, !live, !syncingROI else { return }
        syncingROI = true
        defer { syncingROI = false }
        let oldTarget = target, oldSource = source
        if asSource { source = selection }
        else {
            target = selection
            if selection == nil { source = nil }
        }
        let nextTarget = target, nextSource = source
        roiStatus = "Syncing selection…"
        do {
            try await api.shareROI(frame: image, target: nextTarget, source: nextSource,
                                   record: id, revision: document.revision, bounds: document.bounds,
                                   reference: document.reference)
            roiStatus = nextTarget == nil ? "Selection cleared" : "Selection available in Codex"
        } catch {
            target = oldTarget
            source = oldSource
            roiStatus = "Selection failed. \(error.localizedDescription)"
        }
    }

    func refine() async {
        guard editable else { return }
        do {
            _ = try await api.writeLocal("/mosaic/\(id)/refine", body: ["align": "fine"])
            loaded = false
        } catch { self.error = error.localizedDescription }
    }

    func recapture(intent: String, x: Double? = nil, y: Double? = nil,
                   position: StagePosition? = nil, confirmed: Bool) async {
        guard editable, confirmed, let revision = document?.revision else { return }
        busy = true
        error = ""
        notice = intent == "fov-move" ? "Moving and focusing…" : "Capturing…"
        defer { busy = false; notice = "" }
        do {
            try await api.hardwareIdle()
            let plan: Data
            if intent == "roi", let target {
                plan = try await api.writeLocal("/mosaic/\(id)/recapture-plan", body: ["roi": target.region.dictionary])
            } else {
                var body: [String: Any] = ["intent": intent, "revision": revision]
                if let x, let y { body["x"] = x; body["y"] = y }
                if let position { body["stage_position"] = [position.x, position.y, position.z] }
                plan = try await request("placement-plan", fields: body)
            }
            struct Plan: Decodable { let plan_id: String }
            let planID = try JSONDecoder().decode(Plan.self, from: plan).plan_id
            let started = try await api.writeLocal("/mosaic/\(id)/recapture", body: [
                "plan_id": planID, "confirm_same_sample": true,
            ])
            struct Job: Decodable { let job_id: String?; let phase: String?; let error: String? }
            let job = try JSONDecoder().decode(Job.self, from: started)
            guard let jobID = job.job_id else { throw RuntimeError.message("Could not start acquisition.") }
            let deadline = Date().addingTimeInterval(600)
            while Date() < deadline {
                try await Task.sleep(for: .seconds(1))
                let data = try await api.writeLocal("/mosaic/\(id)/recapture-status", body: ["job_id": jobID])
                let state = try JSONDecoder().decode(Job.self, from: data)
                if state.phase == "completed" {
                    if intent != "fov-move" { try await load() }
                    return
                }
                if state.phase == "error" || state.phase == "failed" {
                    throw RuntimeError.message(state.error ?? "Acquisition failed.")
                }
            }
            throw RuntimeError.message("The capture job is still running. Check the device before retrying.")
        } catch { self.error = error.localizedDescription }
    }
}
