import AppKit
import CryptoKit
import SwiftUI

struct LauncherProfile: Codable, Identifiable, Hashable {
    var name: String
    var prefix: String
    var wine: String
    var kind: String
    var appPath: String?
    var id: String { String(SHA256.hash(data: Data(URL(fileURLWithPath: prefix).resolvingSymlinksInPath().path.utf8)).map { String(format: "%02x", $0) }.joined().prefix(24)) }
    var title: String { kind == "crossover" ? "CrossOver · \(name)" : name }
    static var file: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ShaderWarmer/launcher-profiles.json")
    }
    static func saved() -> [LauncherProfile] {
        guard let data = try? Data(contentsOf: file) else { return [] }
        return (try? JSONDecoder().decode([LauncherProfile].self, from: data)) ?? []
    }
    static func save(_ profiles: [LauncherProfile]) {
        try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? JSONEncoder().encode(profiles).write(to: file, options: .atomic)
    }
    static func discover() -> [LauncherProfile] {
        let fm = FileManager.default
        var profiles = saved()
        var apps = [URL(fileURLWithPath: "/Applications/CrossOver.app"), fm.homeDirectoryForCurrentUser.appendingPathComponent("Applications/CrossOver.app")]
        if let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.codeweavers.CrossOver") { apps.insert(app, at: 0) }
        let bottles = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CrossOver/Bottles")
        if let app = apps.first(where: { fm.fileExists(atPath: $0.appendingPathComponent("Contents/SharedSupport/CrossOver/bin/wine").path) }) {
            for prefix in (try? fm.contentsOfDirectory(at: bottles, includingPropertiesForKeys: nil)) ?? [] {
                guard fm.fileExists(atPath: prefix.appendingPathComponent("user.reg").path) else { continue }
                let profile = LauncherProfile(name: prefix.lastPathComponent, prefix: prefix.path,
                    wine: app.appendingPathComponent("Contents/SharedSupport/CrossOver/bin/wine").path, kind: "crossover", appPath: app.path)
                if !profiles.contains(where: { $0.id == profile.id }) { profiles.append(profile) }
            }
        }
        return profiles.sorted { $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending }
    }
}

struct AddLauncherSheet: View {
    let onSave: (LauncherProfile) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var prefix = ""
    @State private var wine = ""
    @State private var error = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text(L("Подключить лаунчер")).font(.system(size: 24, weight: .bold))
            Text(L("Выбери существующий Wine-контейнер и Wine, которым запускается игра. CrossOver определяется автоматически; другие Wine-лаунчеры подключаются здесь."))
                .font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary).fixedSize(horizontal: false, vertical: true)
            TextField(L("Название лаунчера или среды"), text: $name)
            pathRow(L("Контейнер"), path: $prefix, directory: true)
            pathRow(L("Wine или приложение лаунчера"), path: $wine, directory: false)
            Text(L("В контейнере должны быть user.reg и drive_c. Для других Wine-сред запуск с Async будет доступен из Shader Warmer."))
                .font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
            if !error.isEmpty { Text(error).font(.system(size: 12)).foregroundStyle(WarmerTheme.error) }
            HStack {
                Button(L("Отмена")) { dismiss() }.buttonStyle(WarmerButtonStyle())
                Spacer()
                Button(L("Подключить")) { save() }.buttonStyle(WarmerButtonStyle(prominent: true))
            }
        }.textFieldStyle(.roundedBorder).padding(28).frame(width: 560)
            .foregroundStyle(WarmerTheme.text).background(WarmerTheme.background).preferredColorScheme(.dark)
    }
    private func pathRow(_ title: String, path: Binding<String>, directory: Bool) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 12, weight: .semibold))
            HStack {
                TextField(title, text: path)
                Button(L("Выбрать…")) {
                    let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = !directory
                    panel.allowsMultipleSelection = false; panel.treatsFilePackagesAsDirectories = false
                    if panel.runModal() == .OK, let url = panel.url { path.wrappedValue = url.path }
                }.buttonStyle(WarmerButtonStyle(compact: true))
            }
        }
    }
    private func save() {
        let fm = FileManager.default
        let folder = URL(fileURLWithPath: (prefix as NSString).expandingTildeInPath).resolvingSymlinksInPath()
        let selected = URL(fileURLWithPath: (wine as NSString).expandingTildeInPath)
        var app: URL?; var binary = selected
        if selected.pathExtension == "app" {
            app = selected
            let candidates = ["Contents/SharedSupport/CrossOver/bin/wine", "Contents/Resources/wine/bin/wine64", "Contents/Resources/wine/bin/wine", "Contents/SharedSupport/wine/bin/wine64", "Contents/Resources/wine/bin/wineloader"]
            guard let found = candidates.map({ selected.appendingPathComponent($0) }).first(where: { fm.isExecutableFile(atPath: $0.path) }) else {
                error = L("Wine внутри приложения не найден. Выбери исполняемый bin/wine или bin/wine64 вручную."); return
            }
            binary = found
        } else if let range = selected.path.range(of: ".app/") { app = URL(fileURLWithPath: String(selected.path[..<range.upperBound].dropLast())) }
        guard fm.fileExists(atPath: folder.appendingPathComponent("user.reg").path), fm.fileExists(atPath: folder.appendingPathComponent("drive_c").path) else {
            error = L("Выбери корневую папку готового Wine-контейнера."); return
        }
        guard fm.isExecutableFile(atPath: binary.path), fm.fileExists(atPath: binary.deletingLastPathComponent().appendingPathComponent("wineserver").path) else {
            error = L("Выбери Wine из полного рантайма: рядом должен находиться wineserver."); return
        }
        let cross = binary.path.contains("/SharedSupport/CrossOver/") && fm.fileExists(atPath: folder.appendingPathComponent("cxbottle.conf").path)
        let profile = LauncherProfile(name: name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? folder.lastPathComponent : name,
                                      prefix: folder.path, wine: binary.path, kind: cross ? "crossover" : "wine", appPath: app?.path)
        onSave(profile); dismiss()
    }
}
