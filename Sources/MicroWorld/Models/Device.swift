import Foundation

struct DeviceAddress: Codable, Hashable, Identifiable {
    var id: String { url }
    let url: String
    let source: String

    var display: String {
        let parts = URLComponents(string: url)
        return (parts?.host ?? url) + (parts?.port.map { ":\($0)" } ?? "")
    }
}

struct Device: Codable, Identifiable {
    let id: String
    let openmhs: Bool
    var addresses: [DeviceAddress]
    var shortID: String { String(id.suffix(5)).uppercased() }
}

struct Discovery: Decodable {
    let devices: [Device]
    let tailscale: String
}

struct IdentifiedDevice: Decodable {
    let id: String
    let url: String
    let openmhs: Bool
}

struct Connection: Equatable, Identifiable {
    let id: String
    let address: DeviceAddress
    let workspace: URL
    let history: URL
    let openmhs: Bool
    let generation = UUID()
}

struct SlideLink {
    let deviceID: String
    let sessionID: String
    let requestID = UUID()

    init?(_ url: URL) {
        let parts = url.pathComponents.dropFirst()
        guard url.scheme == "microworld", url.host == "slide", parts.count == 2,
              parts.allSatisfy({ $0.wholeMatch(of: #/[a-zA-Z0-9_-]{1,120}/#) != nil })
        else { return nil }
        deviceID = parts[parts.startIndex]
        sessionID = parts[parts.index(after: parts.startIndex)]
    }


}

enum ToolTab: String, CaseIterable, Identifiable {
    case observe
    case gallery, navigate, capture, settings
    var id: String { rawValue }

    var title: String {
        switch self {
        case .observe: "Observe"
        case .gallery: "Gallery"
        case .navigate: "Navigate"
        case .capture: "Capture"
        case .settings: "Settings"
        }
    }

    var icon: String {
        switch self {
        case .observe: "viewfinder"
        case .gallery: "photo.on.rectangle"
        case .navigate: "move.3d"
        case .capture: "camera"
        case .settings: "gearshape"
        }
    }
}
