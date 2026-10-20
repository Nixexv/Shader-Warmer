"""ctypes wrapper for xxh3/libxxh3.dylib (XXH3_64, D3DMetal's canonical secret)."""
from __future__ import annotations
import ctypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
_lib = ctypes.CDLL(str(HERE / "bin/libxxh3.dylib"))
_lib.xxh3_64.restype = ctypes.c_uint64
_lib.xxh3_64.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
_SECRET = (HERE / "bin/xxh3_secret.bin").read_bytes()[:192]
assert _SECRET[:8].hex() == "b8fe6c3923a44bbe"


def xxh3_64(data: bytes) -> int:
    return _lib.xxh3_64(data, len(data), _SECRET)

_lib.xxh3_128_long.restype = ctypes.c_int
_lib.xxh3_128_long.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64)]


def shader_id(dxbc: bytes) -> bytes | None:
    """D3DMetal 16-byte ShaderID = XXH3-128(dxbc[: len & ~0x3F]) as low64||high64 (None if <= 240 bytes)."""
    n = len(dxbc) & ~0x3F
    out = (ctypes.c_uint64 * 2)()
    if _lib.xxh3_128_long(dxbc[:n], n, _SECRET, out) != 0:
        return None
    return out[0].to_bytes(8, "little") + out[1].to_bytes(8, "little")
