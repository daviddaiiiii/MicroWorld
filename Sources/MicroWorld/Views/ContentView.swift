import SwiftUI

struct ContentView: View {
    @Bindable var store: DeviceStore
    @State private var addressSheet = false
    @State private var address = ""

    var body: some View {
        HSplitView {
            sidebar.frame(minWidth: 208, idealWidth: 226, maxWidth: 270)
            detail.frame(minWidth: 690, maxWidth: .infinity, maxHeight: .infinity)
        }
        .navigationTitle("MicroWorld")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button("Open Workspace", systemImage: "folder", action: store.openWorkspace)
                    Button("Open Conversation History", systemImage: "bubble.left.and.bubble.right", action: store.openHistory)
                    Button("Open Codex", systemImage: "terminal", action: store.openCodex)
                        .disabled(store.connection == nil)
                    if store.connection != nil {
                        Divider()
                        Button("Disconnect", systemImage: "eject") { Task { await store.disconnect() } }
                            .disabled(store.switching)
                    }
                } label: {
                    Label("Workspace Actions", systemImage: "ellipsis.circle")
                }
            }
        }
        .sheet(isPresented: $addressSheet) {
            VStack(alignment: .leading, spacing: 16) {
                Text("Add a microscope").font(.title2.bold())
                TextField("Address, e.g. microscope.local:5000", text: $address)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    Spacer()
                    Button("Cancel") { addressSheet = false }
                        .keyboardShortcut(.cancelAction)
                    Button("Find Device") {
                        let entered = address
                        addressSheet = false
                        Task { await store.addAddress(entered) }
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(address.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .padding(24)
            .frame(width: 420)
        }
        .task { await store.start() }
        .task(id: store.connection?.generation) { await store.watchConnection() }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    if store.connection != nil {
                        sidebarHeading("Workspace")
                        ForEach(ToolTab.allCases) { tab in navigationRow(tab) }
                        Divider().padding(.vertical, 11)
                    }
                    HStack(spacing: 8) {
                        sidebarHeading("Microscopes")
                        Spacer()
                        Button { Task { await store.refresh() } } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .help("Find Devices")
                        .accessibilityLabel("Find Devices")
                        .disabled(store.working)
                        Button { addressSheet = true } label: { Image(systemName: "plus") }
                            .help("Add by Address")
                            .accessibilityLabel("Add by Address")
                    }
                    .padding(.trailing, 5)
                    .buttonStyle(.borderless)
                    ForEach(store.devices) { device in deviceRow(device) }
                    if store.searching {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Finding devices…").foregroundStyle(.secondary)
                        }
                        .padding(8)
                    }
                }
                .padding(13)
            }
            Divider()
            connectionControls.padding(13)
        }
        .background(.regularMaterial)
    }

    private func sidebarHeading(_ title: String) -> some View {
        Text(title)
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .padding(.leading, 9)
            .padding(.vertical, 5)
    }

    private func navigationRow(_ tab: ToolTab) -> some View {
        SidebarRow(selected: store.tab == tab, action: { store.tab = tab }) {
            Image(systemName: tab.icon)
                .foregroundStyle(store.tab == tab ? Color.microworldAccent : .secondary)
                .frame(width: 20)
            Text(tab.title).font(.callout.weight(store.tab == tab ? .semibold : .regular))
        }
        .accessibilityIdentifier("workspace-\(tab.rawValue)")
    }

    private func deviceRow(_ device: Device) -> some View {
        let active = store.connection?.id == device.id
        let available = store.onlineIDs.contains(device.id)
        let status = active ? "Connected" : available ? "Available" : "Offline"
        let address = active ? store.connection?.address.display : device.addresses.first?.display
        return SidebarRow(selected: store.selected == device.id, action: { store.selected = device.id }) {
            Image(systemName: "viewfinder")
                .foregroundStyle(active ? Color.microworldAccent : .secondary)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 3) {
                Text("OpenFlexure \(device.shortID)").font(.callout.weight(.medium))
                Text(active || !available ? status : address ?? status)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            Circle().fill(active ? Color.microworldAccent : available ? Color.secondary : Color.secondary.opacity(0.3))
                .frame(width: 6, height: 6)
                .accessibilityHidden(true)
                .help(status)
        }
        .accessibilityLabel("OpenFlexure \(device.shortID), \(status)")
        .help(address ?? status)
        .accessibilityIdentifier("device-\(device.shortID)")
        .disabled(store.switching)
    }

    @ViewBuilder private var connectionControls: some View {
        VStack(alignment: .leading, spacing: 9) {
            if let device = store.candidate, let first = device.addresses.first {
                let active = store.connection?.id == device.id
                let primaryRoute = active ? store.connection?.address ?? first : first
                HStack(spacing: 6) {
                    Button {
                        Task { await store.connect(device, address: primaryRoute) }
                    } label: {
                        Label(active ? "Connected" : store.switching ? "Connecting…" : "Connect",
                              systemImage: active ? "checkmark" : "link")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(active || store.working)
                    .help("Connect this computer. If the microscope is idle, this replaces its current connection.")
                    .accessibilityIdentifier("connect-device")
                    if device.addresses.count > 1 {
                        Menu {
                            ForEach(device.addresses) { route in
                                Button("\(route.display) · \(route.source)") {
                                    Task { await store.connect(device, address: route) }
                                }
                                .disabled(store.connection?.address == route)
                            }
                        } label: {
                            Image(systemName: "chevron.down").accessibilityLabel("Choose a connection address")
                        }
                        .menuStyle(.borderlessButton)
                        .menuIndicator(.hidden)
                        .fixedSize()
                        .disabled(store.working)
                    }
                }
            } else {
                Button("Add by Address", systemImage: "plus") { addressSheet = true }
                    .buttonStyle(.bordered)
            }
            if !store.tailscale.isEmpty {
                Label("Tailscale · \(store.tailscale)", systemImage: "network")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var detail: some View {
        VStack(spacing: 0) {
            if !store.message.isEmpty {
                Label(store.message, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.orange.opacity(0.09))
            }
            if let connection = store.connection {
                HStack(spacing: 9) {
                    Text(store.tab.title).font(.title2.weight(.semibold))
                        .accessibilityAddTraits(.isHeader)
                    Spacer()
                    Label(connection.address.display, systemImage: "link")
                        .font(.caption.monospacedDigit())
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(.primary.opacity(0.06), in: Capsule())
                        .help("Connected microscope")
                }
                .padding(.horizontal, 18)
                .frame(height: 53)
                Divider()
                InstrumentView(tab: $store.tab, instrumentBusy: $store.instrumentBusy,
                               connection: connection, slideLink: store.slideLink)
                    .id(connection.generation)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                welcome
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var welcome: some View {
        VStack(spacing: 12) {
            Image(systemName: "viewfinder")
                .font(.system(size: 31, weight: .medium))
                .foregroundStyle(Color.microworldAccent)
                .frame(width: 68, height: 68)
                .background(Color.microworldTint.opacity(0.13), in: RoundedRectangle(cornerRadius: 18))
                .accessibilityHidden(true)
            Text("Connect a microscope")
                .font(.system(.largeTitle, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
            if store.working {
                Label(store.switching ? "Connecting…" : "Looking for microscopes…", systemImage: "network")
                    .foregroundStyle(.secondary)
            } else if store.devices.isEmpty {
                Text("Search your network or enter the microscope’s address.")
                    .foregroundStyle(.secondary)
                HStack {
                    Button("Find Devices") { Task { await store.refresh() } }.buttonStyle(.borderedProminent)
                    Button("Add by Address") { addressSheet = true }
                }
                .padding(.top, 6)
            } else {
                Text("Select a microscope in the sidebar, then click Connect.")
                    .foregroundStyle(.secondary)
            }
        }
        .multilineTextAlignment(.center)
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct SidebarRow<Label: View>: View {
    let selected: Bool
    let action: () -> Void
    @ViewBuilder var label: () -> Label
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8, content: label)
                .padding(.horizontal, 9)
                .padding(.vertical, 7)
                .frame(maxWidth: .infinity, minHeight: 32, alignment: .leading)
                .background(selected ? Color.microworldTint.opacity(0.16) :
                                hovering ? Color.primary.opacity(0.055) : .clear,
                            in: RoundedRectangle(cornerRadius: 7))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

extension Color {
    static let microworldTint = Color(nsColor: NSColor(name: nil) { appearance in
        let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return dark ? NSColor(srgbRed: 0.29, green: 0.65, blue: 0.55, alpha: 1)
                    : NSColor(srgbRed: 0.08, green: 0.46, blue: 0.37, alpha: 1)
    })
    static let microworldAccent = microworldTint
}
