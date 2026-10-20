#!/bin/bash
# Build the .dmg installer (drag-to-Applications).
# The app is ad-hoc signed (no paid Apple Developer account), so macOS blocks anything
# downloaded from the net; the bundled "Как открыть.txt" leads with the xattr command,
# because newer macOS no longer offers an "Открыть всё равно" button for such apps.
set -euo pipefail
cd "$(dirname "$0")"
VER=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' Info.plist)
NAME="Shader Warmer"
APP="build/${NAME}.app"
DMG=build/DeadlockWarmer.dmg
STAGE=build/dmg
[ -d "$APP" ] || { echo "сначала собери приложение: bash build_app.sh"; exit 1; }

rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/Read Me.txt" <<TXT
${NAME} ${VER}
======================

Drag Shader Warmer to Applications, then launch it.
The interface follows your macOS preferred languages: English or Russian.
English is used when none of your preferred languages is supported.
Restart the app after changing your macOS language preferences.

This app is locally signed, without Apple notarization. If macOS blocks your
downloaded copy, open Terminal and run after installing it:

  xattr -dr com.apple.quarantine "/Applications/${NAME}.app"

Then open the app again. No Python, Homebrew or Xcode installation is needed.

DXMT Async requires Apple Silicon, macOS 15+, Rosetta 2 and a compatible
Windows x64 game running in DirectX 11 mode.

GameHub: select Wine Proton 11 x64 and DXMT v0.80, launch the game once,
then close it. Select the game in Shader Warmer and click Enable fix.
After activation, launch normally from GameHub.

CrossOver: select the game's bottle, close its Windows apps, then enable Async.
After activation, launch from CrossOver or Steam. Shader Warmer can be closed.
Activation affects the entire bottle. Keep the game in DirectX 11 mode.

Other compatible Wine environments can be connected manually and launched
through Shader Warmer. Compatibility is checked before activation.

Use the disable action to restore settings. Disable Async before removing
Shader Warmer's backups or private runtimes from Library/Application Support.
New objects or effects may briefly appear late while compiling.

Russian installation instructions: Как открыть.txt.
TXT

cat > "$STAGE/Как открыть.txt" <<TXT
${NAME} ${VER}
======================

Приложение подписано «для себя» (платного аккаунта Apple у проекта нет), а у
всего скачанного из интернета macOS ставит карантинный флаг. На свежих macOS
такое сочетание блокируется сразу: показывается «Apple не удалось подтвердить,
что файл не содержит вредоносного ПО», и кнопки «Открыть всё равно» в
настройках может не быть вообще. Снимается флаг одной командой в Терминале.

Самый надёжный порядок — снять карантин с образа ДО установки:

  1. Закрой этот образ (Finder → выгрузить диск).
  2. Терминал:

       xattr -dr com.apple.quarantine ~/Downloads/DeadlockWarmer.dmg

  3. Открой образ заново и перетащи «${NAME}» в Applications.
     Приложение запустится обычным двойным кликом.

Если уже перетащил и получил предупреждение — ничего не удаляй, просто выполни:

       xattr -dr com.apple.quarantine "/Applications/${NAME}.app"

и запусти снова.

Дальше:
  - Нужен Mac с Apple Silicon и macOS 14 или новее.
    DXMT Async требует macOS 15 или новее и установленную Rosetta 2.
    Python, Homebrew и Xcode устанавливать не нужно — всё нужное внутри приложения.
  - На другой Mac переноси только DMG или приложение. Настройки и резервные
    копии из Library/Application Support переносить не нужно.
  - Для Windows x64 DX11-игр основной режим — DXMT Async. Установи и выбери DXMT v0.80
    и Wine Proton 11 x64 в GameHub; один раз запусти и закрой выбранную игру.
  - Другие Wine-лаунчеры: выбери «Все лаунчеры» → «Подключить другой лаунчер».
    Укажи существующий контейнер и его Wine. CrossOver определяется автоматически.
    Кнопка «Сменить лаунчер» на странице игры выбирает нужную среду.
    CrossOver: включи Async один раз, затем запускай игру из CrossOver или Steam.
    Shader Warmer можно закрыть. Настройка действует на всю бутылку; игре нужен DX11.
    Другие Wine-среды: используй «Запустить игру» в Shader Warmer и выбери её .exe.
    Совместимость проверяется перед активацией; поддерживаются совместимые Wine-среды для DX11 x64.
  - В GameHub открой выбранную игру в Shader Warmer и нажми «Включить фикс».
    Если программа просит, подтверди поддержку DirectX 11. Для Unity/Unreal
    режим DX11 выбирается параметром запуска; для остальных выбери DX11 в игре.
    Затем запускай игру через GameHub как обычно.
  - Старый прогрев доступен в режиме GPTK. Для него игра должна использовать
    тот же GPTK в GameHub. Для остальных поддерживаемых игр нажми
    «Прогреть шейдеры» — шейдеры берутся из твоей установки игры.
  - «Отключить для этой игры» возвращает её исходные настройки.
    Общий DXMT восстанавливается после отключения последней игры.
TXT

hdiutil create -volname "${NAME}" \
    -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "built $DMG"
ls -lh "$DMG"
