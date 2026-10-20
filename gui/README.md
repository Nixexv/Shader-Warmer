# Shader Warmer GUI

Native SwiftUI frontend with a searchable game library, DXMT Async / GPTK controls,
launcher selection, game diagnostics and reversible activation.

Build with `bash build_app.sh`, then `bash make_dmg.sh`. Output:
`build/Shader Warmer.app` and `build/DeadlockWarmer.dmg`.
The build needs macOS Command Line Tools. Recipients need no Python, Homebrew or Xcode:
the app includes a relocatable Python runtime and the engine resources.

`Sources/LauncherProfiles.swift` discovers CrossOver bottles and persists manually connected
Wine environments. `GameScanner.swift` finds installed games. `Engine.swift` dispatches GameHub
operations to `dxmt_fix.py`, other Wine environments to `wine_dxmt.py`, and GPTK to the existing
per-game engines. CrossOver Async persists in bottle settings for normal CrossOver/Steam launches.
Other Wine environments launch explicitly from Shader Warmer.

The bundled D3D11 compatibility helper is reproducible from `tools/dxmt_probe.c` with
`x86_64-w64-mingw32-gcc -Os -s -static -o dxmt_async/dxmt_probe.exe tools/dxmt_probe.c`.
Update `probe_sha256` in `dxmt_async/manifest.json` after rebuilding it.

Version 1.51 automatically selects English or Russian from macOS preferred languages, with English fallback. `Sources/Localization.swift` keeps interpolation values separate from translation keys; `Resources/Localizations.json` contains English translations of the Russian source keys. Native user-facing engine messages are translated at the GUI boundary; original diagnostic output remains available.
