import SwiftUI

struct GameLibraryView: View {
    @EnvironmentObject private var model: WarmerModel
    let onSelect: (WarmerModel.Game) -> Void
    let onAdd: () -> Void
    let onRefresh: () -> Void
    @SceneStorage("librarySearch") private var search = ""
    @State private var enabledOnly = false
    @FocusState private var searchFocused: Bool
    @AppStorage("libraryListMode") private var listMode = false
    @AppStorage("showSupportedOnly") private var supportedOnly = false

    private var filtered: [WarmerModel.Game] {
        model.games.filter { game in
            (search.isEmpty || game.name.localizedCaseInsensitiveContains(search)) &&
            (!supportedOnly || game.canUseDXMT || game.supported) &&
            (model.libraryLauncher == "all" || (model.libraryLauncher == "gamehub" ? game.profileID == nil : game.profileID == model.libraryLauncher)) &&
            (!enabledOnly || model.libraryStates[game.id]?.enabled == true)
        }
    }

    var body: some View {
        GeometryReader { geometry in
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 7) {
                        Text(L("Мои игры")).font(.system(size: 29, weight: .bold))
                        Text(L("Меньше пауз при компиляции шейдеров."))
                            .font(.system(size: 13)).foregroundStyle(WarmerTheme.secondary)
                    }
                    Spacer()
                    Button(action: onAdd) { Label(L("Добавить игру"), systemImage: "plus") }
                        .buttonStyle(WarmerButtonStyle(prominent: true))
                }
                if model.isBusy, let game = model.warmingGame {
                    Button { onSelect(game) } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "waveform.path").foregroundStyle(WarmerTheme.accent)
                            Text(game.name).fontWeight(.semibold)
                            Text(model.statusText).foregroundStyle(WarmerTheme.secondary).lineLimit(1)
                            Spacer()
                            Image(systemName: "arrow.up.right")
                        }.font(.system(size: 12)).padding(16)
                            .background(WarmerTheme.surface, in: RoundedRectangle(cornerRadius: 12))
                    }.buttonStyle(.plain)
                }
                HStack {
                    Menu {
                        Button(L("Все лаунчеры")) { model.libraryLauncher = "all" }
                        Button("GameHub") { model.libraryLauncher = "gamehub" }
                        ForEach(model.launcherProfiles) { profile in
                            Button(profile.title) { model.libraryLauncher = profile.id }
                        }
                        Divider()
                        Button(L("Подключить другой лаунчер…")) { model.showingAddLauncher = true }
                    } label: {
                        Label(model.libraryLauncher == "all" ? L("Все лаунчеры") : model.libraryLauncher == "gamehub" ? "GameHub" : model.launcherProfiles.first(where: { $0.id == model.libraryLauncher })?.title ?? L("Выбрать лаунчер"), systemImage: "arrow.left.arrow.right")
                    }.menuStyle(.borderlessButton).fixedSize().font(.system(size: 12, weight: .medium))
                    Spacer()
                }
                toolbar
                HStack(spacing: 18) {
                    filterButton(L("Все игры"), active: !enabledOnly) { enabledOnly = false }
                    filterButton(L("Async включён"), active: enabledOnly) { enabledOnly = true }
                    Spacer()
                    Text(L("\(filtered.count) из \(model.games.count)"))
                        .font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
                }
                if model.games.isEmpty {
                    EmptyLibraryState(title: L("Добавь первую игру"), message: L("Выбери папку Windows-игры или установи её через GameHub."), symbol: "plus.square.dashed")
                } else if filtered.isEmpty && search.isEmpty && !enabledOnly && model.libraryLauncher != "all" {
                    EmptyLibraryState(title: L("Добавь игру для этого лаунчера"), message: L("Нажми «Добавить игру» и выбери папку её Windows-версии. Будем использовать выбранный контейнер."), symbol: "plus.square.dashed")
                } else if filtered.isEmpty {
                    EmptyLibraryState(title: L("Игры не найдены"), message: L("Попробуй другое название или вернись ко всем играм."), symbol: "magnifyingglass")
                } else if listMode {
                    LazyVStack(spacing: 10) {
                        ForEach(filtered) { game in LibraryGameCard(game: game, list: true) { onSelect(game) } }
                    }
                } else {
                    LazyVGrid(columns: columns(width: min(geometry.size.width, 1240) - 64), alignment: .leading, spacing: 22) {
                        ForEach(filtered) { game in LibraryGameCard(game: game) { onSelect(game) } }
                    }
                }
            }.padding(32).frame(maxWidth: 1240).frame(maxWidth: .infinity)
        }.background(WarmerTheme.background)
        }
    }

    private func columns(width: CGFloat) -> [GridItem] {
        let fitting = max(1, Int((width + 18) / 203))
        let count = min(fitting, max(1, filtered.count))
        return Array(repeating: GridItem(.flexible(minimum: 185, maximum: 280), spacing: 18), count: count)
    }

    private var toolbar: some View {
        HStack(spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass").foregroundStyle(WarmerTheme.secondary)
                TextField(L("Поиск игр…"), text: $search).textFieldStyle(.plain).font(.system(size: 13))
                    .accessibilityLabel(L("Поиск игр")).focused($searchFocused)
                if !search.isEmpty {
                    Button { search = "" } label: { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.plain).foregroundStyle(WarmerTheme.secondary).accessibilityLabel(L("Очистить поиск"))
                }
            }.padding(.horizontal, 14).frame(height: 42).frame(maxWidth: 420)
                .background(WarmerTheme.surface, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(searchFocused ? WarmerTheme.accent.opacity(0.7) : WarmerTheme.border))
            Spacer()
            Button(action: onRefresh) { Image(systemName: "arrow.clockwise") }
                .buttonStyle(WarmerButtonStyle(compact: true)).help(L("Обновить библиотеку"))
                .accessibilityLabel(L("Обновить библиотеку")).disabled(model.isBusy)
            HStack(spacing: 2) {
                displayButton("square.grid.2x2.fill", active: !listMode, label: L("Показать сеткой")) { listMode = false }
                displayButton("list.bullet", active: listMode, label: L("Показать списком")) { listMode = true }
            }.padding(4).background(WarmerTheme.surface, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(WarmerTheme.border))
        }
    }

    private func filterButton(_ title: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 0) {
                Text(title).font(.system(size: 12, weight: active ? .semibold : .regular))
                    .foregroundStyle(active ? WarmerTheme.accent : WarmerTheme.secondary)
                    .frame(height: 34)
                Capsule().fill(active ? WarmerTheme.accent : Color.clear).frame(height: 2)
            }
            .padding(.horizontal, 12)
            .fixedSize(horizontal: true, vertical: false)
            .background(WarmerTheme.background)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? .isSelected : [])
    }

    private func displayButton(_ icon: String, active: Bool, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.system(size: 13)).frame(width: 32, height: 30)
                .foregroundStyle(active ? WarmerTheme.text : WarmerTheme.secondary)
                .background(active ? WarmerTheme.raised : Color.clear, in: RoundedRectangle(cornerRadius: 7))
        }.buttonStyle(.plain).accessibilityLabel(label).help(label)
    }
}

