#!/bin/bash
# Build the app: compile Swift, assemble the bundle, embed the engine.
# The bundle keeps a stable Finder name.  The generated icon carries the version badge.
set -euo pipefail
cd "$(dirname "$0")"
VER=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' Info.plist)
NAME="Shader Warmer"
APP="build/${NAME}.app"
ENGINE_SRC=..
rm -rf build && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Build a native macOS icon with the current release number visible in Finder.
swiftc -O -framework AppKit -o build/make_icon make_icon.swift
build/make_icon "$VER" build/AppIcon-1024.png
ICONSET=build/AppIcon.iconset
mkdir -p "$ICONSET"
for spec in "16 icon_16x16.png" "32 icon_16x16@2x.png" \
            "32 icon_32x32.png" "64 icon_32x32@2x.png" \
            "128 icon_128x128.png" "256 icon_128x128@2x.png" \
            "256 icon_256x256.png" "512 icon_256x256@2x.png" \
            "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
    set -- $spec
    sips -z "$1" "$1" build/AppIcon-1024.png --out "$ICONSET/$2" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

swiftc -O -parse-as-library \
    -target arm64-apple-macos14.0 \
    -framework SwiftUI -framework AppKit -framework UserNotifications \
    -o "$APP/Contents/MacOS/DeadlockWarmer" \
    Sources/Model.swift Sources/LauncherProfiles.swift Sources/GameScanner.swift Sources/GameIcons.swift Sources/Engine.swift Sources/Notifier.swift Sources/DesignSystem.swift Sources/GameArtwork.swift Sources/LibraryView.swift Sources/GameDetailView.swift Sources/SettingsView.swift Sources/ContentView.swift Sources/App.swift

cp Info.plist "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName ${NAME}" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName ${NAME}" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconFile" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon.icns" "$APP/Contents/Info.plist"
mkdir -p "$APP/Contents/Resources/engine"
for f in wine_dxmt.py dxmt_game.py dxmt_fix.py warm.py extract_shaders.py gen_plan.py warmgen.exe layout_library.json vfx_zstd_2bc2fa87.bin config.txt ow_warm.py unity_warm.py gptk_pick.py metal_cache.py gamehub_wine.py diagnostics.py; do
    cp "$ENGINE_SRC/$f" "$APP/Contents/Resources/engine/$f"
done
# Overwatch engine: python modules plus its own binaries (extractor, DXBC->DXIL, metallib compiler,
# warmgen, xxh3) — everything the offline build needs besides the user's own GPTK.
cp -R "$ENGINE_SRC/ow" "$APP/Contents/Resources/engine/ow"
# Unity engine: the asset reader and the DXBC carver (pure python, it reuses ow/'s binaries)
cp -R "$ENGINE_SRC/unity" "$APP/Contents/Resources/engine/unity"
cp -R "$ENGINE_SRC/dxmt_async" "$APP/Contents/Resources/engine/dxmt_async"
cp -R "$ENGINE_SRC/lib" "$APP/Contents/Resources/engine/lib"
python3 -B "$ENGINE_SRC/tools/bundle_python.py" "$APP/Contents/Resources/python"
# Do not ship bytecode from a developer's Python version inside sealed app resources.
python3 -B - "$APP/Contents/Resources/engine" <<'PY'
import pathlib, shutil, sys
for cache in pathlib.Path(sys.argv[1]).rglob('__pycache__'):
    shutil.rmtree(cache)
PY
bash ./sign_app.sh "$APP"
echo "built $APP"
