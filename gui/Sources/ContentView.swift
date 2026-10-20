import SwiftUI
import AppKit

struct ContentView: View {
    @EnvironmentObject private var model: WarmerModel
    let engine: Engine
    @StateObject private var artwork = GameArtwork()
    @State private var collectingLogs = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    static let version = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "?"

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Rectangle().fill(WarmerTheme.border.opacity(0.65)).frame(width: 1)
            Group {
                switch model.screen {
                case .main:
                    GameLibraryView(onSelect: select, onAdd: addGame, onRefresh: refresh)
                case .game:
                    GameDetailView(engine: engine, onBack: { model.screen = .main }, onCollectLogs: collectLogs,
                                   onPickGPTK: pickGptk, collectingLogs: collectingLogs)
                case .settings:
                    WarmerSettingsView(onCollectLogs: collectLogs, collectingLogs: collectingLogs)
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .environmentObject(artwork)
        .foregroundStyle(WarmerTheme.text).tint(WarmerTheme.accent)
        .background(WarmerTheme.background).preferredColorScheme(.dark)
        .frame(minWidth: 960, minHeight: 700)
        .transaction { if reduceMotion { $0.animation = nil } }
        .sheet(isPresented: $model.showingAddLauncher) {
            AddLauncherSheet { profile in
                var saved = LauncherProfile.saved(); saved.removeAll { $0.id == profile.id }; saved.append(profile)
                LauncherProfile.save(saved); model.launcherProfiles = LauncherProfile.discover()
                engine.scanLibrary(model); model.libraryLauncher = profile.id
                if model.screen == .game { engine.assignLauncher(model, profile: profile) }
            }
        }
        .task { engine.scanLibrary(model); await engine.detect(model) }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            guard model.phase != .detecting else { return }
            engine.scanLibrary(model)
            if model.phase == .idle && model.detectedForSelection && (model.needsPlay || model.deadlockRunning) {
                Task { await engine.detect(model) }
            }
        }
    }

    private var sidebar: some View {
        VStack(spacing: 16) {
            Image(systemName: "flame.fill").font(.system(size: 25)).foregroundStyle(WarmerTheme.accent)
                .frame(height: 60).padding(.top, 8).accessibilityLabel("Shader Warmer")
            navigation(.main, title: "Главная", symbol: "house.fill")
            navigation(.settings, title: "Настройки", symbol: "gearshape.fill")
            Spacer()
            Button(action: collectLogs) {
                VStack(spacing: 7) {
                    Image(systemName: "doc.zipper").font(.system(size: 18))
                    Text(collectingLogs ? "Собираем…" : "Логи").font(.system(size: 10))
                }.foregroundStyle(WarmerTheme.secondary).frame(width: 68, height: 58)
            }.buttonStyle(.plain).disabled(collectingLogs || model.isBusy).help("Собрать логи")
                .accessibilityLabel("Собрать логи")
            Text("v\(Self.version)").font(.system(size: 10)).foregroundStyle(WarmerTheme.secondary)
                .padding(.bottom, 18)
        }.frame(width: 88).frame(maxHeight: .infinity).background(WarmerTheme.rail)
    }

    private func navigation(_ screen: WarmerModel.Screen, title: String, symbol: String) -> some View {
        let selected = screen == .main ? model.screen != .settings : model.screen == screen
        return Button { model.screen = screen } label: {
            VStack(spacing: 8) {
                Image(systemName: symbol).font(.system(size: 20))
                Text(title).font(.system(size: 10, weight: selected ? .semibold : .regular))
            }.foregroundStyle(selected ? WarmerTheme.accent : WarmerTheme.secondary)
                .frame(width: 68, height: 64)
                .background(selected ? WarmerTheme.accent.opacity(0.09) : Color.clear, in: RoundedRectangle(cornerRadius: 11))
                .overlay(alignment: .leading) { if selected { Capsule().fill(WarmerTheme.accent).frame(width: 2, height: 34) } }
        }.buttonStyle(.plain).accessibilityLabel(title).help(title)
    }

    private func refresh() {
        guard !model.isBusy else { return }
        engine.scanLibrary(model); model.phase = .detecting
        Task { await engine.detect(model) }
    }

