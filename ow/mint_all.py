#!/usr/bin/env python3
"""Build Overwatch's D3DMetal stage cache fully offline (no Wine, no D3DMetal, no Metal compiler).

  DXBC corpus -> DXIL      tools/dxbc2dxil (Microsoft's libdxilconv bundled in D3DMetal), N processes
  keys                     VS / CS one key each, PS one key per RT class of its shape (owplan table)
  DXIL -> metallib+refl    tools/stage2metallib (D3DMetal's private libmetalirconverter), N processes
  stage_cache.bin          records framed like SerializeKey<K>, serials 1..N per stage, arena XXH3

D3DMetal accepts these records as its own (tested: 0 appends).  Only stage records are produced;
the game builds its thin pipeline records itself.  Apple's Metal GPU-binary cache is not filled.

  mint_all.py --corpus corpus_<build> --name NAME [--workers 12] [--classes top] [--reference NS_DIR]
"""
from __future__ import annotations
import argparse, collections, json, mmap, os, random, shutil, struct, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import owplan, merge_ns  # noqa: E402
from xxh3lib import shader_id, xxh3_64  # noqa: E402
import stage_records as sr  # noqa: E402

DXBC2DXIL = HERE / "bin/dxbc2dxil"
VS_TAIL = bytes([0, 0, 0, 0]) + struct.pack("<I", 0) + bytes([0, 0xFF, 0xFF, 0, 0, 0, 0, 0])  # streamOut..tess + padding


def align8(x):
    return (x + 7) & ~7


def record_unhashed(stage, key, sid, metallib, refl):
    """SerializeKey<K> record; serial and arena XXH3 are filled in at assembly."""
    name = f"air_lib_{sid}.metallib".encode()
    ml_off = align8(len(key) + len(name))
    refl_off = align8(ml_off + len(metallib)) if refl else 0
    end = (refl_off + len(refl)) if refl else (ml_off + len(metallib))
    body = bytearray(align8(0x28 + end))
    struct.pack_into("<10I", body, 0, stage, 0, 0, len(key), len(key), len(name), ml_off, len(metallib), refl_off, len(refl))
    body[0x28:0x28 + len(key)] = key
    body[0x28 + len(key):0x28 + len(key) + len(name)] = name
    body[0x28 + ml_off:0x28 + ml_off + len(metallib)] = metallib
    if refl:
        body[0x28 + refl_off:0x28 + refl_off + len(refl)] = refl
    framed = align8(len(body) + 32)
    rec = bytearray(framed)
    struct.pack_into("<QQQ", rec, 0, framed, len(body), 0)
    rec[24:24 + len(body)] = body
    struct.pack_into("<q", rec, framed - 8, -framed)
    return bytes(rec)


# Pixel shaders the game draws with no depth buffer all have few interpolated inputs: in real play
# sessions their input counts were 1, 2 and 8, while ordinary shaders carry 9-12.  Single-target
# shaders with <= 8 inputs are only 3.7k of 119k, so covering all of them costs ~150 MB.
FULLSCREEN_MAX_INPUTS = 8
NO_DEPTH = [(0, 0, 0, 0), (0, 0, 0, 1)]


def classes_for(feat, mode):
    shape, nin = feat
    if mode != "ready":
        return owplan.shape_classes(shape, mode)
    # the huge single-target shape stays at its top classes, every small shape gets the full list
    out = list(owplan.shape_classes(shape, "top" if shape == (0x01, 0x00) else "full"))
    if shape == (0x01, 0x00) and nin <= FULLSCREEN_MAX_INPUTS:
        out += [c for c in NO_DEPTH if c not in out]
    return out


