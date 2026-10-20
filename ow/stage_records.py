#!/usr/bin/env python3
"""Offline stage_cache.bin records for GPTK 3.0.3 D3DMetal (no game, no Wine, no D3DMetal).

Drives tools/stage2metallib/stage2metallib (the private libmetalirconverter bundled in
D3DMetal.framework, configured exactly like D3DMCompiler::CreateIRCompiler) and serializes
records exactly like SerializeKey<K> (@0x5f1ec for Fragment):

  payload = [u32 stage][u32 serial][u32 0][u32 keySize][u32 keySize][u32 nameLen]
            [u32 mlOff=align8(keySize+nameLen)][u32 mlSize][u32 reflOff=align8(mlOff+mlSize)][u32 reflSize]
            [key][name "air_lib_<SID>.metallib" (no NUL)][metallib][reflection]      (offsets from +0x28)
  record  = [u64 framed=align8(payload+32)][u64 payloadSize][u64 XXH3_64(payload)][payload][i64 -framed]

Covered stages: 1 Vertex, 2 Fragment, 8 Compute. Not covered (runtime state / not ported):
7 Geometry, 15 StreamOut, 16 StageIn -- copied verbatim by `rebuild`.

  stage_records.py validate --cache-dir DIR          re-derive 1/2/8 records, compare byte-for-byte
  stage_records.py rebuild  --cache-dir DIR --out F  rebuild the whole stage_cache.bin from offline compiles
  stage_records.py mint     --cache-dir DIR --keys K.jsonl --out F
                            append new records for {"stage":2,"key":"<hex>"} lines to a copy of the arena
"""
from __future__ import annotations
import argparse, collections, json, mmap, os, struct, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from xxh3lib import xxh3_64 as xxh3  # noqa: E402

HARNESS = os.path.join(HERE, 'bin', 'stage2metallib')

# D3DMCompiler defaults on GPTK 3.0.3 / MTLGPUFamilyApple10, no D3DM_* env overrides
# (decoded from D3DMCompiler::D3DMCompiler @0x6860e and CreateIRCompiler @0x54bea).
DEFAULT_OPTIONS = [
    'compat=0x19F',            # BOUNDS_CHECK|FLUSH_POS_INF_TO_NAN|MIN_LOD_CLAMP|LOD_BIAS|POSITION_INVARIANCE|bit7|bit8
    'geomdbg=0', 'force4wide=0', 'unsafepos=0', 'rgb32=0', 'flushnan=0',
    'unaligned=1', 'patchfloat=1', 'shaderdetect=1',
    'yield=1', 'yieldmode=3', 'yielda=128', 'yieldb=2048',
    'gos=1', 'stagein=1', 'ignorers=0', 'ignoredbg=1',
    'gpufamily=1010', 'triple=air64-apple-macosx26.0.0',
]


def options_for(gpu_family: int):
    """The option set D3DMCompiler builds for this Mac's GPU family.

    DEFAULT_OPTIONS were read off an Apple10 (M5).  The constructor @0x6860e derives three of them
    from the family (after its supportsFamily loop, macOS 14.4+):
      - EnableYield(1, 3, 128, 2048) only on Apple9-10; (0, 0, 0, 0) on Apple7-8,
      - EnableShaderDetection from Apple8 on, off on Apple7,
      - compat bit 5 (field +0x54e) is on by default and cleared only on Apple10: 0x1BF vs 0x19F.
    Using the Apple10 set everywhere is why an M1's calibration matched 2 records out of 93.
    """
    opts = dict(o.split('=', 1) for o in DEFAULT_OPTIONS)      # keeps DEFAULT_OPTIONS' order
    opts['gpufamily'] = str(gpu_family)
    opts['compat'] = '0x19F' if gpu_family == 1010 else '0x1BF'
    opts['shaderdetect'] = '1' if gpu_family >= 1008 else '0'
    on = gpu_family in (1009, 1010)
    opts['yield'], opts['yieldmode'], opts['yielda'], opts['yieldb'] = (
        ('1', '3', '128', '2048') if on else ('0', '0', '0', '0'))
    return [f'{k}={v}' for k, v in opts.items()]
COMPILABLE = (1, 2, 8)


def align8(x: int) -> int:
    return (x + 7) & ~7


def walk(buf):
    off = 24
    n = len(buf)
    while off + 32 <= n:
        framed, payload = struct.unpack_from('<QQ', buf, off)
        if framed == 0 or not (payload + 32 <= framed <= payload + 39) or off + framed > n:
            return
        yield off, framed, payload
        off += framed


