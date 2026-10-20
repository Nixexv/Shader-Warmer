"""Install the DXMT async backend for selected x64 DX11 GameHub games, with durable rollback.

No game files or shader caches are changed. The DXMT v0.80 component is shared;
the optimization switches live only in the selected game's Wine prefix.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import tempfile
import uuid

# Engine files are sealed resources inside the signed app bundle.
sys.dont_write_bytecode = True
import gamehub_wine
import dxmt_game

PATCH_SHA = '4db75b44e5918b45458760310d9d81324cea7e124829f498ccc4173a54ef0c77'
SWITCHES = {'DXMT_ASYNC_PSO': '1', 'DXMT_PREWARM': '1',
            'DXMT_COMPILER_PRIORITY': 'low', 'DXMT_COMPILER_THREADS': '4'}
ENV_KEYS = list(SWITCHES) + ['DXMT_FRAMETIME_LOG']
SETTINGS_KEYS = ['start_parameters', 'graphics_stack', 'dxmt_experimental_dx12_support']
PROFILE_KEYS = ['graphics_stack_id', 'graphics_stack_config']


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.shader-warmer-tmp')
    try:
        with tmp.open('wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            tmp.chmod(path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path, obj):
    atomic(path, (json.dumps(obj, indent=2, ensure_ascii=False) + '\n').encode())


def env_section(text):
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith('[Environment]')), None)
    if start is None:
        lines += ['\n', '[Environment]\n', '\n']
        start = len(lines) - 2
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('[')), len(lines))
    return lines, start, end


def env_snapshot(text):
    lines, start, end = env_section(text)
    return [line for line in lines[start + 1:end]
            if any(line.startswith('"' + key + '"=') for key in ENV_KEYS)]


def replace_environment(text, replacement):
    lines, start, end = env_section(text)
    keep = [line for line in lines[start + 1:end]
            if not any(line.startswith('"' + key + '"=') for key in ENV_KEYS)]
    return ''.join(lines[:start + 1] + keep + replacement + ['\n'] + lines[end:])


def snapshot_fields(obj, keys):
    return {key: {'exists': key in obj, 'value': copy.deepcopy(obj.get(key))} for key in keys}


def restore_fields(obj, snapshot):
    for key, entry in snapshot.items():
        if entry['exists']:
            obj[key] = copy.deepcopy(entry['value'])
        else:
            obj.pop(key, None)


def object_file(path, description):
    try:
        value = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise RuntimeError(description + ': файл не найден')
    except PermissionError:
        raise RuntimeError(description + ': нет доступа к файлу')
    except (OSError, ValueError):
        raise RuntimeError(description + ': файл повреждён или недоступен')
    if not isinstance(value, dict):
        raise RuntimeError(description + ': неподдерживаемый формат')
    return value


def relative_file(name):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise RuntimeError('Недопустимый путь в пакете или резервной копии')
    return Path(name)


def platform_report():
    rosetta = False
    if platform.system() == 'Darwin' and platform.machine() == 'arm64':
        try:
            rosetta = subprocess.run(['/usr/bin/arch', '-x86_64', '/usr/bin/true'],
                                     capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return {'system': platform.system(), 'machine': platform.machine(),
            'macos': platform.mac_ver()[0], 'rosetta': rosetta}


class Fix:
    def __init__(self, home=None, assets=None, dx11_confirmed=False):
        self.home = Path(home or Path.home())
        self.root = self.home / 'Library/Application Support/com.gamemac.www'
        self.engine = self.root / 'wine-engine'
        self.component = self.engine / 'downloads/dxmt-v0.80'
        self.assets = Path(assets or Path(__file__).resolve().parent / 'dxmt_async')
        self.state = self.home / 'Library/Application Support/ShaderWarmer/dxmt-async'
        self.backup = self.state / 'original-component'
        self.record = self.state / 'installation.json'
        self.log_dir = self.home / 'Library/Logs/ShaderWarmer/dxmt'
        self.platform_probe = platform_report
        self.dx11_confirmed = dx11_confirmed
        self.current = None

    def select_game(self, game):
        selected = dxmt_game.resolve(self.root, game, allow_missing_settings=True)
        self.record = (self.state / 'installation.json' if selected['key'] == 'steam-1422450'
                       else self.state / 'games' / (selected['key'] + '.json'))
        return selected

    def compatible(self):
        info = self.platform_probe()
        if info.get('system') != 'Darwin' or info.get('machine') != 'arm64':
            raise RuntimeError('DXMT Async поддерживает только Mac с Apple Silicon')
        try:
            major = int(info.get('macos', '0').split('.')[0])
        except ValueError:
            major = 0
        if major < 15:
            raise RuntimeError('Для встроенного DXMT Async нужна macOS 15 или новее')
        if not info.get('rosetta'):
            raise RuntimeError('Нужна Rosetta 2: установи её штатным способом macOS и повтори проверку')

    def package(self):
        manifest = object_file(self.assets / 'manifest.json', 'Встроенный пакет DXMT')
        if not isinstance(manifest.get('files'), dict) or not manifest['files']:
            raise RuntimeError('Список файлов встроенного DXMT повреждён')
        for name, expected in manifest['files'].items():
            path = self.assets / relative_file(name)
            if not path.resolve().is_relative_to(self.assets.resolve()) or not path.is_file() or digest(path) != expected:
                raise RuntimeError('Повреждены встроенные файлы DXMT — переустанови Shader Warmer')
        if digest(self.assets / 'wine/x86_64-windows/d3d11.dll') != PATCH_SHA:
            raise RuntimeError('Контрольная сумма DXMT-фикса не совпала')
        return manifest

    def context(self, game, restoring=False):
        selected_game = self.select_game(game)
        binding = selected_game['binding']
        game = selected_game['game']
        prefix_id = str(binding.get('virtual_container_id', ''))
        if not prefix_id.isdigit() or int(prefix_id) == 0:
            raise RuntimeError('Один раз запусти выбранную игру через GameHub, затем закрой её')
        prefix = self.engine / 'containers/virtual_containers' / prefix_id
        registry = prefix / 'user.reg'
        if not registry.is_file():
            raise RuntimeError('Контейнер игры ещё не готов — сначала запусти её через GameHub')
        settings_path = selected_game['settings_path']
        if settings_path is None:
            raise RuntimeError('Настройки игры не найдены или повреждены — проверь GameHub')
        document = object_file(settings_path, 'GameHub: настройки игры')
        if not isinstance(document.get('settings'), dict):
            raise RuntimeError('Неподдерживаемый формат настроек игры в GameHub')
        settings = document['settings']
        if (not isinstance(settings.get('start_parameters', ''), str)
                or not isinstance(settings.get('graphics_stack', {}), dict)):
            raise RuntimeError('Неподдерживаемый формат параметров запуска или графического стека GameHub')
        profiles = document['settings'].get('runtime_graphics_profiles', {})
        if not isinstance(profiles, dict) or not isinstance(profiles.get('rosetta_x64', {}), dict):
            raise RuntimeError('Неподдерживаемый формат графических профилей GameHub')
        wines = gamehub_wine.installed_wines(self.engine)
        selected = str(document['settings'].get('compatibility_layer', ''))
        wine = next((w for w in wines if w['id'] == selected and (restoring or w['major'] == '11')
                     and w['manifest'].get('architecture') == 'x86_64'), None)
        if wine is None:
            raise RuntimeError('Выбери Wine Proton 11 x64 в настройках выбранной игры в GameHub')
        if not os.access(wine['path'] / 'bin/wine', os.X_OK) or not os.access(wine['path'] / 'bin/wineserver', os.X_OK):
            raise RuntimeError('Установка Wine Proton 11 неполна — переустанови этот компонент в GameHub')
        config = None
        for path in (self.root / 'gamehub/game-settings').glob('*.json'):
            try:
                stack = json.loads(path.read_text()).get('settings', {}).get('graphics_stack', {})
                if not isinstance(stack, dict) or not isinstance(stack.get('component_config'), dict):
                    continue
            except (ValueError, OSError, AttributeError):
                continue
            if stack.get('component_config', {}).get('name') == 'dxmt-v0.80':
                config = copy.deepcopy(stack)
                break
        if not restoring and (config is None or not (self.component / 'manifest.json').is_file()):
            raise RuntimeError('Установи и выбери DXMT v0.80 в GameHub один раз')
        try:
            probe = dxmt_game.probe(game, selected_game['deadlock'])
        except RuntimeError:
            if not restoring:
                raise
            saved = self.load_record(self.record) if self.record.exists() else {}
            probe = {'mode': saved.get('mode', 'generic'), 'dx11_evidence': False,
                     'exe_names': saved.get('exe_names', [])}
        selected_game.update(probe)
        ctx = dict(selected_game, prefix=prefix, registry=registry, settings_path=settings_path,
                   document=document, wine=wine['path'], stack=config)
        self.current = ctx
        return ctx

    def processes(self):
        rows = subprocess.check_output(['/bin/ps', '-ww', '-axo', 'pid,comm'], text=True, timeout=10)
        return [line.strip().split(None, 1) for line in rows.splitlines()[1:] if line.strip()]

    def running(self):
        names = set(self.current['exe_names']) if self.current else {'deadlock.exe'}
        return any(len(row) == 2 and row[1].replace(chr(92), '/').split('/')[-1].lower() in names
                   for row in self.processes())

    def guard(self):
        rows = self.processes()
        if any(len(row) == 2 and re.search(r'crossover|codeweavers', row[1], re.I) for row in rows):
            raise RuntimeError('Закрой другой Wine-рантайм перед изменением GameHub')
        if self.running():
            raise RuntimeError('Закрой ' + (self.current['name'] if self.current else 'Deadlock') + ' перед изменением настроек')
        # The component is shared. Refuse to replace libraries used by another running game.
        for row in rows:
            if len(row) != 2 or not (row[1].endswith('/wine') or row[1].lower().endswith('.exe')):
                continue
            files = subprocess.run(['/usr/sbin/lsof', '-p', row[0], '-Fn'], capture_output=True, text=True, timeout=10)
            if files.returncode not in (0, 1) or (files.returncode == 1 and files.stderr.strip()):
                raise RuntimeError('Не удалось проверить запущенные игры — установка остановлена')
            background = row[1].replace(chr(92), '/').split('/')[-1].lower() in {
                'steam.exe', 'steamwebhelper.exe', 'services.exe', 'winedevice.exe', 'explorer.exe',
                'rpcss.exe', 'svchost.exe', 'winedbg.exe', 'wineboot.exe', 'conhost.exe'}
            if any(line.startswith('n' + str(self.component) + '/') or
                   (not background and self.current and line.startswith('n' + str(self.current['prefix']) + '/'))
                   for line in files.stdout.splitlines()):
                raise RuntimeError('Закрой игры GameHub, использующие DXMT v0.80')

    def detect(self, game, include_pending=True):
        result = {'game': str(game), 'dxmt_available': False, 'dxmt_installed': False,
                  'dxmt_backup': False,
                  'game_running': False, 'runtime_error': None}
        try:
            self.select_game(game)
            result['dxmt_backup'] = self.record.is_file() and self.backup.is_dir()
            self.compatible()
            package = self.package()
            ctx = self.context(game)
            result['game'] = str(ctx['game'])
            result['game_running'] = self.running()
            record = self.load_record(self.record) if self.record.exists() else {}
            result['dxmt_requires_confirmation'] = not (ctx['dx11_evidence'] or record.get('dx11_confirmed'))
            result['dxmt_mode'] = ctx['mode']
            result['dxmt_available'] = not result['dxmt_requires_confirmation'] or self.dx11_confirmed
            dxmt_game.launch_parameters(ctx['document']['settings'].get('start_parameters', ''), ctx['mode'])
            if record and record.get('prefix') != str(ctx['prefix']):
                raise RuntimeError('Контейнер игры изменился — сначала восстанови прежнюю установку')
            patched = self.component / 'wine/x86_64-windows/d3d11.dll'
            switches = env_snapshot(ctx['registry'].read_text())
            values = {}
            for line in switches:
                key, value = line.strip().split('=', 1)
                values[json.loads(key)] = json.loads(value)
            settings = ctx['document']['settings']
            libraries_match = all((self.component / name).is_file() and digest(self.component / name) == expected
                                  for name, expected in package['files'].items())
            result['dxmt_installed'] = bool(record.get('active') and patched.is_file()
                and libraries_match and all(values.get(k) == v for k, v in SWITCHES.items())
                and settings.get('graphics_stack', {}).get('component_config', {}).get('name') == 'dxmt-v0.80'
                and settings.get('start_parameters', '') == dxmt_game.launch_parameters(settings.get('start_parameters', ''), ctx['mode']))
            result['dxmt_backup'] = self.record.is_file() and self.backup.is_dir()
            result['dxmt_detail'] = ('Фикс включён · запускай игру через GameHub' if result['dxmt_installed'] else
                                     'Компиляция шейдеров в фоне · GameHub · DX11')
            result['dxmt_log_directory'] = str(ctx['prefix'] / 'drive_c/ShaderWarmer/Logs')
            if record.get('active') and isinstance(record.get('log_path'), str):
                previous_log = Path(record['log_path']).resolve()
                if (previous_log.is_relative_to(ctx['prefix'].resolve())
                        or previous_log.is_relative_to(self.log_dir.resolve())):
                    result['dxmt_log_directory'] = str(previous_log.parent)
            result['dxmt_shared_games'] = len([r for _, r in self.active_records() if r.get('prefix') != str(ctx['prefix'])])
            if include_pending and (self.state / 'transaction/journal.json').exists():
                result['dxmt_installed'] = False
                result['dxmt_detail'] = 'Операция была прервана — нажми «Применить настройки» для восстановления'
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            result['runtime_error'] = str(exc)
            result['dxmt_available'] = False
            journal = self.state / 'transaction/journal.json'
            if include_pending and journal.exists():
                try:
                    self.compatible()
                    self.package()
                    saved = object_file(journal, 'Журнал восстановления')
                    self.select_game(game)
                    if (self.allowed_record(Path(saved['record'])) and
                            Path(saved['registry']).resolve().is_relative_to((self.engine / 'containers/virtual_containers').resolve())):
                        result.update(dxmt_available=True, runtime_error=None, dxmt_installed=False,
                                      dxmt_detail='Операция прервана — нажми «Включить фикс» для восстановления и повторной проверки')
                except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError):
                    pass
        return result

    @contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / 'operation.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Другая операция DXMT уже выполняется')
            yield

    def stop_idle_server(self, ctx):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('WINE', 'WINEDLL', 'DYLD', 'CX_', 'CROSSOVER'))}
        env['WINEPREFIX'] = str(ctx['prefix'])
        server = str(ctx['wine'] / 'bin/wineserver')
        killed = subprocess.run([server, '-k'], env=env, timeout=15, capture_output=True)
        # Wine returns 1 with no diagnostic when this prefix has no server to stop.
        if killed.returncode != 0 and not (killed.returncode == 1 and not killed.stderr):
            raise RuntimeError('Не удалось остановить Wine-контейнер выбранной игры')
        subprocess.run([server, '-w'], env=env, check=True, timeout=15, capture_output=True)

    def verified_backup(self, record):
        for name, expected in record['backup_hashes'].items():
            relative_file(name)
            if not (self.backup / name).is_file() or digest(self.backup / name) != expected:
                raise RuntimeError('Резервная копия DXMT повреждена — восстановление остановлено')

    def load_record(self, path):
        record = object_file(path, 'Состояние установки Shader Warmer')
        if (record.get('schema') != 1 or not isinstance(record.get('prefix'), str)
                or not isinstance(record.get('backup_hashes'), dict) or not record['backup_hashes']
                or not isinstance(record.get('settings'), dict) or not isinstance(record.get('profile'), dict)):
            raise RuntimeError('Состояние установки повреждено — автоматическое изменение остановлено')
        if not Path(record['prefix']).resolve().is_relative_to((self.engine / 'containers/virtual_containers').resolve()):
            raise RuntimeError('Резервная копия с другого Mac или пользователя: переноси только приложение или DMG')
        for name in record['backup_hashes']:
            relative_file(name)
        return record

    def recover_transaction(self):
        transaction = self.state / 'transaction'
        journal = transaction / 'journal.json'
        if not journal.exists():
            return
        saved = json.loads(journal.read_text())
        for key in ('component_files', 'added_files'):
            for name in saved[key]:
                relative_file(name)
        registry = Path(saved['registry']).resolve()
        prefixes = (self.engine / 'containers/virtual_containers').resolve()
        settings = Path(saved['settings']).resolve()
        if (not registry.is_relative_to(prefixes) or registry.name != 'user.reg'
                or not settings.is_relative_to((self.root / 'gamehub/game-settings').resolve())
                or not self.allowed_record(Path(saved['record']))):
            raise RuntimeError('Журнал относится к другой установке — автоматическое восстановление остановлено')
        for name in saved['component_files']:
            atomic(self.component / name, (transaction / 'component' / name).read_bytes())
        for name in saved['added_files']:
            (self.component / name).unlink(missing_ok=True)
        for key in ('registry', 'settings', 'record'):
            atomic(Path(saved[key]), (transaction / key).read_bytes())
        shutil.rmtree(transaction)

    def recover_pending(self):
        transaction = self.state / 'transaction'
        saved = object_file(transaction / 'journal.json', 'Журнал восстановления')
        registry = Path(saved['registry']).resolve()
        if not registry.is_relative_to((self.engine / 'containers/virtual_containers').resolve()):
            raise RuntimeError('Журнал относится к другому пользователю')
        doc = object_file(transaction / 'settings', 'Исходные настройки транзакции')
        selected = str(doc.get('settings', {}).get('compatibility_layer', ''))
        wine = next((w for w in gamehub_wine.installed_wines(self.engine) if w['id'] == selected), None)
        if wine is None:
            raise RuntimeError('Для восстановления прерванной операции нужен исходный Wine в GameHub')
        self.current = {'prefix': registry.parent, 'name': 'игру', 'exe_names': []}
        self.guard()
        self.stop_idle_server({'prefix': registry.parent, 'wine': wine['path']})

    def begin_transaction(self, ctx, before_files, target_files):
        transaction = self.state / 'transaction'
        if transaction.exists():
            shutil.rmtree(transaction)
        transaction.mkdir()
        for name, data in before_files.items():
            atomic(transaction / 'component' / name, data)
        for name, path in [('registry', ctx['registry']), ('settings', ctx['settings_path']), ('record', self.record)]:
            atomic(transaction / name, path.read_bytes())
        write_json(transaction / 'journal.json', {
            'component_files': list(before_files), 'added_files': list(set(target_files) - set(before_files)),
            'registry': str(ctx['registry']), 'settings': str(ctx['settings_path']), 'record': str(self.record)})

    def allowed_record(self, path):
        path = path.resolve()
        return path == (self.state / 'installation.json').resolve() or (
            path.parent == (self.state / 'games').resolve() and bool(re.fullmatch(r'(steam-[0-9]+|local-[a-f0-9]{24})\.json', path.name)))

    def active_records(self):
        paths = [self.state / 'installation.json'] + sorted((self.state / 'games').glob('*.json'))
        return [(path, self.load_record(path)) for path in paths if path.is_file()
                and self.allowed_record(path) and object_file(path, 'Состояние DXMT').get('active')]

    def prepare_record(self, ctx, import_backup=None, import_environment=None):
        if self.record.exists():
            record = self.load_record(self.record)
            if record['prefix'] != str(ctx['prefix']):
                raise RuntimeError('Контейнер изменился — автоматическая установка остановлена')
            self.verified_backup(record)
            return record
        seed_path = self.record.with_suffix('.seed.json')
        legacy_seed = self.state / 'backup-record.json'
        seed = seed_path if seed_path.exists() else legacy_seed
        if self.backup.exists() and seed.exists():
            old = self.load_record(seed)
            if old['prefix'] == str(ctx['prefix']) and old.get('game_key', 'steam-1422450') == ctx['key']:
                self.verified_backup(old)
                write_json(self.record, old)
                return old
        backup_meta = self.state / 'component-backup.json'
        if self.backup.exists():
            if backup_meta.exists():
                hashes = object_file(backup_meta, 'Резервная копия компонента').get('backup_hashes')
            elif legacy_seed.exists() or (self.state / 'installation.json').exists():
                hashes = self.load_record(legacy_seed if legacy_seed.exists() else self.state / 'installation.json')['backup_hashes']
            else:
                raise RuntimeError('Найдена незавершённая резервная копия — установка остановлена')
            if not isinstance(hashes, dict) or not hashes:
                raise RuntimeError('Резервная копия компонента повреждена')
            self.verified_backup({'backup_hashes': hashes})
        else:
            source = Path(import_backup) if import_backup else self.component
            if digest(source / 'wine/x86_64-windows/d3d11.dll') == PATCH_SHA:
                raise RuntimeError('Фикс установлен вне программы: сначала импортируй исходную резервную копию')
            staging = self.state / 'backup-staging'
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(source, staging)
            hashes = {str(p.relative_to(source)): digest(p) for p in source.rglob('*') if p.is_file()}
            if not hashes or any(digest(staging / name) != expected for name, expected in hashes.items()):
                raise RuntimeError('Не удалось проверить резервную копию DXMT')
        old_env = env_snapshot(ctx['registry'].read_text())
        if import_environment:
            imported = json.loads(Path(import_environment).read_text())
            if Path(imported['registry']).resolve() != ctx['registry'].resolve():
                raise RuntimeError('Резервная копия окружения относится к другому контейнеру')
            old_env = imported['original_lines']
        settings = ctx['document']['settings']
        profile = settings.get('runtime_graphics_profiles', {}).get('rosetta_x64', {})
        record = {'schema': 1, 'active': False, 'prefix': str(ctx['prefix']), 'game': str(ctx['game']),
                  'game_key': ctx['key'], 'game_name': ctx['name'], 'mode': ctx['mode'], 'exe_names': ctx['exe_names'],
                  'backup_hashes': hashes, 'original_environment': old_env,
                  'settings': snapshot_fields(settings, SETTINGS_KEYS),
                  'profile': snapshot_fields(profile, PROFILE_KEYS),
                  'profile_existed': 'rosetta_x64' in settings.get('runtime_graphics_profiles', {}),
                  'profiles_existed': 'runtime_graphics_profiles' in settings}
        write_json(seed_path, record)
        if not self.backup.exists():
            write_json(legacy_seed, record)
            write_json(backup_meta, {'backup_hashes': hashes})
            os.replace(staging, self.backup)
        elif not backup_meta.exists():
            write_json(backup_meta, {'backup_hashes': hashes})
        write_json(self.record, record)
        self.verified_backup(record)
        return record

    def component_bytes(self):
        return {str(p.relative_to(self.component)): p.read_bytes()
                for p in self.component.rglob('*') if p.is_file()}

    def writable_space(self, ctx):
        size = sum(p.stat().st_size for p in self.component.rglob('*') if p.is_file())
        required = 3 * size + sum((self.assets / name).stat().st_size for name in self.package()['files']) + 32 * 1024 * 1024
        for directory in [self.state, self.component, ctx['registry'].parent, ctx['settings_path'].parent,
                          ctx['prefix'] / 'drive_c']:
            parent = directory
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if shutil.disk_usage(parent).free < required:
                raise RuntimeError('Недостаточно места для установки и надёжной резервной копии DXMT')
            try:
                with tempfile.TemporaryFile(dir=parent) as probe:
                    probe.write(b'write-test'); probe.flush(); os.fsync(probe.fileno())
            except OSError:
                raise RuntimeError('Нет прав на запись или диск доступен только для чтения: ' + str(parent))

    def settings_payload(self, ctx, record, restoring):
        doc = json.loads(ctx['settings_path'].read_text())
        settings = doc['settings']
        if restoring:
            restore_fields(settings, record['settings'])
            profile = settings.setdefault('runtime_graphics_profiles', {}).setdefault('rosetta_x64', {})
            restore_fields(profile, record['profile'])
            if not record['profile_existed'] and not profile:
                settings['runtime_graphics_profiles'].pop('rosetta_x64', None)
            if not record['profiles_existed'] and not settings['runtime_graphics_profiles']:
                settings.pop('runtime_graphics_profiles', None)
        else:
            settings['graphics_stack'] = copy.deepcopy(ctx['stack'])
            settings['graphics_stack']['kind'] = 'dxmt'
            settings['dxmt_experimental_dx12_support'] = False
            settings['start_parameters'] = dxmt_game.launch_parameters(settings.get('start_parameters', ''), ctx['mode'])
            profile = settings.setdefault('runtime_graphics_profiles', {}).setdefault('rosetta_x64', {})
            profile.update(graphics_stack_id=ctx['stack']['component_id'],
                           graphics_stack_config=copy.deepcopy(ctx['stack']['component_config']))
        now = int(time.time())
        settings['updated_at'] = doc['updated_at'] = now
        for key in ('settings_revision', 'runtime_config_revision'):
            if isinstance(doc.get(key), int):
                doc[key] += 1
        return (json.dumps(doc, indent=2, ensure_ascii=False) + '\n').encode()

    def apply(self, game, restoring=False, import_backup=None, import_environment=None, emit=lambda *a, **kw: None):
        with self.locked():
            if not restoring:
                self.compatible()
            self.current = None
            self.guard()
            if (self.state / 'transaction/journal.json').exists():
                self.recover_pending()
            self.recover_transaction()
            self.package()
            ctx = self.context(game, restoring=restoring)
            self.guard()
            others = [(p, r) for p, r in self.active_records() if p != self.record]
            if any(r['prefix'] == str(ctx['prefix']) for _, r in others):
                raise RuntimeError('Этот Wine-контейнер уже используется другой игрой с Async — нужен отдельный контейнер GameHub')
            previous = self.load_record(self.record) if self.record.exists() else {}
            if not restoring and not (ctx['dx11_evidence'] or self.dx11_confirmed or previous.get('dx11_confirmed')):
                raise RuntimeError('Подтверди на странице игры, что она поддерживает DirectX 11')
            if not restoring:
                dxmt_game.launch_parameters(ctx['document']['settings'].get('start_parameters', ''), ctx['mode'])
            self.writable_space(ctx)
            self.stop_idle_server(ctx)
            emit('check', name='GameHub · Wine Proton 11 · DXMT v0.80', ok=True, detail='Игра закрыта')
            if restoring and not self.record.exists():
                raise RuntimeError('Резервной копии для отката пока нет')
            record = self.prepare_record(ctx, import_backup, import_environment)
            before_files = self.component_bytes()
            before_registry = ctx['registry'].read_bytes()
            before_settings = ctx['settings_path'].read_bytes()
            before_record = self.record.read_bytes()
            # ASCII C: path also works with Cyrillic usernames, external drives and no Z: mapping.
            frame_dir = ctx['prefix'] / 'drive_c/ShaderWarmer/Logs'
            frame_dir.mkdir(parents=True, exist_ok=True)
            filename = ctx['key'] + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.txt'
            logfile = frame_dir / filename
            values = dict(SWITCHES, DXMT_FRAMETIME_LOG='C:\\ShaderWarmer\\Logs\\' + filename)
            replacement = (record['original_environment'] if restoring else
                           ['"' + k + '"=' + json.dumps(v, ensure_ascii=False) + '\n' for k, v in values.items()])
            restore_component = restoring and not others
            source = self.backup if restore_component else self.assets
            files = record['backup_hashes'] if restore_component else self.package()['files']
            self.begin_transaction(ctx, before_files, files)
            emit('stage', name='install')
            try:
                for name in files:
                    atomic(self.component / name, (source / name).read_bytes())
                if restore_component:
                    for name in set(self.package()['files']) - set(files):
                        (self.component / name).unlink(missing_ok=True)
                atomic(ctx['registry'], replace_environment(before_registry.decode(), replacement).encode())
                atomic(ctx['settings_path'], self.settings_payload(ctx, record, restoring))
                record.update(active=not restoring, dx11_confirmed=True, updated_at=time.time(),
                              log_path=None if restoring else str(logfile),
                              package_version=self.package()['version'])
                write_json(self.record, record)
                if not restoring and not self.detect(game, include_pending=False)['dxmt_installed']:
                    raise RuntimeError('Проверка установленного фикса не прошла')
            except BaseException:
                for name, data in before_files.items():
                    atomic(self.component / name, data)
                for name in set(files) - set(before_files):
                    (self.component / name).unlink(missing_ok=True)
                atomic(ctx['registry'], before_registry)
                atomic(ctx['settings_path'], before_settings)
                atomic(self.record, before_record)
                shutil.rmtree(self.state / 'transaction')
                raise
            shutil.rmtree(self.state / 'transaction')
            emit('check', name='Исходный компонент сохранён', ok=True, detail='Откат доступен в приложении')
            emit('done', method='dxmt', enabled=not restoring, backup=True, shared_retained=bool(restoring and others), log_directory=str(frame_dir))
            return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', required=True)
    parser.add_argument('--detect', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--dx11', action='store_true', help='User confirms this game supports DirectX 11')
    parser.add_argument('--import-backup', type=Path)
    parser.add_argument('--import-environment', type=Path)
    args = parser.parse_args()
    fix = Fix(dx11_confirmed=args.dx11)
    if args.detect:
        print(json.dumps(fix.detect(args.game), ensure_ascii=False))
        return 0
    def emit(event, **fields):
        if args.json:
            print('@@ ' + json.dumps(dict(event=event, **fields), ensure_ascii=False), flush=True)
    try:
        os.setpgrp()
        emit('stage', name='preflight')
        fix.apply(args.game, args.restore, args.import_backup, args.import_environment, emit)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        emit('error', reason=str(exc))
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
