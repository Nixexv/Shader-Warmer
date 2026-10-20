import Foundation
import AppKit
import Darwin
import Metal

// Runs the bundled Python engine (warm.py + helpers) and streams its output.
@MainActor
final class Engine {
    private var process: Process?
    private var monitor: Process?   // background `log stream` of D3DMetal cache messages
    private var cancelRequested = false

    init() {
        // don't leave an orphaned `log stream` behind if the app quits while monitoring
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.stopProcessTree(force: true)
                self?.monitor?.terminate(); self?.monitor = nil
            }
        }
    }

    static var logDir: String { (NSHomeDirectory() as NSString).appendingPathComponent("Library/Logs/DeadlockWarmer") }

    // Toggle a continuous capture of D3DMetal's own cache messages — leave it ON while the
    // user plays, to see whether the game itself ever clears the cache (warm-time capture
    // stops at DONE; this covers everything after).
    func toggleMonitor(_ model: WarmerModel) {
        if monitor != nil { monitor?.terminate(); model.monitoring = false; return }
        startMonitor(model)
    }

    private func startMonitor(_ model: WarmerModel) {
        guard monitor == nil, let python = Engine.python else { return }
        let df = DateFormatter(); df.dateFormat = "yyyyMMdd_HHmmss"
        let path = (Engine.logDir as NSString).appendingPathComponent("d3dmetal_monitor_\(df.string(from: Date())).log")
        try? FileManager.default.createDirectory(atPath: Engine.logDir, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: path, contents: nil)
        guard let fh = FileHandle(forWritingAtPath: path) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-E", "-s", "-B", Engine.engineDir.appendingPathComponent("diagnostics.py").path,
                       "--watch", "--out", (NSHomeDirectory() as NSString)
                        .appendingPathComponent("Library/Logs/ShaderWarmer")]
        p.environment = Engine.engineEnvironment()
        p.standardOutput = fh
        p.standardError = fh
        p.terminationHandler = { [weak self] _ in
            try? fh.close()
            Task { @MainActor in self?.monitor = nil; model.monitoring = false }
        }
        do { try p.run(); monitor = p; model.monitoring = true }
        catch { model.monitoring = false }
    }

    // engine/ folder inside the .app bundle Resources
    static var engineDir: URL {
        if let r = Bundle.main.resourceURL {
            let e = r.appendingPathComponent("engine", isDirectory: true)
            if FileManager.default.fileExists(atPath: e.path) { return e }
        }
        // dev fallback: engine next to the built binary
        return URL(fileURLWithPath: CommandLine.arguments[0])
            .deletingLastPathComponent().appendingPathComponent("engine")
    }

    // A release must never depend on Apple's python3/CLT installer stub or Homebrew.
    private static var python: String? {
        if Bundle.main.bundleURL.pathExtension == "app" {
            guard let path = Bundle.main.resourceURL?.appendingPathComponent("python/bin/python3").path,
                  FileManager.default.isExecutableFile(atPath: path) else { return nil }
            return path
        }
        return ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
            .first(where: { FileManager.default.isExecutableFile(atPath: $0) })
    }

    private func baseArgs(_ model: WarmerModel, forWarm: Bool) -> [String] {
        let script = model.usesDXMT ? (model.containerScope ? "wine_dxmt.py" : "dxmt_fix.py") : (model.selectedGame?.script ?? "warm.py")
        var a = [Engine.engineDir.appendingPathComponent(script).path]
        // Always point the engine at the selected game when it is one we warm — detection too.
        // Leaving detect to find "a" Deadlock or Overwatch on its own could answer about a different
        // install than the one on screen (a hand-added folder, a second library).
        let selected = model.operationSupported ? model.selectedGame?.path : nil
        let game = selected ?? model.overrideGame
        if let g = game { a += ["--game", g] }
        if model.usesDXMT, let profile = model.selectedProfile {
            a += ["--prefix", profile.prefix, "--wine", profile.wine, "--launcher", profile.kind]
        }
        if model.usesDXMT && model.dx11Confirmed { a += ["--dx11"] }
        // detect enumerates every GPTK (no pin, unless the user browsed to a custom folder);
        // warm pins the version chosen in the dropdown.
        let gptk = forWarm ? (model.selectedGptkPath ?? model.overrideGptk) : model.overrideGptk
        if let k = gptk, !model.usesDXMT { a += ["--gptk", k] }
        // Only the Deadlock engine has a class-set option; the Unity and Overwatch engines reject
        // the flag, so a toggle left on from Deadlock used to crash their warm at argument parsing.
        if forWarm && model.effectiveFullWarm {
            a += ["--classes", "full"]
        }
        return a
    }

    // Scan the Mac's Steam libraries for installed games and detect the launcher.
    func scanLibrary(_ model: WarmerModel) {
        let (scanned, report, _) = GameScanner.scanWithReport()
        // Hand-added games first: a rescan (the app rescans whenever it becomes active) used to
        // drop them, and move the selection away with them.
        model.launcherProfiles = LauncherProfile.discover()
        var games = model.manualGames.map { saved -> WarmerModel.Game in
            var game = GameScanner.manual(URL(fileURLWithPath: saved.path))
            game.profileID = saved.profileID; game.launchExecutable = saved.launchExecutable; game.launchArguments = saved.launchArguments
            return game
        }
        let others = GameScanner.scanProfiles(model.launcherProfiles)
        games += others.filter { s in !games.contains(where: { $0.id == s.id }) }
        games += scanned.filter { s in !games.contains(where: { $0.id == s.id }) }
        model.games = games
        refreshLibraryStates(model)
        Engine.writeDiagnostic("scan.log", report)       // what was looked at, for "Собрать логи"
        if model.isBusy { return }                        // the running warm owns the selection
        if model.selectedGame == nil || !games.contains(where: { $0.id == model.selectedGame?.id }) {
            let last = model.lastWarm.flatMap { w in games.first(where: { $0.name == w.game }) }
            model.selectedGame = last ?? games.first(where: { $0.supported }) ?? games.first
        }
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        let gamehub = home.appendingPathComponent("Library/Application Support/com.gamemac.www/wine-engine")
        let crossover = home.appendingPathComponent("Library/Application Support/CrossOver")
        if fm.fileExists(atPath: gamehub.path) { model.launcher = .gamehub; model.launcherPath = gamehub.path }
        else if fm.fileExists(atPath: crossover.path) { model.launcher = .crossover; model.launcherPath = crossover.path }
    }

    private func refreshLibraryStates(_ model: WarmerModel) {
        let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ShaderWarmer/dxmt-async")
        var files = [root.appendingPathComponent("installation.json")]
        files += ((try? FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("games"), includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "json" && !$0.lastPathComponent.hasSuffix(".seed.json") }
        var states: [String: WarmerModel.LibraryState] = [:]
        for file in files {
            guard let data = try? Data(contentsOf: file),
                  let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let path = record["game"] as? String else { continue }
            states[URL(fileURLWithPath: path).standardized.path] = .init(enabled: record["active"] as? Bool ?? false,
                updatedAt: (record["updated_at"] as? Double).map { Date(timeIntervalSince1970: $0) })
        }
        for profile in model.launcherProfiles {
            let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ShaderWarmer/wine-environments/\(profile.id)/installation.json")
            guard let data = try? Data(contentsOf: file), let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  record["prefix"] as? String == URL(fileURLWithPath: profile.prefix).resolvingSymlinksInPath().path else { continue }
            for game in model.games where game.profileID == profile.id {
                states[game.id] = .init(enabled: record["active"] as? Bool ?? false,
                                       updatedAt: (record["updated_at"] as? Double).map { Date(timeIntervalSince1970: $0) })
            }
        }
        model.libraryStates = states
    }

    func assignLauncher(_ model: WarmerModel, profile: LauncherProfile?) {
        guard !model.isBusy, var game = model.selectedGame else { return }
        game.profileID = profile?.id
        if let profile, !LauncherProfile.saved().contains(where: { $0.id == profile.id }) {
            LauncherProfile.save(LauncherProfile.saved() + [profile])
        }
        model.manualGames.removeAll { $0.id == game.id }
        model.manualGames.insert(game, at: 0)
        WarmerModel.Game.saveManual(model.manualGames)
        model.selectedGame = game; model.method = .dxmt
        model.dx11Confirmed = false; model.clearDetected(); model.resetProgress()
        model.log = []; model.checks = []; model.stageHistory = []; model.currentStage = ""
        model.phase = .detecting
        scanLibrary(model)
        Task { await detect(model) }
    }

    func launchGame(_ model: WarmerModel) {
        guard !model.isBusy, model.dxmtInstalled, let game = model.selectedGame, model.selectedProfile != nil else { return }
        let panel = NSOpenPanel(); panel.canChooseDirectories = false; panel.canChooseFiles = true
        panel.allowsMultipleSelection = false; panel.directoryURL = URL(fileURLWithPath: game.path)
        panel.message = "Выбери .exe этой игры. Запуск использует выбранный Wine и DXMT Async."
        if let name = game.exeName { panel.nameFieldStringValue = name }
        guard panel.runModal() == .OK, let exe = panel.url else { return }
        let arguments = baseArgs(model, forWarm: false) + ["--launch", "--exe", exe.path, "--launch-args", game.launchArguments ?? ""]
        Task {
            let (out, err) = await runCapture(arguments)
            guard model.selectedGame?.id == game.id else { return }
            if !err.isEmpty {
                model.statusText = err.trimmingCharacters(in: .whitespacesAndNewlines); model.appendLog(err)
                let alert = NSAlert(); alert.alertStyle = .warning
                alert.messageText = "Не удалось запустить игру"; alert.informativeText = model.statusText
                alert.runModal()
            }
            else { model.statusText = "Команда запуска передана Wine" }
            if !out.isEmpty { model.appendLog(out) }
        }
    }

    // One-shot detection: warm.py --detect prints a JSON line.
    func detect(_ model: WarmerModel) async {
        if model.containerScope && model.selectedProfile == nil {
            model.runtimeError = "Лаунчер этой игры не найден. Нажми «Сменить лаунчер» и выбери среду заново."
            model.phase = .idle; model.detectedGameID = model.selectedGame?.id; return
        }
        if model.containerScope { model.method = .dxmt }
        guard Engine.python != nil else {
            scanLibrary(model)                            // the game list needs no Python
            model.runtimeError = "Встроенный Python не найден — переустанови Shader Warmer из DMG"
            model.phase = .idle
            model.statusText = "Повреждена установка приложения"
            return
        }
        let target = model.selectedGame?.id
        let targetDXMT = model.usesDXMT
        let targetConfirmed = model.dx11Confirmed
        let (out, err) = await runCapture(baseArgs(model, forWarm: false) + ["--detect"])
        // Two quick clicks start two detects; whichever finishes last used to win, putting one
        // game's cache and GPTK under the other game's name.  Keep only the answer for the game
        // that is still selected.
        if model.selectedGame?.id != target || model.usesDXMT != targetDXMT || model.dx11Confirmed != targetConfirmed { return }
        // an answer only belongs to a game we warm; with nothing warmable selected it is just
        // the runtime survey (GPTK list for Settings)
        model.detectedGameID = model.operationSupported ? target : nil
        guard let data = out.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            // The game list does not depend on the engine: scan anyway.  Skipping it here is what
            // emptied the whole window when --detect broke on a drive macOS would not let us read.
            scanLibrary(model)
            Engine.writeDiagnostic("detect_error_gui.log", "stdout:\n\(out)\n\nstderr:\n\(err)")
            let last = err.split(separator: "\n").last.map(String.init) ?? ""
            model.runtimeError = "не удалось опросить систему" + (last.isEmpty ? "" : ": \(last)")
            model.phase = .idle
            return
        }
        model.gamePath = obj["game"] as? String
        model.gptkPath = obj["gptk"] as? String
        model.gptkVersion = obj["gptk_version"] as? String
        model.availableGptks = (obj["gptks"] as? [[String: Any]] ?? []).compactMap {
            guard let p = $0["path"] as? String else { return nil }
            return WarmerModel.GptkOption(path: p, version: $0["version"] as? String ?? "?")
        }
        model.selectedGptkPath = obj["gptk"] as? String
        model.cacheShaders = obj["cache_shaders"] as? Int ?? 0
        model.cachePipelines = obj["cache_pipelines"] as? Int ?? 0
        model.cacheStale = obj["cache_stale"] as? Bool ?? false
        model.deadlockRunning = (obj["deadlock_running"] as? Bool) ?? (obj["game_running"] as? Bool) ?? false
        model.needsPlay = obj["needs_play"] as? Bool ?? false
        model.dxmtAvailable = obj["dxmt_available"] as? Bool ?? false
        model.dxmtInstalled = obj["dxmt_installed"] as? Bool ?? false
        model.dxmtCanRestore = obj["dxmt_backup"] as? Bool ?? false
        model.dxmtDetail = obj["dxmt_detail"] as? String ?? ""
        model.dxmtLogDirectory = obj["dxmt_log_directory"] as? String
        model.dxmtRequiresDX11Confirmation = obj["dxmt_requires_confirmation"] as? Bool ?? false
        model.runtimeError = obj["runtime_error"] as? String
        scanLibrary(model)
        model.phase = .idle
        if model.needsPlay && model.runtimeError == nil {
            model.runtimeError = "Запусти игру один раз — прогрев берёт форматы кадра из её кэша"
        }
        model.statusText = model.deadlockRunning ? "Закрой игру и обнови"
            : model.ready ? (model.usesDXMT ? model.dxmtDetail : "Готово к прогреву") : model.usesDXMT && model.dxmtRequiresDX11Confirmation ? "Подтверди поддержку DirectX 11" : "Укажи недостающие пути"
    }

    // Full warm run: stream @@-events into the model.
    func warm(_ model: WarmerModel, restoreDXMT: Bool = false) {
        guard process == nil || process?.isRunning == false else { return }
        guard let python = Engine.python else {
            model.phase = .failed
            model.statusText = "Встроенный Python не найден — переустанови Shader Warmer"
            return
        }
        model.phase = .running
        model.cancelling = false
        model.installing = false
        model.resetProgress()                             // no "N / M · осталось" from the last run
        model.addedShaders = 0
        model.gpuMode = false
        model.restored = false
        model.errorReported = false
        model.warmingGame = model.selectedGame
        model.statusText = "Запуск…"
        model.currentStage = ""; model.stageHistory = []
        model.log.removeAll()
        model.checks.removeAll()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.environment = Engine.engineEnvironment()
        p.arguments = ["-E", "-s", "-B"] + baseArgs(model, forWarm: true) + ["--json"] + (restoreDXMT && model.usesDXMT ? ["--restore"] : [])
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        let lines = LineBuffer()
        pipe.fileHandleForReading.readabilityHandler = { h in
            let chunk = h.availableData
            if chunk.isEmpty { return }
            for line in lines.feed(chunk) {
                Task { @MainActor in self.handleLine(line, model) }
            }
        }
        p.terminationHandler = { proc in
            Task { @MainActor in
                pipe.fileHandleForReading.readabilityHandler = nil
                let cancelled = self.cancelRequested
                if self.process === proc { self.process = nil }
                self.cancelRequested = false
                model.cancelling = false
                model.installing = false
                if cancelled {
                    model.phase = .cancelled
                    model.statusText = "Отменено"
                } else if model.phase == .running || model.phase == .preflight {
                    model.phase = proc.terminationStatus == 0 ? .done : .failed
                    if model.phase == .failed && !model.errorReported {
                        // otherwise the card would keep saying e.g. "Извлекаю шейдеры из игры…"
                        model.statusText = "Операция прервалась — подробности в журнале"
                    }
                }
            }
        }
        self.process = p
        do { try p.run() } catch {
            self.process = nil
            model.phase = .failed
            model.statusText = "не удалось запустить python3"
        }
    }

    func cancel(_ model: WarmerModel) {
        guard !model.installing, let p = process, p.isRunning else { return }
        cancelRequested = true
        model.cancelling = true
        model.statusText = "Останавливаю…"
        stopProcessTree(force: false)
        Task { @MainActor [weak self, weak p] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard let self, let p, self.process === p, p.isRunning else { return }
            self.stopProcessTree(force: true)
        }
    }

    private func stopProcessTree(force: Bool) {
        guard let p = process, p.isRunning else { return }
        let signal = force ? SIGKILL : SIGTERM
        // Every bundled engine makes itself a process-group leader before spawning Wine,
        // extractors and compilers, so one signal stops the complete run.
        if Darwin.kill(-p.processIdentifier, signal) != 0 {
            if force { Darwin.kill(p.processIdentifier, SIGKILL) }
            else { p.terminate() }
        }
    }

    private func handleLine(_ line: String, _ model: WarmerModel) {
        if model.cancelling { return }
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return }
        if trimmed.hasPrefix("@@ "),
           let data = trimmed.dropFirst(3).data(using: .utf8),
           let e = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let event = e["event"] as? String {
            switch event {
            case "stage":
                let name = e["name"] as? String ?? ""
                model.currentStage = name
                if model.stageHistory.last != name { model.stageHistory.append(name) }
                model.statusText = ["extract": "Извлекаю шейдеры из игры…",
                                    "plan": "Строю план прогрева…",
                                    "calibrate": "Сверяю D3DMetal этой машины…",
                                    "build": "Собираю кэш…",
                                    "install": model.usesDXMT ? "Применяю настройки DXMT…" : "Устанавливаю готовый кэш…",
                                    "preflight": "Проверка перед запуском…",
                                    "warm": "Прогреваю кэш…"][name] ?? name
                model.installing = name == "install"
                // Progress is per stage: extraction's "10 / 10 · 100%" used to stay on screen through
                // calibration and the whole slow build.
                model.resetProgress()
                if name == "preflight" { model.phase = .preflight }
                if ["extract", "plan", "calibrate", "build", "warm", "install"].contains(name) {
                    model.phase = .running
                    model.chunksTotal = e["chunks"] as? Int ?? model.chunksTotal
                }
            case "check":
                model.checks.append(.init(name: e["name"] as? String ?? "",
                                          ok: e["ok"] as? Bool ?? false,
                                          detail: e["detail"] as? String ?? ""))
            case "preflight_ok":
                model.gpuMode = e["gpu"] as? Bool ?? false
                model.statusText = "Все проверки пройдены"
            case "log":
                model.logPath = e["path"] as? String
            case "plan_ready":
                model.chunksTotal = e["chunks"] as? Int ?? 0
            case "progress":
                model.unitsDone = e["units_done"] as? Int ?? model.unitsDone
                model.unitsTotal = e["units_total"] as? Int ?? model.unitsTotal
                model.etaSeconds = e["eta"] as? Int ?? model.etaSeconds
                model.ratePerSec = e["rate"] as? Int ?? model.ratePerSec
                model.elapsedSeconds = e["elapsed"] as? Int ?? model.elapsedSeconds
                if let pct = e["percent"] as? Double { model.percent = pct; model.progress = pct / 100 }
                else if let pct = e["percent"] as? Int { model.percent = Double(pct); model.progress = Double(pct)/100 }
                model.statusText = "Прогрев шейдеров"
            case "chunk":
                model.chunksDone = e["done"] as? Int ?? model.chunksDone
                model.chunksTotal = e["total"] as? Int ?? model.chunksTotal
                model.unitsDone = e["units_done"] as? Int ?? model.unitsDone
                model.unitsTotal = e["units_total"] as? Int ?? model.unitsTotal
                model.etaSeconds = e["eta"] as? Int ?? 0
                model.ratePerSec = e["rate"] as? Int ?? 0
                model.elapsedSeconds = e["elapsed"] as? Int ?? 0
                if let pct = e["percent"] as? Double { model.percent = pct; model.progress = pct / 100 }
                else if let pct = e["percent"] as? Int { model.percent = Double(pct); model.progress = Double(pct)/100 }
                model.statusText = "Прогрев шейдеров"
            case "done":
                model.installing = false
                refreshLibraryStates(model)
                if e["method"] as? String == "dxmt" {
                    model.dxmtInstalled = e["enabled"] as? Bool ?? false
                    model.dxmtCanRestore = e["backup"] as? Bool ?? false
                    model.dxmtDetail = (e["detail"] as? String) ?? (model.dxmtInstalled ? "Фикс включён · запускай игру через GameHub"
                        : (e["shared_retained"] as? Bool == true ? "Настройки игры восстановлены · Async сохранён для других игр" : "Исходный DXMT и настройки игры восстановлены"))
                    model.dxmtLogDirectory = e["log_directory"] as? String
                    model.statusText = model.dxmtDetail
                    model.rememberWarm(shaders: 0, added: 0, method: model.dxmtInstalled ? "dxmt" : "dxmt_restore")
                    model.progress = 1; model.percent = 100; model.phase = .done
                    Notifier.shared.notify(title: "Shader Warmer", body: model.dxmtDetail)
                    break
                }
                model.rememberWarm(shaders: (e["shaders"] as? Int) ?? 0, added: (e["added"] as? Int) ?? 0)
                // the fresh cache matches the installed game; "update came out" no longer applies
                model.cacheStale = false
                model.needsPlay = false
                model.addedShaders = e["added"] as? Int ?? 0
                model.cacheShaders = e["shaders"] as? Int ?? model.cacheShaders
                model.cachePipelines = e["pipelines"] as? Int ?? model.cachePipelines
                model.restored = e["restored"] as? Bool ?? model.restored
                model.progress = 1
                model.percent = 100
                model.etaSeconds = 0
                model.statusText = "Готово"
                model.phase = .done
                if UserDefaults.standard.object(forKey: "autoDiagnostics") as? Bool ?? true { self.startMonitor(model) }
                Notifier.shared.notify(title: "Shader Warmer",
                    body: "\(model.warmingGame?.name ?? "Игра"): кэш прогрет"
                        + (model.addedShaders > 0 ? " (+\(model.addedShaders))" : "") + ". Запускай игру.")
            case "error":
                model.installing = false
                model.errorReported = true
                let reason = e["reason"] as? String ?? "неизвестно"
                model.statusText = [
                    "deadlock_running": "Deadlock запущен — закрой игру",
                    "game_not_found": "Игра не найдена",
                    "runtime_not_found": "GPTK не найден",
                    "preflight_failed": "Проверки не пройдены — кэш не тронут",
                    "smoke_failed": "Пробный прогон не удался — кэш не тронут",
                    "stray_family_dir": "D3DMetal ушёл в запасную папку",
                    "warm_failed": "Прогрев прервался",
                    "build_failed": "Не удалось собрать кэш",
                    "extract_failed": "Не удалось извлечь шейдеры",
                    "game_unreadable": "Файлы игры не читаются — проверь диск, на котором она лежит",
                    "plan_failed": "Не удалось построить план прогрева",
                    "warm_empty": "Прогрев не оставил записей",
                ][reason] ?? ("Ошибка: " + reason)
                model.phase = .failed
                Notifier.shared.notify(title: "Shader Warmer",
                                       body: "\(model.warmingGame?.name ?? "Прогрев"): \(model.statusText)")
            default: break
            }
        } else {
            model.appendLog(trimmed)
        }
    }

    // Run a process and return combined stdout.
    // The GPU family D3DMetal names its cache folder after (MTLGPUFamilyApple<N>_0), asked of
    // Metal itself.  Engines used to guess it from the chip name, and the guess was wrong for M4
    // (Apple9, not Apple10); asking the device is right for every chip, including ones not out yet.
    static let gpuFamilyDir: String? = {
        guard let dev = MTLCreateSystemDefaultDevice() else { return nil }
        var best: Int?
        for raw in 1001...1030 {
            if let fam = MTLGPUFamily(rawValue: raw), dev.supportsFamily(fam) { best = raw - 1000 }
        }
        return best.map { "MTLGPUFamilyApple\($0)_0" }
    }()

    static func engineEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        for key in ["PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "VIRTUAL_ENV"] { env.removeValue(forKey: key) }
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if let fam = gpuFamilyDir { env["SHADER_WARMER_GPU_FAMILY"] = fam }
        return env
    }

    // stdout and stderr of a short engine call.  stderr used to go to /dev/null, which is why a
    // crashing --detect left nothing behind to diagnose.
    private func runCapture(_ args: [String]) async -> (String, String) {
        await withCheckedContinuation { cont in
            guard let python = Engine.python else { cont.resume(returning: ("", "python3 не найден")); return }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.environment = Engine.engineEnvironment()
            p.arguments = ["-E", "-s", "-B"] + args
            let outPipe = Pipe(), errPipe = Pipe()
            p.standardOutput = outPipe
            p.standardError = errPipe
            let output = CaptureOutput()
            let readers = DispatchGroup()
            readers.enter(); readers.enter()
            DispatchQueue.global(qos: .utility).async {
                output.setOut(outPipe.fileHandleForReading.readDataToEndOfFile())
                readers.leave()
            }
            DispatchQueue.global(qos: .utility).async {
                output.setErr(errPipe.fileHandleForReading.readDataToEndOfFile())
                readers.leave()
            }
            let deadline = CaptureDeadline()
            let watchdog = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
            watchdog.schedule(deadline: .now() + 45)
            watchdog.setEventHandler {
                guard p.isRunning else { return }
                deadline.markExpired()
                p.terminate()
                DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
                    if p.isRunning { Darwin.kill(p.processIdentifier, SIGKILL) }
                }
            }
            watchdog.resume()
            p.terminationHandler = { _ in
                watchdog.setEventHandler {}; watchdog.cancel()
                readers.wait()
                if deadline.expired {
                    cont.resume(returning: ("", "Проверка не завершилась за 45 секунд. Проверь доступность диска и повтори."))
                    return
                }
                cont.resume(returning: output.strings)
            }
            do { try p.run() } catch {
                watchdog.setEventHandler {}; watchdog.cancel()
                try? outPipe.fileHandleForWriting.close()
                try? errPipe.fileHandleForWriting.close()
                cont.resume(returning: ("", "\(error)"))
            }
        }
    }

    func captureDiagnostics(_ model: WarmerModel) async {
        let version = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "?"
        var args = [Engine.engineDir.appendingPathComponent("diagnostics.py").path, "--out",
                    (NSHomeDirectory() as NSString).appendingPathComponent("Library/Logs/ShaderWarmer"),
                    "--app-version", version]
        if let game = model.selectedGame?.path { args += ["--game", game] }
        let (out, err) = await runCapture(args)
        if !err.isEmpty { Engine.writeDiagnostic("diagnostic_error.log", err) }
        if !out.isEmpty { Engine.writeDiagnostic("diagnostic_export.log", out) }
    }

    // A small text file in the app's log dir, so "Собрать логи" carries it.
    static func writeDiagnostic(_ name: String, _ text: String) {
        let dir = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Logs/ShaderWarmer")
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let stamp = ISO8601DateFormatter().string(from: Date())
        try? ("\(stamp)\n\(text)\n").write(toFile: (dir as NSString).appendingPathComponent(name),
                                            atomically: true, encoding: .utf8)
    }
}

