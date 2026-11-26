import AppKit
import SwiftUI

@main
struct MicroWorldApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var store = DeviceStore()

    var body: some Scene {
        WindowGroup("MicroWorld", id: "microworld") {
            ContentView(store: store)
                .frame(minWidth: 960, minHeight: 620)
                .tint(Color.microworldTint)
                .onAppear { appDelegate.store = store }
        }
        .defaultSize(width: 1260, height: 810)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Instrument") {
                Button("Find Devices") { Task { await store.refresh() } }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
                Divider()
                ForEach(Array(ToolTab.allCases.enumerated()), id: \.element) { item in
                    Button(item.element.title) { store.tab = item.element }
                        .keyboardShortcut(KeyEquivalent(Character(String(item.offset + 1))), modifiers: .command)
                }
                Divider()
                Button("Open Workspace", action: store.openWorkspace)
                Button("Open Conversation History", action: store.openHistory)
                Button("Open Codex", action: store.openCodex)
                    .disabled(store.connection == nil)
            }
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var store: DeviceStore? {
        didSet {
            if let pendingLink {
                self.pendingLink = nil
                store?.requestSlide(pendingLink)
            }
        }
    }
    private var pendingLink: SlideLink?

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let link = urls.compactMap(SlideLink.init).last else { return }
        if let store { store.requestSlide(link) }
        else { pendingLink = link }
        NSApp.activate(ignoringOtherApps: true)
        NSApp.windows.first(where: { $0.canBecomeKey })?.makeKeyAndOrderFront(nil)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationWillTerminate(_ notification: Notification) {
        store?.runtime.terminate()
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