def load_dxil(bytecode_cache: str) -> dict[bytes, bytes]:
    out = {}
    with open(bytecode_cache, 'rb') as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        for off, framed, payload in walk(m):
            head = m[off + 24:off + 24 + 28]
            total = struct.unpack_from('<I', head, 24)[0]
            out[bytes(head[8:24])] = bytes(m[off + 24 + 28:off + 24 + 28 + total])
    return out


def sid_and_params(stage: int, key: bytes) -> tuple[str, str]:
    """ShaderID string (goes into the reflection) and per-stage compiler settings, from the key."""
    lo, hi = struct.unpack_from('<QQ', key, 0)
    rs, dxbc_len = struct.unpack_from('<II', key, 16)
    sid = f'8{lo}{hi}{rs}{dxbc_len}'
    if stage == 2:
        mask = struct.unpack_from('<I', key, 0x1c)[0]
        sid += f'{key[0x18]}{key[0x19]}{key[0x1a]}{key[0x1b]}{mask}'
        return sid, f'dsb={key[0x18]},depthfb={key[0x19]},intrt={key[0x1a]},unorm8={key[0x1b]},samplemask={mask}'
    if stage == 1:
        as_uint = lambda b: b if b < 128 else b + 4294967296 - 256
        streamout, stripcut, pointtopo = key[0x18], struct.unpack_from('<I', key, 0x1c)[0], key[0x20]
        vp, rt = struct.unpack_from('<bb', key, 0x21)
        gs, tess = key[0x23], key[0x24]
        sid += f'{streamout}{stripcut}{pointtopo}{as_uint(key[0x21])}{as_uint(key[0x22])}{gs}{tess}'
        return sid, (f'streamout={streamout},stripcut={stripcut},pointtopo={pointtopo},vpindex={vp},rtindex={rt},'
                     f'gstsemu={1 if (gs or tess) else streamout},tess={tess}')
    return sid, ''


def compile_keys(items, dxil: dict[bytes, bytes], workdir: str):
    """items: list of (tag, stage, key). Returns {tag: (sid, metallib, reflection)} for successes."""
    jobs, meta = [], {}
    for tag, stage, key in items:
        blob = dxil.get(key[:16])
        if blob is None:
            continue
        dx = os.path.join(workdir, key[:16].hex() + '.dxil')
        if not os.path.exists(dx):
            with open(dx, 'wb') as f:
                f.write(blob)
        sid, params = sid_and_params(stage, key)
        ml, rf = os.path.join(workdir, f'{tag}.ml'), os.path.join(workdir, f'{tag}.rf')
        jobs.append(f'{dx}\t{ml}\t{rf}\t{stage}\t{params + "," if params else ""}sid={sid}')
        meta[tag] = (sid, ml, rf)
    jobs_path = os.path.join(workdir, 'jobs.tsv')
    with open(jobs_path, 'w') as f:
        f.write('\n'.join(jobs) + '\n')
    subprocess.run(['arch', '-x86_64', HARNESS, '--batch', jobs_path] + DEFAULT_OPTIONS,
                   capture_output=True, text=True)
    out = {}
    for tag, (sid, ml, rf) in meta.items():
        if os.path.exists(ml):
            rfb = open(rf, 'rb').read() if os.path.exists(rf) else b''
            out[tag] = (sid, open(ml, 'rb').read(), rfb)
    return out


def build_record(stage: int, serial: int, key: bytes, sid: str, metallib: bytes, refl: bytes) -> bytes:
    name = f'air_lib_{sid}.metallib'.encode()
    ml_off = align8(len(key) + len(name))
    refl_off = align8(ml_off + len(metallib)) if refl else 0
    end = (refl_off + len(refl)) if refl else (ml_off + len(metallib))
    body = bytearray(align8(0x28 + end))
    struct.pack_into('<10I', body, 0, stage, serial, 0, len(key), len(key), len(name),
                     ml_off, len(metallib), refl_off, len(refl))
    body[0x28:0x28 + len(key)] = key
    body[0x28 + len(key):0x28 + len(key) + len(name)] = name
    body[0x28 + ml_off:0x28 + ml_off + len(metallib)] = metallib
    if refl:
        body[0x28 + refl_off:0x28 + refl_off + len(refl)] = refl
    body = bytes(body)
    framed = align8(len(body) + 32)
    rec = bytearray(framed)
    struct.pack_into('<QQQ', rec, 0, framed, len(body), xxh3(body))
    rec[24:24 + len(body)] = body
    struct.pack_into('<q', rec, framed - 8, -framed)
    return bytes(rec)


