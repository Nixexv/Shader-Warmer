#!/usr/bin/env python3
"""Generate a warmgen plan that warms every shipped Overwatch shader individually (D3D11, GPTK 3.0-3).

Stage records are keyed by the shader alone (VS, CS) or by shader + render-target class (PS), and do
not depend on the partner shader or the input layout used to produce them.  So every shader is drawn
once per needed class with any compatible partner:

  CS   one Dispatch per compute shader
  PS   one Draw per (PS, RT class); classes are chosen per PS *shape* (which SV_Targets it writes and
       which of them are integer) from the table below, learned from real Overwatch play sessions
  VS   one Draw with a layout synthesized from its own input signature (the VS stage record is
       layout-independent) and a resource-light partner PS

  owplan.py --corpus DIR --out DIR [--classes top|full] [--stats-only]
"""
from __future__ import annotations
import argparse, collections, json, struct
from pathlib import Path

# RT class = (dsb, depthfb, intRT, unorm8) as D3DMetal writes it into the PS stage key.
# Per PS shape (target mask, integer-target mask): classes seen in 41 real OW caches, most frequent
# first.  "top" takes the entries before the '|' marker (>= ~90% of that shape's real keys), "full"
# takes them all.  Unseen shapes fall back to DEFAULT_CLASSES.
SHAPE_CLASSES = {
    (0x01, 0x00): [(0, 1, 0, 0), (0, 1, 0, 1), "|", (0, 0, 0, 0), (0, 0, 0, 1), (0, 1, 0, 3), (0, 1, 0, 2)],
    (0x0f, 0x08): [(0, 1, 8, 1), (0, 1, 0, 15)],
    (0x03, 0x00): [(0, 1, 0, 3), (0, 1, 0, 0), (1, 1, 0, 0), "|", (0, 0, 0, 2), (0, 1, 0, 2), (0, 0, 0, 0),
                   (1, 1, 0, 1), (0, 0, 0, 3)],
    (0x00, 0x00): [(0, 1, 0, 0), (0, 1, 0, 1)],
    (0x0f, 0x00): [(0, 1, 0, 17), (0, 1, 0, 15)],
    (0x07, 0x00): [(0, 1, 0, 7), (0, 1, 0, 0), "|", (0, 1, 0, 4), (0, 1, 8, 1)],
    (0x05, 0x00): [(0, 1, 0, 0), (0, 1, 0, 3)],
    (0x03, 0x01): [(0, 1, 1, 0), (0, 1, 0, 3)],
    (0x1f, 0x00): [(0, 1, 0, 17), (0, 1, 0, 31)],
    (0x3f, 0x08): [(0, 1, 0, 63), (0, 1, 8, 1)],
}
DEFAULT_CLASSES = [(0, 1, 0, 0), (0, 1, 0, 1)]

FORMAT_SIZE = {1: 16, 2: 16, 3: 16, 4: 16, 5: 12, 6: 12, 7: 12, 8: 12, 9: 8, 10: 8, 11: 8, 12: 8, 13: 8, 14: 8,
               15: 8, 16: 8, 17: 8, 18: 8, 19: 8, 20: 8, 21: 8, 22: 8, 23: 4, 24: 4, 25: 4, 26: 4, 27: 4, 28: 4,
               29: 4, 30: 4, 31: 4, 32: 4, 33: 4, 34: 4, 35: 4, 36: 4, 37: 4, 38: 4, 39: 4, 40: 4, 41: 4, 42: 4,
               43: 4, 44: 4, 45: 4, 46: 4, 47: 4, 48: 2, 49: 2, 50: 2, 51: 2, 52: 2, 53: 2, 54: 2, 55: 2, 56: 2,
               57: 2, 58: 2, 59: 2, 60: 1, 61: 1, 62: 1, 63: 1, 64: 1, 65: 1, 87: 4, 88: 4, 89: 4, 90: 4, 91: 4}
SYSTEM_INPUTS = {"SV_ISFRONTFACE", "SV_SAMPLEINDEX", "SV_COVERAGE", "SV_PRIMITIVEID", "SV_VERTEXID", "SV_INSTANCEID"}
COMPTYPE_FMT = {1: 3, 2: 4, 3: 2}  # DXBC component type (uint, int, float) -> R32G32B32A32_{UINT,SINT,FLOAT}


class Element(tuple):
    """(semantic, index, register, component type, component mask) that still unpacks as a 4-tuple
    for the older call sites."""
    def __new__(cls, name, index, reg, ct, mask):
        return super().__new__(cls, (name, index, reg, ct, mask))

    def __iter__(self):
        return iter(self[:4])

    def __len__(self):
        return 4


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
        no, si, sv, ct, reg, masks = struct.unpack_from("<6I", d, 8 + i * 24)
        # masks: low byte = components present, second byte = components read (PS) / unused (VS)
        used = (masks >> 8) & 0xFF
        els.append(Element(d[no:d.index(b"\0", no)].decode("latin1").upper(), si, reg, ct,
                           used if used else masks & 0xFF))
    return els


def resources(d: bytes | None, stage: int):
    """warmgen CB/SRV/SAMP lines from an RDEF chunk."""
    if not d:
        return []
    cb_count, cb_off, res_count, res_off = struct.unpack_from("<4I", d, 0)
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
    """Input layout from the VS's own input signature: each non-system input becomes a 4-component
    32-bit element in slot 0."""
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


