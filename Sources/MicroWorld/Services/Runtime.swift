import Foundation

enum RuntimeError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        if case .message(let description) = self { return description }
        return nil
    }
}

final class Runtime {
    static let origin = URL(string: "http://127.0.0.1:18766")!

    let root = Bundle.main.bundleURL.deletingLastPathComponent()
    private let resources = Bundle.main.resourceURL!
    private var server: Process?

    private func process(_ args: [String]) -> Process {
        let process = Process()
        process.executableURL = resources.appendingPathComponent("Python/bin/python3")
        process.arguments = ["-E", "-s", "-B", resources.appendingPathComponent("Runtime/desktop.py").path] + args
        process.currentDirectoryURL = root
        return process
    }

    func command<T: Decodable>(_ args: [String], as type: T.Type) async throws -> T {
        let process = process(args)
        return try await Task.detached(priority: .userInitiated) {
            let stdout = Pipe()
            let stderr = Pipe()
            process.standardOutput = stdout
            process.standardError = stderr
            try process.run()
            let data = stdout.fileHandleForReading.readDataToEndOfFile()
            let errors = stderr.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                throw RuntimeError.message(String(decoding: errors, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))
            }
            return try JSONDecoder().decode(type, from: data)
        }.value
    }

    func read<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        let url = URL(string: path, relativeTo: Self.origin)!
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]
        let session = URLSession(configuration: config)
        defer { session.finishTasksAndInvalidate() }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else {
            throw RuntimeError.message("The local MicroWorld service is unavailable.")
        }
        return try JSONDecoder().decode(type, from: data)
    }

    func connect(_ device: Device, to address: DeviceAddress, takeover: Bool) async throws -> Connection {
        let logDir = root.appendingPathComponent("Logs", isDirectory: true)
        try FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        let logURL = logDir.appendingPathComponent("desktop.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try FileHandle(forWritingTo: logURL)
        let aliases = device.addresses.flatMap { ["--alias", $0.url] }
        let next = process(["serve", "--root", root.path, "--url", address.url,
                            "--id", device.id, "--parent", String(ProcessInfo.processInfo.processIdentifier)]
                            + aliases + (takeover ? ["--takeover"] : []))
        next.standardOutput = log
        next.standardError = log
        try next.run()
        server = next
        defer { try? log.close() }
        for _ in 0..<60 {
            if !next.isRunning { break }
            if let info = try? await read("/desktop/status", as: DesktopStatus.self),
               info.deviceID == device.id, info.microscope == address.url,
               info.connected == true, info.workerRunning {
                return Connection(id: device.id, address: address,
                                  workspace: URL(fileURLWithPath: info.workspace, isDirectory: true),
                                  history: URL(fileURLWithPath: info.history, isDirectory: true),
                                  openmhs: info.openmhs,
                                  apiNamespace: info.apiNamespace ?? "org.openmhs")
            }
            try await Task.sleep(for: .milliseconds(250))
        }
        await disconnect()
        let contents = (try? String(contentsOf: logURL, encoding: .utf8)) ?? ""
        let message = contents.split(separator: "\n").last.map(String.init) ?? "The local service did not start."
        throw RuntimeError.message(message)
    }

    func disconnect() async {
        guard let current = server else { return }
        server = nil
        if current.isRunning { current.terminate() }
        await Task.detached { current.waitUntilExit() }.value
    }

    func terminate() {
        if server?.isRunning == true { server?.terminate() }
    }
}

struct DesktopStatus: Decodable {
    let deviceID: String
    let microscope: String
    let workerRunning: Bool
    let openmhs: Bool
    let connected: Bool?
    let disconnectReason: String?
    let apiNamespace: String?
    let workspace: String
    let history: String

    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case workerRunning = "worker_running"
        case connected
        case disconnectReason = "disconnect_reason"
        case apiNamespace = "api_namespace"
        case microscope, openmhs, workspace, history
    }
}

struct HardwareStatus: Decodable { let busy: Bool }
