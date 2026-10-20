import SwiftUI
import AppKit

struct GameDetailView: View {
    @EnvironmentObject private var model: WarmerModel
    let engine: Engine
    let onBack: () -> Void
    let onCollectLogs: () -> Void
    let onPickGPTK: () -> Void
    var collectingLogs = false
    @State private var tab = DetailTab.overview
    private enum DetailTab: String, CaseIterable { case overview = "Обзор", parameters = "Параметры", diagnostics = "Диагностика" }

    private var nativeCrossOver: Bool { model.selectedProfile?.kind == "crossover" }
    private var game: WarmerModel.Game? { model.selectedGame }
    private var canApply: Bool { model.operationSupported && model.detectedForSelection && model.ready && !model.isBusy && model.phase != .detecting }
    private var blocked: Bool { model.phase == .failed || model.runtimeError != nil || (model.detectedForSelection && !model.ready && !model.isBusy) }
    private var stateTitle: String {
        if model.phase == .detecting { return "Проверяем игру" }
        if model.cancelling { return "Останавливаем прогрев" }
        if model.isBusy {
            if model.usesDXMT { return "Применяем DXMT Async" }
            return ["preflight": "Проверяем окружение", "extract": "Извлекаем шейдеры", "plan": "Готовим план прогрева", "calibrate": "Проверяем GPTK", "build": "Собираем кэш", "install": "Сохраняем кэш"][model.currentStage] ?? "Прогреваем шейдеры"
        }
        if model.phase == .failed { return "Нужна проверка" }
        if model.phase == .cancelled { return "Прогрев остановлен" }
        if model.deadlockRunning { return "Игра запущена" }
        if model.usesDXMT && model.dxmtInstalled { return "Можно играть" }
        if blocked { return "Нужна подготовка" }
        if model.phase == .done { return model.usesDXMT ? (model.containerScope ? "Async отключён" : "Настройки восстановлены") : "Кэш готов" }
        return model.usesDXMT ? "Меньше пауз в игре" : "Подготовь кэш заранее"
    }
    private var stateDescription: String {
        if model.phase == .detecting { return "Проверяем папку игры, выбранный лаунчер и доступность компонентов." }
        if model.phase == .cancelled { return "Операция остановлена. Можно запустить подготовку повторно." }
        if model.phase == .failed || model.cancelling { return model.statusText }
        if model.isBusy { return model.usesDXMT ? model.statusText : "Подготавливаем шейдеры из твоей игры для этого Mac." }
        if let error = model.runtimeError { return error }
        if model.deadlockRunning { return "Закрой игру перед изменением настроек." }
        if model.usesDXMT {
            if model.dxmtInstalled {
                if nativeCrossOver { return "DXMT Async сохранён для этой бутылки. Запускай игру прямо из CrossOver или Steam; Shader Warmer можно закрыть." }
                return model.containerScope ? "Среда DXMT Async готова. Используй «Запустить игру»: выберем нужный Wine и Async-библиотеки. Обычный запуск из лаунчера сохраняет прежнее поведение." : "DXMT Async включён. Запускай игру из \(model.launcherTitle): новые шейдеры будут компилироваться в фоне."
            }
            if model.dxmtRequiresDX11Confirmation && !model.dx11Confirmed { return "Подтверди поддержку DirectX 11 во вкладке «Параметры»." }
            if model.phase == .done { return model.containerScope ? "Подготовленная среда Async отключена. Обычный запуск из лаунчера работает с исходными настройками." : "Исходные настройки этой игры возвращены. Другие игры сохраняют свои настройки Async." }
            return "Включи фикс один раз. Шейдеры будут готовиться в фоне, чтобы сократить паузы при появлении новых эффектов."
        }
        if model.phase == .done { return "Кэш подготовлен на этом Mac. Для игры используй тот же GPTK в GameHub." }
        return "Шейдеры из установленной игры будут заранее скомпилированы через GPTK для этого Mac."
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                if let game { banner(game) }
                VStack(alignment: .leading, spacing: 22) {
                    tabBar
                    switch tab {
                    case .overview: overview
                    case .parameters: parameters
                    case .diagnostics: diagnostics
                    }
                }.padding(.horizontal, 32).padding(.top, 22).padding(.bottom, 32)
                    .frame(maxWidth: 1120)
            }.frame(maxWidth: .infinity)
        }.background(WarmerTheme.background)
    }

    private func banner(_ game: WarmerModel.Game) -> some View {
        ZStack(alignment: .bottomLeading) {
            ArtworkView(game: game, kind: .hero)
            LinearGradient(stops: [.init(color: .black.opacity(0.14), location: 0),
                                   .init(color: WarmerTheme.background.opacity(0.3), location: 0.4),
                                   .init(color: WarmerTheme.background, location: 1)], startPoint: .top, endPoint: .bottom)
            VStack(alignment: .leading) {
                Button(action: onBack) { Label("К играм", systemImage: "arrow.left") }
                    .buttonStyle(WarmerButtonStyle(compact: true)).padding(.top, 20)
                Spacer()
                HStack(alignment: .bottom, spacing: 18) {
                    ZStack {
                        WarmerTheme.surface
                        if let image = GameIcons.art(name: game.name, installPath: game.path, exeName: game.exeName) {
                            Image(nsImage: image).resizable().aspectRatio(contentMode: .fit).padding(10)
                        } else { Image(systemName: "gamecontroller.fill").font(.system(size: 32)).foregroundStyle(WarmerTheme.secondary) }
                    }.frame(width: 88, height: 88).clipShape(RoundedRectangle(cornerRadius: 16))
                        .overlay(RoundedRectangle(cornerRadius: 16).stroke(WarmerTheme.border))
                    VStack(alignment: .leading, spacing: 10) {
                        Text(game.name).font(.system(size: 30, weight: .bold)).lineLimit(2)
                        HStack(spacing: 9) {
                            Text(model.launcherTitle).font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
                            Text("Windows").font(.system(size: 10, weight: .medium)).padding(.horizontal, 7).padding(.vertical, 3)
                                .background(WarmerTheme.raised, in: RoundedRectangle(cornerRadius: 5))
                        }
                        Text(shorten(game.path)).font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
                            .lineLimit(1).truncationMode(.middle).textSelection(.enabled)
                    }
                    Spacer(minLength: 12)
                    launcherMenu
                    Button { NSWorkspace.shared.open(URL(fileURLWithPath: game.path)) } label: {
                        Label("Открыть папку", systemImage: "folder")
                    }.buttonStyle(WarmerButtonStyle(compact: true))
                }.padding(.bottom, 7)
            }.padding(.horizontal, 32).frame(maxWidth: 1120).frame(maxWidth: .infinity)
        }.frame(height: 265).clipped()
    }

    private var tabBar: some View {
        HStack(spacing: 28) {
            ForEach(DetailTab.allCases, id: \.self) { item in
                Button { tab = item } label: {
                    Text(item.rawValue).font(.system(size: 13, weight: tab == item ? .semibold : .regular))
                        .foregroundStyle(tab == item ? WarmerTheme.accent : WarmerTheme.secondary)
                        .padding(.bottom, 13)
                        .overlay(alignment: .bottom) { if tab == item { Capsule().fill(WarmerTheme.accent).frame(height: 2) } }
                }.buttonStyle(.plain)
            }
            Spacer()
            Button { tab = .parameters } label: { Label(model.usesDXMT ? "DXMT Async" : "GPTK", systemImage: model.usesDXMT ? "bolt.fill" : "flame") }
                .buttonStyle(.plain).font(.system(size: 11, weight: .medium)).foregroundStyle(WarmerTheme.accent)
                .padding(.bottom, 12).disabled(model.isBusy)
        }.overlay(alignment: .bottom) { Rectangle().fill(WarmerTheme.border).frame(height: 1).zIndex(-1) }
    }

    @ViewBuilder private var overview: some View {
        if model.usesDXMT { dxmtOverview } else { gptkOverview }
    }

    private var dxmtOverview: some View {
        WarmerPanel {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .center, spacing: 24) {
                    ZStack {
                        Circle().fill((blocked ? WarmerTheme.error : WarmerTheme.accent).opacity(0.09))
                        Circle().stroke((blocked ? WarmerTheme.error : WarmerTheme.accent).opacity(0.25), lineWidth: 1)
                        if model.phase == .detecting || model.isBusy {
                            ProgressView().controlSize(.large).tint(WarmerTheme.accent)
                        } else {
                            Image(systemName: blocked ? "exclamationmark" : model.dxmtInstalled ? "checkmark" : "bolt.fill")
                                .font(.system(size: 35, weight: .medium)).foregroundStyle(blocked ? WarmerTheme.error : WarmerTheme.accent)
                        }
                    }.frame(width: 102, height: 102).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 10) {
                        Text(stateTitle).font(.system(size: 23, weight: .semibold))
                        Text(stateDescription).font(.system(size: 13)).foregroundStyle(WarmerTheme.secondary)
                            .fixedSize(horizontal: false, vertical: true).lineSpacing(3).frame(maxWidth: 620, alignment: .leading)
                    }
                    Spacer(minLength: 0)
                }
                Rectangle().fill(WarmerTheme.border).frame(height: 1)
                HStack(alignment: .top, spacing: 30) {
                    benefit("waveform.path", "Компиляция в фоне", "Шейдеры готовятся во время игры.")
                    benefit("arrow.uturn.backward", "Можно отключить", model.containerScope ? "Используется отдельная копия Wine." : model.dxmtCanRestore ? "Исходные настройки сохранены отдельно." : "Создадим копию исходных настроек.")
                }
                if !model.stageHistory.isEmpty && model.isBusy { stageList }
                dxmtActions
                if model.dxmtInstalled {
                    Text("Новые объекты или эффекты могут кратко появляться с задержкой, пока компилируются их шейдеры.")
                        .font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
                }
            }
        }
    }

    private func benefit(_ icon: String, _ title: String, _ body: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon).font(.system(size: 16)).foregroundStyle(WarmerTheme.accent).frame(width: 22)
            VStack(alignment: .leading, spacing: 5) {
                Text(title).font(.system(size: 12, weight: .medium))
                Text(body).font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private var dxmtActions: some View {
        HStack(spacing: 12) {
            if model.isBusy {
                Button(model.installing ? "Применяем настройки…" : model.cancelling ? "Останавливаем…" : "Отменить") { engine.cancel(model) }
                    .buttonStyle(WarmerButtonStyle(prominent: true)).disabled(model.installing || model.cancelling)
            } else if model.dxmtInstalled && model.runtimeError == nil && model.phase != .failed {
                Button {
                    if model.containerScope && !nativeCrossOver { engine.launchGame(model) } else { openLauncher() }
                } label: { Label(nativeCrossOver ? "Открыть CrossOver" : model.containerScope ? "Запустить игру" : "Открыть GameHub", systemImage: "arrow.up.right") }
                    .buttonStyle(WarmerButtonStyle(prominent: true))
                Button("Применить заново") { engine.warm(model) }.buttonStyle(WarmerButtonStyle()).disabled(!canApply)
            } else if model.dxmtRequiresDX11Confirmation && !model.dx11Confirmed {
                Button("Подтвердить DirectX 11") { tab = .parameters }.buttonStyle(WarmerButtonStyle(prominent: true))
            } else {
                Button { engine.warm(model) } label: { Label("Включить фикс", systemImage: "bolt.fill") }
                    .buttonStyle(WarmerButtonStyle(prominent: true)).disabled(!canApply)
            }
            if !model.isBusy {
                Button { recheck() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(WarmerButtonStyle()).accessibilityLabel("Проверить состояние").help("Проверить состояние")
                    .disabled(model.phase == .detecting)
            }
            Spacer()
            if model.dxmtCanRestore && !model.isBusy {
                Button(model.dxmtInstalled ? (model.containerScope ? "Отключить Async для среды" : "Отключить для этой игры") : "Восстановить настройки") { engine.warm(model, restoreDXMT: true) }
                    .font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary).buttonStyle(.plain)
                    .disabled(model.deadlockRunning || model.phase == .detecting)
            }
        }
    }

    private var gptkOverview: some View {
        WarmerPanel {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .center, spacing: 34) {
                    WarmProgressRing(progress: model.progress, showPercent: model.unitsTotal > 0 || model.phase == .done,
                                     title: model.phase == .done ? "Кэш готов" : model.isBusy ? "Прогрев шейдеров" : "GPTK-прогрев",
                                     failed: model.phase == .failed)
                    VStack(alignment: .leading, spacing: 14) {
                        Text(stateTitle).font(.system(size: 22, weight: .semibold))
                        Text(stateDescription).font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary).lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                        if !model.stageHistory.isEmpty { stageList }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                if model.unitsTotal > 0 || model.phase == .done {
                    HStack(spacing: 0) {
                        metric("Всего в этапе", value: model.unitsTotal > 0 ? grouped(model.unitsTotal) : "Нет данных")
                        metric("Обработано", value: model.unitsTotal > 0 ? grouped(model.unitsDone) : grouped(model.cacheShaders))
                        metric("Скорость", value: model.ratePerSec > 0 ? "\(model.ratePerSec)/с" : "Нет данных")
                        metric("Осталось", value: model.etaSeconds > 0 ? duration(model.etaSeconds) : model.phase == .done ? "Готово" : "Считаем")
                    }.padding(.top, 18).overlay(alignment: .top) { Rectangle().fill(WarmerTheme.border).frame(height: 1) }
                }
                if model.cacheStale { inlineNote("Игра обновилась. Подготовь кэш заново.", icon: "arrow.down.circle", color: WarmerTheme.accent) }
                if model.gptkOverridden { inlineNote("Выбран другой GPTK. Игра должна запускаться с той же версией, что используется для прогрева.", icon: "exclamationmark.triangle", color: WarmerTheme.accent) }
                gptkActions
                if model.gpuMode { inlineNote("Этот прогрев использует GPU. Во время подготовки Mac может быть загружен.", icon: "cpu", color: WarmerTheme.secondary) }
                if model.restored { inlineNote("После сброса D3DMetal восстановлено лучшее сохранённое состояние кэша.", icon: "arrow.uturn.backward", color: WarmerTheme.secondary) }
            }
        }
    }

    @ViewBuilder private var gptkActions: some View {
        HStack(spacing: 12) {
            if model.isBusy {
                Button { engine.cancel(model) } label: { Label(model.installing ? "Устанавливаем кэш…" : model.cancelling ? "Останавливаем…" : "Остановить прогрев", systemImage: "stop.fill") }
                    .buttonStyle(WarmerButtonStyle(prominent: true)).disabled(model.installing || model.cancelling)
            } else {
                Button { engine.warm(model) } label: { Label(model.phase == .done ? "Прогреть заново" : "Прогреть шейдеры", systemImage: "flame.fill") }
                    .buttonStyle(WarmerButtonStyle(prominent: true)).disabled(!canApply)
                Button { recheck() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(WarmerButtonStyle()).accessibilityLabel("Проверить состояние").disabled(model.phase == .detecting)
            }
            Spacer()
            if model.cacheShaders > 0 { Text("В кэше: \(grouped(model.cacheShaders))").font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary) }
        }
    }

    private func metric(_ label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label).font(.system(size: 10)).foregroundStyle(WarmerTheme.secondary)
            Text(value).font(.system(size: 18, weight: .semibold)).monospacedDigit()
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private var stageList: some View {
        VStack(alignment: .leading, spacing: 9) {
            ForEach(Array(model.stageHistory.enumerated().suffix(4)), id: \.offset) { index, stage in
                let completed = index < model.stageHistory.count - 1 || model.phase == .done
                HStack(spacing: 10) {
                    Image(systemName: completed ? "checkmark.circle.fill" : model.phase == .failed ? "exclamationmark.circle" : "circle.dotted")
                        .foregroundStyle(completed ? WarmerTheme.success : WarmerTheme.accent)
                    Text(stageName(stage)).foregroundStyle(completed ? WarmerTheme.secondary : WarmerTheme.text)
                    Spacer()
                    Text(completed ? "Готово" : model.phase == .failed ? "Прервано" : model.phase == .cancelled ? "Остановлено" : "Выполняется")
                        .font(.system(size: 10)).foregroundStyle(WarmerTheme.secondary)
                }.font(.system(size: 11))
            }
        }
    }

    private var parameters: some View {
        VStack(spacing: 16) {
            WarmerPanel {
                VStack(alignment: .leading, spacing: 14) {
                    SectionTitle(title: "Лаунчер", subtitle: model.launcherTitle)
                    launcherMenu
                    if let profile = model.selectedProfile {
                        Text("Контейнер: \(shorten(profile.prefix))").font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary).textSelection(.enabled)
                        Text(nativeCrossOver ? "Async включается для всей бутылки CrossOver. После включения запускай игры как обычно из CrossOver или Steam. Прежние настройки сохраняются для восстановления." : "Для выбранного контейнера создаётся отдельная Wine-среда с DXMT Async. Файлы лаунчера и настройки контейнера остаются исходными.")
                            .font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary)
                        Text("Перед подготовкой среды и запуском закрой Windows-приложения из других Wine-сред.")
                            .font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
                        if !nativeCrossOver {
                        TextField("Параметры запуска из Shader Warmer", text: Binding(get: { model.selectedGame?.launchArguments ?? "" }, set: { value in
                            guard var game = model.selectedGame else { return }
                            game.launchArguments = value; model.selectedGame = game
                            model.manualGames.removeAll { $0.id == game.id }; model.manualGames.append(game)
                            WarmerModel.Game.saveManual(model.manualGames)
                        })).textFieldStyle(.roundedBorder).disabled(model.isBusy)
                        }
                        if nativeCrossOver {
                            Text("Shader Warmer не нужен во время игры. Режим DirectX 11 выбирается в параметрах игры или Steam. Не переключай графический режим бутылки после включения Async.")
                                .font(.system(size: 12)).foregroundStyle(WarmerTheme.accent)
                        } else if model.containerScope {
                            Text("Для этого лаунчера используй запуск из Shader Warmer. При обычном запуске лаунчер может выбрать другие графические библиотеки.")
                                .font(.system(size: 12)).foregroundStyle(WarmerTheme.accent)
                        }
                    } else {
                        Text("Выбор лаунчера меняет среду подготовки. Игра остаётся в своей папке.").font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary)
                    }
                }
            }
            WarmerPanel {
                VStack(alignment: .leading, spacing: 18) {
                    SectionTitle(title: "Способ подготовки", subtitle: "Выбери, как уменьшать задержки компиляции в этой игре.")
                    HStack(spacing: 12) {
                        if game?.canUseDXMT == true { methodOption(.dxmt, icon: "bolt.fill", subtitle: "Компиляция в фоне") }
                        if model.legacyAvailable { methodOption(.legacy, icon: "flame", subtitle: "Кэш перед запуском") }
                    }
                    if model.usesDXMT {
                        Text("Игра должна использовать DirectX 11. Выбери DX11 в игре или её параметрах запуска в лаунчере.")
                            .font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary).fixedSize(horizontal: false, vertical: true)
                        if model.dxmtRequiresDX11Confirmation {
                            Toggle("Игра поддерживает DirectX 11", isOn: $model.dx11Confirmed).toggleStyle(.checkbox)
                                .disabled(model.isBusy || model.phase == .detecting)
                                .onChange(of: model.dx11Confirmed) { recheck(clear: true) }
                        }
                        inlineNote("Async может кратко задерживать появление новых объектов или эффектов.", icon: "info.circle", color: WarmerTheme.secondary)
                    } else {
                        gptkParameters
                    }
                }
            }
            if model.usesDXMT {
                WarmerPanel {
                    VStack(alignment: .leading, spacing: 14) {
                        SectionTitle(title: "Восстановление")
                        Text(nativeCrossOver ? "Отключение возвращает прежний графический режим бутылки CrossOver. Другие настройки и файлы игр сохраняются." : model.containerScope ? "Отключение выключает запуск с Async из Shader Warmer. Исходные файлы лаунчера и параметры контейнера не менялись." : "У каждой игры своя копия исходных настроек. Общий компонент DXMT возвращается после отключения последней игры с Async.")
                            .font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary).fixedSize(horizontal: false, vertical: true)
                        if model.dxmtCanRestore {
                            Button(model.containerScope ? "Отключить подготовленную среду" : "Восстановить настройки этой игры") { engine.warm(model, restoreDXMT: true) }
                                .buttonStyle(WarmerButtonStyle()).disabled(model.isBusy || model.deadlockRunning || model.phase == .detecting)
                        } else { Text(model.containerScope ? "Отдельная Wine-среда будет создана при включении." : "Копия будет создана при первом включении.").font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary) }
                    }
                }
            }
        }
    }

    private func methodOption(_ method: WarmerModel.Method, icon: String, subtitle: String) -> some View {
        Button { model.method = method; recheck(clear: true) } label: {
            HStack(spacing: 12) {
                Image(systemName: icon).font(.system(size: 20)).foregroundStyle(WarmerTheme.accent)
                VStack(alignment: .leading, spacing: 5) {
                    Text(method.rawValue).font(.system(size: 14, weight: .semibold))
                    Text(subtitle).font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
                }
                Spacer()
                Image(systemName: model.method == method ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(model.method == method ? WarmerTheme.accent : WarmerTheme.secondary)
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                .background(model.method == method ? WarmerTheme.accent.opacity(0.07) : WarmerTheme.raised, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(model.method == method ? WarmerTheme.accent.opacity(0.7) : WarmerTheme.border))
        }.buttonStyle(.plain).disabled(model.isBusy || model.phase == .detecting)
    }

    private var gptkParameters: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Версия GPTK").font(.system(size: 12))
                Spacer()
                if model.availableGptks.count > 1 {
                    Picker("Версия GPTK", selection: Binding(get: { model.selectedGptkPath ?? model.gptkPath }, set: { model.selectedGptkPath = $0 })) {
                        ForEach(model.availableGptks) { option in Text(option.version).tag(option.path as String?) }
                    }.labelsHidden().frame(width: 150).disabled(model.isBusy)
                } else { Text(model.selectedGptkVersion ?? "Не найден").font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary) }
                Button("Выбрать папку") { onPickGPTK() }.buttonStyle(WarmerButtonStyle(compact: true)).disabled(model.isBusy)
            }
            if game?.isDeadlock == true {
                Toggle("Полный прогрев", isOn: $model.fullWarm).toggleStyle(.switch).disabled(model.isBusy)
                Text("Больше классов рендеринга. Подготовка занимает больше времени.").font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
            }
            Text("Для запуска игры выбери тот же GPTK в GameHub.").font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary)
        }
    }

    private var diagnostics: some View {
        VStack(spacing: 16) {
            WarmerPanel {
                VStack(alignment: .leading, spacing: 18) {
                    SectionTitle(title: "Логи и диагностика", subtitle: "Собери данные после игрового теста, чтобы проверить задержки кадров.")
                    HStack(spacing: 12) {
                        Button(action: onCollectLogs) { Label(collectingLogs ? "Собираем логи…" : "Собрать логи", systemImage: "square.and.arrow.up") }
                            .buttonStyle(WarmerButtonStyle(prominent: true)).disabled(collectingLogs || model.isBusy)
                        if model.usesDXMT {
                            Button("Открыть логи кадров") {
                                let path = model.dxmtLogDirectory ?? NSHomeDirectory() + "/Library/Logs/ShaderWarmer/dxmt"
                                let url = URL(fileURLWithPath: path)
                                try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
                                NSWorkspace.shared.open(url)
                            }.buttonStyle(WarmerButtonStyle())
                        } else {
                            Button(model.monitoring ? "Остановить запись" : "Записывать события D3DMetal") { engine.toggleMonitor(model) }
                                .buttonStyle(WarmerButtonStyle())
                        }
                    }
                    if model.monitoring { inlineNote("Идёт запись системных событий D3DMetal.", icon: "record.circle", color: WarmerTheme.accent) }
                }
            }
            WarmerPanel {
                VStack(alignment: .leading, spacing: 14) {
                    SectionTitle(title: "Журнал операции")
                    if model.log.isEmpty && model.checks.isEmpty {
                        Text("Здесь появятся результаты проверок и сообщения движка после запуска операции.")
                            .font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary).padding(.vertical, 16)
                    } else {
                        ForEach(model.checks) { check in
                            inlineNote(check.name + (check.detail.isEmpty ? "" : ": " + check.detail),
                                       icon: check.ok ? "checkmark.circle" : "exclamationmark.circle", color: check.ok ? WarmerTheme.success : WarmerTheme.error)
                        }
                        if !model.log.isEmpty { LogConsole(lines: model.log) }
                    }
                }
            }
        }
    }

    private func inlineNote(_ text: String, icon: String, color: Color) -> some View {
        Label { Text(text).fixedSize(horizontal: false, vertical: true) } icon: { Image(systemName: icon) }
            .font(.system(size: 11)).foregroundStyle(color)
    }
    private func stageName(_ stage: String) -> String {
        ["preflight": "Проверка окружения", "extract": "Извлечение шейдеров", "plan": "План прогрева", "calibrate": "Калибровка GPTK", "build": "Сборка кэша", "warm": "Прогрев шейдеров", "install": model.usesDXMT ? "Применение настроек" : "Установка кэша"][stage] ?? stage
    }
    private func grouped(_ value: Int) -> String { value.formatted(.number.grouping(.automatic)).replacingOccurrences(of: ",", with: " ") }
    private func duration(_ value: Int) -> String { value >= 60 ? "~ \(value / 60) мин" : "\(value) с" }
    private func shorten(_ path: String) -> String { path.replacingOccurrences(of: NSHomeDirectory(), with: "~") }
    private func recheck(clear: Bool = false) {
        if clear { model.clearDetected(); model.stageHistory = []; model.currentStage = ""; model.resetProgress() }
        model.phase = .detecting
        Task { await engine.detect(model) }
    }
    private var launcherMenu: some View {
        Menu {
            Button("GameHub") { engine.assignLauncher(model, profile: nil) }
            ForEach(model.launcherProfiles) { profile in
                Button(profile.title) { engine.assignLauncher(model, profile: profile) }
            }
            Divider()
            Button("Подключить другой лаунчер…") { model.showingAddLauncher = true }
        } label: { Label("Сменить лаунчер", systemImage: "arrow.left.arrow.right") }
            .menuStyle(.borderlessButton).fixedSize().font(.system(size: 12, weight: .semibold))
            .disabled(model.isBusy || model.phase == .detecting)
    }
    private func openLauncher() {
        let url = model.selectedProfile?.appPath.map { URL(fileURLWithPath: $0) }
            ?? NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.gamemac.www")
        guard let url else {
            let alert = NSAlert(); alert.messageText = "Лаунчер не найден"; alert.informativeText = "Открой выбранный лаунчер вручную."; alert.runModal(); return
        }
        NSWorkspace.shared.open(url)
    }

}

