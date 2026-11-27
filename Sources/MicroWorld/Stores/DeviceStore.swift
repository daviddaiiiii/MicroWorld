import AppKit
import Observation

@MainActor @Observable
final class DeviceStore {
    let runtime = Runtime()
    var onlineIDs: Set<String> = []
    var slideLink: SlideLink?
    private var pendingSlide: SlideLink?
    private var latestSlideRequest: UUID?
    private var discoveryComplete = false
    private let localNetwork = LocalNetwork()

    func start() async {
        localNetwork.start { [weak self] in
            Task { @MainActor in
                guard let self, self.devices.isEmpty else { return }
                await self.refresh()
            }
        }
        await refresh()
    }
    var devices: [Device] = []
    var selected: String?
    var connection: Connection?
    var searching = false
    var switching = false
    var instrumentBusy = false
    var tailscale = ""
    var message = ""
    var tab = ToolTab.observe
    private var known: [Device] = []

    var candidate: Device? { devices.first { $0.id == selected } }
    var working: Bool { switching || searching }

    private var knownFile: URL {
        runtime.root.appendingPathComponent("State/known-devices.json")
    }

    func refresh() async {
        guard !working else { return }
        searching = true
        message = ""
        defer {
            searching = false
            discoveryComplete = true
            Task { await openRequestedSlide() }
        }
        if let data = try? Data(contentsOf: knownFile),
           let remembered = try? JSONDecoder().decode([Device].self, from: data) {
            known = remembered
        }
        do {
            let result = try await runtime.command(["discover"] + known.flatMap { $0.addresses.map(\.url) }, as: Discovery.self)
            onlineIDs = Set(result.devices.map(\.id))
            let offline = known.filter { !onlineIDs.contains($0.id) }
            devices = result.devices + offline
            tailscale = result.tailscale
            if candidate == nil {
                let state = runtime.root.appendingPathComponent("State/connection.json")
                let last = (try? Data(contentsOf: state))
                    .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }?["id"] as? String
                selected = connection?.id ?? devices.first { $0.id == last }?.id ?? devices.first?.id
            }
            for device in result.devices { remember(device) }
        } catch {
            message = error.localizedDescription
        }
    }

    func addAddress(_ value: String) async {
        guard !working, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        searching = true
        message = ""
        defer {
            searching = false
            Task { await openRequestedSlide() }
        }
        do {
            let item = try await runtime.command(["identify", value], as: IdentifiedDevice.self)
            let address = DeviceAddress(url: item.url, source: "Manual")
            if let index = devices.firstIndex(where: { $0.id == item.id }) {
                if !devices[index].addresses.contains(where: { $0.url == item.url }) {
                    devices[index].addresses.append(address)
                }
                remember(devices[index])
            } else {
                let device = Device(id: item.id, openmhs: item.openmhs, addresses: [address])
                devices.append(device)
                remember(device)
            }
            onlineIDs.insert(item.id)
            selected = item.id
        } catch {
            message = error.localizedDescription
        }
    }

    private func remember(_ device: Device) {
        if let index = known.firstIndex(where: { $0.id == device.id }) {
            let prior = known[index].addresses.filter { old in !device.addresses.contains { $0.url == old.url } }
            known[index] = Device(id: device.id, openmhs: device.openmhs, addresses: device.addresses + prior)
        } else {
            known.append(device)
        }
        do {
            try FileManager.default.createDirectory(at: knownFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try JSONEncoder().encode(known).write(to: knownFile, options: .atomic)
        } catch {
            message = error.localizedDescription
        }
    }

    func connect(_ device: Device, address: DeviceAddress, takeover: Bool = true) async {
        guard !working, connection?.address != address else { return }
        switching = true
        message = ""
        defer {
            switching = false
            Task { await openRequestedSlide() }
        }
        if await acquisitionIsBusy() {
            message = "Wait for the current acquisition to finish before switching devices."
            return
        }
        do {
            let live = try await runtime.command(["identify", address.url], as: IdentifiedDevice.self)
            guard live.id == device.id else {
                throw RuntimeError.message("That address now belongs to a different microscope. Refresh Devices.")
            }
            slideLink = nil
            connection = nil
            await runtime.disconnect()
            let next = try await runtime.connect(device, to: address, takeover: takeover)
            connection = next
            selected = device.id
            tab = device.openmhs ? .observe : .navigate
        } catch {
            message = error.localizedDescription
        }
    }

    func disconnect() async {
        guard !switching else { return }
        switching = true
        defer {
            switching = false
            Task { await openRequestedSlide() }
        }
        if await acquisitionIsBusy() {
            message = "Wait for the current acquisition to finish before disconnecting."
            return
        }
        slideLink = nil
        connection = nil
        await runtime.disconnect()
    }

    func watchConnection() async {
        guard let generation = connection?.generation else { return }
        while !Task.isCancelled, connection?.generation == generation {
            if let status = try? await runtime.read("/desktop/status", as: DesktopStatus.self),
               status.connected == false, connection?.generation == generation, !switching {
                switching = true
                slideLink = nil
                connection = nil
                message = status.disconnectReason ?? "The microscope disconnected."
                await runtime.disconnect()
                switching = false
                return
            }
            try? await Task.sleep(for: .seconds(2))
        }
    }

    private func acquisitionIsBusy() async -> Bool {
        if instrumentBusy { return true }
        guard connection?.openmhs == true else { return false }
        let path = InstrumentAPI.scan + "/hardware"
        return (try? await runtime.read(path, as: HardwareStatus.self))?.busy == true
    }

    func requestSlide(_ link: SlideLink) {
        latestSlideRequest = link.requestID
        pendingSlide = link
        Task { await openRequestedSlide() }
    }

    private func openRequestedSlide() async {
        guard !working, discoveryComplete || connection != nil, let link = pendingSlide else { return }
        pendingSlide = nil
        if connection == nil, let device = devices.first(where: { $0.id == link.deviceID }),
           let address = device.addresses.first {
            await connect(device, address: address, takeover: false)
        }
        guard latestSlideRequest == link.requestID else { return }
        guard connection?.id == link.deviceID else {
            if message.isEmpty {
                message = "Connect to OpenFlexure \(link.deviceID.suffix(5).uppercased()) to open this Slide."
            }
            return
        }
        if await acquisitionIsBusy() {
            message = "Wait for the current acquisition to finish before opening this Slide."
            return
        }
        guard connection?.id == link.deviceID, !switching, latestSlideRequest == link.requestID else { return }
        message = ""
        tab = .observe
        slideLink = link
    }

    func openWorkspace() {
        let url = connection?.workspace ?? runtime.root.appendingPathComponent("Workspace")
        NSWorkspace.shared.open(url)
    }

    func openHistory() {
        let url = connection?.history ?? runtime.root.appendingPathComponent("ConversationHistory")
        NSWorkspace.shared.open(url)
    }

    func openCodex() {
        NSWorkspace.shared.open(runtime.root.appendingPathComponent("Open Codex.command"))
    }
}
