import SwiftUI

@main
struct DeadlockWarmerApp: App {
    @StateObject private var model = WarmerModel()
    private let engine = Engine()

    var body: some Scene {
        Window("Shader Warmer " + ((Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? ""), id: "main") {
            ContentView(engine: engine).environmentObject(model)
                .onAppear { Notifier.shared.requestAuthorization() }
        }
        .defaultSize(width: 1100, height: 790)
        .windowResizability(.contentMinSize)
    }
}
