#!/usr/bin/env python3
"""Merge several D3DMetal cache namespaces (e.g. from parallel warm workers) into one, streaming.

  stage_cache.bin     dedupe on (stage, key) with VS key padding masked; serials renumbered 1..N per
                      stage in output order (exactly how D3DMetal numbers them) and the arena XXH3 of
                      every record recomputed
  pipeline_cache.bin  dedupe on the keyblob (payload[0x40:]), records copied verbatim
  bytecode_cache.bin  empty arena by default (D3DMetal does not read it on a stage hit); --bytecode
                      merges it, deduped on the ShaderID
  rootsignature_cache.bin / version.bin  empty arena / from the first input (all inputs must agree)

  merge_ns.py --out OUT_FAMILY_DIR IN_FAMILY_DIR [IN_FAMILY_DIR ...] [--bytecode]
"""
from __future__ import annotations
import argparse, collections, json, mmap, struct, time
from pathlib import Path

from xxh3lib import xxh3_64

PAGE = 0x10000


def records(path: Path):
    """yield (framed record bytes view offset, framed, payload) over an arena via mmap"""
    if not path.exists() or path.stat().st_size < 24:
        return
    f = path.open("rb")
    m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    end = struct.unpack_from("<QQQ", m, 0)[1]
    off = 24
    while off + 32 <= end:
        framed, payload = struct.unpack_from("<QQ", m, off)
        if framed == 0 or not (payload + 32 <= framed <= payload + 39) or off + framed > end:
            raise SystemExit(f"{path}: broken record at {off}")
        yield m[off:off + framed]
        off += framed


def stage_ident(rec: bytes):
    st, _, _, ks = struct.unpack_from("<4I", rec, 24)
    key = rec[24 + 0x28:24 + 0x28 + ks]
    return st, (key[:0x19] + key[0x1c:0x25]) if st == 1 else key


class ArenaWriter:
    def __init__(self, path: Path):
        self.f = path.open("wb")
        self.f.write(b"\0" * 24)
        self.end = 24
        self.n = 0

    def add(self, rec: bytes):
        self.f.write(rec)
        self.end += len(rec)
        self.n += 1

    def close(self):
        size = max(PAGE, (self.end + PAGE - 1) & ~(PAGE - 1))
        self.f.truncate(size)
        self.f.seek(0)
        self.f.write(struct.pack("<QQQ", size, self.end, self.end))
        self.f.close()


class VersionMismatch(ValueError):
    """Caches written by different D3DMetal builds; mixing them would make the game drop the lot."""


# Pipeline records point at their stage records by SERIAL NUMBER, from u32 slots in the payload's
# 64-byte header (same layout on GPTK 3.0-3 and 4.0-2; VS/PS/GS confirmed by the ShaderID in the
# pipeline key).  Stage serials are renumbered when inputs are merged, so these pointers must be
# rewritten with them — copying pipelines verbatim bound every pipeline from the second input on to
# someone else's shaders, which on GPTK 4 is a black screen (Deadlock, 154 pipelines of the game's
# own startup records).
PIPELINE_STAGE_SLOTS = {5: 7, 7: 1, 8: 2, 9: 8, 10: 16, 11: 15}   # slot -> stage: GS VS PS CS StageIn StreamOut
_NOT_A_REFERENCE = {0, 1, 14}                                      # bucket hash, and a flags word


def _stage_keys(fam: Path) -> dict:
    """(stage, serial) -> the stage record's key, whose first 16 bytes are the shader's ShaderID."""
    keys = {}
    for rec in records(fam / "stage_cache.bin"):
        st, ser, _, ks = struct.unpack_from("<4I", rec, 24)
        keys[(st, ser)] = bytes(rec[24 + 0x28:24 + 0x28 + ks])
    return keys


def _pipeline_ok(payload: bytes, keys: dict) -> bool:
    """Do this pipeline's VS and PS pointers land on the shaders its own key names?"""
    blob = payload[0x40:]
    for slot, st in ((7, 1), (8, 2)):
        ser = struct.unpack_from("<I", payload, 4 * slot)[0]
        if ser and keys.get((st, ser), b"")[:16] not in blob:
            return False
    return True


def damaged_pipelines(fam: Path) -> int:
    """Pipelines whose stage pointers do not match their own key — left by an old merge."""
    keys = _stage_keys(fam)
    return sum(1 for rec in records(fam / "pipeline_cache.bin") if not _pipeline_ok(bytes(rec[24:]), keys))


