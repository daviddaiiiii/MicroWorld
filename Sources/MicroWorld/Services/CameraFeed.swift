import AppKit
import Foundation
import Observation

@MainActor @Observable
final class CameraFeed {
    private(set) var frame: NSImage?
    private(set) var status = "Connecting…"

    func watch(_ api: InstrumentAPI) async {
        status = "Connecting…"
        while !Task.isCancelled {
            let reader = MJPEGReader()
            let url = api.devicePath("/api/v2/streams/mjpeg")
            let stream = reader.open(url)
            for await jpeg in stream {
                guard !Task.isCancelled else { break }
                if let image = NSImage(data: jpeg) {
                    frame = image
                    status = "Live"
                }
            }
            reader.close()
            guard !Task.isCancelled else { return }
            status = "Reconnecting…"
            do {
                let data = try await api.bytes(api.devicePath("/api/v2/streams/snapshot"), timeout: 3)
                if let image = NSImage(data: data) { frame = image }
            } catch { }
            try? await Task.sleep(for: .seconds(1))
        }
    }
}

private final class MJPEGReader: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private static let start = Data([0xff, 0xd8])
    private static let end = Data([0xff, 0xd9])
    private var buffer = Data()
    private var session: URLSession?
    private let feed = AsyncStream<Data>.makeStream(bufferingPolicy: .bufferingNewest(1))

    func open(_ url: URL) -> AsyncStream<Data> {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 8
        config.timeoutIntervalForResource = 6 * 60 * 60
        config.connectionProxyDictionary = [:]
        let session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
        self.session = session
        feed.continuation.onTermination = { [weak session] _ in session?.invalidateAndCancel() }
        session.dataTask(with: url).resume()
        return feed.stream
    }

    func close() { session?.invalidateAndCancel() }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        while let start = buffer.range(of: Self.start) {
            guard let end = buffer.range(of: Self.end, in: start.upperBound..<buffer.endIndex) else { break }
            feed.continuation.yield(buffer.subdata(in: start.lowerBound..<end.upperBound))
            buffer.removeSubrange(buffer.startIndex..<end.upperBound)
        }
        if buffer.count > 8 * 1024 * 1024 { buffer.removeAll(keepingCapacity: true) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        feed.continuation.finish()
    }
}