struct WarmProgressRing: View {
    let progress: Double
    let showPercent: Bool
    let title: String
    var failed = false
    var body: some View {
        ZStack {
            Circle().stroke(WarmerTheme.border.opacity(0.7), lineWidth: 9)
            if showPercent {
                Circle().trim(from: 0, to: max(0, min(progress, 1))).stroke(failed ? WarmerTheme.error : WarmerTheme.accent,
                    style: StrokeStyle(lineWidth: 9, lineCap: .round)).rotationEffect(.degrees(-90))
            }
            VStack(spacing: 8) {
                if showPercent { Text("\(Int((max(0, min(progress, 1)) * 100).rounded()))%").font(.system(size: 37, weight: .semibold)).monospacedDigit() }
                else { Image(systemName: failed ? "exclamationmark" : "flame").font(.system(size: 33, weight: .light)).foregroundStyle(WarmerTheme.accent) }
                Text(title).font(.system(size: 10)).foregroundStyle(WarmerTheme.secondary)
            }
        }.frame(width: 160, height: 160).padding(6)
            .accessibilityElement(children: .ignore).accessibilityLabel(showPercent ? "\(title): \(Int((max(0, min(progress, 1)) * 100).rounded())) процентов" : title)
    }
}

struct LogConsole: View {
    let lines: [String]
    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 5) {
                    ForEach(Array(lines.suffix(100).enumerated()), id: \.offset) { index, line in
                        Text(line).font(.system(size: 10, design: .monospaced)).foregroundStyle(WarmerTheme.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled).id(index)
                    }
                }.padding(15)
            }.frame(height: 145).background(WarmerTheme.background, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(WarmerTheme.border))
                .onChange(of: lines.count) { proxy.scrollTo(min(lines.count, 100) - 1, anchor: .bottom) }
        }
    }
}
