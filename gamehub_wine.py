"""Keep a GameHub game's configured Wine major in sync with its warmer.

GameHub stores one JSON document per game.  The warmer already selects its own
Wine binary, but the game must use the same major afterwards or it can reject or
replace the generated cache.  Updates are atomic and preserve a sibling backup.
"""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path


def _proton_major(manifest: dict) -> str:
    metadata = manifest.get("metadata")
    value = str(metadata.get("proton_version", "")) if isinstance(metadata, dict) else ""
    return value.split(".", 1)[0]


def installed_wines(engine_root: Path) -> list[dict]:
    result = []
    for directory in sorted((engine_root / "containers/wine_installations").glob("*")):
        manifest_path = directory / "manifest.json"
        executable = directory / "bin/wine"
        if not manifest_path.is_file() or not executable.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        result.append({"id": directory.name, "path": directory, "manifest": manifest,
                       "major": _proton_major(manifest)})
    return result


def game_settings_file(engine_root: Path, platform_app_id: str) -> Path | None:
    settings_dir = engine_root.parent / "gamehub/game-settings"
    for path in sorted(settings_dir.glob("*.json")):
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict) or not isinstance(document.get("key"), dict):
            continue
        if str(document.get("key", {}).get("platform_app_id")) == str(platform_app_id):
            return path
    return None


def configured_major(engine_root: Path, platform_app_id: str) -> str | None:
    path = game_settings_file(engine_root, platform_app_id)
    if path is None:
        return None
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    selected = str(document.get("settings", {}).get("compatibility_layer", ""))
    return next((w["major"] for w in installed_wines(engine_root) if w["id"] == selected), None)


def _ui_config(engine_root: Path, wine: dict) -> dict:
    """Reuse GameHub's own complete UI config for this Wine when one is available."""
    settings_dir = engine_root.parent / "gamehub/game-settings"
    for path in sorted(settings_dir.glob("*.json")):
        try:
            settings = json.loads(path.read_text()).get("settings", {})
        except (OSError, ValueError):
            continue
        if str(settings.get("compatibility_layer")) == wine["id"]:
            config = settings.get("compatibility_layer_config")
            if isinstance(config, dict):
                return copy.deepcopy(config)

    manifest = wine["manifest"]
    major = wine["major"]
    return {
        "display_name": f"wine-proton_{major}.0",
        "framework": "X64" if manifest.get("architecture") == "x86_64" else "ARM64",
        "framework_type": "proton",
        "id": int(wine["id"]) if wine["id"].isdigit() else wine["id"],
        "is_steam": 1,
        "name": f"wine-proton_{major}.0",
        "version": manifest.get("metadata", {}).get("proton_version", f"{major}.0"),
        "version_code": int(manifest.get("metadata", {}).get("version_code", 0) or 0),
    }


def ensure_game_wine(engine_root: Path, platform_app_id: str, required_major: str) -> dict:
    """Select the installed x86_64 Proton major for a GameHub game.

    Returns a report with ``changed`` and the selected installation ``path``.
    Raises a descriptive error without changing anything when GameHub or the
    required Wine installation cannot be identified.
    """
    engine_root = Path(engine_root)
    candidates = [w for w in installed_wines(engine_root)
                  if w["major"] == str(required_major)
                  and w["manifest"].get("architecture") == "x86_64"]
    if not candidates:
        raise RuntimeError(f"Wine Proton {required_major} x86_64 не установлен в GameHub")
    selected = candidates[-1]
    settings_path = game_settings_file(engine_root, platform_app_id)
    if settings_path is None:
        raise RuntimeError(f"настройки GameHub для игры {platform_app_id} не найдены")
    document = json.loads(settings_path.read_text())
    settings = document.setdefault("settings", {})
    old_id = str(settings.get("compatibility_layer", ""))
    if old_id == selected["id"]:
        return {"changed": False, "path": selected["path"], "settings": settings_path,
                "old_id": old_id, "new_id": selected["id"]}

    settings["compatibility_layer"] = selected["id"]
    settings["compatibility_layer_config"] = _ui_config(engine_root, selected)
    now = int(time.time())
    settings["updated_at"] = now
    document["updated_at"] = now
    for key in ("settings_revision", "runtime_config_revision"):
        if isinstance(document.get(key), int):
            document[key] += 1

    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    backup = settings_path.with_suffix(settings_path.suffix + ".shader-warmer-backup")
    if not backup.exists():
        backup.write_bytes(settings_path.read_bytes())
    temporary = settings_path.with_suffix(settings_path.suffix + ".shader-warmer-tmp")
    temporary.write_text(payload)
    os.replace(temporary, settings_path)
    return {"changed": True, "path": selected["path"], "settings": settings_path,
            "backup": backup, "old_id": old_id, "new_id": selected["id"]}
