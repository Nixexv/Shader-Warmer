#!/usr/bin/env python3
"""Read-only evidence for game stutters. No cache rewrites, injected DLLs or settings changes."""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import signal
import shutil
import struct
import subprocess
import time
from pathlib import Path

PREDICATE = 'subsystem == "D3DMetal" OR eventMessage CONTAINS[c] "shader cache"'
ARENAS = ('stage_cache.bin', 'pipeline_cache.bin', 'bytecode_cache.bin', 'rootsignature_cache.bin')


def command(args, timeout=5):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {'status': r.returncode, 'text': r.stdout.strip(), 'error': r.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'status': -1, 'text': '', 'error': str(e)}


def arena_info(path):
    """Scan framing and keys only, never load multi-GB metallib payloads into memory.

    The keys fingerprint lets two exports show whether the game kept the warm, grew it or
    discarded it. Framing errors are reported; a changing file during play is not called corrupt.
    """
    path = Path(path)
    try:
        before = path.stat()
        out = {'bytes': before.st_size, 'mtime_ns': before.st_mtime_ns}
        with path.open('rb') as f:
            header = f.read(24)
            if len(header) != 24:
                raise ValueError('arena header truncated')
            capacity, end, committed = struct.unpack('<3Q', header)
            out.update(capacity=capacity, used=end, committed=committed)
            if not (24 <= end <= before.st_size) or capacity != before.st_size:
                raise ValueError('arena size/header mismatch')
            off, n = 24, 0
            keys, stages = hashlib.sha256(), collections.Counter()
            while off < end:
                f.seek(off)
                head = f.read(64)
                if len(head) < 32:
                    raise ValueError('record header truncated at %d' % off)
                framed, payload, _checksum = struct.unpack_from('<3Q', head)
                if not payload + 32 <= framed <= payload + 39 or off + framed > end:
                    raise ValueError('record framing mismatch at %d' % off)
                f.seek(off + framed - 8)
                if struct.unpack('<q', f.read(8))[0] != -framed:
                    raise ValueError('record footer mismatch at %d' % off)
                if path.name == 'stage_cache.bin':
                    st, _serial, _flags, size = struct.unpack_from('<4I', head, 24)
                    if size > payload - 40:
                        raise ValueError('stage key exceeds record at %d' % off)
                    f.seek(off + 24 + 40)
                    key = f.read(size)
                    keys.update(struct.pack('<I', st) + key)
                    stages[st] += 1
                n += 1
                off += framed
            out['records'] = n
            if path.name == 'stage_cache.bin':
                out['per_stage'] = dict(stages)
                out['ordered_keys_sha256'] = keys.hexdigest()
        after = path.stat()
        out['changed_during_read'] = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        return out
    except (OSError, ValueError, struct.error) as e:
        return {'error': str(e)}


def cache_inventory(root):
    out = {}
    for exe in ('deadlock.exe', 'Overwatch.exe'):
        base = Path(root) / 'd3dm' / exe / 'shaders.cache'
        try:
            families = sorted(base.glob('MTLGPUFamily*'))
        except OSError as e:
            out[exe] = {'error': str(e)}
            continue
        out[exe] = {}
        for fam in families:
            files = {name: arena_info(fam / name) for name in ARENAS}
            try:
                stamp = (fam / 'version.bin').read_bytes()
                files['version'] = {'hex': stamp.hex(), 'sha256': hashlib.sha256(stamp).hexdigest()}
            except OSError as e:
                files['version'] = {'error': str(e)}
            out[exe][fam.name] = files
    return out


