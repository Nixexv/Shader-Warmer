import Foundation
import Observation

// State shared between the engine runner and the views.  Deliberately an ObservableObject rather
// than @Observable: the macro needs Xcode's macro plugins, and the app must build with nothing but
// the Command Line Tools installed.
@MainActor
final class WarmerModel: ObservableObject {
    enum Phase { case detecting, idle, preflight, running, done, failed, cancelled }
    enum Screen { case main, game, settings }

    /// What the home card shows: the warm this app did last, remembered between launches.
    struct LastWarm: Codable {
        var game: String
        var date: Date
        var shaders: Int
        var added: Int
        var method: String? = nil
    }
    enum Method: String, CaseIterable, Identifiable {
        case dxmt = "DXMT Async"
        case legacy = "GPTK"
        var id: String { rawValue }
    }
    @Published var method: Method = UserDefaults.standard.string(forKey: "defaultMethod") == "gptk" ? .legacy : .dxmt
    @Published var dxmtAvailable = false
    @Published var dxmtInstalled = false
    @Published var dxmtCanRestore = false
    @Published var dxmtDetail = ""
    @Published var dxmtLogDirectory: String?
    @Published var dxmtRequiresDX11Confirmation = false
    @Published var dx11Confirmed = false
    var usesDXMT: Bool { (selectedGame?.canUseDXMT ?? false) && method == .dxmt }
    var operationSupported: Bool { usesDXMT || legacyAvailable }
    enum Launcher: String, CaseIterable, Identifiable {
        case gamehub = "GameHub", crossover = "CrossOver", other = "Другой"
        var id: String { rawValue }
    }
    enum GameEngine: String { case deadlock, overwatch, unity }
    struct Game: Identifiable, Hashable, Codable {
        var id: String { profileID.map { path + "#" + $0 } ?? path }
        let name: String
        let path: String        // install dir (…/Deadlock/game for Deadlock)
        let source: String      // GameHub / Steam / Внешний диск / вручную
        let supported: Bool     // warming supported (Deadlock, Overwatch, Unity)
        let exeName: String?
        var profileID: String? = nil
        var launchExecutable: String? = nil
        var launchArguments: String? = nil
        var dxmtCandidate: Bool? = nil
        var canUseDXMT: Bool { dxmtCandidate ?? (supported || exeName != nil) }
        var engine: String = ""  // empty for games we cannot warm
        var isDeadlock: Bool { supported && engine == GameEngine.deadlock.rawValue }
        var script: String {
            switch GameEngine(rawValue: engine) ?? .deadlock {
            case .overwatch: return "ow_warm.py"
            case .unity: return "unity_warm.py"
            case .deadlock: return "warm.py"
            }
        }
    }

    @Published var phase: Phase = .detecting
    @Published var screen: Screen = .main
    struct GptkOption: Identifiable, Hashable { var id: String { path }; let path: String; let version: String }

    @Published var launcherProfiles: [LauncherProfile] = LauncherProfile.discover()
    @Published var libraryLauncher = "all"
    @Published var showingAddLauncher = false
    var selectedProfile: LauncherProfile? { launcherProfiles.first { $0.id == selectedGame?.profileID } }
    var launcherTitle: String { selectedProfile?.title ?? (selectedGame?.profileID == nil ? "GameHub" : "Среда недоступна") }
    var containerScope: Bool { selectedGame?.profileID != nil }
    var legacyAvailable: Bool { selectedGame?.supported == true && !containerScope }

    // library
    struct LibraryState { let enabled: Bool; let updatedAt: Date? }
    @Published var libraryStates: [String: LibraryState] = [:]
    @Published var games: [Game] = []
    @Published var selectedGame: Game?
    // Games added by hand survive rescans and relaunches — the scanner cannot find them itself.
    @Published var manualGames: [Game] = Game.loadManual()
    // Which game the per-game fields below (cache size, GPTK, "needs play", errors) belong to.
    // They come from that game's --detect; showing them under another game was a steady source of
    // wrong numbers and wrong warnings.
    @Published var detectedGameID: String?
    // The game the running (or last) warm was started for; the selection may not change mid-run.
    @Published var warmingGame: Game?
    // An engine that exits with an error without saying why must not leave the last step's text up.
    @Published var errorReported = false

    // launcher (Settings)
    @Published var launcher: Launcher = .gamehub
    @Published var launcherPath: String?

    @Published var gamePath: String?
    @Published var gptkPath: String?
    @Published var gptkVersion: String?
    @Published var availableGptks: [GptkOption] = []   // every GPTK found, for the dropdown
    @Published var selectedGptkPath: String?           // the one the warm will use
    @Published var cacheStale = false            // game was updated since the last warm — cache needs redoing
    @Published var cacheShaders: Int = 0        // stage records — the number that matters
    @Published var cachePipelines: Int = 0      // pipeline records — diagnostic only
    @Published var deadlockRunning = false
    @Published var runtimeError: String?
    @Published var gpuMode = false   // true on chips (M1/M2/M3) that need real GPU draws
    @Published var restored = false  // a D3DMetal reset shrank the cache; best snapshot restored
    @Published var monitoring = false // D3DMetal cache-log monitor running (capture during play)
    @Published var cancelling = false // cancellation was requested; wait for the whole process tree
    @Published var installing = false // live cache replacement is intentionally not interruptible
    // A Unity game's render-target formats are read out of the cache the game itself wrote, so it
    // has to have been launched once before it can be warmed.
    @Published var needsPlay = false
    @Published var lastWarm: LastWarm? = LastWarm.load()

