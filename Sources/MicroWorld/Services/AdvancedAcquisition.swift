import Foundation

struct AcquisitionCapabilities {
    var mosaicScan: String?
    var stockScan: String?
    var smartStack: String?
    var skipsEmptyFields = false
    var calibrateXY: String?
    var calibrationData: String?
    var moveInPixels: String?
}

extension InstrumentAPI {
    func acquisitionCapabilities() async throws -> AcquisitionCapabilities {
        struct Description: Decodable {
            var actions: [String: Entry]?
            var properties: [String: Entry]?
            struct Entry: Decodable { var forms: [Form]? }
            struct Form: Decodable { var href: String; var op: String? }
        }
        let description: Description = try await read("/api/v2/")
        func path(_ href: String?) -> String? {
            guard let href, let url = URLComponents(string: href),
                  url.percentEncodedPath.hasPrefix("/api/v2/extensions/"),
                  url.query == nil, url.fragment == nil else { return nil }
            return url.percentEncodedPath
        }
        func action(_ name: String) -> String? {
            path(description.actions?[name]?.forms?.first(where: { $0.op == "invokeaction" })?.href)
        }
        let mapping = "org.openflexure.camera_stage_mapping/"
        let mosaic = "\(connection.apiNamespace).scan-mosaic"
        var capabilities = AcquisitionCapabilities()
        capabilities.mosaicScan = action(mosaic + "/tile")
        capabilities.stockScan = action("org.openflexure.scan/tile")
        capabilities.smartStack = action("org.openflexure.smart-stack/tile")
        capabilities.skipsEmptyFields = description.actions?.keys.contains {
            $0.hasPrefix("org.openflexure.background-detect/")
        } == true
        capabilities.calibrateXY = action(mapping + "calibrate_xy")
        capabilities.moveInPixels = action(mapping + "move_in_image_coordinates")
        capabilities.calibrationData = path(description.properties?[mapping + "get_calibration"]?.forms?.first?.href)
        // Older OF releases may advertise optional plugins only in this index.
        struct ExtensionInfo: Decodable {
            let title: String
            let links: [String: Link]?
            struct Link: Decodable { let href: String }
        }
        if let extensions: [ExtensionInfo] = try? await read("/api/v2/extensions") {
            func link(_ title: String, _ key: String) -> String? {
                path(extensions.first { $0.title == title }?.links?[key]?.href)
            }
            let title = "org.openflexure.camera_stage_mapping"
            capabilities.mosaicScan = capabilities.mosaicScan ?? link(mosaic, "tile")
            capabilities.stockScan = capabilities.stockScan ?? link("org.openflexure.scan", "tile")
            capabilities.smartStack = capabilities.smartStack ?? link("org.openflexure.smart-stack", "tile")
            capabilities.calibrateXY = capabilities.calibrateXY ?? link(title, "calibrate_xy")
            capabilities.calibrationData = capabilities.calibrationData ?? link(title, "get_calibration")
            capabilities.moveInPixels = capabilities.moveInPixels ?? link(title, "move_in_image_coordinates")
            capabilities.skipsEmptyFields = capabilities.skipsEmptyFields ||
                link("org.openflexure.background-detect", "grab_and_classify_image") != nil
        }
        return capabilities
    }
}

struct CaptureAnnotation: Identifiable, Codable, Equatable {
    var id = UUID()
    var key = ""
    var value = ""
}

struct AdvancedCaptureOptions: Codable, Equatable {
    var filename = ""
    var notes = ""
    var tags = ""
    var fullResolution = true
    var temporary = false
    var bayer = false
    var resize = false
    var width = 640
    var height = 480
    var annotations = [CaptureAnnotation(key: "Client", value: "MicroWorld")]

