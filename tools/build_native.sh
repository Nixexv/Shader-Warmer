#!/bin/bash
# Build the existing converters for macOS 14, with no developer-machine dylib paths.
set -euo pipefail
cd "$(dirname "$0")/.."
RES="${SHADER_WARMER_GPTK_RES:-}"
if [ -z "$RES" ]; then
    for candidate in "$HOME/Library/Application Support/com.gamemac.www/wine-engine/downloads/gptk-3.0-3/external/D3DMetal.framework/Versions/A/Resources" \
        "$HOME/Library/Application Support/com.gamemac.www/wine-engine/downloads/"gptk-*/external/D3DMetal.framework/Versions/A/Resources; do
        if [ -f "$candidate/libmetalirconverter.dylib" ]; then RES="$candidate"; break; fi
    done
fi
[ -f "$RES/libmetalirconverter.dylib" ] || { echo "Set SHADER_WARMER_GPTK_RES to GameHub GPTK Resources" >&2; exit 1; }
clang++ -target x86_64-apple-macos14.0 -std=c++17 -fno-rtti -O2 native/dxbc2dxil.cpp -o ow/bin/dxbc2dxil
clang++ -target x86_64-apple-macos14.0 -std=c++17 -O2 native/stage2metallib.cpp \
    -L"$RES" -lmetalirconverter -Wl,-rpath,@executable_path/gptk_res -o ow/bin/stage2metallib
clang -target arm64-apple-macos14.0 -O3 -dynamiclib native/xxh3.c \
    -Wl,-install_name,@rpath/libxxh3.dylib -o ow/bin/libxxh3.dylib
for binary in ow/bin/dxbc2dxil ow/bin/stage2metallib ow/bin/libxxh3.dylib; do
    codesign --force --sign - "$binary"
done
