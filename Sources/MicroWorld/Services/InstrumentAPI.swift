import Foundation

struct StagePosition: Codable, Equatable {
    var x: Int
    var y: Int
    var z: Int

    static let zero = StagePosition(x: 0, y: 0, z: 0)
    var values: [String: Int] { ["x": x, "y": y, "z": z] }
}

struct DeviceAction: Decodable {
    let href: String?
    let status: String
    let output: Output?
    let log: [LogEntry]?
    let progress: Double?
    let errorMessage: String?

    struct Output: Decodable {
        let session_id: String?
        let tiles_seen: Int?
    }

    struct LogEntry: Decodable {
        let message: String?
    }

    private enum CodingKeys: String, CodingKey { case href, status, output, log, progress }

    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        href = try fields.decodeIfPresent(String.self, forKey: .href)
        status = try fields.decode(String.self, forKey: .status)
        output = try? fields.decode(Output.self, forKey: .output)
        log = try? fields.decode([LogEntry].self, forKey: .log)
        progress = try? fields.decode(Double.self, forKey: .progress)
        errorMessage = try? fields.decode(String.self, forKey: .output)
    }
}

struct AcquisitionStatus: Decodable {
    let busy: Bool
    let operation: String?
    let scan_phase: String?
    let scan_session_id: String?
    let scan_tiles_seen: Int?
    let advanced_capture: Bool?
    let bayer_scan: Bool?
    let continuous_scan: Bool?
    let focus_phase: String?
}

struct CanvasSummary: Decodable, Identifiable {
    let session_id: String
    let view_id: String?
    let cells: Int?
    let refined: Bool?
    let mtime: String?
    var id: String { session_id }
    var displayTitle: String {
        let parts = session_id.split(separator: "-")
        guard parts.count >= 2, parts[0].count == 8, parts[1].count == 6,
              parts[0].allSatisfy(\.isASCII), parts[0].allSatisfy(\.isNumber),
              parts[1].allSatisfy(\.isNumber) else { return session_id }
        let date = parts[0], time = parts[1]
        return "\(date.prefix(4))-\(date.dropFirst(4).prefix(2))-\(date.suffix(2)) · \(time.prefix(2)):\(time.dropFirst(2).prefix(2)):\(time.suffix(2))"
    }
}

struct CameraSettings: Decodable {
    let jpeg_quality: Int?
    let mjpeg_quality: Int?
    let stream_resolution: [Int]?
    let picamera: Sensor?

    struct Sensor: Decodable {
        let shutter_speed: Double?
        let analog_gain: Double?
        let digital_gain: Double?
        let framerate: Double?
        let awb_gains: [Double]?
    }
}

@MainActor
final class InstrumentAPI {
    static let scan = "/api/v2/extensions/org.microworld.scan-mosaic"
    static let prompts = "/api/v2/extensions/org.microworld.visual-prompts"
    static let position = "/api/v2/instrument/state/stage/position"
    let connection: Connection
    let session: URLSession

    init(_ connection: Connection) {
        self.connection = connection
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]
        config.timeoutIntervalForRequest = 15
        session = URLSession(configuration: config)
    }

    deinit { session.invalidateAndCancel() }

    func devicePath(_ path: String) -> URL {
        // The local service verifies the device and attaches the Pi's connection token.
        localPath(path)
    }

    func localPath(_ path: String) -> URL {
        URL(string: path, relativeTo: Runtime.origin)!.absoluteURL
    }

    func bytes(_ url: URL, method: String = "GET", body: [String: Any]? = nil,
               timeout: TimeInterval = 15) async throws -> Data {
        if method != "GET", method != "HEAD" {
            try await verifyLocalSession()
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = method
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw RuntimeError.message("The service sent an invalid response.")
        }
        guard 200..<300 ~= http.statusCode else {
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let detail = json?["message"] as? String ?? json?["error"] as? String
            throw RuntimeError.message(detail ?? "Request failed (HTTP \(http.statusCode)).")
        }
        return data
    }

    func read<T: Decodable>(_ path: String, local: Bool = false, as type: T.Type = T.self) async throws -> T {
        let data = try await bytes(local ? localPath(path) : devicePath(path))
        return try JSONDecoder().decode(type, from: data)
    }

    func verifyDevice() async throws {
        struct Description: Decodable { let id: String?; let title: String? }
        let info: Description = try await read("/api/v2/")
        let identity = "openflexure:microscope:\(connection.id)"
        guard info.id?.lowercased().contains(identity) == true || info.title?.lowercased().contains(identity) == true else {
            throw RuntimeError.message("The device at this address changed. Reconnect before sending controls.")
        }
    }

    func writeDevice(_ path: String, body: [String: Any], method: String = "POST",
                     timeout: TimeInterval = 20) async throws -> Data {
        try await verifyDevice()
        return try await bytes(devicePath(path), method: method, body: body, timeout: timeout)
    }

    private func verifyLocalSession() async throws {
        struct LocalSession: Decodable { let device_id: String }
        let active: LocalSession = try await read("/desktop/session", local: true)
        guard active.device_id == connection.id else {
            throw RuntimeError.message("The connected microscope changed. Reconnect before sending controls.")
        }
    }

    func writeLocal(_ path: String, body: [String: Any], timeout: TimeInterval = 45) async throws -> Data {
        try await bytes(localPath(path), method: "POST", body: body, timeout: timeout)
    }

    func run(_ path: String, body: [String: Any], timeout: TimeInterval = 120,
             onProgress: ((String, DeviceAction) -> Void)? = nil) async throws -> DeviceAction {
        let data = try await writeDevice(path, body: body)
        var action = try JSONDecoder().decode(DeviceAction.self, from: data)
        if action.status == "completed" { return action }
        if ["error", "failed", "cancelled"].contains(action.status) {
            throw RuntimeError.message(action.errorMessage ?? action.log?.last?.message ?? "Action \(action.status).")
        }
        guard let href = action.href, let route = URL(string: href)?.path,
              route.wholeMatch(of: #/\/api\/v2\/actions\/[A-Za-z0-9_-]+/#) != nil else {
            throw RuntimeError.message("The microscope did not return an action to monitor.")
        }
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            try Task.checkCancellation()
            switch action.status {
            case "completed": return action
            case "error", "failed", "cancelled":
                throw RuntimeError.message(action.errorMessage ?? action.log?.last?.message ?? "Action \(action.status).")
            default: break
            }
            onProgress?(route, action)
            try await Task.sleep(for: .milliseconds(750))
            action = try await read(route)
        }
        throw RuntimeError.message("Timed out waiting for the action. Check the instrument status before retrying.")
    }

    func hardwareIdle() async throws {
        if connection.openmhs {
            let status: AcquisitionStatus = try await read(Self.scan + "/hardware")
            if status.busy {
                throw RuntimeError.message("The microscope is busy with \(status.operation ?? "another operation").")
            }
        }
    }
}
