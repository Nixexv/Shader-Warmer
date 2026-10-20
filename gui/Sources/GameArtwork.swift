import AppKit
import SwiftUI

@MainActor
final class GameArtwork: ObservableObject {
    enum Kind: String, CaseIterable { case cover, hero }
    @Published private var images: [String: NSImage] = [:]
    private var finished: Set<String> = []
    private var loading: Set<String> = []
    private var ids: [String: String] = [:]
    private let fm = FileManager.default
    private var cacheRoot: URL {
        fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Caches/com.nixexv.deadlockwarmer/artwork")
    }

    func image(_ game: WarmerModel.Game, kind: Kind) -> NSImage? { images[game.id + kind.rawValue] }

    func load(_ game: WarmerModel.Game, retry: Bool = false) async {
        guard let id = steamID(game) else { return }
        for kind in Kind.allCases {
            let key = game.id + kind.rawValue
            if retry && images[key] == nil { finished.remove(key) }
            guard !finished.contains(key), !loading.contains(key) else { continue }
            loading.insert(key)
            if let local = localImage(id: id, kind: kind) {
                images[key] = local; finished.insert(key); loading.remove(key); continue
            }
            if UserDefaults.standard.object(forKey: "downloadArtwork") as? Bool ?? true {
                let file = kind == .cover ? "library_600x900.jpg" : "library_hero.jpg"
                let url = URL(string: "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/\(id)/\(file)")!
                var request = URLRequest(url: url, timeoutInterval: 8)
                request.cachePolicy = .returnCacheDataElseLoad
                do {
                    let (data, response) = try await URLSession.shared.data(for: request)
                    if (response as? HTTPURLResponse)?.statusCode == 200,
                       data.count < 8 * 1024 * 1024, let image = NSImage(data: data) {
                        images[key] = image
                        let destination = cacheRoot.appendingPathComponent(id).appendingPathComponent(file)
                        try? fm.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
                        try? data.write(to: destination, options: .atomic)
                    }
                } catch { /* Offline and missing artwork fall back to a local executable icon. */ }
            }
            loading.remove(key)
            if !Task.isCancelled { finished.insert(key) }
        }
    }

    private func steamID(_ game: WarmerModel.Game) -> String? {
        if let cached = ids[game.id] { return cached }
        let path = URL(fileURLWithPath: game.path).standardized.pathComponents
        guard let index = path.lastIndex(of: "steamapps"), path.count > index + 2,
              path[index + 1] == "common" else { return nil }
        let directory = URL(fileURLWithPath: NSString.path(withComponents: Array(path.prefix(index + 1))))
        let folder = path[index + 2]
        let files = (try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        for file in files where file.lastPathComponent.hasPrefix("appmanifest_") && file.pathExtension == "acf" {
            guard let data = try? String(contentsOf: file, encoding: .utf8),
                  let range = data.range(of: #""installdir"\s+"([^"\r\n]+)""#, options: .regularExpression) else { continue }
            let record = String(data[range])
            guard record.contains("\"\(folder)\"") else { continue }
            let id = file.deletingPathExtension().lastPathComponent.replacingOccurrences(of: "appmanifest_", with: "")
            guard !id.isEmpty, id.allSatisfy(\.isNumber) else { continue }
            ids[game.id] = id; return id
        }
        return nil
    }

    private func localImage(id: String, kind: Kind) -> NSImage? {
        let names = kind == .cover ? ["library_600x900.jpg", "library_capsule.jpg", "library_600x900_2x.jpg"]
                                  : ["library_hero.jpg", "library_header.jpg", "header.jpg"]
        let home = fm.homeDirectoryForCurrentUser
        let roots = [cacheRoot.appendingPathComponent(id),
                     home.appendingPathComponent("Library/Application Support/Steam/appcache/librarycache/\(id)"),
                     home.appendingPathComponent("Library/Application Support/com.gamemac.www/wine-engine/downloads/steam_client/drive_c/Program Files (x86)/Steam/appcache/librarycache/\(id)")]
        for root in roots {
            for name in names {
                if let image = NSImage(contentsOf: root.appendingPathComponent(name)) { return image }
            }
            let directories = (try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: [.isDirectoryKey])) ?? []
            for directory in directories.prefix(80) {
                for name in names {
                    if let image = NSImage(contentsOf: directory.appendingPathComponent(name)) { return image }
                }
            }
        }
        return nil
    }
}

struct ArtworkView: View {
    let game: WarmerModel.Game
    var kind: GameArtwork.Kind = .cover
    @EnvironmentObject private var artwork: GameArtwork
    var body: some View {
        GeometryReader { geometry in
            ZStack {
                WarmerTheme.raised
                if let image = artwork.image(game, kind: kind) ?? artwork.image(game, kind: .cover) {
                    Image(nsImage: image).resizable().aspectRatio(contentMode: .fill)
                        .frame(width: geometry.size.width, height: geometry.size.height).clipped()
                } else if let icon = GameIcons.art(name: game.name, installPath: game.path, exeName: game.exeName) {
                    Image(nsImage: icon).resizable().aspectRatio(contentMode: .fit)
                        .padding(geometry.size.width * 0.18)
                } else {
                    Image(systemName: "gamecontroller").font(.system(size: 42, weight: .light))
                        .foregroundStyle(WarmerTheme.secondary)
                }
            }
        }.accessibilityHidden(true).task(id: game.id) { await artwork.load(game) }
    }
}
