import SwiftUI

enum ROITool: String, CaseIterable, Identifiable {
    case rectangle = "Rectangle"
    case freehand = "Freehand"
    case polygon = "Polygon"
    var id: Self { self }
}

struct NativeImageSurface: View {
    let image: NSImage
    var zoom: Double = 1
    var interactiveZoom: Binding<Double>?
    var resetViewport = 0
    var drawing = false
    var tool: ROITool = .rectangle
    var target: ROISelection?
    var source: ROISelection?
    var pyramid: SlidePyramid?
    var select: (ROISelection) -> Void = { _ in }

    @State private var stroke: [CGPoint] = []

    var body: some View {
        CanvasViewport(zoom: interactiveZoom ?? .constant(zoom), navigationEnabled: !drawing && interactiveZoom != nil, reset: resetViewport) { viewport in
            let aspect = max(0.001, image.size.width / max(1, image.size.height))
            let width = max(1, min(viewport.width, viewport.height * aspect) * zoom)
            let height = width / aspect
            Group {
                ZStack(alignment: .topLeading) {
                    Image(nsImage: image)
                        .resizable()
                        .interpolation(.high)
                        .accessibilityLabel("Microscope image")
                    if zoom > 1, let pyramid {
                        SlidePyramidLayer(pyramid: pyramid, viewport: viewport)
                    }
                    Canvas { context, size in
                        if let target { mark(target, color: .mint, size: size, in: context) }
                        if let source { mark(source, color: .yellow, size: size, in: context) }
                        if let draft = selection(stroke) { mark(draft, color: .white, size: size, in: context) }
                    }
                    .allowsHitTesting(false)
                    if drawing {
                        let size = CGSize(width: width, height: height)
                        Rectangle().fill(.clear).contentShape(Rectangle())
                            .gesture(DragGesture(minimumDistance: 2)
                                .onChanged { value in
                                    guard tool != .polygon else { return }
                                    let point = normalized(value.location, size: size)
                                    if tool == .rectangle {
                                        stroke = [normalized(value.startLocation, size: size), point]
                                    } else {
                                        stroke.append(point)
                                    }
                                }
                                .onEnded { _ in
                                    guard tool != .polygon else { return }
                                    finish()
                                })
                            .simultaneousGesture(SpatialTapGesture()
                                .onEnded { value in
                                    guard tool == .polygon else { return }
                                    stroke.append(normalized(value.location, size: size))
                                })
                            .simultaneousGesture(SpatialTapGesture(count: 2)
                                .onEnded { _ in
                                    if tool == .polygon { finish() }
                                })
                            .accessibilityLabel("Draw selection on image")
                    }
                }
                .frame(width: width, height: height)
                .frame(minWidth: viewport.width, minHeight: viewport.height)
            }
            .coordinateSpace(name: "SlideViewport")
        }
        .background(Color.black)
        .onChange(of: drawing) { _, _ in stroke = [] }
        .onChange(of: tool) { _, _ in stroke = [] }
    }

    private func normalized(_ p: CGPoint, size: CGSize) -> CGPoint {
        CGPoint(x: min(1, max(0, p.x / size.width)), y: min(1, max(0, p.y / size.height)))
    }

    private func selection(_ points: [CGPoint]) -> ROISelection? {
        guard let xmin = points.map(\.x).min(), let xmax = points.map(\.x).max(),
              let ymin = points.map(\.y).min(), let ymax = points.map(\.y).max(),
              xmax - xmin > 0.003, ymax - ymin > 0.003 else { return nil }
        let rect = CGRect(x: xmin, y: ymin, width: xmax - xmin, height: ymax - ymin)
        return ROISelection(region: ImageRegion(rect), points: tool == .rectangle ? [] : points)
    }

    private func finish() {
        if let selected = selection(stroke) { select(selected) }
        stroke = []
    }

    private func mark(_ roi: ROISelection, color: Color, size: CGSize, in context: GraphicsContext) {
        let area = roi.region
        var path = Path(CGRect(x: area.x * size.width, y: area.y * size.height,
                               width: area.width * size.width, height: area.height * size.height))
        if let first = roi.points.first {
            path = Path()
            path.move(to: CGPoint(x: first.x * size.width, y: first.y * size.height))
            for point in roi.points.dropFirst() {
                path.addLine(to: CGPoint(x: point.x * size.width, y: point.y * size.height))
            }
            path.closeSubpath()
        }
        context.fill(path, with: .color(color.opacity(0.12)))
        context.stroke(path, with: .color(color), lineWidth: 2)
    }
}
