import SwiftUI

struct SlidePyramid {
    let info: Info
    let route: URL

    struct Info: Decodable {
        let revision: String
        let tile_size: Int
        let tile_overlap: Int
        let levels: [Level]
    }

    struct Level: Decodable {
        let level: Int
        let width: Int
        let height: Int
        let tiles_x: Int
        let tiles_y: Int
    }

    struct Tile: Identifiable {
        let id: String
        let url: URL
        let frame: CGRect
    }

    func tiles(displaySize: CGSize, visible: CGRect, backingScale: Double) -> [Tile] {
        let target = displaySize.width * backingScale
        guard let level = info.levels.first(where: { Double($0.width) >= target }) ?? info.levels.last,
              level.width > 0, level.height > 0, info.tile_size > 0, level.tiles_x > 0, level.tiles_y > 0,
              !visible.isEmpty else { return [] }
        let px = displaySize.width / Double(level.width)
        let py = displaySize.height / Double(level.height)
        let edge = Double(info.tile_size)
        let left = min(level.tiles_x - 1, max(0, Int(visible.minX / px / edge)))
        let right = min(level.tiles_x - 1, max(left, Int(visible.maxX / px / edge)))
        let top = min(level.tiles_y - 1, max(0, Int(visible.minY / py / edge)))
        let bottom = min(level.tiles_y - 1, max(top, Int(visible.maxY / py / edge)))
        var result: [Tile] = []
        for y in top...bottom {
            for x in left...right {
                let x0 = max(0, x * info.tile_size - info.tile_overlap)
                let y0 = max(0, y * info.tile_size - info.tile_overlap)
                let x1 = min(level.width, (x + 1) * info.tile_size + info.tile_overlap)
                let y1 = min(level.height, (y + 1) * info.tile_size + info.tile_overlap)
                var url = URLComponents(url: route, resolvingAgainstBaseURL: false)!
                url.queryItems = [
                    URLQueryItem(name: "level", value: String(level.level)),
                    URLQueryItem(name: "x", value: String(x)),
                    URLQueryItem(name: "y", value: String(y)),
                    URLQueryItem(name: "revision", value: info.revision),
                ]
                if let path = url.url {
                    let rect = CGRect(x: Double(x0) * px, y: Double(y0) * py,
                                      width: Double(x1 - x0) * px, height: Double(y1 - y0) * py)
                    result.append(Tile(id: path.absoluteString, url: path, frame: rect))
                }
            }
        }
        return result
    }
}

struct SlidePyramidLayer: View {
    let pyramid: SlidePyramid
    let viewport: CGSize
    @Environment(\.displayScale) private var displayScale

    var body: some View {
        GeometryReader { geometry in
            let frame = geometry.frame(in: .named("SlideViewport"))
            let visible = CGRect(origin: CGPoint(x: -frame.minX, y: -frame.minY), size: viewport)
                .intersection(CGRect(origin: .zero, size: geometry.size))
            ZStack(alignment: .topLeading) {
                ForEach(pyramid.tiles(displaySize: geometry.size, visible: visible, backingScale: displayScale)) { tile in
                    AsyncImage(url: tile.url) { image in image.resizable().interpolation(.high) }
                        placeholder: { Color.clear }
                        .frame(width: tile.frame.width, height: tile.frame.height)
                        .offset(x: tile.frame.minX, y: tile.frame.minY)
                }
            }
            .frame(width: geometry.size.width, height: geometry.size.height, alignment: .topLeading)
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}
