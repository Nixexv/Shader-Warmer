"""The carver must read every serialized format from Unity 5 on, and shaders packed in bundles."""
import ctypes
import json
import lzma
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "unity"))
import unity_bundle  # noqa: E402
import unity_dxbc  # noqa: E402

_lib = ctypes.CDLL("/usr/lib/libcompression.dylib")
_lib.compression_encode_buffer.restype = ctypes.c_size_t


def lz4(data: bytes) -> bytes:
    """LZ4 block via Apple's own encoder — an implementation independent of ours."""
    buf = ctypes.create_string_buffer(len(data) + 4096)
    n = _lib.compression_encode_buffer(buf, len(buf), data, len(data), None, 0x101)
    return buf.raw[:n]


def fake_dxbc() -> bytes:
    """A minimal valid pixel-shader container: one SHEX chunk, program type 0."""
    shex = b"SHEX" + struct.pack("<II", 8, 0x50) + b"\0" * 8
    body = struct.pack("<I", 36) + shex
    total = 32 + len(body)
    return b"DXBC" + b"\0" * 16 + struct.pack("<III", 1, total, 1) + body + b"\0" * 200


def serialized(version: int, payload: bytes) -> bytes:
    """One Shader object (class 48) in the given serialized-file format."""
    head = 48 if version >= 22 else 20
    meta = bytearray(b"2017.4.40f1\0" + struct.pack("<i", 19))
    if version >= 13:
        meta += b"\0"
    meta += struct.pack("<ii", 1, 48)
    if version >= 16:
        meta += b"\0"
    if version >= 17:
        meta += struct.pack("<h", -1)
    if version >= 13:
        meta += b"\0" * 16
    if 7 <= version < 14:
        meta += struct.pack("<i", 0)
    meta += struct.pack("<i", 1)
    while (head + len(meta)) % 4:
        meta += b"\0"
    meta += struct.pack("<i", 1) if version < 14 else struct.pack("<q", 1)
    meta += struct.pack("<q", 0) if version >= 22 else struct.pack("<I", 0)
    meta += struct.pack("<Ii", len(payload), 0)
    if version < 16:
        meta += struct.pack("<hh", 48, -1)
    if version in (15, 16):
        meta += b"\0"
    offset = head + len(meta)
    total = offset + len(payload)
    if version >= 22:
        top = (struct.pack(">IIII", 0, 0, version, 0) + b"\0" * 4 + struct.pack(">I", len(meta))
               + struct.pack(">qqq", total, offset, 0))
    else:
        top = struct.pack(">IIII", len(meta), total, version, offset) + b"\0" * 4
    return top + bytes(meta) + payload


def bundle(files, block_kind: int) -> bytes:
    """A UnityFS archive holding [(name, bytes)], blocks compressed as asked (0 none, 1 LZMA, 2 LZ4)."""
    stream = b"".join(blob for _, blob in files)
    if block_kind == 1:
        raw = lzma.compress(stream, format=lzma.FORMAT_ALONE, filters=[{"id": lzma.FILTER_LZMA1}])
        packed = raw[:5] + raw[13:]
    elif block_kind == 2:
        packed = lz4(stream)
    else:
        packed = stream
    info = b"\0" * 16 + struct.pack(">i", 1) + struct.pack(">IIH", len(stream), len(packed), block_kind)
    info += struct.pack(">i", len(files))
    offset = 0
    for name, blob in files:
        info += struct.pack(">qqI", offset, len(blob), 4) + name.encode() + b"\0"
        offset += len(blob)
    top = bytearray(b"UnityFS\0" + struct.pack(">I", 7) + b"5.x.x\0" + b"2021.3.45f2\0")
    size_at = len(top)
    top += struct.pack(">q", 0) + struct.pack(">III", len(info), len(info), 0x40)
    top += b"\0" * ((-len(top)) % 16)
    out = bytearray(top) + info + packed
    struct.pack_into(">q", out, size_at, len(out))
    return bytes(out)


class UnityFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.shader = fake_dxbc()

    def carve(self, data_dir: Path) -> int:
        summary = unity_dxbc.run(data_dir, self.tmp / "out")
        return summary["unique"]

    def test_every_format_from_unity5_to_2021(self):
        # padded past 1 KB: the carver skips files that small as not worth opening
        payload = b"\0" * 64 + lz4(b"SerializedProgram" * 8 + self.shader) + b"\0" * 2048
        for version in (9, 14, 15, 17, 19, 20, 21, 22):
            with self.subTest(format=version):
                d = self.tmp / f"v{version}_Data"
                d.mkdir()
                (d / "sharedassets0.assets").write_bytes(serialized(version, payload))
                self.assertEqual(self.carve(d), 1)
                shutil.rmtree(self.tmp / "out", ignore_errors=True)

    def test_shaders_inside_addressables_bundles(self):
        assets = serialized(22, b"\0" * 64 + lz4(b"SerializedProgram" * 8 + self.shader) + b"\0" * 2048)
        for kind in (0, 1, 2):
            with self.subTest(compression=kind):
                d = self.tmp / f"b{kind}_Data" / "StreamingAssets/aa/StandaloneWindows64"
                d.mkdir(parents=True)
                # a texture blob rides along, as in real bundles, so the archive stays past 1 KB
                files = [("CAB-test", assets), ("CAB-test.resS", bytes(range(256)) * 16 + os.urandom(4096))]
                (d / "shaders.bundle").write_bytes(bundle(files, kind))
                self.assertEqual(self.carve(self.tmp / f"b{kind}_Data"), 1)
                shutil.rmtree(self.tmp / "out", ignore_errors=True)

    def test_lz4_decoder_matches_apple(self):
        data = (b"UnityShaderDXBC" * 3000) + bytes(range(256)) * 40
        self.assertEqual(unity_bundle.lz4_block(lz4(data), len(data)), data)


if __name__ == "__main__":
    unittest.main()
