"""DXMT for CrossOver bottles and explicitly selected Wine environments.

A private runtime keeps vendor files intact. CrossOver bottle settings persist
the Async backend for ordinary launcher starts, with field-level rollback.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time

import dxmt_fix as core
import dxmt_game

def identity(prefix):
    return hashlib.sha256(str(Path(prefix).resolve()).encode()).hexdigest()[:24]


class WineFix:
    def __init__(self, prefix, wine, launcher='wine', home=None, assets=None, dx11=False):
        self.home = Path(home or Path.home())
        self.prefix = Path(prefix).expanduser().resolve()
        self.wine = Path(wine).expanduser().absolute()  # CrossOver's wrapper path must retain its app root.
        self.launcher = launcher
        self.core = core.Fix(self.home, assets, dx11_confirmed=dx11)
        self.state = self.home / 'Library/Application Support/ShaderWarmer/wine-environments' / identity(self.prefix)
        self.record = self.state / 'installation.json'
        self.component = self.state / 'dxmt'
        self.registry = self.prefix / 'user.reg'
        self.conf = self.prefix / 'cxbottle.conf'
        self.dx11 = dx11

    def validate(self):
        if self.launcher not in ('crossover', 'wine'): raise RuntimeError('Неизвестный тип Wine-среды')
        if not self.registry.is_file() or not (self.prefix / 'drive_c').is_dir():
            raise RuntimeError('Выбери готовый Wine-контейнер: внутри должны быть user.reg и drive_c')
        gamehub = (self.home / 'Library/Application Support/com.gamemac.www/wine-engine/containers').resolve()
        if self.prefix.is_relative_to(gamehub): raise RuntimeError('Для контейнеров GameHub выбери интеграцию GameHub')
        if not self.wine.is_file() or not os.access(self.wine, os.X_OK): raise RuntimeError('Исполняемый файл Wine не найден')
        if self.launcher == 'crossover' and not self.conf.is_file(): raise RuntimeError('В выбранном контейнере нет cxbottle.conf')
        if self.prefix == Path('/') or self.prefix == self.home.resolve(): raise RuntimeError('Недопустимый контейнер')
        if self.registry.is_symlink() or (self.conf.exists() and self.conf.is_symlink()):
            raise RuntimeError('Файлы настроек контейнера должны быть обычными файлами')

    def saved(self):
        if not self.record.exists(): return {}
        saved = core.object_file(self.record, 'Состояние Wine-среды')
        if saved.get('schema') != 2 or saved.get('prefix') != str(self.prefix) or (saved.get('launcher') != self.launcher and saved.get('active')):
            raise RuntimeError('Резервная копия относится к другому контейнеру или лаунчеру')
        if not isinstance(saved.get('original'), dict): raise RuntimeError('Повреждена резервная копия Wine-среды')
        native = saved.get('native')
        if native is not None:
            if (not isinstance(native,dict) or not isinstance(native.get('before'),str)
                or not isinstance(native.get('installed'),str) or not isinstance(native.get('fields'),dict)
                or set(native['fields']) != set(self.native_fields())
                or any(not isinstance(lines,list) or any(not isinstance(line,str) for line in lines) for lines in native['fields'].values())):
                raise RuntimeError('Повреждена резервная копия настроек CrossOver')
        return saved

    def stock(self):
        root = self.wine.parent.parent
        return next((p for p in [root/'lib/wine',root/'lib64/wine'] if p.is_dir()),root/'lib/wine')

    def dllpath(self, component=None):
        stock = self.stock()
        if not stock.is_dir(): raise RuntimeError('Не найдены библиотеки выбранного Wine: выбери bin/wine из полного рантайма')
        overlay = self.state/'runtime/lib/wine'
        return ':'.join(str(p) for p in [overlay,overlay/'x86_64-windows',overlay/'i386-windows',stock])

    def fingerprint(self):
        stock=self.stock(); loader=stock/'x86_64-unix/wine'
        entries=[]
        for path in sorted(stock.rglob('*')):
            if path.is_file():
                stat=path.stat();entries.append((str(path.relative_to(stock)),stat.st_size,stat.st_mtime_ns,stat.st_ino))
        return dict(ntdll=core.digest(stock/'x86_64-unix/ntdll.so'),wine=str(self.wine),
                    loader=core.digest(loader) if loader.is_file() else None,
                    source_version=hashlib.sha256(json.dumps(entries).encode()).hexdigest())

    def stage_runtime(self):
        stock = self.stock(); root = self.wine.parent.parent; runtime = self.state/'runtime'
        ntdll = stock/'x86_64-unix/ntdll.so'
        if not ntdll.is_file(): raise RuntimeError('Нужен Wine для macOS с x86_64-unix/ntdll.so')
        marker = runtime/'source.json'
        fingerprint = self.fingerprint()
        if marker.exists():
            if core.object_file(marker,'Wine-среда') != fingerprint:
                raise RuntimeError('Wine обновлён: отключи фикс, затем подключи среду заново')
            return
        temp = Path(tempfile.mkdtemp(prefix='runtime-',dir=self.state))
        try:
            for architecture in ['x86_64-unix','x86_64-windows','i386-windows','i386-unix']:
                source = stock/architecture
                if not source.is_dir(): continue
                dest = temp/'lib/wine'/architecture;dest.mkdir(parents=True)
                for file in source.iterdir():
                    target = dest/file.name
                    if file.is_dir(): shutil.copytree(file,target,symlinks=True)
                    else: shutil.copy2(file,target)
            for name in self.core.package()['files']:
                relative = Path(name)
                if relative.parts[0] != 'wine': continue
                target = temp/'lib'/relative
                target.parent.mkdir(parents=True,exist_ok=True);target.unlink(missing_ok=True)
                shutil.copy2(self.component/name,target)
            (temp/'bin').mkdir()
            for file in (root/'bin').iterdir():
                target = temp/'bin'/file.name
                if file.name in ['wine','wine64','wineloader','wine-preloader','wine64-preloader'] and file.read_bytes()[:4] in [bytes.fromhex('cffaedfe'),bytes.fromhex('cafebabe')]:
                    shutil.copy2(file,target)
                else: target.symlink_to(file.resolve(),target_is_directory=file.is_dir())
            for file in root.iterdir():
                if file.name == 'lib64' and stock.parent.name == 'lib64':
                    (temp/'lib64').mkdir();(temp/'lib64/wine').symlink_to('../lib/wine')
                    for child in file.iterdir():
                        if child.name != 'wine': (temp/'lib64'/child.name).symlink_to(child.resolve(),target_is_directory=child.is_dir())
                elif file.name not in ['bin','lib']:
                    (temp/file.name).symlink_to(file.resolve(),target_is_directory=file.is_dir())
            for file in ((root/'lib').iterdir() if (root/'lib').is_dir() else []):
                if file.name != 'wine': (temp/'lib'/file.name).symlink_to(file.resolve(),target_is_directory=file.is_dir())
            core.write_json(temp/'source.json',fingerprint)
            os.replace(temp,runtime)
        finally:
            if temp.exists(): shutil.rmtree(temp)

    def native_fields(self):
        return dict(CX_ROOT=str(self.state/'runtime'), CX_GRAPHICS_BACKEND='dxmt',
                    **core.SWITCHES, DXMT_FRAMETIME_LOG=r'C:\ShaderWarmer\Logs\dxmt.txt')

    @staticmethod
    def config_values(text, keys):
        section = None; values = {key: [] for key in keys}
        for line in text.splitlines(keepends=True):
            header = re.match(r'^\s*\[([^]]+)\]', line)
            if header: section = header[1]
            if section == 'EnvironmentVariables':
                match = re.match(r'^\s*"([^"\r\n]+)"\s*=', line)
                if match and match[1] in values: values[match[1]].append(line)
        return values

    @staticmethod
    def config_replace(text, values):
        lines = text.splitlines(keepends=True); kept = []; section = None
        for line in lines:
            header = re.match(r'^\s*\[([^]]+)\]', line)
            if header: section = header[1]
            match = re.match(r'^\s*"([^"\r\n]+)"\s*=', line)
            if section == 'EnvironmentVariables' and match and match[1] in values: continue
            kept.append(line)
        text = ''.join(kept)
        # CrossOver merges repeated sections. Put managed values last, preserving all others.
        extra = ''.join(line for entries in values.values() for line in entries)
        return text + ('\n[EnvironmentVariables]\n' + extra if extra else '')

    def configured(self, text=None):
        text = self.conf.read_text() if text is None else text
        return self.config_replace(text, {k: ['"'+k+'" = '+json.dumps(v, ensure_ascii=False)+'\n']
                                          for k,v in self.native_fields().items()})

    def native_matches(self, saved):
        if not saved.get('native') or not self.conf.is_file(): return False
        actual = self.config_values(self.conf.read_text(), self.native_fields())
        for key, value in self.native_fields().items():
            if len(actual[key]) != 1: return False
            try: found = json.loads(actual[key][0].split('=',1)[1].strip())
            except ValueError: return False
            if found != value: return False
        return self.backend_matches()

    def backend_matches(self):
        backend = self.state/'runtime/lib/dxmt'
        return not backend.is_symlink() and all((backend/Path(n).relative_to('wine')).is_file()
            and core.digest(backend/Path(n).relative_to('wine')) == h for n,h in self.core.package()['files'].items())

    def stage_crossover_backend(self):
        if self.launcher != 'crossover' or self.backend_matches(): return
        backend = self.state/'runtime/lib/dxmt'
        if backend.is_symlink(): backend.unlink()  # Never follow the vendor backend link.
        elif backend.exists(): shutil.rmtree(backend)
        shutil.copytree(self.component/'wine',backend)

    def restore_native(self, saved):
        native = saved.get('native')
        if not native: return
        if self.conf.is_symlink(): raise RuntimeError('Настройки бутылки стали символической ссылкой')
        text = self.conf.read_text()
        if text == native['installed']: restored = native['before']
        else: restored = self.config_replace(text, native['fields'])
        core.atomic(self.conf, restored.encode())

    def loader(self):
        folder = self.state/'runtime/bin'
        return next((p for p in [self.state/'runtime/lib/wine/x86_64-unix/wine',folder/'wineloader',folder/'wine64',folder/'wine'] if p.is_file()),folder/'wine')

    def environment(self, prefix=None, component=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('WINE', 'WINEDLL', 'DYLD', 'CX_', 'CROSSOVER', 'DXMT_'))}
        prefix = Path(prefix or self.prefix)
        env.update(core.SWITCHES,DXMT_FRAMETIME_LOG=r'C:\ShaderWarmer\Logs\dxmt.txt', WINEPREFIX=str(prefix), WINEDLLPATH=self.dllpath(component),
                   WINEDLLOVERRIDES='d3d11,dxgi,winemetal,d3d10core=b', WINEDEBUG='-all')
        return env

    def command(self, executable, args=(), prefix=None):
        return [str(self.loader()), str(executable), *args]

    def guard(self, allow_own=False):
        # A bottle is shared by Steam and other games. Never kill its processes to apply a fix.
        for row in self.core.processes():
            if len(row) == 2 and re.search(r'\.exe$|(?:^|/)(?:wine(?:64)?|wineloader|wine(?:64)?-preloader)$', row[1], re.I):
                if allow_own:
                    opened=subprocess.run(['/usr/sbin/lsof','-p',row[0],'-Fn'],capture_output=True,text=True,timeout=10)
                    if any(line.startswith('n'+str(self.state/'runtime')+'/') for line in opened.stdout.splitlines()): continue
                raise RuntimeError('Закрой Windows-приложения из других Wine-сред и повтори запуск')

    def stage_component(self):
        package = self.core.package()
        if self.component.exists():
            if not all((self.component / n).is_file() and core.digest(self.component / n) == h for n,h in package['files'].items()):
                raise RuntimeError('Копия DXMT повреждена — отключи фикс и удали её через переустановку среды')
            return
        temp = Path(tempfile.mkdtemp(prefix='component-', dir=self.state))
        try:
            for name, expected in package['files'].items():
                dest = temp / core.relative_file(name); dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.core.assets / name, dest)
                if core.digest(dest) != expected: raise RuntimeError('Ошибка проверки копии DXMT')
            os.replace(temp, self.component)
        finally:
            if temp.exists(): shutil.rmtree(temp)

    def probe(self):
        probe = self.core.assets / 'dxmt_probe.exe'
        expected = self.core.package().get('probe_sha256')
        if not expected or core.digest(probe) != expected: raise RuntimeError('Повреждён тест совместимости DXMT')
        with tempfile.TemporaryDirectory(prefix='ShaderWarmer Wine ') as tmp:
            prefix = Path(tmp) / 'Probe'; (prefix / 'drive_c/windows/system32').mkdir(parents=True)
            (prefix / 'dosdevices').mkdir(); (prefix / 'dosdevices/c:').symlink_to('../drive_c'); (prefix / 'dosdevices/z:').symlink_to('/')
            for name in ('system.reg','user.reg','userdef.reg'):
                source = self.prefix / name
                if source.is_file(): shutil.copy2(source, prefix / name)
            env = self.environment(prefix)
            command = self.command(probe, prefix=prefix)
            if self.launcher == 'crossover':
                (prefix/'cxbottle.conf').write_text(self.configured())
                env = {k:v for k,v in os.environ.items() if not k.startswith(('WINE','CX_','CROSSOVER','DXMT_','DYLD'))}
                env.update(CX_BOTTLE_PATH=tmp, CX_BOTTLE='Probe', WINEPREFIX=str(prefix), WINEDEBUG='-all')
                command = [str(self.wine), '--no-gui', str(probe)]
            try:
                p = subprocess.run(command, env=env, capture_output=True, text=True, timeout=40)
                if p.returncode or 'SHADER_WARMER_DXMT_OK' not in p.stdout:
                    detail = (p.stderr + p.stdout)[-1800:]
                    raise RuntimeError('Этот Wine не прошёл проверку DXMT. Контейнер не изменён. ' + detail)
            finally:
                server = self.wine.parent / 'wineserver'
                subprocess.run([str(server), '-k'], env=env, capture_output=True, timeout=15)
                subprocess.run([str(server), '-w'], env=env, capture_output=True, timeout=15)

    def runtime_matches(self):
        runtime = self.state/'runtime'
        if not (runtime/'source.json').is_file(): return False
        stock = self.stock()
        source = core.object_file(runtime/'source.json','Wine-среда')
        if source != self.fingerprint(): return False
        if source.get('loader') and core.digest(runtime/'lib/wine/x86_64-unix/wine') != source['loader']: return False
        if core.digest(runtime/'lib/wine/x86_64-unix/ntdll.so') != source['ntdll']: return False
        return all((runtime/'lib'/name).is_file() and core.digest(runtime/'lib'/name)==expected
                   for name,expected in self.core.package()['files'].items())

    def detect(self, game):
        result = dict(game=str(game), dxmt_available=False, dxmt_installed=False, dxmt_backup=False,
                      game_running=False, runtime_error=None, dxmt_scope='container')
        try:
            saved = self.saved(); result['dxmt_backup'] = bool(saved); self.validate()
            self.core.compatible(); self.core.package(); self.dllpath()
            evidence = dxmt_game.probe(game, (Path(game) / 'citadel').is_dir())
            result['dxmt_requires_confirmation'] = not (evidence['dx11_evidence'] or saved.get('dx11_confirmed'))
            result['dxmt_available'] = self.dx11 or not result['dxmt_requires_confirmation']
            result['dxmt_installed'] = bool(saved.get('active') and saved.get('wine')==str(self.wine) and self.runtime_matches()
                and (self.launcher != 'crossover' or self.native_matches(saved)))
            if (saved.get('active') and not result['dxmt_installed']) or saved.get('pending'):
                result['runtime_error'] = 'Настройки CrossOver или Wine изменились. Отключи Async и включи его заново.'
                result['dxmt_available'] = False
            result['dxmt_detail'] = ('Async включён · запускай из CrossOver' if self.launcher == 'crossover' else 'Async готов · запускай через Shader Warmer') if result['dxmt_installed'] else 'Проверим Wine и подготовим отдельную среду DXMT Async'
            result['dxmt_log_directory'] = str(self.prefix/'drive_c/ShaderWarmer/Logs')
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
            result['runtime_error'] = str(exc)
        return result

    def apply(self, game, restoring=False, emit=lambda *a, **kw: None):
        self.state.mkdir(parents=True,exist_ok=True);self.core.state=self.state
        with self.core.locked():
            saved=self.saved()
            if not restoring:self.validate()
            if saved.get('pending') and not restoring: raise RuntimeError('Установка прервалась: сначала восстанови настройки бутылки')
            if restoring:
                if not saved: raise RuntimeError('Подготовленная среда не найдена')
                if saved.get('native'):
                    self.guard()
                    self.restore_native(saved)
                saved.update(active=False,pending=False,updated_at=time.time());core.write_json(self.record,saved)
            else:
                self.core.compatible()
                evidence=dxmt_game.probe(game,(Path(game)/'citadel').is_dir())
                if not (self.dx11 or evidence['dx11_evidence'] or saved.get('dx11_confirmed')):
                    raise RuntimeError('Подтверди, что игра поддерживает DirectX 11')
                self.guard()
                if saved.get('active') and (saved.get('wine')!=str(self.wine) or not self.runtime_matches()):
                    raise RuntimeError('Среда изменилась: сначала отключи Async, затем включи его заново')
                if not saved.get('active'):
                    # These are private generated files, never the user's runtime or prefix.
                    for name in ['runtime','dxmt']:
                        path=self.state/name
                        if path.is_symlink(): raise RuntimeError('Недопустимая ссылка в подготовленной среде')
                        if path.exists(): shutil.rmtree(path)
                required = sum(p.stat().st_size for p in self.stock().rglob('*') if p.is_file()) + 128*1024*1024
                if shutil.disk_usage(self.state).free < required:
                    raise RuntimeError('Недостаточно места для отдельной Wine-среды: нужно около ' + str((required+1024*1024-1)//(1024*1024)) + ' МБ')
                self.stage_component();self.stage_runtime();self.stage_crossover_backend()
                self.probe()
                emit('check',name='Async DLL и D3D11 проверены',ok=True,detail='На отдельном временном контейнере')
                if not self.runtime_matches():raise RuntimeError('Проверка файлов подготовленной среды не прошла')
                emit('stage',name='install')
                previous = saved
                saved=dict(schema=2,active=True,prefix=str(self.prefix),wine=str(self.wine),launcher=self.launcher,
                           game=str(game),dx11_confirmed=True,updated_at=time.time(),original={})
                if self.launcher == 'crossover':
                    (self.prefix/'drive_c/ShaderWarmer/Logs').mkdir(parents=True,exist_ok=True)
                    before = self.conf.read_text()
                    installed = self.configured(before)
                    native = previous.get('native') if previous.get('active') else None
                    if native:
                        if not self.native_matches(previous): raise RuntimeError('Настройки бутылки изменились: сначала отключи Async')
                        native = dict(native,installed=installed)
                    else:
                        native = dict(before=before, installed=installed, fields=self.config_values(before,self.native_fields()))
                    saved.update(native=native, active=False, pending=True)
                    core.write_json(self.record,saved)  # Durable rollback precedes bottle mutation.
                    try:
                        core.atomic(self.conf,installed.encode())
                        saved.update(active=True,pending=False)
                        core.write_json(self.record,saved)
                    except BaseException:
                        core.atomic(self.conf,before.encode())
                        core.write_json(self.record,previous or dict(saved,active=False,pending=False))
                        raise
                else: core.write_json(self.record,saved)
            emit('done',method='dxmt',enabled=not restoring,backup=True,scope='container',
                 detail='Async отключён для этой среды' if restoring else ('Async включён · запускай из CrossOver' if self.launcher == 'crossover' else 'Async готов · запускай через Shader Warmer'),
                 log_directory=str(self.prefix/'drive_c/ShaderWarmer/Logs'))

    def launch(self, game, executable, arguments=''):
        self.core.state=self.state
        with self.core.locked():
            self._launch(game,executable,arguments)

    def _launch(self, game, executable, arguments=''):
        self.validate()
        exe = Path(executable).resolve(); game = Path(game).resolve()
        if not exe.is_relative_to(game) or not exe.is_file() or exe.suffix.lower() != '.exe':
            raise RuntimeError('Выбери .exe внутри папки этой игры')
        info=dxmt_game.pe_info(exe)
        if not info or info[0]!=0x8664: raise RuntimeError('Для запуска с Async выбери Windows x64 .exe')
        if not self.detect(game)['dxmt_installed']: raise RuntimeError('Сначала включи DXMT Async для выбранного контейнера')
        self.guard(allow_own=True)
        evidence = dxmt_game.probe(game, (game/'citadel').is_dir())
        args = shlex.split(dxmt_game.launch_parameters(arguments,evidence['mode']))
        (self.prefix/'drive_c/ShaderWarmer/Logs').mkdir(parents=True,exist_ok=True)
        (self.state/'logs').mkdir(exist_ok=True)
        log = self.state/'logs/launch.log'
        with log.open('ab') as out:
            subprocess.Popen(self.command(exe,args),env=self.environment(),cwd=exe.parent,
                             stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--game',required=True); parser.add_argument('--prefix',required=True); parser.add_argument('--wine',required=True)
    parser.add_argument('--launcher',choices=['crossover','wine'],required=True)
    parser.add_argument('--detect',action='store_true'); parser.add_argument('--restore',action='store_true')
    parser.add_argument('--dx11',action='store_true'); parser.add_argument('--json',action='store_true')
    parser.add_argument('--launch',action='store_true'); parser.add_argument('--exe'); parser.add_argument('--launch-args',default='')
    args=parser.parse_args(); fix=WineFix(args.prefix,args.wine,args.launcher,dx11=args.dx11)
    def emit(event,**fields):
        if args.json: print('@@ '+json.dumps(dict(event=event,**fields),ensure_ascii=False),flush=True)
    if args.detect:
        print(json.dumps(fix.detect(args.game),ensure_ascii=False)); return 0
    try:
        if args.launch: fix.launch(args.game,args.exe or '',args.launch_args)
        else:
            os.setpgrp(); emit('stage',name='preflight'); fix.apply(args.game,args.restore,emit)
        return 0
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as exc:
        emit('error',reason=str(exc)); print(str(exc),file=__import__('sys').stderr); return 1

if __name__=='__main__': raise SystemExit(main())