def derive_all(cache_dir: str, workdir: str):
    arena = open(os.path.join(cache_dir, 'stage_cache.bin'), 'rb').read()
    dxil = load_dxil(os.path.join(cache_dir, 'bytecode_cache.bin'))
    items, recs = [], []
    for off, framed, payload in walk(arena):
        stage, serial, _, ks = struct.unpack_from('<4I', arena, off + 24)
        key = arena[off + 24 + 0x28:off + 24 + 0x28 + ks]
        recs.append((off, framed, stage, serial, key))
        if stage in COMPILABLE:
            items.append((off, stage, key))
    compiled = compile_keys(items, dxil, workdir)
    return arena, recs, compiled


def cmd_validate(a):
    t = time.time()
    with tempfile.TemporaryDirectory() as wd:
        arena, recs, compiled = derive_all(a.cache_dir, wd)
    res = collections.Counter()
    for off, framed, stage, serial, key in recs:
        if stage not in COMPILABLE:
            res[(stage, 'not compilable (runtime state)')] += 1
            continue
        if off not in compiled:
            res[(stage, 'no DXIL / compile failed')] += 1
            continue
        sid, ml, rf = compiled[off]
        ok = build_record(stage, serial, key, sid, ml, rf) == arena[off:off + framed]
        res[(stage, 'identical' if ok else 'DIFFERENT')] += 1
    for k in sorted(res):
        print(f'stage {k[0]:>2}  {k[1]:32} {res[k]}')
    print(f'{time.time() - t:.1f}s')
    return 1 if any(v == 'DIFFERENT' for _, v in res) else 0


def cmd_rebuild(a):
    with tempfile.TemporaryDirectory() as wd:
        arena, recs, compiled = derive_all(a.cache_dir, wd)
    out = bytearray(arena[:24])
    derived = verbatim = 0
    for off, framed, stage, serial, key in recs:
        if off in compiled:
            sid, ml, rf = compiled[off]
            out += build_record(stage, serial, key, sid, ml, rf)
            derived += 1
        else:
            out += arena[off:off + framed]
            verbatim += 1
    out += b'\0' * (len(arena) - len(out))
    with open(a.out, 'wb') as f:
        f.write(out)
    print(f'records: derived offline={derived} copied verbatim={verbatim}; '
          f'file identical to original: {bytes(out) == arena}')


def cmd_mint(a):
    arena = open(os.path.join(a.cache_dir, 'stage_cache.bin'), 'rb').read()
    dxil = load_dxil(os.path.join(a.cache_dir, 'bytecode_cache.bin'))
    last = 24
    serial = collections.Counter()
    existing = set()
    for off, framed, payload in walk(arena):
        stage, s, _, ks = struct.unpack_from('<4I', arena, off + 24)
        serial[stage] = max(serial[stage], s)
        existing.add((stage, arena[off + 24 + 0x28:off + 24 + 0x28 + ks]))
        last = off + framed
    items = []
    for i, line in enumerate(open(a.keys)):
        if line.strip():
            j = json.loads(line)
            key = bytes.fromhex(j['key'])
            if (j['stage'], key) not in existing:
                items.append((i, j['stage'], key))
    with tempfile.TemporaryDirectory() as wd:
        compiled = compile_keys(items, dxil, wd)
    out = bytearray(arena[:last])
    minted = 0
    for tag, stage, key in items:
        if tag in compiled:
            serial[stage] += 1
            sid, ml, rf = compiled[tag]
            out += build_record(stage, serial[stage], key, sid, ml, rf)
            minted += 1
    logical_end = len(out)
    size = max(len(arena), (logical_end + 0xFFFF) & ~0xFFFF)
    out += b'\0' * (size - len(out))
    struct.pack_into('<QQQ', out, 0, size, logical_end, logical_end)
    with open(a.out, 'wb') as f:
        f.write(out)
    print(f'minted {minted}/{len(items)} new records; logical_end={logical_end} file={size}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    v = sub.add_parser('validate'); v.add_argument('--cache-dir', required=True)
    r = sub.add_parser('rebuild'); r.add_argument('--cache-dir', required=True); r.add_argument('--out', required=True)
    m = sub.add_parser('mint'); m.add_argument('--cache-dir', required=True); m.add_argument('--keys', required=True)
    m.add_argument('--out', required=True)
    a = ap.parse_args()
    return {'validate': cmd_validate, 'rebuild': cmd_rebuild, 'mint': cmd_mint}[a.cmd](a) or 0


if __name__ == '__main__':
    raise SystemExit(main())
