#!/usr/bin/env python3
"""Generate a warmgen2 plan that warms every shipped Deadlock shader individually.

Stage records are keyed by the shader alone (VS), shader + render-target class (PS),
or shader (CS), and were shown to be byte-identical regardless of the partner shader
or layout used to produce them.  So instead of recipes observed in play, each shader is
drawn once per needed class with any compatible partner:

  CS   one Dispatch per compute shader
  PS   one Draw per (PS, RT class) with a carrier VS whose outputs cover the PS inputs
       and which has a real game input layout
  VS   one Draw per (VS, real layout covering its inputs) with a carrier PS, which also
       produces the stage-in record for that layout

  gen_warmall_plan.py --corpus DIR --layouts layout_library.json --out plan_dir
                      [--classes top5|all] [--max-layouts-per-vs 4] [--stats-only]
"""
from __future__ import annotations
import argparse, collections, json, struct
from pathlib import Path

# (depth, rtmask) as warmgen writes them; key (depthfb, unorm8) equals this pair
CLASSES_ALL = [(1, 0), (0, 1), (1, 6), (0, 0), (1, 1), (0, 3), (1, 3), (0, 2)]
CLASSES_TOP5 = [(1, 0), (0, 1), (1, 6), (0, 0), (1, 1)]
FORMAT_SIZE = {1: 16, 2: 16, 3: 16, 4: 16, 5: 12, 6: 12, 7: 12, 8: 12, 9: 8, 10: 8, 11: 8, 12: 8, 13: 8, 14: 8,
               15: 8, 16: 8, 17: 8, 18: 8, 19: 8, 20: 8, 21: 8, 22: 8, 23: 4, 24: 4, 25: 4, 26: 4, 27: 4, 28: 4,
               29: 4, 30: 4, 31: 4, 32: 4, 33: 4, 34: 4, 35: 4, 36: 4, 37: 4, 38: 4, 39: 4, 40: 4, 41: 4, 42: 4,
               43: 4, 44: 4, 45: 4, 46: 4, 47: 4, 48: 2, 49: 2, 50: 2, 51: 2, 52: 2, 53: 2, 54: 2, 55: 2, 56: 2,
               57: 2, 58: 2, 59: 2, 60: 1, 61: 1, 62: 1, 63: 1, 64: 1, 65: 1, 87: 4, 88: 4, 89: 4, 90: 4, 91: 4}
SYSTEM_INPUTS = {"SV_ISFRONTFACE", "SV_SAMPLEINDEX", "SV_COVERAGE", "SV_PRIMITIVEID", "SV_VERTEXID", "SV_INSTANCEID"}
# DXBC component type (1=uint,2=int,3=float) -> a 4-component 32-bit DXGI format that covers any mask
COMPTYPE_FMT = {1: 3, 2: 4, 3: 2}  # R32G32B32A32_{UINT, SINT, FLOAT}


def chunks(b: bytes) -> dict:
    n = struct.unpack_from("<I", b, 28)[0]
    out = {}
    for o in struct.unpack_from("<%dI" % n, b, 32):
        out[b[o:o + 4]] = b[o + 8:o + 8 + struct.unpack_from("<I", b, o + 4)[0]]
    return out


def signature(d: bytes | None):
    if not d:
        return []
    els = []
    for i in range(struct.unpack_from("<I", d, 0)[0]):
        no, si, sv, ct, reg = struct.unpack_from("<5I", d, 8 + i * 24)
        els.append((d[no:d.index(b"\0", no)].decode("latin1").upper(), si, reg, ct))
    return els


