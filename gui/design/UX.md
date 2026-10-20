# Shader Warmer 1.47

## Design read

Native macOS game library with a dark launcher aesthetic, authentic game artwork and a soft orange accent.
DESIGN_VARIANCE: 5. MOTION_INTENSITY: 3. VISUAL_DENSITY: 4.

## Audit

The previous interface used system gray panels, a horizontal strip of small icons, a large last-run
card and a segmented method control. The library felt secondary. The game page repeated its status
and exposed configuration before the main action. Artwork mixed executable icons and wide banners.

Preserve the Shader Warmer name, Главная / Настройки navigation, game discovery, manual additions,
DXMT / GPTK choice, preflight errors, cancellation, independent restoration and diagnostic export.
Keep the existing engine and restore formats. Do not add controls for unimplemented CPU limits,
cache deletion, pause, startup integration or automatic updates.

## Composition

- Home: searchable cover grid, working list switch, truthful per-game status, add-game action.
- Game: wide artwork, folder access, Обзор / Параметры / Диагностика tabs.
- DXMT overview: enabled / available / blocked state and one primary action. No invented shader
  counts or gameplay compilation percentages. Shader compilation occurs during the game.
- GPTK overview: circular progress from real engine values, actual stages and measured counts/ETA.
- Settings: working defaults, notification preference, artwork download preference, automatic
  diagnostics preference and access to application folders.

## Tokens

Background #131414, rail #171818, surface #1D1E1F, raised #252627, border #343638.
Text #F1F1EF, secondary #ABADAF, accent #E9A66F with dark text #211810.
Green and salmon are reserved for real success/error states. Rounded panels 16, controls 10,
cover corners 14. Native SF system typography and SF Symbols; SF Mono for diagnostic output.

## Verification

Check library grid/list, search and empty results; game navigation, method switching, all tabs,
DXMT enable/restore and preservation of another active game. Inspect minimum window size,
large window, keyboard focus, disabled controls, error states and a separate GPTK progress preview.
Artwork is loaded from the current user's Steam cache first, then optionally from Steam's public
CDN. It is cached outside the signed bundle; offline icon fallback remains available.