def run_parallel(jobs, workers, fn):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, jobs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="output MTLGPUFamily*_0 directory")
    ap.add_argument("--res", type=Path, required=True, help="D3DMetal.framework/Versions/A/Resources of the GPTK to use")
    ap.add_argument("--gpu-family", type=int, default=1010, help="Metal GPU family (Apple10 = 1010)")
    ap.add_argument("--harness", type=Path, help="stage2metallib to use (defaults to the bundled one); it "
                                                 "finds libmetalirconverter through a gptk_res symlink next to it")
    ap.add_argument("--work", type=Path, help="scratch dir (default mint_<NAME>)")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--classes", choices=("top", "full", "ready"), default="top",
                    help="ready = top for the big single-target shape, full for the small shapes, plus the "
                         "no-depth classes for fullscreen/UI shaders (<=2 non-system PS inputs)")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--version-bin", type=Path, required=True,
                    help="version.bin written by this machine's GPTK 3.0-3 D3DMetal")
    ap.add_argument("--reference", type=Path, help="a D3DMetal-written family dir to spot-check records against")
    a = ap.parse_args()
    work = a.work or HERE / "mint_work"
    (work / "dxil").mkdir(parents=True, exist_ok=True)
    (work / "parts").mkdir(exist_ok=True)
    out = a.out
    if out.exists():
        raise SystemExit(f"{out} exists")
    options = sr.options_for(a.gpu_family)
    if a.harness:
        sr.HARNESS = str(a.harness)
    T = {}
    t0 = time.time()

    # 1. corpus -> ShaderIDs, shapes
    shaders = []   # (stage, path, sid, dxbc_len, shape)
    short = 0
    for sub, st in (("vs", 1), ("ps", 2), ("cs", 8)):
        for p in sorted((a.corpus / sub).glob("*.dxbc")):
            d = p.read_bytes()
            s = shader_id(d)
            if s is None:
                short += 1
                continue
            feat = None
            if st == 2:
                c = owplan.chunks(d)
                shape = owplan.ps_shape({"out": owplan.signature(c.get(b"OSGN"))})
                nin = sum(1 for n, si, r, t in owplan.signature(c.get(b"ISGN")) if n not in owplan.SYSTEM_INPUTS)
                feat = (shape, nin)
            shaders.append((st, p, s, len(d), feat))
    T["hash_s"] = round(time.time() - t0, 1)
    print(f"corpus: {len(shaders)} shaders ({short} tiny ones skipped) in {T['hash_s']}s", flush=True)

    # 2. DXBC -> DXIL, one fresh converter per shader, N processes
    t = time.time()
    todo = [(p, work / "dxil" / f"{s.hex()}.dxil") for st, p, s, n, f_ in shaders if not (work / "dxil" / f"{s.hex()}.dxil").exists()]
    parts = [todo[i::a.workers] for i in range(a.workers)]

    def conv(i):
        if not parts[i]:
            return 0
        jf = work / f"dxil_jobs_{i}.tsv"
        jf.write_text("".join(f"{src}\t{dst}\n" for src, dst in parts[i]))
        try:
            # libdxilconv hangs on some shaders (feature-level 9.x ones); never wait forever
            subprocess.run(["arch", "-x86_64", str(DXBC2DXIL), str(a.res), str(jf)],
                           capture_output=True, timeout=60 + 0.2 * len(parts[i]))
        except subprocess.TimeoutExpired:
            pass
        return len(parts[i])
    run_parallel(range(a.workers), a.workers, conv)
    missing = sum(1 for st, p, s, n, f_ in shaders if not (work / "dxil" / f"{s.hex()}.dxil").exists())
    T["dxil_s"] = round(time.time() - t, 1)
    print(f"dxil: {len(todo)} converted, {missing} failed, {T['dxil_s']}s", flush=True)

    # 3. keys
    keys = []   # (stage, key, dxil path)
    for st, p, s, n, feat in shaders:
        dx = work / "dxil" / f"{s.hex()}.dxil"
        if not dx.exists():
            continue
        base = s + struct.pack("<II", 0, n)
        if st == 1:
            keys.append((1, base + VS_TAIL, dx))
        elif st == 8:
            keys.append((8, base, dx))
        else:
            for dsb, dfb, intrt, un8 in classes_for(feat, a.classes):
                keys.append((2, base + bytes([dsb, dfb, intrt, un8]) + struct.pack("<I", 0xFFFFFFFF), dx))
    print(f"keys: {collections.Counter(k[0] for k in keys)}", flush=True)

    # 4. compile in batches across N harness processes; each batch -> one part file
    t = time.time()
    batches = [keys[i:i + a.batch] for i in range(0, len(keys), a.batch)]
    done = [0]
    lock = threading.Lock()

    def compile_batch(bi):
        part = work / "parts" / f"part_{bi:05d}.bin"
        if part.exists():
            return len(batches[bi]), 0
        bd = work / f"b{bi:05d}"
        bd.mkdir(exist_ok=True)
        lines, meta = [], []
        for j, (st, key, dx) in enumerate(batches[bi]):
            sid, params = sr.sid_and_params(st, key)
            ml, rf = bd / f"{j}.ml", bd / f"{j}.rf"
            lines.append(f"{dx}\t{ml}\t{rf}\t{st}\t{params + ',' if params else ''}sid={sid}")
            meta.append((st, key, sid, ml, rf))
        (bd / "jobs.tsv").write_text("\n".join(lines) + "\n")
        subprocess.run(["arch", "-x86_64", str(sr.HARNESS), "--batch", str(bd / "jobs.tsv")] + options, capture_output=True)
        ok = fail = 0
        with (work / "parts" / f"part_{bi:05d}.tmp").open("wb") as f:
            for st, key, sid, ml, rf in meta:
                if not ml.exists():
                    fail += 1
                    continue
                f.write(record_unhashed(st, key, sid, ml.read_bytes(), rf.read_bytes() if rf.exists() else b""))
                ok += 1
        (work / "parts" / f"part_{bi:05d}.tmp").rename(part)
        shutil.rmtree(bd)
        with lock:
            done[0] += 1
            if done[0] % 20 == 0 or done[0] == len(batches):
                el = time.time() - t
                print(f"  compiled {done[0]}/{len(batches)} batches, {done[0] * a.batch / el:.0f} keys/s", flush=True)
        return ok, fail
    res = run_parallel(range(len(batches)), a.workers, compile_batch)
    T["compile_s"] = round(time.time() - t, 1)
    failed = sum(f for _, f in res)
    print(f"compile: {sum(o for o, _ in res)} ok, {failed} failed, {T['compile_s']}s", flush=True)

    # 5. assemble: serial 1..N per stage, arena XXH3
    t = time.time()
    out.mkdir(parents=True)
    w = merge_ns.ArenaWriter(out / "stage_cache.bin")
    serial = collections.Counter()
    for bi in range(len(batches)):
        data = (work / "parts" / f"part_{bi:05d}.bin").read_bytes()
        off = 0
        while off < len(data):
            framed, plen = struct.unpack_from("<QQ", data, off)
            r = bytearray(data[off:off + framed])
            st = struct.unpack_from("<I", r, 24)[0]
            serial[st] += 1
            struct.pack_into("<I", r, 28, serial[st])
            struct.pack_into("<Q", r, 16, xxh3_64(bytes(r[24:24 + plen])))
            w.add(r)
            off += framed
    w.close()
    for f in ("pipeline_cache.bin", "bytecode_cache.bin", "rootsignature_cache.bin"):
        merge_ns.ArenaWriter(out / f).close()
    shutil.copy2(a.version_bin, out / "version.bin")
    T["assemble_s"] = round(time.time() - t, 1)

    # 6. spot-check against a D3DMetal-written cache (serial and uninitialised VS key padding masked)
    check = None
    if a.reference:
        def recs(path):
            f = open(path, "rb")
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            end, off, d = struct.unpack_from("<QQQ", m, 0)[1], 24, {}
            while off < end:
                framed, plen = struct.unpack_from("<QQ", m, off)
                st, _, _, ks = struct.unpack_from("<4I", m, off + 24)
                key = bytes(m[off + 24 + 0x28:off + 24 + 0x28 + ks])
                d[(st, key[:0x19] + key[0x1c:0x25] if st == 1 else key)] = (m, off + 24, plen)
                off += framed
            return d
        mine, ref = recs(out / "stage_cache.bin"), recs(a.reference / "stage_cache.bin")
        common = sorted(set(mine) & set(ref))
        sample = random.Random(5).sample(common, min(2000, len(common)))

        def norm(m, o, n, st):
            b = bytearray(m[o:o + n])
            b[4:8] = b"\0\0\0\0"
            if st == 1:
                b[0x28 + 0x19:0x28 + 0x1c] = b"\0\0\0"
                b[0x28 + 0x25:0x28 + 0x28] = b"\0\0\0"
            return bytes(b)
        same = sum(norm(*mine[k], k[0]) == norm(*ref[k], k[0]) for k in sample)
        check = {"common_keys": len(common), "sampled": len(sample), "identical": same,
                 "only_in_minted": len(set(mine) - set(ref)), "only_in_reference": len(set(ref) - set(mine))}
        print("reference check:", json.dumps(check), flush=True)

    result = {"namespace": str(out), "records": dict(serial), "failed": failed, "timings_s": T,
              "total_s": round(time.time() - t0, 1), "stage_bytes": (out / "stage_cache.bin").stat().st_size,
              "reference_check": check}
    (work / "result.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