def metal_inventory(root):
    # Index headers and file sizes suffice to see shard growth/eviction. Cache code is not exported.
    out = {}
    for bundle in ('org.winehq.wine', 'org.winehq.warm'):
        rows = []
        base = Path(root) / bundle / 'com.apple.metal'
        try:
            lists = sorted(base.glob('*/functions*.list'))
        except OSError:
            lists = []
        for lst in lists:
            try:
                with lst.open('rb') as f:
                    head = f.read(44)
                row = {'file': str(lst.relative_to(base)), 'index_bytes': lst.stat().st_size,
                       'data_bytes': lst.with_suffix('.data').stat().st_size,
                       'mtime_ns': lst.with_suffix('.data').stat().st_mtime_ns}
                if len(head) == 44:
                    row['base_entries'] = struct.unpack_from('<I', head, 36)[0]
                    row['index_rows'] = (row['index_bytes'] - 44) // 56
                    row['cache_id'] = head[4:36].hex()
                rows.append(row)
            except OSError as e:
                rows.append({'file': str(lst), 'error': str(e)})
        out[bundle] = rows
    return out


def runtime_choices(home):
    root = Path(home) / 'Library/Application Support/com.gamemac.www'
    out = []
    try:
        files = sorted((root / 'gamehub/game-settings').glob('*.json'))
    except OSError:
        return out
    for f in files:
        try:
            obj = json.loads(f.read_text())
            appid = str(obj.get('key', {}).get('platform_app_id'))
            if appid not in ('1422450', '2357570'):
                continue
            settings = obj.get('settings', {})
            # Only runtime choices; do not export complete account/launcher settings.
            out.append({'appid': appid, 'wine': settings.get('compatibility_layer'),
                        'graphics_stack': settings.get('graphics_stack')})
        except (OSError, ValueError, AttributeError) as e:
            out.append({'file': f.name, 'error': str(e)})
    return out


def system_sample():
    ps = command(['/bin/ps', '-axo', 'pid,ppid,rss,%cpu,comm'])
    if ps['status'] == 0:
        ps['text'] = '\n'.join(line for line in ps['text'].splitlines()
                               if any(s in line.lower() for s in ('wine', '.exe', 'gamehub', 'shader warmer')))
    return {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'memory': command(['/usr/bin/vm_stat']),
            'swap': command(['/usr/sbin/sysctl', '-n', 'vm.swapusage']),
            'processes': ps}


def export_dxmt_logs(out):
    """Include recent frame logs from each game's own Async installation."""
    home = Path.home()
    state = home / 'Library/Application Support/ShaderWarmer/dxmt-async'
    allowed = [home / 'Library/Application Support/com.gamemac.www/wine-engine/containers/virtual_containers',
               home / 'Library/Logs/ShaderWarmer/dxmt']
    records = [state / 'installation.json'] + [p for p in (state / 'games').glob('*.json') if not p.name.endswith('.seed.json')]
    copied = []
    budget = 128 * 1024 * 1024
    for record in records:
        try:
            document = json.loads(record.read_text())
            log = Path(document.get('log_path') or '')
            if not log.is_file() or not any(log.resolve().is_relative_to(p.resolve()) for p in allowed):
                continue
            destination = Path(out) / 'dxmt'
            destination.mkdir(exist_ok=True)
            for path in sorted(log.parent.glob('*.txt'), key=lambda p: p.stat().st_mtime)[-10:]:
                if not path.name.startswith(('deadlock-', 'steam-', 'local-')):
                    continue
                if not path.is_file() or path.resolve().parent != log.resolve().parent:
                    continue
                size = path.stat().st_size
                if size > min(budget, 64 * 1024 * 1024) or path.name in copied:
                    continue
                target = destination / path.name
                if target.resolve() != path.resolve():
                    shutil.copy2(path, target)
                copied.append(path.name)
                budget -= size
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    environments=home/'Library/Application Support/ShaderWarmer/wine-environments'
    try:
        profiles=json.loads((home/'Library/Application Support/ShaderWarmer/launcher-profiles.json').read_text())
        prefixes={str(Path(p['prefix']).resolve()) for p in profiles if isinstance(p,dict) and isinstance(p.get('prefix'),str)}
    except (OSError,ValueError,TypeError): prefixes=set()
    for record in environments.glob('*/installation.json'):
        try:
            document=json.loads(record.read_text());prefix=Path(document['prefix']).resolve()
            key=hashlib.sha256(str(prefix).encode()).hexdigest()[:24]
            known=prefix.is_relative_to((home/'Library/Application Support/CrossOver/Bottles').resolve()) or str(prefix) in prefixes
            if document.get('schema')!=2 or record.parent.name!=key or not known: continue
            logs=prefix/'drive_c/ShaderWarmer/Logs'
            candidates=[(logs/'dxmt.txt','dxmt.txt'),(record.parent/'logs/launch.log','launch.log')]
            for source,name in candidates:
                if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(prefix if name=='dxmt.txt' else environments.resolve()): continue
                size=source.stat().st_size
                if size>min(budget,64*1024*1024):continue
                target=Path(out)/'dxmt'/('wine-'+key+'-'+name);target.parent.mkdir(exist_ok=True)
                if source.resolve()!=target.resolve():shutil.copy2(source,target)
                copied.append(target.name);budget-=size
        except (OSError,ValueError,TypeError,KeyError):continue
    return copied