final class CaptureDeadline: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    var expired: Bool { lock.lock(); defer { lock.unlock() }; return value }
    func markExpired() { lock.lock(); value = true; lock.unlock() }
}

final class CaptureOutput: @unchecked Sendable {
    private let lock = NSLock()
    private var out = Data(), err = Data()
    func setOut(_ data: Data) { lock.lock(); out = data; lock.unlock() }
    func setErr(_ data: Data) { lock.lock(); err = data; lock.unlock() }
    var strings: (String, String) {
        lock.lock(); defer { lock.unlock() }
        return (String(data: out, encoding: .utf8) ?? "", String(data: err, encoding: .utf8) ?? "")
    }
}


// Buffers partial reads and splits on newlines. Its own methods are the only
// access to the stored data, so it is safe to use from the read handler queue.
final class LineBuffer: @unchecked Sendable {
    private var data = Data()
    private let lock = NSLock()
    func feed(_ chunk: Data) -> [String] {
        lock.lock(); defer { lock.unlock() }
        data.append(chunk)
        var out: [String] = []
        while let nl = data.firstIndex(of: 0x0A) {
            out.append(String(data: data[data.startIndex..<nl], encoding: .utf8) ?? "")
            data.removeSubrange(data.startIndex...nl)
        }
        return out
    }
}
