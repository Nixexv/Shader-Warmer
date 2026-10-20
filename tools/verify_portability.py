"""Release gate: inspect native dependencies and run the relocated app's engine."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

MAGIC = {bytes.fromhex(h) for h in ('cffaedfe', 'cefaedfe', 'feedfacf', 'feedface',
                                  'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60, **kwargs).stdout


def verify(app):
    app = app.resolve()
    command(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    native = []
    for path in app.rglob('*'):
        if path.is_symlink():
            if not path.resolve().is_relative_to(app):
                raise RuntimeError('External bundle symlink: ' + str(path))
            continue
        if not path.is_file():
            continue
        with path.open('rb') as file:
            if file.read(4) not in MAGIC:
                continue
        load = command(['/usr/bin/otool', '-l', str(path)])
        minimum = re.findall(r'\bminos\s+([\d.]+)', load)
        minimum += re.findall(r'cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version ([\d.]+)', load)
        relative = str(path.relative_to(app))
        limit = 15 if '/dxmt_async/' in relative else 14
        if not minimum or any(int(v.split('.')[0]) > limit for v in minimum):
            raise RuntimeError(f'Unsupported minimum macOS: {relative}: {minimum}')
        dependencies = command(['/usr/bin/otool', '-L', str(path)])
        for line in dependencies.splitlines()[1:]:
            dep = line.strip().split(' (compatibility', 1)[0]
            if dep.startswith('/') and not dep.startswith(('/usr/lib/', '/System/Library/')):
                raise RuntimeError(f'External dependency: {relative}: {dep}')
        for rpath in re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path (.+?) \(offset', load):
            if rpath.startswith('/') and not rpath.startswith(('/usr/lib/', '/System/Library/')):
                raise RuntimeError(f'External rpath: {relative}: {rpath}')
        native.append({'file': relative, 'minimum_macos': minimum})

    with tempfile.TemporaryDirectory(prefix='Другой Mac ') as tmp:
        base = Path(tmp)
        moved = base / 'Перенесённый Shader Warmer.app'
        command(['/usr/bin/ditto', str(app), str(moved)])
        home = base / 'Новый пользователь'
        home.mkdir()
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
               'PYTHONHOME': '/missing/foreign-python', 'PYTHONPATH': '/missing/foreign-packages',
               'PYTHONDONTWRITEBYTECODE': '1', 'LC_CTYPE': 'UTF-8'}
        engine = moved / 'Contents/Resources/engine'
        python = moved / 'Contents/Resources/python/bin/python3'
        smoke = command([str(python), '-E', '-s', '-B', '-c', '''
import ctypes, json, pathlib, ssl, sqlite3, lzma, sys
sys.path.insert(0, sys.argv[1])
import dxmt_fix, wine_dxmt, warm, ow_warm, unity_warm, diagnostics, extract_shaders
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'ow'))
import xxh3lib
assert isinstance(xxh3lib.xxh3_64(b'portable'), int)
zstd = extract_shaders.find_zstd()
zstd.ZSTD_versionNumber.restype = ctypes.c_uint
assert zstd.ZSTD_versionNumber() == 10507
assert dxmt_fix.Fix().package()['files']
report = dxmt_fix.Fix().detect(str(pathlib.Path.home() / 'Missing Game'))
assert not report['dxmt_available'] and report['runtime_error']
print(json.dumps({'python': sys.version.split()[0], 'zstd': zstd._name,
                  'missing_game_error': report['runtime_error']}, ensure_ascii=False))
''', str(engine)], env=env, cwd=base)
        command(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(moved)])
    command(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    return {'native_files': native, 'relocation_smoke': json.loads(smoke),
            'checks': ['native macOS minimums', 'no external dylib paths', 'no escaping symlinks',
                       'Unicode and spaced relocation path', 'empty home', 'system-only PATH',
                       'foreign Python environment ignored', 'engine imports', 'DXMT package checksums',
                       'missing game handled', 'bundle signature remains valid']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = verify(args.app)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(f"Portability checks passed: {len(report['native_files'])} native files; relocated engine OK")