def resources(d: bytes | None, stage: int):
    """warmgen CB/SRV/SAMP lines from an RDEF chunk."""
    if not d:
        return []
    cb_count, cb_off, res_count, res_off = struct.unpack_from("<4I", d, 0)
    # RD11 header (SM5) declares the binding record size at +0x28: 32 for SM5.0, 40 for SM5.1
    rsize = struct.unpack_from("<I", d, 0x28)[0] if d[28:32] == b"RD11" else 32
    cb_size = {}
    for i in range(cb_count):
        name_off, _, _, size, _, _ = struct.unpack_from("<6I", d, cb_off + i * 24)
        cb_size[d[name_off:d.index(b"\0", name_off)]] = size
    lines = []
    for i in range(res_count):
        e = struct.unpack_from("<8I", d, res_off + i * rsize)
        name = d[e[0]:d.index(b"\0", e[0])]
        rtype, dim, point, count = e[1], e[3], e[5], max(e[6], 1)
        limit = {0: 14, 3: 16}.get(rtype, 128)       # D3D11 slot counts: CB 14, sampler 16, SRV 128
        for j in range(min(count, max(0, limit - point))):
            slot = point + j
            if rtype == 0:
                lines.append(f"CB {stage} {slot} {max(16, (cb_size.get(name, 256) + 15) // 16 * 16)}")
            elif rtype == 3:
                lines.append(f"SAMP {stage} {slot}")
            elif rtype in (2, 5, 7):
                if rtype in (5, 7) or dim in (1, 11):
                    lines.append(f"SRV {stage} {slot} STRUCTURED BUFFER")
                else:
                    lines.append(f"SRV {stage} {slot} TEXTURE {'TEXTURECUBE' if dim in (9, 10) else 'TEXTURE2D'}")
    return lines


def synth_layout(vs):
    """A synthetic input layout built from the VS's own input signature — lets a VS with no
    harvested real layout still be warmed (its VS stage record is layout-independent). Each
    non-system input becomes a 4-component 32-bit element in slot 0."""
    els, off = [], 0
    for name, si, reg, ct in vs["in"]:
        if name in SYSTEM_INPUTS:
            continue
        els.append({"semantic": name, "index": si, "format": COMPTYPE_FMT.get(ct, 2),
                    "slot": 0, "offset": off, "class": 0, "step": 0})
        off += 16
    return {"elements": els}


