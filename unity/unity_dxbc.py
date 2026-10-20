#!/usr/bin/env python3
"""Extract the D3D11 shader bytecode (DXBC) a Unity game ships, without the game or Unity.

Release builds strip the type trees, so the Shader object's field layout is unknown; but its
sub-program blobs are LZ4 block streams, and LZ4 fails fast on anything that is not one.  So for
every Shader object (class 48) we try to decode an LZ4 stream at each offset and keep whatever
decodes to a valid DXBC container.  Verified against the cache the game itself wrote: every shader
it compiled must be in the extracted set (ShaderID = XXH3-128 of the DXBC).

  unity_dxbc.py --data <Game_Data dir> --out corpus_dir
"""
from __future__ import annotations
import argparse, collections, hashlib, json, mmap, struct, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from unity_assets import parse, parse_bytes, SHADER, COMPUTE_SHADER  # noqa: E402
import unity_bundle  # noqa: E402

STAGE_DIR = {0: "ps", 1: "vs", 2: "gs", 3: "hs", 4: "ds", 5: "cs"}


def lz4_decode(src: bytes, start: int, cap: int = 4 << 20):
    """Decode an LZ4 block starting at `start` until it ends or stops making sense.
    Returns the output produced so far (bytes) — carving only needs "enough"."""
    out = bytearray()
    i, n = start, len(src)
    while i < n and len(out) < cap:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
                if lit > cap:
                    return bytes(out)
        if i + lit > n:
            return bytes(out)
        out += src[i:i + lit]
        i += lit
        if i + 2 > n:
            return bytes(out)
        off = src[i] | (src[i + 1] << 8)
        i += 2
        if off == 0 or off > len(out):
            return bytes(out)
        mlen = token & 15
        if mlen == 15:
            while i < n:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
                if mlen > cap:
                    return bytes(out)
        mlen += 4
        pos = len(out) - off
        for _ in range(mlen):                    # matches may overlap
            out.append(out[pos])
            pos += 1
    return bytes(out)


def dxbc_at(buf: bytes, off: int):
    """A DXBC container at `off`, validated without its checksum, or None."""
    if buf[off:off + 4] != b"DXBC" or off + 32 > len(buf):
        return None
    total = struct.unpack_from("<I", buf, off + 24)[0]
    if not (32 < total <= len(buf) - off) or total > 8 << 20:
        return None
    n = struct.unpack_from("<I", buf, off + 28)[0]
    if not (1 <= n <= 32) or off + 32 + 4 * n > len(buf):
        return None
    stage = None
    for k in range(n):
        o = struct.unpack_from("<I", buf, off + 32 + 4 * k)[0]
        if o + 12 > total:
            return None
        fourcc = buf[off + o:off + o + 4]
        if fourcc in (b"SHEX", b"SHDR"):
            stage = struct.unpack_from("<I", buf, off + o + 8)[0] >> 16
    if stage is None or stage not in STAGE_DIR:
        return None
    return buf[off:off + total], stage


def carve_object(data: bytes, start: int, size: int, seen: set, out: Path, index: list):
    found = 0
    obj = data[start:start + size]
    i = 0
    while i < len(obj):
        dec = lz4_decode(obj, i)
        if len(dec) < 256:
            i += 1
            continue
        pos = 0
        hit = False
        while True:
            j = dec.find(b"DXBC", pos)
            if j < 0:
                break
            got = dxbc_at(dec, j)
            pos = j + 4
            if not got:
                continue
            blob, stage = got
            sha = hashlib.sha256(blob).hexdigest()
            hit = True
            if sha in seen:
                continue
            seen.add(sha)
            d = out / STAGE_DIR[stage]
            d.mkdir(parents=True, exist_ok=True)
            (d / (sha[:16] + ".dxbc")).write_bytes(blob)
            index.append({"sha": sha, "stage": STAGE_DIR[stage], "size": len(blob)})
            found += 1
        # a decoded stream is worth skipping past; otherwise every offset inside it is retried
        i += max(1, len(dec) // 8) if hit else 1
    return found


def _serialized_files(path: Path):
    """The serialized files this path holds: itself, or everything packed in it when it is a bundle.

    Addressables put the shaders inside .bundle archives, so a carver that only looks at loose
    .assets files silently misses them — and the game then compiles them at play time.
    """
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return
    if not unity_bundle.looks_like_bundle(head):
        # Map instead of read: a big game ships multi-GB .assets, and reading one whole into
        # memory on an 8 GB Mac only to carve a few shader objects out of it is how a warm dies.
        try:
            with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                yield m
        except (OSError, ValueError):
            pass
        return
    try:
        inner = unity_bundle.files(path.read_bytes())
    except Exception:
        return
    for _, blob in inner:
        yield blob


def run(data: Path, out: Path, on_file=None):
    """Carve every shipped shader out of a *_Data directory.  on_file(name, done, total, found)
    is called after each serialized file."""
    out.mkdir(parents=True, exist_ok=True)
    skip = {".resS", ".resource", ".json", ".config", ".info", ".dll", ".dat", ".txt"}
    files = [p for p in sorted(data.rglob("*")) if p.is_file() and p.suffix not in skip and p.stat().st_size > 1024]
    seen, index = set(), []
    t0 = time.time()
    for i, p in enumerate(files, 1):
        for blob in _serialized_files(p):
            try:
                info = parse_bytes(blob)
            except Exception:
                continue
            for _, start, size, cls in info["objects"]:
                if cls in (SHADER, COMPUTE_SHADER):
                    carve_object(info["data"], start, size, seen, out, index)
        if on_file:
            on_file(p.name, i, len(files), len(index))
    (out / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in index))
    stages = collections.Counter(r["stage"] for r in index)
    summary = {"files": len(files), "unique": len(index), "stages": dict(stages),
               "seconds": round(time.time() - t0, 1)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True, help="the game's *_Data directory")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit-files", type=int)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    # Every serialized file under *_Data, including Resources/unity_builtin_extra and
    # "unity default resources" — Unity's built-in shaders live there, not in the .assets files.
    skip = {".resS", ".resource", ".json", ".config", ".info", ".dll", ".dat", ".bundle", ".txt"}
    files = [p for p in sorted(a.data.rglob("*"))
             if p.is_file() and p.suffix not in skip and p.stat().st_size > 1024]
    seen, index = set(), []
    t0 = time.time()
    for p in files[:a.limit_files]:
        try:
            info = parse(p)
        except Exception as e:
            print(f"{p.name}: skipped ({e})")
            continue
        shaders = [o for o in info["objects"] if o[3] in (SHADER, COMPUTE_SHADER)]
        if not shaders:
            continue
        t = time.time()
        got = 0
        for _, start, size, _ in shaders:
            got += carve_object(info["data"], start, size, seen, a.out, index)
        print(f"{p.name}: {len(shaders)} shader objects -> {got} new shaders ({time.time() - t:.1f}s)", flush=True)
    (a.out / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in index))
    stages = collections.Counter(r["stage"] for r in index)
    summary = {"files": len(files), "unique": len(index), "stages": dict(stages), "seconds": round(time.time() - t0, 1)}
    (a.out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
