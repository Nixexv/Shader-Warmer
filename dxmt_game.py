"""Resolve one installed GameHub game and inspect local DX11/x64 evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct


def pe_info(path, max_bytes=16 * 1024 * 1024):
    try:
        with Path(path).open('rb') as file:
            head = file.read(64)
            if len(head) < 64 or head[:2] != b'MZ':
                return None
            file.seek(struct.unpack_from('<I', head, 60)[0])
            pe = file.read(6)
            if pe[:4] != b'PE\0\0':
                return None
            machine = int.from_bytes(pe[4:6], 'little')
            # Bounded read; this is support evidence, not a claim about the active renderer.
            file.seek(0)
            first = max_bytes // 2
            data = file.read(first)
            size = file.seek(0, 2)
            file.seek(max(len(data), size - (max_bytes - first)))
            data += file.read(max_bytes - first)
            data = data.lower()
            dx11 = b'd3d11.dll\0' in data or 'd3d11.dll\0'.encode('utf-16le') in data
            return machine, dx11
    except (OSError, struct.error):
        return None


def probe(game, deadlock=False):
    game = Path(game)
    mode = 'deadlock' if deadlock else 'generic'
    if any((p / 'globalgamemanagers').is_file() for p in game.glob('*_Data')):
        mode = 'unity'
    elif (game / 'Engine').is_dir():
        mode = 'unreal'
    x64 = False
    x86 = False
    dx11 = deadlock
    executables = []
    scanned = 0
    budget = 128 * 1024 * 1024
    for directory, dirs, files in os.walk(game):
        relative = Path(directory).relative_to(game)
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not d.endswith('_Data') and d.lower() not in
                   ('shadercache', 'steamapps', 'node_modules', 'movies', 'streamingassets'))
        if len(relative.parts) >= 6:
            dirs[:] = []
        for name in files:
            if Path(name).suffix.lower() not in ('.exe', '.dll'):
                continue
            scanned += 1
            path = Path(directory) / name
            limit = min(budget, 16 * 1024 * 1024)
            info = pe_info(path, limit)
            try:
                budget -= min(limit, path.stat().st_size)
            except OSError:
                pass
            if info:
                helper = bool(re.search(r'crash|redist|prereq|setup|unins|installer|updater', name, re.I))
                renderer = (name.lower().endswith('.exe') and not helper) or name.lower() == 'unityplayer.dll'
                x64 |= renderer and info[0] == 0x8664
                x86 |= renderer and info[0] == 0x14c
                dx11 |= info[0] == 0x8664 and info[1]
                if name.lower().endswith('.exe'):
                    executables.append(name.lower())
            if scanned >= 250:
                break
        if scanned >= 250:
            break
    if not x64:
        reason = '32-битные игры пока не поддерживаются Async-пакетом' if x86 else 'Не найден Windows x64 исполняемый файл игры'
        raise RuntimeError(reason)
    return {'mode': mode, 'dx11_evidence': dx11, 'exe_names': executables}


def resolve(root, game, allow_missing_settings=False):
    game = Path(game).resolve()
    if not game.is_dir():
        raise RuntimeError('Игра не найдена — выбери папку установленной Windows-игры')
    try:
        store = json.loads((root / 'gamehub/game_container_store.json').read_text())
    except FileNotFoundError:
        raise RuntimeError('GameHub: список игр: файл не найден')
    except (OSError, ValueError):
        raise RuntimeError('GameHub: список игр повреждён или недоступен')
    if not isinstance(store, dict) or not isinstance(store.get('bindings'), list):
        raise RuntimeError('Неподдерживаемый формат контейнеров GameHub — обнови Shader Warmer')
    matches = []
    for binding in store['bindings']:
        if not isinstance(binding, dict) or not isinstance(binding.get('game_path'), str):
            continue
        location = Path(binding['game_path']).resolve()
        platform = binding.get('platform')
        app_id = str(binding.get('platform_app_id', ''))
        if platform == 'steam' and app_id.isdigit():
            manifest = location / f'steamapps/appmanifest_{app_id}.acf'
            try:
                text = manifest.read_text()
                install = re.search(r'"installdir"\s+"([^"\r\n]+)"', text, re.I)
                manifest_id = re.search(r'"appid"\s+"(\d+)"', text, re.I)
            except OSError:
                install = manifest_id = None
            if install and manifest_id and manifest_id[1] == app_id:
                relative = Path(install[1])
                if relative.is_absolute() or '..' in relative.parts:
                    continue
                install_root = (location / 'steamapps/common' / relative).resolve()
            elif app_id == '1422450':
                # Preserve the existing Deadlock integration if its Steam manifest is unavailable.
                install_root = location / 'steamapps/common/Deadlock'
            else:
                continue
            key = 'steam-' + app_id
        elif platform == 'local' and binding.get('local_game_id'):
            install_root = location.parent if location.suffix.lower() == '.exe' else location
            key = 'local-' + hashlib.sha256(str(binding['local_game_id']).encode()).hexdigest()[:24]
        else:
            continue
        if game != install_root and not game.is_relative_to(install_root):
            continue
        deadlock = platform == 'steam' and app_id == '1422450'
        selected = install_root / 'game' if deadlock else install_root
        if not selected.is_dir():
            continue
        settings = None
        for path in sorted((root / 'gamehub/game-settings').glob('*.json')):
            try:
                document = json.loads(path.read_text())
                k = document.get('key', {})
                if not isinstance(k, dict):
                    continue
                match = (str(k.get('platform_app_id')) == app_id and k.get('platform', 'steam') == 'steam'
                         if platform == 'steam' else k.get('local_game_id') == binding['local_game_id'])
                if match:
                    settings = path
                    break
            except (ValueError, OSError, AttributeError):
                continue
        if settings is None and not allow_missing_settings:
            raise RuntimeError('Не найдены настройки выбранной игры — один раз запусти её через GameHub')
        matches.append({'game': selected, 'binding': binding, 'settings_path': settings,
                        'key': key, 'name': binding.get('game_name') or install_root.name,
                        'deadlock': deadlock})
    if len(matches) != 1:
        raise RuntimeError('Не найдена однозначная привязка выбранной игры: другая копия или игра ещё не запускалась через GameHub')
    return matches[0]


def launch_parameters(original, mode):
    # Preserve quoting and unrelated options exactly, including Windows paths.
    wrong = r'-(?:dx12|d3d12|vulkan|force-d3d12|force-vulkan|force-glcore|force-opengl)'
    if mode == 'generic':
        if re.search(r'(?i)(?<!\S)' + wrong + r'(?=\s|$)', original):
            raise RuntimeError('В параметрах игры выбран другой API — сначала выбери DirectX 11 в GameHub или игре')
        return original
    remove = wrong + r'|-(?:dx11|d3d11|force-d3d11)'
    cleaned = re.sub(r'(?i)(?<!\S)(?:' + remove + r')(?=\s|$)', '', original).strip()
    flag = '-force-d3d11' if mode == 'unity' else '-dx11'
    return (cleaned + ' ' + flag).strip()