    private func select(_ g: WarmerModel.Game) {
        if model.isBusy {
            // The running warm owns the selection (switching mid-run filed the result under the
            // wrong game and restarted detection over it) — but its own tile and the home card must
            // still lead back to its page, or there is no way to watch or cancel it.
            if g.id == model.warmingGame?.id { model.screen = .game }
            return
        }
        let same = model.selectedGame?.id == g.id
        model.selectedGame = g
        if !same { model.dx11Confirmed = false }
        if !g.supported || g.profileID != nil { model.method = .dxmt }
        model.screen = .game
        // reopening the game just warmed keeps its result and journal; another game starts fresh
        if !same {
            if model.phase == .done || model.phase == .failed || model.phase == .cancelled { model.phase = .idle }
            model.stageHistory = []; model.currentStage = ""
            model.log.removeAll()
            model.checks.removeAll()
        }
        guard g.supported || g.canUseDXMT, !same || !model.detectedForSelection else { return }
        model.clearDetected()
        model.phase = .idle
        model.overrideGame = g.path
        Task { model.phase = .detecting; await engine.detect(model) }
    }

    private func addGame() {
        guard let url = chooseFolder("Папка установленной игры") else { return }
        var g = GameScanner.manual(url)
        if let profile = model.launcherProfiles.first(where: { $0.id == model.libraryLauncher }) { g.profileID = profile.id }
        if !model.manualGames.contains(where: { $0.id == g.id }) {
            model.manualGames.insert(g, at: 0)
            WarmerModel.Game.saveManual(model.manualGames)    // or the next rescan drops it
        }
        if !model.games.contains(where: { $0.id == g.id }) { model.games.insert(g, at: 0) }
        select(g)
    }

    private func pickGptk() {
        guard !model.isBusy,
              let url = chooseFolder("Папка GPTK (внутри неё external/D3DMetal.framework)") else { return }
        model.overrideGptk = url.path
        Task { model.phase = .detecting; await engine.detect(model) }
    }

    private func chooseFolder(_ message: String) -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Выбрать"
        panel.message = message
        return panel.runModal() == .OK ? panel.url : nil
    }

    private func collectLogs() {
        guard !collectingLogs else { return }
        collectingLogs = true
        Task { @MainActor in
            await engine.captureDiagnostics(model)
            archiveLogs()
            collectingLogs = false
        }
    }

    private func archiveLogs() {
        let fm = FileManager.default
        let home = NSHomeDirectory() as NSString
        let out = home.appendingPathComponent("Desktop/ShaderWarmer-logs.zip")
        let dirs = ["Library/Logs/DeadlockWarmer", "Library/Logs/OverwatchWarmer", "Library/Logs/ShaderWarmer"]
            .map { home.appendingPathComponent($0) }
            .filter { fm.fileExists(atPath: $0) }
        guard !dirs.isEmpty else {
            showLogExportError("Логов пока нет. Сначала запусти прогрев или запись диагностики.")
            return
        }
        let staging = fm.temporaryDirectory.appendingPathComponent("ShaderWarmer-logs-\(UUID().uuidString)")
        do {
            try fm.createDirectory(at: staging, withIntermediateDirectories: true)
            for source in dirs {
                let src = URL(fileURLWithPath: source)
                try fm.copyItem(at: src, to: staging.appendingPathComponent(src.lastPathComponent))
            }
            if fm.fileExists(atPath: out) { try fm.removeItem(atPath: out) }
        } catch {
            try? fm.removeItem(at: staging)
            showLogExportError("Не удалось подготовить логи: \(error.localizedDescription)")
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
        p.arguments = ["-c", "-k", "--sequesterRsrc", "--keepParent", staging.path, out]
        let errors = Pipe()
        p.standardError = errors
        p.terminationHandler = { proc in
            let detail = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            try? fm.removeItem(at: staging)
            DispatchQueue.main.async {
                if proc.terminationStatus == 0 && fm.fileExists(atPath: out) {
                    model.statusText = "Логи сохранены на Рабочем столе"
                    NSWorkspace.shared.selectFile(out, inFileViewerRootedAtPath: NSHomeDirectory() + "/Desktop")
                } else {
                    showLogExportError("Не удалось создать архив логов" + (detail.isEmpty ? "." : ": \(detail)"))
                }
            }
        }
        do { try p.run() }
        catch {
            try? fm.removeItem(at: staging)
            showLogExportError("Не удалось запустить архиватор: \(error.localizedDescription)")
        }
    }

    private func showLogExportError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Сбор логов"
        alert.informativeText = message
        alert.runModal()
    }
}
