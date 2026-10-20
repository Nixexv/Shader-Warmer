#!/usr/bin/env python3
"""Extract Deadlock shader bytecode (DXBC) straight from the installed game.

Reads the game's `shaders_pc_*.vpk` archives (Valve VPK v2), pulls out every
`.vcs` compiled-shader resource, decompresses its zstd frames with Valve's VCS
dictionary, and writes each unique DXBC program to <out>/{vs,ps,cs}/<sha256>.dxbc.

No network, no external tools: uses the bundled libzstd and 64 KB
dictionary. Runs on the recipient's machine so the shaders always match their
installed game version.

  extract_shaders.py --game <Deadlock/game dir> --out <dir> [--zstd libzstd.dylib]
"""
from __future__ import annotations
import argparse, ctypes, hashlib, struct, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
CS_ERR, CS_UNK = (1 << 64) - 1, (1 << 64) - 2
STAGE_SUFFIX = ("vs", "ps", "cs")


def load_zstd(path):
    lib = ctypes.CDLL(path)
    lib.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_findFrameCompressedSize.restype = ctypes.c_size_t
    lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_getFrameContentSize.restype = ctypes.c_uint64
    lib.ZSTD_createDCtx.restype = ctypes.c_void_p
    lib.ZSTD_freeDCtx.argtypes = [ctypes.c_void_p]
    lib.ZSTD_decompress_usingDict.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                              ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_decompress_usingDict.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    return lib


def find_zstd(path=None):
    if path:
        return load_zstd(str(path))
    # A system library can live in dyld's shared cache without a file on disk.
    for candidate in (str(HERE / 'lib/libzstd.dylib'), '/usr/lib/libzstd.1.dylib',
                      '/opt/homebrew/lib/libzstd.dylib', '/usr/local/lib/libzstd.dylib'):
        try:
            return load_zstd(candidate)
        except OSError:
            continue
    raise RuntimeError('Не найдена библиотека zstd — переустанови Shader Warmer')


def read_vpk(dir_vpk: Path):
    """Yield (full_path, bytes) for every entry in a VPK v2 archive set."""
    data = dir_vpk.read_bytes()
    sig, ver, tree_size = struct.unpack_from("<III", data, 0)
    if sig != 0x55AA1234 or ver != 2:
        raise ValueError(f"{dir_vpk}: not a VPK v2 archive")
    off = 28  # v2 header
    base = dir_vpk.name[:-len("_dir.vpk")]
    archives = {}

    def archive(idx):
        if idx == 0x7FFF:
            return data, off  # inline: data section right after the tree
        if idx not in archives:
            archives[idx] = (dir_vpk.parent / f"{base}_{idx:03d}.vpk").read_bytes()
        return archives[idx], 0

    def cstr():
        nonlocal off
        end = data.index(b"\0", off)
        s = data[off:end].decode("latin1")
        off = end + 1
        return s

    while True:
        ext = cstr()
        if ext == "":
            break
        while True:
            path = cstr()
            if path == "":
                break
            while True:
                name = cstr()
                if name == "":
                    break
                crc, preload, arch_idx, entry_off, entry_len, term = struct.unpack_from("<IHHIIH", data, off)
                off += 18
                preload_bytes = data[off:off + preload]
                off += preload
                buf, adj = archive(arch_idx)
                body = buf[adj + entry_off: adj + entry_off + entry_len] if entry_len else b""
                full = f"{path}/{name}.{ext}" if path not in ("", " ") else f"{name}.{ext}"
                yield full, preload_bytes + body