    func payload() throws -> [String: Any] {
        var fields: [String: Any] = ["temporary": temporary, "use_video_port": !fullResolution && !bayer, "bayer": bayer]
        if !filename.isEmpty { fields["filename"] = filename }
        if resize && !bayer {
            guard width > 0 && height > 0 else { throw RuntimeError.message("Resize dimensions must be positive.") }
            fields["resize"] = ["width": width, "height": height]
        }
        var metadata: [String: String] = [:]
        for item in annotations {
            let key = item.key.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !key.isEmpty else { continue }
            guard metadata[key] == nil else { throw RuntimeError.message("Annotation keys must be unique.") }
            metadata[key] = item.value
        }
        if !notes.isEmpty { metadata["Notes"] = notes }
        fields["annotations"] = metadata
        fields["tags"] = tags.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return fields
    }
}

struct AdvancedScanOptions: Codable, Equatable {
    enum Focus: String, CaseIterable, Codable {
        case off, coarse, medium, fine, fast
        var dz: Int {
            switch self {
            case .off: 0
            case .coarse: 100
            case .medium: 30
            case .fine: 10
            case .fast: 2000
            }
        }
    }
    enum Pattern: String, CaseIterable, Codable { case raster, snake, spiral }
    enum Naming: String, CaseIterable, Codable { case coordinates, number }

    var grid = StagePosition(x: 3, y: 3, z: 1)
    var stride = StagePosition(x: 800, y: 640, z: 50)
    var autofocus = Focus.fast
    var pattern = Pattern.raster
    var naming = Naming.coordinates
    var automaticStride = true
    var skipEmpty = false
    var smartAutofocusRange = 3000
    var smartAlignmentRange = 900

    func request(_ capture: AdvancedCaptureOptions, capabilities: AcquisitionCapabilities,
                 smart: Bool) throws -> (path: String, body: [String: Any], mosaic: Bool) {
        let counts = [grid.x, grid.y, grid.z]
        let steps = [stride.x, stride.y, stride.z]
        guard counts.allSatisfy({ $0 > 0 }) else { throw RuntimeError.message("Each axis needs at least one image.") }
        guard zip(counts, steps).allSatisfy({ $0.0 == 1 || $0.1 != 0 }) else {
            throw RuntimeError.message("An axis with multiple images needs a nonzero step.")
        }
        let mosaic = !smart && capabilities.mosaicScan != nil
        guard let path = smart ? capabilities.smartStack : (capabilities.mosaicScan ?? capabilities.stockScan) else {
            throw RuntimeError.message("This microscope does not advertise the requested scan mode.")
        }
        var body = try capture.payload()
        body["use_video_port"] = !capture.bayer
        body["grid"] = counts
        body["stride_size"] = steps
        body["style"] = pattern.rawValue
        body["namemode"] = naming.rawValue
        body["autofocus_dz"] = autofocus.dz
        body["fast_autofocus"] = autofocus == .fast
        body["detect_empty_fields_and_skip_autofocus"] = skipEmpty && capabilities.skipsEmptyFields
        if mosaic {
            guard grid.x <= 32 && grid.y <= 32 else {
                throw RuntimeError.message("MicroWorld supports up to 32 images per XY axis.")
            }
            body["auto_stride"] = automaticStride
            body["target_overlap"] = 0.25
            // The Mac archives a single JPEG per XY field. Keep the Pi
            // dataset for acquisitions with more than one plane or Bayer data.
            body["mac_preview_only"] = grid.z == 1 && !capture.bayer && naming == .coordinates
            body["keep_tiles"] = true
            body["auto_refine"] = false
            // Respect the autofocus option in this panel, including Off.
            body["scan_focus_guard"] = false
            body["adaptive_focus_stack"] = false
            body["center_tile"] = false
        }
        if smart {
            guard grid.z > 1, autofocus == .fast, smartAutofocusRange >= 1000, smartAlignmentRange >= 100 else {
                throw RuntimeError.message("Smart Stack needs Fast autofocus, more than one Z image, and valid autofocus and alignment ranges.")
            }
            body["threshold"] = 0.9
            body["width"] = 200
            body["align_dist"] = smartAlignmentRange
            body["backlash"] = 100
            body["fit_style"] = "chevy"
            body["m_and_m_index"] = 10
            body["autofocus_dz"] = smartAutofocusRange
        }
        return (path, body, mosaic)
    }
}
