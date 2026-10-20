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
                    Text(L("Настройки")).font(.system(size: 29, weight: .bold))
                    Text(L("Поведение приложения и инструменты для проверки результата."))
                        .font(.system(size: 13)).foregroundStyle(WarmerTheme.secondary)
                }.padding(.bottom, 5)
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 20) {
                        Label(L("Общие"), systemImage: "slider.horizontal.3").font(.system(size: 16, weight: .semibold))
                        settingRow(L("Язык интерфейса"), detail: L("Автоматически по языку macOS. Изменения применяются при следующем запуске.")) {
                            Text(L10n.current.language == "ru" ? "Русский" : "English")
                                .font(.system(size: 13)).foregroundStyle(WarmerTheme.secondary)
                        }
                        settingRow(L("Способ по умолчанию"), detail: L("Используется при следующем запуске приложения.")) {
                            Picker(L("Способ по умолчанию"), selection: $defaultMethod) {
                                Text("DXMT Async").tag("dxmt")
                                Text("GPTK").tag("gptk")
                            }.labelsHidden().frame(width: 170).disabled(model.isBusy)
                        }
                        settingRow(L("Уведомления"), detail: L("Сообщать о завершении операции или ошибке.")) {
                            Toggle(L("Уведомления"), isOn: $notifications).labelsHidden().toggleStyle(.switch)
                        }
                    }
                }
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 20) {
                        Label(L("Библиотека"), systemImage: "square.grid.2x2").font(.system(size: 16, weight: .semibold))
                        settingRow(L("Загружать обложки Steam"), detail: L("Сначала используем локальный кэш. Интернет нужен только для недостающих изображений.")) {
                            Toggle(L("Загружать обложки Steam"), isOn: $downloadArtwork).labelsHidden().toggleStyle(.switch)
                        }
                        settingRow(L("Только игры с доступным методом"), detail: L("Скрывать папки, где не найдена поддерживаемая Windows-игра.")) {
                            Toggle(L("Только игры с доступным методом"), isOn: $supportedOnly).labelsHidden().toggleStyle(.switch)
                        }
                    }
                }
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 20) {
                        Label(L("Диагностика"), systemImage: "waveform.path.ecg").font(.system(size: 16, weight: .semibold))
                        settingRow(L("Запись после GPTK-прогрева"), detail: L("Автоматически собирать события D3DMetal до 30 минут, пока приложение открыто.")) {
                            Toggle(L("Запись после GPTK-прогрева"), isOn: $autoDiagnostics).labelsHidden().toggleStyle(.switch)
                        }
                        HStack(spacing: 12) {
                            Button(action: onCollectLogs) { Label(collectingLogs ? L("Собираем логи…") : L("Собрать логи"), systemImage: "square.and.arrow.up") }
                                .buttonStyle(WarmerButtonStyle(prominent: true)).disabled(collectingLogs || model.isBusy)
                            Button { openFolder("Library/Logs/ShaderWarmer") } label: { Label(L("Папка логов"), systemImage: "folder") }
                                .buttonStyle(WarmerButtonStyle())
                        }
                    }
                }
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 17) {
                        Label(L("Хранилище и восстановление"), systemImage: "externaldrive").font(.system(size: 16, weight: .semibold))
                        Text(L("Исходные настройки хранятся отдельно от приложения. Восстановить их можно на странице каждой игры."))
                            .font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary)
                        HStack(spacing: 12) {
                            Button(L("Резервные копии")) { openFolder("Library/Application Support/ShaderWarmer/dxmt-async") }
                                .buttonStyle(WarmerButtonStyle())
                            Button(L("Кэш обложек")) { openFolder("Library/Caches/com.nixexv.deadlockwarmer/artwork") }
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
