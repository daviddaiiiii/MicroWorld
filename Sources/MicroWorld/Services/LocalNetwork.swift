import Foundation
import Network

/// Start Bonjour from the application so macOS can grant local network access
/// to MicroWorld before the bundled discovery process opens its LAN sockets.
final class LocalNetwork {
    private var browser: NWBrowser?

    func start(onReady: @escaping () -> Void) {
        guard browser == nil else { return }
        let browser = NWBrowser(for: .bonjour(type: "_http._tcp", domain: "local."), using: .tcp)
        browser.stateUpdateHandler = { state in
            if case .ready = state { DispatchQueue.main.async(execute: onReady) }
        }
        self.browser = browser
        browser.start(queue: DispatchQueue(label: "org.openmhs.microworld.bonjour"))
    }

    deinit { browser?.cancel() }
}