    // progress
    @Published var statusText = "Проверяю систему…"
    @Published var currentStage = ""
    @Published var stageHistory: [String] = []
    @Published var progress: Double = 0        // 0…1 during warm
    @Published var chunksDone = 0
    @Published var chunksTotal = 0
    @Published var addedShaders = 0
    @Published var log: [String] = []

    // Steam-style progress
    @Published var percent: Double = 0        // 0…100
    @Published var unitsDone = 0
    @Published var unitsTotal = 0
    @Published var etaSeconds = 0
    @Published var ratePerSec = 0
    @Published var elapsedSeconds = 0

    struct Check: Identifiable { let id = UUID(); let name: String; let ok: Bool; let detail: String }
    @Published var checks: [Check] = []
    @Published var logPath: String?

    var ready: Bool { gamePath != nil && (usesDXMT ? dxmtAvailable : gptkPath != nil) && !deadlockRunning && runtimeError == nil }
    var isBusy: Bool { phase == .running || phase == .preflight }
    /// Detect results that belong to the game on screen.
    var detectedForSelection: Bool { selectedGame != nil && detectedGameID == selectedGame?.id }
    var selectedGptkVersion: String? {
        availableGptks.first(where: { $0.path == selectedGptkPath })?.version ?? gptkVersion
    }
    /// The engine picks the GPTK the game actually runs under (its cache stamp, GameHub's per-game
    /// setting); warn only when the user chose a different one in Settings.  The old hardcoded
    /// "Overwatch = 3.0-3, Deadlock = 4.0-2" check told people to switch away from the right one.
    var gptkOverridden: Bool {
        guard detectedForSelection, let chosen = selectedGptkPath, let engineChoice = gptkPath else { return false }
        return chosen != engineChoice
    }
    /// "Full 100%" is a Deadlock plan option; the other engines reject the flag outright.
    var effectiveFullWarm: Bool { !usesDXMT && fullWarm && (selectedGame?.isDeadlock ?? false) }

    func resetProgress() {
        progress = 0; percent = 0
        unitsDone = 0; unitsTotal = 0
        etaSeconds = 0; ratePerSec = 0; elapsedSeconds = 0
        chunksDone = 0
    }

    /// Forget everything the previous game's --detect said, before asking about another one.
    func clearDetected() {
        detectedGameID = nil
        gamePath = nil; gptkPath = nil; gptkVersion = nil; selectedGptkPath = nil
        cacheShaders = 0; cachePipelines = 0
        cacheStale = false; needsPlay = false; deadlockRunning = false; runtimeError = nil
        dxmtAvailable = false; dxmtInstalled = false; dxmtCanRestore = false; dxmtDetail = ""
        dxmtLogDirectory = nil; dxmtRequiresDX11Confirmation = false
    }

    // user overrides picked via the file panel
    @Published var fullWarm = false   // warm 100% of shipped shaders (all classes, no skips) — bigger, slower
    @Published var overrideGame: String?
    @Published var overrideGptk: String?

    func rememberWarm(shaders: Int, added: Int, method: String? = nil) {
        let w = LastWarm(game: (warmingGame ?? selectedGame)?.name ?? "?", date: Date(), shaders: shaders, added: added, method: method)
        lastWarm = w
        w.save()
    }

    func appendLog(_ line: String) {
        log.append(line)
        if log.count > 400 { log.removeFirst(log.count - 400) }
    }
}

extension WarmerModel.Game {
    private static var manualFile: URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ShaderWarmer")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("manual_games.json")
    }

    static func loadManual() -> [WarmerModel.Game] {
        guard let data = try? Data(contentsOf: manualFile),
              let games = try? JSONDecoder().decode([WarmerModel.Game].self, from: data) else { return [] }
        return games.filter { FileManager.default.fileExists(atPath: $0.path) }
    }

    static func saveManual(_ games: [WarmerModel.Game]) {
        try? JSONEncoder().encode(games).write(to: manualFile)
    }
}

extension WarmerModel.LastWarm {
    private static var file: URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ShaderWarmer")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("last_warm.json")
    }

    static func load() -> WarmerModel.LastWarm? {
        guard let data = try? Data(contentsOf: file) else { return nil }
        return try? JSONDecoder().decode(WarmerModel.LastWarm.self, from: data)
    }

    func save() {
        try? JSONEncoder().encode(self).write(to: Self.file)
    }
}