private struct LibraryGameCard: View {
    @EnvironmentObject private var model: WarmerModel
    let game: WarmerModel.Game
    var list = false
    let action: () -> Void
    @State private var hovered = false

    private var issue: Bool { model.detectedGameID == game.id && model.usesDXMT && model.runtimeError != nil }
    private var enabled: Bool {
        if model.detectedGameID == game.id && model.usesDXMT { return model.dxmtInstalled }
        return model.libraryStates[game.id]?.enabled == true
    }
    private var busy: Bool { model.isBusy && model.warmingGame?.id == game.id }
    private var status: String {
        if busy { return model.usesDXMT ? L("Применение фикса") : L("Идёт прогрев") }
        if issue { return L("Нужна подготовка") }
        if enabled { return L("Фикс включён") }
        if model.lastWarm?.game == game.name && model.lastWarm?.method == nil { return L("Прогрев выполнен") }
        return game.canUseDXMT ? L("Доступен DXMT Async") : game.supported ? L("Доступен GPTK") : L("Нет доступного метода")
    }
    private var statusColor: Color { issue ? WarmerTheme.error : enabled ? WarmerTheme.success : game.canUseDXMT ? WarmerTheme.accent : WarmerTheme.secondary }
    private var selected: Bool { model.selectedGame?.id == game.id }

    var body: some View {
        Button(action: action) {
            Group {
                if list {
                    HStack(spacing: 20) {
                        ArtworkView(game: game).frame(width: 68, height: 90).clipShape(RoundedRectangle(cornerRadius: 8))
                        VStack(alignment: .leading, spacing: 9) {
                            Text(game.name).font(.system(size: 15, weight: .semibold)).lineLimit(1)
                            Text(game.profileID.flatMap { id in model.launcherProfiles.first { $0.id == id }?.title } ?? L10n.message(game.source)).font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary)
                        }
                        Spacer()
                        Label(status, systemImage: enabled ? "checkmark.circle.fill" : "circle.fill")
                            .font(.system(size: 11)).foregroundStyle(statusColor)
                        Image(systemName: "chevron.right").font(.system(size: 11)).foregroundStyle(WarmerTheme.secondary).padding(.leading, 18)
                    }.padding(12)
                } else {
                    VStack(alignment: .leading, spacing: 0) {
                        ArtworkView(game: game).aspectRatio(0.82, contentMode: .fit).clipped()
                        VStack(alignment: .leading, spacing: 10) {
                            Text(game.name).font(.system(size: 13, weight: .semibold)).lineLimit(2).frame(height: 34, alignment: .topLeading)
                            Text(game.profileID.flatMap { id in model.launcherProfiles.first { $0.id == id }?.title } ?? "GameHub")
                                .font(.system(size: 10)).foregroundStyle(WarmerTheme.secondary).lineLimit(1)
                            Label(status, systemImage: enabled ? "checkmark.circle.fill" : "circle.fill")
                                .font(.system(size: 10, weight: .medium)).foregroundStyle(statusColor).lineLimit(1)
                        }.padding(15).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .foregroundStyle(WarmerTheme.text)
            .background(hovered ? WarmerTheme.raised : WarmerTheme.surface)
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(selected ? WarmerTheme.accent.opacity(0.8) : hovered ? WarmerTheme.border : WarmerTheme.border.opacity(0.6), lineWidth: selected ? 1.5 : 1))
            .shadow(color: .black.opacity(hovered ? 0.2 : 0), radius: 12, y: 5)
            .offset(y: hovered ? -2 : 0)
        }.buttonStyle(.plain).onHover { hovered = $0 }
            .accessibilityLabel("\(game.name), \(status)")
            .contextMenu {
                Button(L("Открыть папку игры")) { NSWorkspace.shared.open(URL(fileURLWithPath: game.path)) }
            }
    }
}
