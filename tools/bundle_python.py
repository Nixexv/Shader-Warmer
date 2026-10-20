"""Bundle a checksum-pinned, relocatable CPython. Build-time download only."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def bundle(destination):
    config = json.loads((ROOT / 'runtime/python.json').read_text())
    cache = ROOT / '.runtime-cache'
    cache.mkdir(exist_ok=True)
    archive = cache / config['archive']
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != config['sha256']:
        temporary = archive.with_suffix('.download')
        subprocess.run(['/usr/bin/curl', '-fL', '--retry', '2', '-o', str(temporary), config['url']], check=True)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != config['sha256']:
            temporary.unlink(missing_ok=True)
            raise RuntimeError('Python archive checksum mismatch')
        temporary.replace(archive)
    with tempfile.TemporaryDirectory(dir=cache) as temp:
        base = Path(temp)
        with tarfile.open(archive) as package:
            # This runs on the build machine; reject archive traversal on older Python too.
            for item in package.getmembers():
                target = (base / item.name).resolve()
                if not target.is_relative_to(base.resolve()):
                    raise RuntimeError('Unsafe runtime archive path')
                if item.issym() or item.islnk():
                    link = (target.parent / item.linkname).resolve()
                    if not link.is_relative_to(base.resolve()):
                        raise RuntimeError('Unsafe runtime archive link')
            package.extractall(base, filter='data')
        shutil.copytree(base / 'python', destination, symlinks=True)
    # Keep licenses, stdlib and extension modules; build headers and pip are unnecessary at runtime.
    shutil.rmtree(destination / 'include', ignore_errors=True)
    for directory in destination.rglob('__pycache__'):
        shutil.rmtree(directory)
    (destination / 'build-provenance.json').write_text(json.dumps(config, indent=2) + '\n')
    subprocess.run([str(destination / 'bin/python3'), '-E', '-s', '-B', '-c',
                    'import ctypes,ssl,sqlite3,lzma,fcntl,json; print("Bundled Python ready")'], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    bundle(parser.parse_args().destination.resolve())
