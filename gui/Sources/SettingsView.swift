import SwiftUI
import AppKit

struct WarmerSettingsView: View {
    @EnvironmentObject private var model: WarmerModel
    @EnvironmentObject private var artwork: GameArtwork
    let onCollectLogs: () -> Void
    var collectingLogs = false
    @AppStorage("defaultMethod") private var defaultMethod = "dxmt"
    @AppStorage("notificationsEnabled") private var notifications = true
    @AppStorage("downloadArtwork") private var downloadArtwork = true
    @AppStorage("autoDiagnostics") private var autoDiagnostics = true
    @AppStorage("showSupportedOnly") private var supportedOnly = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Настройки").font(.system(size: 29, weight: .bold))
                    Text("Поведение приложения и инструменты для проверки результата.")
                        .font(.system(size: 13)).foregroundStyle(WarmerTheme.secondary)
                }.padding(.bottom, 5)
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 20) {
                        Label("Общие", systemImage: "slider.horizontal.3").font(.system(size: 16, weight: .semibold))
                        settingRow("Способ по умолчанию", detail: "Используется при следующем запуске приложения.") {
                            Picker("Способ по умолчанию", selection: $defaultMethod) {
                                Text("DXMT Async").tag("dxmt")
                                Text("GPTK").tag("gptk")
                            }.labelsHidden().frame(width: 170).disabled(model.isBusy)
                        }
                        settingRow("Уведомления", detail: "Сообщать о завершении операции или ошибке.") {
                            Toggle("Уведомления", isOn: $notifications).labelsHidden().toggleStyle(.switch)
                        }
                    }
                }
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 20) {
                        Label("Библиотека", systemImage: "square.grid.2x2").font(.system(size: 16, weight: .semibold))
                        settingRow("Загружать обложки Steam", detail: "Сначала используем локальный кэш. Интернет нужен только для недостающих изображений.") {
                            Toggle("Загружать обложки Steam", isOn: $downloadArtwork).labelsHidden().toggleStyle(.switch)
                        }
                        settingRow("Только игры с доступным методом", detail: "Скрывать папки, где не найдена поддерживаемая Windows-игра.") {
                            Toggle("Только игры с доступным методом", isOn: $supportedOnly).labelsHidden().toggleStyle(.switch)
                        }
                    }
                }
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 20) {
                        Label("Диагностика", systemImage: "waveform.path.ecg").font(.system(size: 16, weight: .semibold))
                        settingRow("Запись после GPTK-прогрева", detail: "Автоматически собирать события D3DMetal до 30 минут, пока приложение открыто.") {
                            Toggle("Запись после GPTK-прогрева", isOn: $autoDiagnostics).labelsHidden().toggleStyle(.switch)
                        }
                        HStack(spacing: 12) {
                            Button(action: onCollectLogs) { Label(collectingLogs ? "Собираем логи…" : "Собрать логи", systemImage: "square.and.arrow.up") }
                                .buttonStyle(WarmerButtonStyle(prominent: true)).disabled(collectingLogs || model.isBusy)
                            Button { openFolder("Library/Logs/ShaderWarmer") } label: { Label("Папка логов", systemImage: "folder") }
                                .buttonStyle(WarmerButtonStyle())
                        }
                    }
                }
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 17) {
                        Label("Хранилище и восстановление", systemImage: "externaldrive").font(.system(size: 16, weight: .semibold))
                        Text("Исходные настройки хранятся отдельно от приложения. Восстановить их можно на странице каждой игры.")
                            .font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary)
                        HStack(spacing: 12) {
                            Button("Резервные копии") { openFolder("Library/Application Support/ShaderWarmer/dxmt-async") }
                                .buttonStyle(WarmerButtonStyle())
                            Button("Кэш обложек") { openFolder("Library/Caches/com.nixexv.deadlockwarmer/artwork") }
                                .buttonStyle(WarmerButtonStyle())
                        }
                        HStack {
                            Text("Shader Warmer \(ContentView.version)")
                            Spacer()
                            Text("Apple Silicon")
                        }.font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary).padding(.top, 5)
                    }
                }
            }.padding(32).frame(maxWidth: 940, alignment: .leading).frame(maxWidth: .infinity)
        }.background(WarmerTheme.background)
            .onChange(of: notifications) { if notifications { Notifier.shared.requestAuthorization() } }
            .onChange(of: downloadArtwork) {
                if downloadArtwork { Task { for game in model.games { await artwork.load(game, retry: true) } } }
            }
    }

    private func settingRow<Content: View>(_ title: String, detail: String, @ViewBuilder control: () -> Content) -> some View {
        HStack(spacing: 30) {
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.system(size: 13, weight: .medium))
                Text(detail).font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 15)
            control()
        }
    }
    private func openFolder(_ path: String) {
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(path)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        NSWorkspace.shared.open(folder)
    }
}