def merge(inputs, out: Path, with_bytecode: bool):
    out.mkdir(parents=True, exist_ok=True)
    vbs = {(p / "version.bin").read_bytes() for p in inputs}
    if len(vbs) != 1:
        # A real exception, not SystemExit: callers have a whole warm riding on this and must be
        # able to catch it instead of having the process torn down under them.
        raise VersionMismatch("inputs have different version.bin (different GPTK/macOS?) - refusing to merge")
    report = {}

    w = ArenaWriter(out / "stage_cache.bin")
    survivor = {}                                   # stage ident -> its serial in the output
    remap = [dict() for _ in inputs]                # per input: (stage, old serial) -> new serial
    serial, dup = collections.Counter(), 0
    for i, p in enumerate(inputs):
        for rec in records(p / "stage_cache.bin"):
            ident = stage_ident(rec)
            st, old = struct.unpack_from("<2I", rec, 24)
            if ident in survivor:
                dup += 1
                remap[i][(st, old)] = survivor[ident]
                continue
            serial[st] += 1
            survivor[ident] = remap[i][(st, old)] = serial[st]
            r = bytearray(rec)
            struct.pack_into("<I", r, 24 + 4, serial[st])
            plen = struct.unpack_from("<Q", r, 8)[0]
            struct.pack_into("<Q", r, 16, xxh3_64(bytes(r[24:24 + plen])))
            w.add(r)
    w.close()
    report["stage_cache.bin"] = {"records": w.n, "duplicates_dropped": dup, "per_stage": dict(serial)}

    out_keys = _stage_keys(out)
    w = ArenaWriter(out / "pipeline_cache.bin")
    seen, dup, unresolved, broken, skipped = set(), 0, 0, 0, 0
    for i, p in enumerate(inputs):
        if damaged_pipelines(p):
            # damaged by an old merge: some bad pointers pass any check we can make, so take none
            skipped += sum(1 for _ in records(p / "pipeline_cache.bin"))
            continue
        for rec in records(p / "pipeline_cache.bin"):
            plen = struct.unpack_from("<Q", rec, 8)[0]
            key = bytes(rec[24 + 0x40:24 + plen])
            if key in seen:
                dup += 1
                continue
            r = bytearray(rec)
            ok = True
            for slot in range(16):
                if slot in _NOT_A_REFERENCE:
                    continue
                v = struct.unpack_from("<I", r, 24 + 4 * slot)[0]
                if not v:
                    continue
                st = PIPELINE_STAGE_SLOTS.get(slot)
                new = remap[i].get((st, v)) if st else None
                if new is None:                     # points at nothing we kept, or a slot we do not know
                    ok = False
                    break
                struct.pack_into("<I", r, 24 + 4 * slot, new)
            if not ok:
                unresolved += 1
                continue
            if not _pipeline_ok(bytes(r[24:24 + plen]), out_keys):
                broken += 1                         # damaged before this merge: D3DMetal rebuilds it
                continue
            struct.pack_into("<Q", r, 16, xxh3_64(bytes(r[24:24 + plen])))
            seen.add(key)
            w.add(r)
    w.close()
    report["pipeline_cache.bin"] = {"records": w.n, "duplicates_dropped": dup, "unresolved_dropped": unresolved,
                                    "broken_dropped": broken, "damaged_input_dropped": skipped}

    w = ArenaWriter(out / "bytecode_cache.bin")
    seen, dup = set(), 0
    if with_bytecode:
        for p in inputs:
            for rec in records(p / "bytecode_cache.bin"):
                k = bytes(rec[24 + 8:24 + 24])
                if k in seen:
                    dup += 1
                    continue
                seen.add(k)
                w.add(rec)
    w.close()
    report["bytecode_cache.bin"] = {"records": w.n, "duplicates_dropped": dup}

    ArenaWriter(out / "rootsignature_cache.bin").close()
    (out / "version.bin").write_bytes(vbs.pop())
    return report


REPAIR_VERSION = 2


def _drop_pipelines(fam: Path) -> int:
    """Replace pipeline_cache.bin with an empty arena; returns how many records it held."""
    pc = Path(fam) / "pipeline_cache.bin"
    n = sum(1 for _ in records(pc))
    staged = Path(fam) / "pipeline_cache.bin.repairing"
    ArenaWriter(staged).close()
    staged.replace(pc)
    return n


def repair(fam: Path, work: Path) -> int:
    """Drop every pipeline of a cache an old merge damaged; returns how many were dropped.

    Checking each pointer is not enough: a warm writes the render-target variants of one shader
    next to each other, so a shifted serial often lands on another variant of the same shader —
    the ShaderID matches, the format does not, and the game renders with wrong colors (1.36
    dropped only the mismatched ones and Deadlock's menu came up orange and green).  The warm lives
    in the stage records, which are keyed and intact; D3DMetal rebuilds the pipelines from them.
    """
    if not damaged_pipelines(fam):
        return 0
    return _drop_pipelines(fam)


def repair_once(fam: Path, work: Path) -> int:
    """repair(), once per state of the cache: reading every stage key of a multi-GB cache on each
    detect would be slow, so the (size, mtime) of pipeline_cache.bin is remembered.  A cache that
    1.36 already checked counts as damaged: it may have been only half repaired."""
    pc = Path(fam) / "pipeline_cache.bin"
    if not pc.exists():
        return 0
    state = Path.home() / "Library/Application Support/ShaderWarmer/pipeline_checks.json"
    try:
        known = json.loads(state.read_text())
    except (OSError, ValueError):
        known = {}
    sig = [pc.stat().st_size, int(pc.stat().st_mtime)]
    entry = known.get(str(fam))
    if isinstance(entry, dict) and entry.get("v") == REPAIR_VERSION and entry.get("sig") == sig:
        return 0
    if isinstance(entry, list):                     # written by 1.36, whose repair was partial
        dropped = _drop_pipelines(Path(fam))
    else:
        dropped = repair(Path(fam), Path(work))
    known[str(fam)] = {"v": REPAIR_VERSION, "sig": [pc.stat().st_size, int(pc.stat().st_mtime)]}
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(known, indent=1))
    except OSError:
        pass
    return dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--bytecode", action="store_true")
    ap.add_argument("inputs", type=Path, nargs="+")
    a = ap.parse_args()
    t = time.time()
    r = merge(a.inputs, a.out, a.bytecode)
    r["seconds"] = round(time.time() - t, 1)
    print(json.dumps(r, indent=1))


if __name__ == "__main__":
    try:
        main()
    except VersionMismatch as e:
        raise SystemExit(str(e))