def winp(p: Path) -> str:
    return "Z:" + str(Path(p).resolve()).replace("/", "\\")


def load(path: Path):
    c = chunks(path.read_bytes())
    return {"path": path, "in": signature(c.get(b"ISGN")), "out": signature(c.get(b"OSGN")), "res": c.get(b"RDEF")}


def ps_shape(ps):
    tm = sum(1 << reg for n, s, reg, ct in ps["out"] if n == "SV_TARGET")
    im = sum(1 << reg for n, s, reg, ct in ps["out"] if n == "SV_TARGET" and ct in (1, 2))
    return tm, im


def shape_classes(shape, mode):
    table = SHAPE_CLASSES.get(shape, DEFAULT_CLASSES if shape[0] & 1 else DEFAULT_CLASSES[:1])
    if mode == "top" and "|" in table:
        table = table[:table.index("|")]
    return [c for c in table if c != "|"]


class Planner:
    """Emits warmgen recipes.  PS draws need a carrier VS whose outputs cover the PS inputs; VS draws
    need a partner PS whose inputs the VS outputs cover."""

    def __init__(self, vs_list, ps_list):
        self.recipes = []
        self.rid = 0
        self.carrier_by_out = {}
        for v in vs_list:
            self.carrier_by_out.setdefault(self.sig_out(v), v)
        self.fallback = max(self.carrier_by_out.values(), key=lambda v: len(v["out"]), default=None)
        self.ps_by_in = {}
        for p in sorted(ps_list, key=lambda p: (p["res"] is not None, len(p["res"] or b""))):
            self.ps_by_in.setdefault(self.sig_in(p), p)
        self.carrier_for_in, self.partner_for_out, self.compat = {}, {}, {}

    @staticmethod
    def sig_out(vs):
        return frozenset((n, si, reg) for n, si, reg, ct in vs["out"])

    @staticmethod
    def sig_in(ps):
        return frozenset((n, si, reg) for n, si, reg, ct in ps["in"] if n not in SYSTEM_INPUTS)

    def covers(self, out_sig, in_sig):
        k = (out_sig, in_sig)
        if k not in self.compat:
            have = {(n, si): reg for n, si, reg in out_sig}
            self.compat[k] = all(have.get((n, si)) == reg for n, si, reg in in_sig)
        return self.compat[k]

    def emit(self, vs, ps, cls):
        dsb, depth, intmask, rtmask = cls
        self.rid += 1
        lines = [f"RECIPE ow_{self.rid:07d}", f"VS {winp(vs['path'])}", f"PS {winp(ps['path'])}"]
        lines += layout_lines(synth_layout(vs))
        lines += resources(vs["res"], 0) + resources(ps["res"], 1)
        lines += [f"RTMASK {rtmask}", f"INTMASK {intmask}", f"DSB {dsb}", f"DEPTH {depth}", "TOPO 4"]
        self.recipes.append("\n".join(lines))

    def ps(self, ps, classes):
        si = self.sig_in(ps)
        if si not in self.carrier_for_in:
            self.carrier_for_in[si] = next((v for o, v in self.carrier_by_out.items() if self.covers(o, si)), None)
        car = self.carrier_for_in[si] or self.fallback
        if car is None:
            return 0
        for c in classes:
            self.emit(car, ps, c)
        return len(classes)

    def vs(self, vs):
        so = self.sig_out(vs)
        if so not in self.partner_for_out:
            self.partner_for_out[so] = next((p for i, p in self.ps_by_in.items() if self.covers(so, i)), None)
        partner = self.partner_for_out[so]
        if partner is None:
            return 0
        self.emit(vs, partner, (0, 1, 0, 0))
        return 1


def write_plan(out: Path, recipes, cs_paths, stats):
    out.mkdir(parents=True, exist_ok=True)
    text = "\n".join(recipes) + "\n" + "".join(f"CS {winp(p)}\n" for p in cs_paths)
    (out / "prewarm_plans.txt").write_text(text)
    (out / "plan_stats.json").write_text(json.dumps(stats, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--classes", choices=("top", "full"), default="top")
    ap.add_argument("--stats-only", action="store_true")
    a = ap.parse_args()

    vs_list = [load(p) for p in sorted((a.corpus / "vs").glob("*.dxbc"))]
    ps_list = [load(p) for p in sorted((a.corpus / "ps").glob("*.dxbc"))]
    cs_paths = sorted((a.corpus / "cs").glob("*.dxbc"))
    pl = Planner(vs_list, ps_list)
    shapes = collections.Counter()
    ps_miss = 0
    for ps in ps_list:
        shape = ps_shape(ps)
        shapes[shape] += 1
        if not pl.ps(ps, shape_classes(shape, a.classes)):
            ps_miss += 1
    ps_draws = len(pl.recipes)
    vs_miss = sum(1 for vs in vs_list if not pl.vs(vs))
    stats = {"classes": a.classes, "ps_shaders": len(ps_list), "ps_draws": ps_draws, "ps_without_carrier": ps_miss,
             "vs_shaders": len(vs_list), "vs_draws": len(pl.recipes) - ps_draws, "vs_without_partner": vs_miss,
             "cs_dispatches": len(cs_paths),
             "ps_shapes": {f"{t:#04x}/{i:#04x}": n for (t, i), n in shapes.most_common()}}
    print(json.dumps(stats, indent=1))
    if not a.stats_only:
        write_plan(a.out, pl.recipes, cs_paths, stats)


if __name__ == "__main__":
    main()
