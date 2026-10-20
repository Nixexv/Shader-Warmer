import Foundation

// Finds games installed on this Mac by scanning the usual Steam "steamapps/common"
// locations (native Steam, GameHub, GameHub's wine containers, external volumes).
// Deadlock is detected as warm-able; everything else is listed but not yet supported.
enum GameScanner {
    private static let fm = FileManager.default
    private static let skip: Set<String> = ["Steamworks Shared", "Steam Controller Configs"]

    private static func commonRoots() -> [URL] {
        let home = fm.homeDirectoryForCurrentUser
        // Only GPTK/GameHub libraries — native macOS Steam games don't use D3DMetal, nothing to warm.
        var roots: [URL] = [
            home.appendingPathComponent("GameHub/steamapps/common"),
        ]
        let containers = home.appendingPathComponent(
            "Library/Application Support/com.gamemac.www/wine-engine/containers/virtual_containers")
        if let subs = try? fm.contentsOfDirectory(at: containers, includingPropertiesForKeys: nil) {
            for c in subs {
                roots.append(c.appendingPathComponent("drive_c/Program Files (x86)/Steam/steamapps/common"))
            }
        }
        if let vols = try? fm.contentsOfDirectory(atPath: "/Volumes") {
            for v in vols {
                for pat in ["steamGames/steamapps/common", "SteamLibrary/steamapps/common", "steamapps/common"] {
                    roots.append(URL(fileURLWithPath: "/Volumes/\(v)/\(pat)"))
                }
            }
        }
        return roots
    }

    private static func sourceLabel(_ root: URL) -> String {
        let p = root.path
        if p.hasPrefix("/Volumes/") { return "Внешний диск" }
        return "GameHub"
    }

