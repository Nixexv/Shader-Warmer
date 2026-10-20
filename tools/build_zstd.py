"""Rebuild the bundled BSD-licensed zstd for Apple Silicon / macOS 14."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz'
SHA256 = 'eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3'


def main():
    cache = ROOT / '.runtime-cache'
    cache.mkdir(exist_ok=True)
    archive = cache / 'zstd-1.5.7.tar.gz'
    if not archive.exists():
        subprocess.run(['/usr/bin/curl', '-fL', '--retry', '2', '-o', str(archive), URL], check=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('zstd source checksum mismatch')
    # Upstream makefiles cannot handle a source checkout whose path contains spaces.
    with tempfile.TemporaryDirectory(prefix='shader-warmer-zstd-', dir='/private/tmp') as temp:
        with tarfile.open(archive) as package:
            package.extractall(temp, filter='data')
        source = Path(temp) / 'zstd-1.5.7'
        subprocess.run(['/usr/bin/make', '-C', str(source / 'lib'), '-j4', 'libzstd.1.5.7.dylib',
                        'CC=clang -target arm64-apple-macos14.0', 'CFLAGS=-O2',
                        'SONAME_FLAGS=-install_name @rpath/libzstd.dylib -compatibility_version 1 -current_version 1.5.7'], check=True)
        destination = ROOT / 'lib'
        destination.mkdir(exist_ok=True)
        shutil.copy2(source / 'lib/libzstd.1.5.7.dylib', destination / 'libzstd.dylib')
        shutil.copy2(source / 'LICENSE', destination / 'LICENSE-zstd')
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(destination / 'libzstd.dylib')], check=True)


if __name__ == '__main__':
    main()