def snapshot(app_version='?', game=None):
    out = system_sample()
    out.update(app_version=app_version, selected_game=game,
               ram=command(['/usr/sbin/sysctl', '-n', 'hw.memsize']),
               chip=command(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string']),
               os=command(['/usr/bin/sw_vers']),
               thermal=command(['/usr/bin/pmset', '-g', 'therm']),
               gamehub=runtime_choices(Path.home()))
    cache = command(['/usr/bin/getconf', 'DARWIN_USER_CACHE_DIR'])
    if cache['status'] == 0 and cache['text'].startswith('/'):
        root = Path(cache['text'])
        out['cache_root'] = str(root)
        out['d3dmetal'] = cache_inventory(root)
        out['metal'] = metal_inventory(root)
    else:
        out['cache_error'] = cache
    return out


def recover_logs(out):
    path = out / 'd3dmetal_recent.log'
    # Live log stream drops events under shader load. Recover what macOS retained separately.
    with path.open('w') as f:
        try:
            r = subprocess.run(['/usr/bin/log', 'show', '--last', '1h', '--style', 'compact', '--info',
                                '--predicate', PREDICATE], stdout=f, stderr=subprocess.PIPE,
                               text=True, timeout=20)
            return {'status': r.returncode, 'error': r.stderr.strip(), 'file': path.name}
        except (OSError, subprocess.TimeoutExpired) as e:
            return {'status': -1, 'error': str(e), 'file': path.name, 'partial': True}


def watch(out, seconds=1800):
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + str(os.getpid())
    (out / ('session_' + stamp + '_start.json')).write_text(json.dumps(snapshot(), indent=1))
    stream = None
    def stop(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        with (out / ('session_' + stamp + '_d3dmetal.log')).open('w') as log, \
             (out / ('session_' + stamp + '_memory.jsonl')).open('w') as samples:
            stream = subprocess.Popen(['/usr/bin/log', 'stream', '--style', 'compact', '--info',
                                       '--predicate', PREDICATE], stdout=log, stderr=log)
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                samples.write(json.dumps(system_sample()) + '\n')
                samples.flush()
                time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        if stream is not None:
            stream.terminate()
            try:
                stream.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stream.kill()
                stream.wait()
        (out / ('session_' + stamp + '_end.json')).write_text(json.dumps(snapshot(), indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--app-version', default='?')
    ap.add_argument('--game')
    ap.add_argument('--watch', action='store_true')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.watch:
        watch(a.out)
    else:
        report = snapshot(a.app_version, a.game)
        report['dxmt_frame_logs'] = export_dxmt_logs(a.out)
        report['system_log'] = recover_logs(a.out)
        (a.out / 'device_cache_snapshot.json').write_text(json.dumps(report, indent=1))
        print(json.dumps({'snapshot': 'device_cache_snapshot.json', 'system_log': report['system_log']}))


if __name__ == '__main__':
    main()
