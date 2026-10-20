#!/usr/bin/env python3
"""Open Unity asset bundles (UnityFS) and hand back the serialized files inside them.

Games that use Addressables ship their shaders in .bundle archives instead of the .assets files
next to the executable, so without this the carver simply never sees them.  A bundle is a header,
a directory of blocks and nodes, and then the blocks themselves; decompressing the blocks and
concatenating them gives one stream that the nodes cut into ordinary serialized files.

  unity_bundle.py FILE [FILE ...]      # list what is inside
"""
from __future__ import annotations
import lzma, struct, sys
from pathlib import Path

MAGIC = (b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")


def looks_like_bundle(head: bytes) -> bool:
    return any(head.startswith(m) for m in MAGIC)


def lz4_block(src: bytes, out_size: int) -> bytes:
    """LZ4 block format, decoded to a known output size (unlike the carver's best-effort scan)."""
    out = bytearray(out_size)
    i, o, n = 0, 0, len(src)
    while i < n and o < out_size:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out[o:o + lit] = src[i:i + lit]
        i += lit
        o += lit
        if o >= out_size:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = token & 15
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        pos = o - off
        if pos < 0:
            raise ValueError("bad LZ4 match offset")
        for _ in range(mlen):                     # matches may overlap, so copy byte by byte
            out[o] = out[pos]
            o += 1
            pos += 1
    if o != out_size:
        raise ValueError(f"LZ4 block produced {o} of {out_size} bytes")
    return bytes(out)


def lzma_block(src: bytes, out_size: int) -> bytes:
    """Unity stores the raw LZMA properties followed by the stream, with the size kept elsewhere."""
    head = src[:5] + struct.pack("<Q", out_size)
    d = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
    return d.decompress(head + src[5:])[:out_size]


def _decompress(src: bytes, out_size: int, flags: int) -> bytes:
    kind = flags & 0x3F
    if kind == 0:
        return src[:out_size]
    if kind == 1:
        return lzma_block(src, out_size)
    if kind in (2, 3):
        return lz4_block(src, out_size)
    raise ValueError(f"unknown bundle compression {kind}")


class _R:
    """Bundle headers are big-endian."""

    def __init__(self, d: bytes, p: int = 0):
        self.d, self.p = d, p

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def _n(self, fmt, size):
        v = struct.unpack_from(">" + fmt, self.d, self.p)[0]
        self.p += size
        return v

    def u16(self): return self._n("H", 2)
    def i32(self): return self._n("i", 4)
    def u32(self): return self._n("I", 4)
    def i64(self): return self._n("q", 8)

    def cstr(self):
        e = self.d.index(b"\0", self.p)
        s = self.d[self.p:e].decode("utf-8", "replace")
        self.p = e + 1
        return s

    def align(self, n):
        self.p = (self.p + n - 1) & ~(n - 1)


def files(data: bytes):
    """-> [(name, bytes)] for every file packed in the bundle."""
    r = _R(data)
    signature = r.cstr()
    if signature != "UnityFS":
        # UnityRaw/UnityWeb predate Unity 5.3; no game we target still ships them.
        raise ValueError(f"bundle format {signature!r} not supported")
    version = r.u32()
    r.cstr()                                      # unity version
    r.cstr()                                      # unity revision
    size = r.i64()
    comp_info, raw_info = r.u32(), r.u32()
    flags = r.u32()
    if version >= 7:
        r.align(16)
    if flags & 0x80:                              # directory parked at the end of the file
        info = data[size - comp_info:size]
    else:
        info = data[r.p:r.p + comp_info]
        r.p += comp_info
    if flags & 0x200:
        r.align(16)

    d = _R(_decompress(info, raw_info, flags))
    d.p += 16                                     # hash of the uncompressed data
    blocks = [(d.u32(), d.u32(), d.u16()) for _ in range(d.i32())]
    nodes = [(d.i64(), d.i64(), d.u32(), d.cstr()) for _ in range(d.i32())]

    stream = bytearray()
    for raw_size, comp_size, block_flags in blocks:
        stream += _decompress(data[r.p:r.p + comp_size], raw_size, block_flags)
        r.p += comp_size
    return [(path, bytes(stream[offset:offset + node_size])) for offset, node_size, _, path in nodes]


def main():
    for f in sys.argv[1:]:
        data = Path(f).read_bytes()
        try:
            inner = files(data)
        except Exception as e:
            print(f"{Path(f).name}: {e}")
            continue
        print(f"{Path(f).name}: {len(inner)} файлов")
        for name, blob in inner:
            print(f"   {name:50s} {len(blob)} байт")


if __name__ == "__main__":
    main()