def layout_lines(layout):
    lines, stride = [], collections.defaultdict(int)
    for e in layout["elements"]:
        lines.append(f"EL {e['semantic']} {e['index']} {e['format']} {e['slot']} {e['offset']} {e['class']} {e['step']}")
        stride[e["slot"]] = max(stride[e["slot"]], e["offset"] + FORMAT_SIZE.get(e["format"], 4))
    for slot, s in sorted(stride.items()):
        lines.append(f"SLOT {slot} {(s + 3) // 4 * 4}")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--layouts", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--classes", choices=("top5", "all", "full"), default="top5")
    ap.add_argument("--max-layouts-per-vs", type=int, default=4)
    ap.add_argument("--stats-only", action="store_true")
    a = ap.parse_args()

    import time
    t0 = time.time()
    layouts = json.loads(a.layouts.read_text())
    for L in layouts:
        L["sem"] = {(e["semantic"].upper(), e["index"]) for e in L["elements"]}
    classes = CLASSES_ALL if a.classes in ("all", "full") else CLASSES_TOP5
    full = a.classes == "full"   # warm EVERY shipped shader: synth layouts + fallback carriers, no skips

    shaders = {"VS": [], "PS": [], "CS": []}
    for stage, sub in (("VS", "vs"), ("PS", "ps"), ("CS", "cs")):
        d = a.corpus / sub
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.dxbc")):
            c = chunks(path.read_bytes())
            shaders[stage].append({
                "path": path, "in": signature(c.get(b"ISGN")), "out": signature(c.get(b"OSGN")),
                "res": c.get(b"RDEF")})

    def need_of(vs):
        return frozenset((n, si) for n, si, reg, ct in vs["in"] if n not in SYSTEM_INPUTS)

    layout_cache = {}

    def vs_layouts(vs):
        need = need_of(vs)
        if need not in layout_cache:
            exact = [L for L in layouts if L["sem"] == need]
            cover = [L for L in layouts if need <= L["sem"] and L["sem"] != need]
            layout_cache[need] = (exact + cover)[:a.max_layouts_per_vs]
        return layout_cache[need]

    def sig_out(vs):
        return frozenset((n, si, reg) for n, si, reg, ct in vs["out"])

    def sig_in(ps):
        return frozenset((n, si, reg) for n, si, reg, ct in ps["in"] if n not in SYSTEM_INPUTS)

    compat_cache = {}

    def covers_sig(out_sig, in_sig):
        k = (out_sig, in_sig)
        if k not in compat_cache:
            have = {(n, si): reg for n, si, reg in out_sig}
            compat_cache[k] = all(have.get((n, si)) == reg for n, si, reg in in_sig)
        return compat_cache[k]

    # carriers: one VS with a real layout per distinct output signature
    carrier_by_out = {}
    for v in shaders["VS"]:
        ls = vs_layouts(v)
        if ls and sig_out(v) not in carrier_by_out:
            carrier_by_out[sig_out(v)] = (v, ls[0])
    # full mode: a universal fallback carrier (the richest-output VS) for PS whose inputs no
    # real carrier covers — the PS stage record is partner-independent, so any valid draw warms it.
    fallback_carrier = max(carrier_by_out.values(), key=lambda c: len(sig_out(c[0])), default=None) if full else None
    # partner PS for VS draws: one resource-light PS per distinct input signature
    ps_by_in = {}
    for p in sorted(shaders["PS"], key=lambda p: (p["res"] is not None, len(p["res"] or b""))):
        ps_by_in.setdefault(sig_in(p), p)

    ps_carrier_miss = 0
    recipes = []
    rid = 0

    def emit(vs, ps, layout, depth, rtmask):
        nonlocal rid
        rid += 1
        lines = [f"RECIPE warmall_{rid:06d}", f"VS {winp(vs['path'])}", f"PS {winp(ps['path'])}"]
        lines += layout_lines(layout)
        lines += resources(vs["res"], 0) + resources(ps["res"], 1)
        lines += [f"RTMASK {rtmask}", f"DEPTH {depth}", "TOPO 4"]
        recipes.append("\n".join(lines))

    carrier_for_in = {}
    for ps in shaders["PS"]:
        si = sig_in(ps)
        if si not in carrier_for_in:
            carrier_for_in[si] = next((c for o, c in carrier_by_out.items() if covers_sig(o, si)), None)
        car = carrier_for_in[si] or fallback_carrier
        if car is None:
            ps_carrier_miss += 1
            continue
        tmask = sum(1 << reg for n, s, reg, ct in ps["out"] if n == "SV_TARGET")
        for depth, rtmask in classes:
            if rtmask & ~tmask:
                continue
            emit(car[0], ps, car[1], depth, rtmask)
    ps_draws = len(recipes)

    vs_no_layout = vs_no_partner = 0
    partner_for_out = {}
    for vs in shaders["VS"]:
        ls = vs_layouts(vs)
        if not ls and full:
            ls = [synth_layout(vs)]   # cover VS that has no harvested real layout
        if not ls:
            vs_no_layout += 1
            continue
        so = sig_out(vs)
        if so not in partner_for_out:
            partner_for_out[so] = next((p for i, p in ps_by_in.items() if covers_sig(so, i)), None)
        partner = partner_for_out[so]
        if partner is None:
            vs_no_partner += 1
            continue
        for L in ls:
            emit(vs, partner, L, 1, 0)
    vs_draws = len(recipes) - ps_draws
    print(f"generated in {time.time() - t0:.1f}s")

    stats = {"ps_shaders": len(shaders["PS"]), "ps_without_carrier": ps_carrier_miss, "ps_draws": ps_draws,
             "vs_shaders": len(shaders["VS"]), "vs_without_real_layout": vs_no_layout,
             "vs_without_partner_ps": vs_no_partner, "vs_draws": vs_draws,
             "cs_dispatches": len(shaders["CS"]), "classes": a.classes}
    print(json.dumps(stats, indent=1))
    if a.stats_only:
        return
    a.out.mkdir(parents=True, exist_ok=True)
    text = "\n".join(recipes) + "\n" + "".join(f"CS {winp(c['path'])}\n" for c in shaders["CS"])
    (a.out / "prewarm_plans.txt").write_text(text)
    (a.out / "plan_stats.json").write_text(json.dumps(stats, indent=1))


def winp(p: Path) -> str:
    return "Z:" + str(p.resolve()).replace("/", "\\")


if __name__ == "__main__":
    main()