def decompress_frames(raw: bytes, zstd, dictionary: bytes):
    pos = 0
    while True:
        pos = raw.find(ZSTD_MAGIC, pos)
        if pos < 0:
            return
        suffix = raw[pos:]
        csize = zstd.ZSTD_findFrameCompressedSize(ctypes.c_char_p(suffix), len(suffix))
        if zstd.ZSTD_isError(csize) or csize > len(suffix):
            pos += 4
            continue
        frame = suffix[:csize]
        content = zstd.ZSTD_getFrameContentSize(ctypes.c_char_p(frame), len(frame))
        if content in (CS_ERR, CS_UNK) or content > 512 * 1024 * 1024:
            pos += csize
            continue
        dst = ctypes.create_string_buffer(content)
        ctx = zstd.ZSTD_createDCtx()
        try:
            got = zstd.ZSTD_decompress_usingDict(ctx, dst, content, ctypes.c_char_p(frame), len(frame),
                                                 ctypes.c_char_p(dictionary), len(dictionary))
        finally:
            zstd.ZSTD_freeDCtx(ctx)
        if not zstd.ZSTD_isError(got):
            yield dst.raw[:got]
        pos += csize


def valid_dxbc(blob: bytes) -> bool:
    if len(blob) < 32 or blob[:4] != b"DXBC":
        return False
    declared, count = struct.unpack_from("<II", blob, 24)
    if declared != len(blob) or count > 128 or 32 + 4 * count > len(blob):
        return False
    offs = struct.unpack_from(f"<{count}I", blob, 32) if count else ()
    return all(0 <= o <= len(blob) - 8 for o in offs)


def dxbc_from(data: bytes):
    pos = 0
    while True:
        pos = data.find(b"DXBC", pos)
        if pos < 0:
            return
        if pos + 28 <= len(data):
            size = struct.unpack_from("<I", data, pos + 24)[0]
            if 32 <= size <= len(data) - pos and valid_dxbc(data[pos:pos + size]):
                yield data[pos:pos + size]
                pos += size
                continue
        pos += 4


def program_type(dxbc: bytes) -> int:
    n = struct.unpack_from("<I", dxbc, 28)[0]
    for o in struct.unpack_from(f"<{n}I", dxbc, 32):
        if dxbc[o:o + 4] in (b"SHEX", b"SHDR"):
            return (struct.unpack_from("<I", dxbc, o + 8)[0] >> 16) & 0xFFFF
    return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", type=Path, required=True, help="Deadlock/game directory")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--zstd", default=None)
    ap.add_argument("--dictionary", type=Path, default=HERE / "vfx_zstd_2bc2fa87.bin")
    a = ap.parse_args()

    zstd = find_zstd(a.zstd)
    dictionary = a.dictionary.read_bytes()

    # Deadlock keeps its shaders in citadel/ and core/.  Open those by name first: listing a folder
    # is exactly what a flaky external drive (exFAT under FSKit) fails on while plain opens work.
    vpks = [p for p in (a.game / "citadel/shaders_pc_dir.vpk", a.game / "core/shaders_pc_dir.vpk")
            if p.is_file()]
    if not vpks:
        vpks = sorted(a.game.glob("*/shaders_pc_dir.vpk"))
    if not vpks:
        print(f"no shaders_pc_dir.vpk under {a.game}", file=sys.stderr)
        return 2
    for st in STAGE_SUFFIX:
        (a.out / st).mkdir(parents=True, exist_ok=True)

    TYPE_DIR = {0: "ps", 1: "vs", 5: "cs"}
    seen = set()
    counts = {st: 0 for st in STAGE_SUFFIX}
    for vpk in vpks:
        for name, body in read_vpk(vpk):
            if not name.endswith(".vcs"):
                continue
            for frame in decompress_frames(body, zstd, dictionary):
                for dxbc in dxbc_from(frame):
                    d = TYPE_DIR.get(program_type(dxbc))
                    if not d:
                        continue
                    h = hashlib.sha256(dxbc).hexdigest()
                    if h in seen:
                        continue
                    seen.add(h)
                    (a.out / d / f"{h}.dxbc").write_bytes(dxbc)
                    counts[d] += 1
    print(f"extracted VS={counts['vs']} PS={counts['ps']} CS={counts['cs']} total={sum(counts.values())}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as e:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gptk_pick
        game = Path(sys.argv[sys.argv.index("--game") + 1]) if "--game" in sys.argv else Path(e.filename or ".")
        print(gptk_pick.unreadable_message(e, game))
        raise SystemExit(3)
