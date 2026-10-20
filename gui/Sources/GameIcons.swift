import AppKit

// Artwork for a game tile.  First choice is the icon the game itself carries — the one Windows shows
// on the desktop, stored in the .exe's resource section — because that is what a player recognises.
// If the executable has none, GameHub's own icon file is used (it is not always square: Deadlock's is
// a 231x87 banner), and failing that the view falls back to a symbol.
enum GameIcons {
    private static var cache: [String: NSImage?] = [:]

    static func art(name: String, installPath: String?, exeName: String?) -> NSImage? {
        if let hit = cache[name] { return hit }
        var image: NSImage?
        if let dir = installPath, let exe = exeName, let url = findExecutable(dir: dir, name: exe) {
            image = iconFromExecutable(url)
        }
        if image == nil { image = launcherIcon(for: name) }
        cache[name] = image
        return image
    }

    // MARK: GameHub's icon cache

    private static let iconDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/com.gamemac.www/icons")

    private static var launcherFiles: [(key: String, url: URL)] = {
        let items = (try? FileManager.default.contentsOfDirectory(at: iconDir, includingPropertiesForKeys: nil)) ?? []
        return items.filter { $0.pathExtension.lowercased() == "png" }
            .map { (normalize($0.deletingPathExtension().lastPathComponent), $0) }
    }()

    private static func normalize(_ s: String) -> String { s.lowercased().filter { $0.isLetter || $0.isNumber } }

    private static func launcherIcon(for name: String) -> NSImage? {
        let key = normalize(name)
        let match = launcherFiles.first { $0.key == key }
            ?? launcherFiles.first { $0.key.hasPrefix(key) || key.hasPrefix($0.key) }
        return match.flatMap { NSImage(contentsOf: $0.url) }
    }

    // MARK: the game's own executable

    /// The exe is not always at the top of the install dir (Deadlock's lives in game/bin/win64).
    private static func findExecutable(dir: String, name: String) -> URL? {
        let fm = FileManager.default
        let root = URL(fileURLWithPath: dir)
        let direct = root.appendingPathComponent(name)
        if fm.fileExists(atPath: direct.path) { return direct }
        var queue = [(root, 0)]
        var visited = 0
        while !queue.isEmpty, visited < 400 {
            let (d, depth) = queue.removeFirst()
            guard let items = try? fm.contentsOfDirectory(at: d, includingPropertiesForKeys: [.isDirectoryKey],
                                                          options: [.skipsHiddenFiles]) else { continue }
            for it in items {
                visited += 1
                if it.lastPathComponent.caseInsensitiveCompare(name) == .orderedSame { return it }
                var isDir: ObjCBool = false
                if depth < 4, fm.fileExists(atPath: it.path, isDirectory: &isDir), isDir.boolValue {
                    queue.append((it, depth + 1))
                }
            }
        }
        return nil
    }

    /// Pull the largest RT_ICON out of a PE file's resource directory.
    private static func iconFromExecutable(_ url: URL) -> NSImage? {
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe), data.count > 0x40 else { return nil }
        func u16(_ o: Int) -> Int { o + 2 <= data.count ? Int(data[o]) | Int(data[o + 1]) << 8 : 0 }
        func u32(_ o: Int) -> Int {
            o + 4 <= data.count ? Int(data[o]) | Int(data[o + 1]) << 8 | Int(data[o + 2]) << 16 | Int(data[o + 3]) << 24 : 0
        }
        let peOff = u32(0x3C)
        guard peOff > 0, peOff + 24 < data.count, data[peOff] == 0x50, data[peOff + 1] == 0x45 else { return nil }
        let sections = u16(peOff + 6)
        let optSize = u16(peOff + 20)
        let optOff = peOff + 24
        let magic = u16(optOff)
        let dirOff = optOff + (magic == 0x20b ? 112 : 96)          // PE32+ has wider fields before the tables
        let resourceRVA = u32(dirOff + 2 * 8)                      // data directory 2 = resources
        guard resourceRVA > 0 else { return nil }

        // section table: RVA -> file offset
        var secs: [(rva: Int, size: Int, raw: Int)] = []
        let secOff = optOff + optSize
        for i in 0..<sections {
            let o = secOff + i * 40
            guard o + 40 <= data.count else { break }
            secs.append((u32(o + 12), max(u32(o + 8), u32(o + 16)), u32(o + 20)))
        }
        func offset(_ rva: Int) -> Int? {
            for s in secs where rva >= s.rva && rva < s.rva + s.size { return s.raw + (rva - s.rva) }
            return nil
        }
        guard let resBase = offset(resourceRVA) else { return nil }

        // walk type -> name -> language, collecting every RT_ICON payload
        func entries(_ dir: Int) -> [(id: Int, offset: Int, isDir: Bool)] {
            guard dir + 16 <= data.count else { return [] }
            let named = u16(dir + 12), ids = u16(dir + 14)
            var out: [(Int, Int, Bool)] = []
            for i in 0..<(named + ids) {
                let e = dir + 16 + i * 8
                guard e + 8 <= data.count else { break }
                let nameOrId = u32(e), off = u32(e + 4)
                out.append((nameOrId & 0x7FFFFFFF, off & 0x7FFFFFFF, off & 0x8000_0000 != 0))
            }
            return out
        }
        var best: Data?
        var bestSize = 0
        for type in entries(resBase) where type.id == 3 && type.isDir {          // RT_ICON
            for name in entries(resBase + type.offset) where name.isDir {
                for lang in entries(resBase + name.offset) where !lang.isDir {
                    let e = resBase + lang.offset
                    guard e + 16 <= data.count, let start = offset(u32(e)) else { continue }
                    let size = u32(e + 4)
                    guard size > bestSize, start + size <= data.count else { continue }
                    best = data.subdata(in: start..<(start + size))
                    bestSize = size
                }
            }
        }
        guard let payload = best else { return nil }
        if payload.count > 8, payload[0] == 0x89, payload[1] == 0x50 {           // Vista+ PNG icon
            return NSImage(data: payload)
        }
        return NSImage(data: icoWrapper(dib: payload))
    }

    /// An RT_ICON payload is a bare DIB; NSImage reads it once it is wrapped in an .ico container.
    private static func icoWrapper(dib: Data) -> Data {
        func u32(_ o: Int) -> Int {
            o + 4 <= dib.count ? Int(dib[o]) | Int(dib[o + 1]) << 8 | Int(dib[o + 2]) << 16 | Int(dib[o + 3]) << 24 : 0
        }
        let width = u32(4), height = u32(8) / 2                                   // DIB height covers mask too
        let planes = dib.count > 14 ? Int(dib[12]) | Int(dib[13]) << 8 : 1
        let bits = dib.count > 16 ? Int(dib[14]) | Int(dib[15]) << 8 : 32
        var out = Data()
        func put16(_ v: Int) { out.append(UInt8(v & 0xFF)); out.append(UInt8((v >> 8) & 0xFF)) }
        func put32(_ v: Int) { put16(v & 0xFFFF); put16((v >> 16) & 0xFFFF) }
        put16(0); put16(1); put16(1)                                              // ICONDIR
        out.append(UInt8(width >= 256 ? 0 : width))
        out.append(UInt8(height >= 256 ? 0 : height))
        out.append(0); out.append(0)                                              // palette, reserved
        put16(planes); put16(bits)
        put32(dib.count); put32(22)                                               // size, offset
        out.append(dib)
        return out
    }
}
