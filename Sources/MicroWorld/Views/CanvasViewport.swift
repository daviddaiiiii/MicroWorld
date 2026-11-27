import AppKit
import SwiftUI

struct CanvasViewport<Content: View>: View {
    @Binding var zoom: Double
    var navigationEnabled = true
    var reset = 0
    @ViewBuilder let content: (CGSize, CGSize) -> Content
    @State private var pan = CGSize.zero

    var body: some View {
        GeometryReader { geometry in
            content(geometry.size, pan)
                .offset(pan)
                .frame(width: geometry.size.width, height: geometry.size.height)
                .background {
                    CanvasNavigation(enabled: navigationEnabled) { factor, point in
                        let next = min(64, max(0.1, zoom * factor))
                        let ratio = next / zoom
                        pan = CGSize(
                            width: (pan.width - point.x) * ratio + point.x,
                            height: (pan.height - point.y) * ratio + point.y)
                        zoom = next
                    } pan: { delta in
                        pan.width += delta.width
                        pan.height += delta.height
                    }
                }
                .clipped()
        }
        .onChange(of: reset) { _, _ in pan = .zero }
    }
}

private struct CanvasNavigation: NSViewRepresentable {
    let enabled: Bool
    let zoom: (Double, CGPoint) -> Void
    let pan: (CGSize) -> Void

    func makeNSView(context: Context) -> NavigationView { NavigationView() }

    func updateNSView(_ view: NavigationView, context: Context) {
        view.enabled = enabled
        view.zoom = zoom
        view.pan = pan
    }

    final class NavigationView: NSView {
        var enabled = true
        var zoom: ((Double, CGPoint) -> Void)?
        var pan: ((CGSize) -> Void)?
        private var monitor: Any?
        private var spaceHeld = false

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
            spaceHeld = false
            guard window != nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(
                matching: [.scrollWheel, .magnify, .otherMouseDragged, .leftMouseDragged, .keyDown, .keyUp]
            ) { [weak self] event in
                guard let self, self.enabled, event.window === self.window,
                      !self.isHiddenOrHasHiddenAncestor else { return event }
                if event.type == .keyDown || event.type == .keyUp {
                    guard event.keyCode == 49,
                          !(self.window?.firstResponder is NSTextView),
                          self.window?.attachedSheet == nil else { return event }
                    let cursor = self.convert(self.window?.mouseLocationOutsideOfEventStream ?? .zero, from: nil)
                    guard self.bounds.contains(cursor) else {
                        self.spaceHeld = false
                        return event
                    }
                    self.spaceHeld = event.type == .keyDown
                    return nil
                }
                let location = self.convert(event.locationInWindow, from: nil)
                guard self.bounds.contains(location) else { return event }
                let anchor = CGPoint(x: location.x - self.bounds.midX,
                                     y: self.bounds.midY - location.y)
                switch event.type {
                case .magnify:
                    self.zoom?(max(0.1, 1 + event.magnification), anchor)
                case .scrollWheel:
                    if !event.hasPreciseScrollingDeltas || event.modifierFlags.contains(.option) {
                        self.zoom?(exp(Double(event.scrollingDeltaY) * 0.025), anchor)
                    } else {
                        self.pan?(CGSize(width: event.scrollingDeltaX, height: event.scrollingDeltaY))
                    }
                case .leftMouseDragged, .otherMouseDragged:
                    guard event.buttonNumber == 2 || self.spaceHeld else { return event }
                    self.pan?(CGSize(width: event.deltaX, height: event.deltaY))
                default: return event
                }
                return nil
            }
        }

        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        deinit {
            if let monitor { NSEvent.removeMonitor(monitor) }
        }
    }
}

struct StageKeyboardControl: NSViewRepresentable {
    let enabled: Bool
    let jog: (Int, Int, Int) -> Void

    func makeNSView(context: Context) -> KeyboardView { KeyboardView() }

    func updateNSView(_ view: KeyboardView, context: Context) {
        view.enabled = enabled
        view.jog = jog
    }

    final class KeyboardView: NSView {
        var enabled = false
        var jog: ((Int, Int, Int) -> Void)?
        private var monitor: Any?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
            guard window != nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard let self, self.enabled, event.window === self.window,
                      self.window?.attachedSheet == nil,
                      !(self.window?.firstResponder is NSTextView),
                      !(self.window?.firstResponder is NSControl),
                      event.modifierFlags.intersection([.command, .control, .option]).isEmpty
                else { return event }
                let delta: (Int, Int, Int)
                switch event.keyCode {
                case 123: delta = (-1, 0, 0)
                case 124: delta = (1, 0, 0)
                case 125: delta = (0, -1, 0)
                case 126: delta = (0, 1, 0)
                case 116: delta = (0, 0, 1)
                case 121: delta = (0, 0, -1)
                default: return event
                }
                if !event.isARepeat { self.jog?(delta.0, delta.1, delta.2) }
                return nil
            }
        }

        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        deinit {
            if let monitor { NSEvent.removeMonitor(monitor) }
        }
    }
}