    // A game runs through GPTK/D3DMetal only if it's a Windows build — i.e. ships a .exe.
    // Native macOS games have no .exe, so this drops them from the list. Bounded scan.
    private static func hasWindowsExe(_ dir: URL) -> Bool {
        var queue: [(URL, Int)] = [(dir, 0)]
        var visited = 0
        while !queue.isEmpty, visited < 500 {
            let (d, depth) = queue.removeFirst()
            guard let items = try? fm.contentsOfDirectory(
                at: d, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles]) else { continue }
            for it in items {
                visited += 1
                if it.pathExtension.lowercased() == "exe" { return true }
                if depth < 4 {
                    var isDir: ObjCBool = false
                    if fm.fileExists(atPath: it.path, isDirectory: &isDir), isDir.boolValue { queue.append((it, depth + 1)) }
                }
            }
        }
        return false
    }

    // Deadlock ships game/citadel/shaders_pc_dir.vpk, Overwatch data/.build.config + Overwatch.exe —
    // that's what each warmer needs to read the game's shaders.
    // A Unity build is "<name>.exe" next to "<name>_Data/globalgamemanagers".
    private static func unityExe(_ dir: URL) -> String? {
        guard let items = try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return nil }
        for d in items where d.lastPathComponent.hasSuffix("_Data") {
            guard fm.fileExists(atPath: d.appendingPathComponent("globalgamemanagers").path) else { continue }
            let name = String(d.lastPathComponent.dropLast(5)) + ".exe"
            if fm.fileExists(atPath: dir.appendingPathComponent(name).path) { return name }
        }
        return nil
    }

    private static func isOverwatch(_ dir: URL) -> Bool {
        fm.fileExists(atPath: dir.appendingPathComponent("data/.build.config").path)
            && fm.fileExists(atPath: dir.appendingPathComponent("Overwatch.exe").path)
    }

    private static func game(dir: URL, name: String, source: String) -> WarmerModel.Game {
        let inner = dir.appendingPathComponent("game/citadel/shaders_pc_dir.vpk")
        let isDeadlock = name.lowercased() == "deadlock" || fm.fileExists(atPath: inner.path)
        if isDeadlock {
            let path = fm.fileExists(atPath: inner.path) ? dir.appendingPathComponent("game").path : dir.path
            return .init(name: name, path: path, source: source, supported: true, exeName: "deadlock.exe",
                         engine: WarmerModel.GameEngine.deadlock.rawValue)
        }
        if isOverwatch(dir) {
            return .init(name: name, path: dir.path, source: source, supported: true, exeName: "Overwatch.exe",
                         engine: WarmerModel.GameEngine.overwatch.rawValue)
        }
        if let exe = unityExe(dir) {
            return .init(name: name, path: dir.path, source: source, supported: true, exeName: exe,
                         engine: WarmerModel.GameEngine.unity.rawValue)
        }
        return .init(name: name, path: dir.path, source: source, supported: false, exeName: nil, dxmtCandidate: true)
    }

    // The libraries GameHub itself knows: each game binding records its library root, which is
    // the only place an external library (say /Volumes/T5/steamGames) is written down.
    private static func gamehubLibraries() -> [String] {
        let store = fm.homeDirectoryForCurrentUser.appendingPathComponent(
            "Library/Application Support/com.gamemac.www/gamehub/game_container_store.json")
        guard let data = try? Data(contentsOf: store),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let bindings = obj["bindings"] as? [[String: Any]] else { return [] }
        return bindings.compactMap { $0["game_path"] as? String }
    }

    private static func isMissing(_ error: Error) -> Bool {
        let ns = error as NSError
        if ns.code == NSFileReadNoSuchFileError || ns.code == NSFileNoSuchFileError { return true }
        if let u = ns.userInfo[NSUnderlyingErrorKey] as? NSError, u.domain == NSPOSIXErrorDomain {
            return u.code == Int(ENOENT) || u.code == Int(ENOTDIR)
        }
        return false
    }

    /// Games found, a plain-text report of every place looked at (written to the logs), and the
    /// folders macOS would not let us read — usually an external drive the app was never allowed
    /// onto; an ad-hoc signed build loses that permission on every update.
    static func scanWithReport() -> ([WarmerModel.Game], String, [String]) {
        var report: [String] = []
        var issues: [String] = []
        var roots = commonRoots().filter { !$0.path.hasPrefix("/Volumes/") }
        roots += gamehubLibraries().map { URL(fileURLWithPath: $0).appendingPathComponent("steamapps/common") }
        if let vols = try? fm.contentsOfDirectory(atPath: "/Volumes") {
            for v in vols where !v.hasPrefix(".") {
                let vol = URL(fileURLWithPath: "/Volumes").appendingPathComponent(v)
                do {
                    let top = try fm.contentsOfDirectory(atPath: vol.path)
                    roots.append(vol.appendingPathComponent("steamapps/common"))
                    for d in top where !d.hasPrefix(".") {
                        roots.append(vol.appendingPathComponent(d).appendingPathComponent("steamapps/common"))
                    }
                } catch where !isMissing(error) {
                    issues.append(vol.path)
                    report.append("НЕТ ДОСТУПА \(vol.path): \(error.localizedDescription)")
                } catch {}
            }
        }

        var seenRoots = Set<String>(), seen = Set<String>()
        var out: [WarmerModel.Game] = []
        for root in roots {
            let key = root.standardizedFileURL.path
            if seenRoots.contains(key) { continue }
            seenRoots.insert(key)
            let entries: [URL]
            do {
                entries = try fm.contentsOfDirectory(at: root, includingPropertiesForKeys: [.isDirectoryKey])
            } catch where !isMissing(error) {
                issues.append(root.path)
                report.append("НЕТ ДОСТУПА \(root.path): \(error.localizedDescription)")
                continue
            } catch {
                continue                                   // no library here — the normal case
            }
            report.append("библиотека \(root.path): \(entries.count) папок")
            for e in entries {
                var isDir: ObjCBool = false
                guard fm.fileExists(atPath: e.path, isDirectory: &isDir), isDir.boolValue else { continue }
                let name = e.lastPathComponent
                if skip.contains(name) || name.hasPrefix(".") || seen.contains(name.lowercased()) { continue }
                // Known games by their own markers first: the bounded .exe search below can run out of
                // budget in a big game folder before reaching the exe, and drop the very games we warm.
                let g = game(dir: e, name: name, source: sourceLabel(root))
                if !g.supported { guard hasWindowsExe(e) else { continue } }
                seen.insert(name.lowercased())
                out.append(g)
                report.append("  игра \(name) [\(g.supported ? g.engine : "не поддерживается")] \(g.path)")
            }
        }
        if out.isEmpty { report.append("игр не найдено") }
        let games = out.sorted {
            $0.supported != $1.supported ? $0.supported
                : $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
        return (games, report.joined(separator: "\n"), Array(Set(issues)).sorted())
    }

    static func scanProfiles(_ profiles: [LauncherProfile]) -> [WarmerModel.Game] {
        var games: [WarmerModel.Game] = []
        for profile in profiles {
            let prefix = URL(fileURLWithPath: profile.prefix)
            var roots = [prefix.appendingPathComponent("drive_c/Program Files (x86)/Steam/steamapps/common"),
                         prefix.appendingPathComponent("drive_c/Program Files/Steam/steamapps/common")]
            // Steam libraries may live outside the bottle on another volume.
            for root in roots {
                let vdf = root.deletingLastPathComponent().appendingPathComponent("libraryfolders.vdf")
                if let text = try? String(contentsOf: vdf, encoding: .utf8),
                   let regex = try? NSRegularExpression(pattern: #""path"\s+"([^"\r\n]+)""#) {
                    let ns = text as NSString
                    for match in regex.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
                        let raw = ns.substring(with: match.range(at: 1)).replacingOccurrences(of: "\\\\", with: "\\")
                        let path: URL?
                        if raw.hasPrefix("/") { path = URL(fileURLWithPath: raw) }
                        else if raw.count > 2, raw.dropFirst().hasPrefix(":") {
                            let drive = String(raw.prefix(1)).lowercased() + ":"
                            path = prefix.appendingPathComponent("dosdevices/" + drive).appendingPathComponent(String(raw.dropFirst(3)).replacingOccurrences(of: "\\", with: "/"))
                        } else { path = nil }
                        if let path { roots.append(path.appendingPathComponent("steamapps/common")) }
                    }
                }
            }
            var seen = Set<String>()
            for root in roots {
                for folder in (try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? [] {
                    let key = folder.resolvingSymlinksInPath().path
                    guard !seen.contains(key), !skip.contains(folder.lastPathComponent), !folder.lastPathComponent.hasPrefix(".") else { continue }
                    var item = manual(folder.resolvingSymlinksInPath())
                    // Recognized game markers must win over the bounded executable search.
                    // Large Source 2 installs can exhaust its budget before game/bin/win64.
                    guard item.supported || item.dxmtCandidate == true else { continue }
                    seen.insert(key)
                    item.profileID = profile.id
                    item = .init(name: item.name, path: item.path, source: profile.title, supported: item.supported,
                                 exeName: item.exeName, profileID: profile.id, dxmtCandidate: true, engine: item.engine)
                    games.append(item)
                }
            }
        }
        return games
    }

    static func scan() -> [WarmerModel.Game] { scanWithReport().0 }

    // A folder the user picked by hand. Accept the game dir or its parent.
    static func manual(_ dir: URL) -> WarmerModel.Game {
        let hereVPK = dir.appendingPathComponent("citadel/shaders_pc_dir.vpk")
        let innerVPK = dir.appendingPathComponent("game/citadel/shaders_pc_dir.vpk")
        if fm.fileExists(atPath: hereVPK.path) || fm.fileExists(atPath: innerVPK.path) {
            let path = fm.fileExists(atPath: innerVPK.path) ? dir.appendingPathComponent("game").path : dir.path
            return .init(name: dir.lastPathComponent, path: path, source: "вручную", supported: true,
                         exeName: "deadlock.exe", engine: WarmerModel.GameEngine.deadlock.rawValue)
        }
        if isOverwatch(dir) {
            return .init(name: dir.lastPathComponent, path: dir.path, source: "вручную", supported: true,
                         exeName: "Overwatch.exe", engine: WarmerModel.GameEngine.overwatch.rawValue)
        }
        if let exe = unityExe(dir) {
            return .init(name: dir.lastPathComponent, path: dir.path, source: "вручную", supported: true,
                         exeName: exe, engine: WarmerModel.GameEngine.unity.rawValue)
        }
        return .init(name: dir.lastPathComponent, path: dir.path, source: "вручную", supported: false, exeName: nil, dxmtCandidate: hasWindowsExe(dir))
    }
}
