#!/usr/bin/env python3
"""Install a warm-all cache built by run_ow_warm.py into Overwatch's D3DMetal namespace.

Dry run by default: prints every check and what it would do.  With --apply it backs up the live
namespace (verified SHA-256), copies the built cache over it and writes a record-count snapshot that
diff_ow.py compares against after a play session.

  install_ow.py [--built NS_DIR] [--apply]
  install_ow.py --restore BACKUP_DIR [--apply]
"""
from __future__ import annotations
import argparse, hashlib, json, mmap, shutil, struct, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
FILES = ("bytecode_cache.bin", "pipeline_cache.bin", "rootsignature_cache.bin", "stage_cache.bin", "version.bin")
STAGE_KIND = {1: "VS", 2: "PS", 8: "CS", 16: "StageIn", 7: "GS", 15: "StreamOut"}


def d3dm_root() -> Path:
    return Path(subprocess.run(["getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True, text=True).stdout.strip()) / "d3dm"


def _family_name() -> str:
    """Apple10 is this developer's Mac, not everyone's: an M1 writes Apple7 and never reads Apple10."""
    sys.path.insert(0, str(HERE.parent))
    import gptk_pick
    return gptk_pick.family_dir_name(d3dm_root())


LIVE_PARENT = d3dm_root() / "Overwatch.exe/shaders.cache"
LIVE = LIVE_PARENT / _family_name()


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 24), b""):
            h.update(b)
    return h.hexdigest()


def arena(p: Path):
    """(consistent, records, per-kind counts for stage_cache)"""
    if p.stat().st_size < 24:
        return False, 0, {}
    f = p.open("rb")
    b = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    size, end, mirror = struct.unpack_from("<QQQ", b, 0)
    ok = size == len(b) and end == mirror and end <= size
    off, n, kinds = 24, 0, {}
    while off + 32 <= end:
        fr, pz = struct.unpack_from("<QQ", b, off)
        if fr == 0 or not (pz + 32 <= fr <= pz + 39) or off + fr > end or struct.unpack_from("<q", b, off + fr - 8)[0] != -fr:
            ok = False
            break
        if p.name == "stage_cache.bin":
            k = STAGE_KIND.get(struct.unpack_from("<I", b, off + 24)[0], "other")
            kinds[k] = kinds.get(k, 0) + 1
        n += 1
        off += fr
    return ok, n, kinds


def snapshot(ns: Path) -> dict:
    out = {"namespace": str(ns), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    for f in FILES[:-1]:
        p = ns / f
        if p.exists():
            ok, n, kinds = arena(p)
            out[f] = {"consistent": ok, "records": n, "bytes": p.stat().st_size, **({"kinds": kinds} if kinds else {})}
    vb = ns / "version.bin"
    if vb.exists():
        out["version_bin_sha256"] = sha(vb)
        out["target_triple"] = vb.read_bytes()[0x4c:].decode("latin1")
    return out


def game_running() -> bool:
    return subprocess.run(["pgrep", "-if", r"Overwatch\.exe"], capture_output=True).returncode == 0


def backup_live(apply: bool) -> Path | None:
    dst = LAB / "live_cache_backups" / f"ow_live_backup_prewarmall_{time.strftime('%Y%m%d_%H%M%S')}"
    if not LIVE.exists():
        print("live namespace does not exist yet; nothing to back up")
        return None
    print(f"backup  {LIVE} -> {dst}")
    if apply:
        shutil.copytree(LIVE_PARENT, dst / "shaders.cache", copy_function=shutil.copy2)
        sums = [f"{sha(p)}  {p.relative_to(dst)}" for p in sorted((dst / "shaders.cache").rglob("*")) if p.is_file()]
        (dst / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n")
        for line in sums:
            h, rel = line.split("  ", 1)
            src = LIVE_PARENT.parent / rel
            if src.exists() and sha(src) != h:
                raise SystemExit(f"backup verification failed for {rel}")
        print(f"backup verified ({len(sums)} files)")
    return dst


def copy_into_live(src: Path, apply: bool):
    for f in FILES:
        print(f"copy    {src / f} -> {LIVE / f}")
    if apply:
        LIVE.mkdir(parents=True, exist_ok=True)
        for f in FILES:
            tmp = LIVE / (f + ".installing")
            subprocess.run(["cp", "-c", str(src / f), str(tmp)], check=True)   # APFS clone
            tmp.replace(LIVE / f)
        for f in FILES:
            if sha(LIVE / f) != sha(src / f):
                raise SystemExit(f"installed {f} does not match the source")
        print("installed files verified")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--built", type=Path)
    ap.add_argument("--restore", type=Path, help="backup dir made by this script")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    mode = "APPLY" if a.apply else "DRY RUN (add --apply to do it)"
    print(f"== Overwatch warm-all install: {mode}")

    if game_running():
        raise SystemExit("Overwatch is running; quit it first")
    extra = [d.name for d in LIVE_PARENT.iterdir() if d.is_dir() and d.name != LIVE.name] if LIVE_PARENT.exists() else []
    if extra:
        print(f"note: other GPU-family dirs present (game reads _0 only): {extra}")

    if a.restore:
        src = a.restore / "shaders.cache" / LIVE.name
        if not all((src / f).exists() for f in FILES):
            raise SystemExit(f"{src} is not a complete backup")
        copy_into_live(src, a.apply)
        return

    built = a.built or (d3dm_root() / (HERE / "full_ns_name.txt").read_text().strip() / "shaders.cache" / LIVE.name)
    missing = [f for f in FILES if not (built / f).exists()]
    if missing:
        raise SystemExit(f"built cache {built} lacks {missing}")
    snap = snapshot(built)
    print(json.dumps(snap, indent=1))
    bad = [f for f in FILES[:-1] if not snap[f]["consistent"]]
    if bad:
        raise SystemExit(f"built arenas inconsistent: {bad}")
    ref = LIVE / "version.bin"
    if ref.exists() and (built / "version.bin").read_bytes()[:0x4c] != ref.read_bytes()[:0x4c]:
        raise SystemExit("built version.bin D3DMetal flags differ from the live Overwatch cache (different GPTK?)")
    if ref.exists():
        print(f"live cache target triple: {ref.read_bytes()[0x4c:].decode('latin1')}  "
              f"built: {snap['target_triple']}  (live is rebuilt by D3DMetal anyway if its triple is stale)")
        print("live cache now:", json.dumps(snapshot(LIVE)))

    backup = backup_live(a.apply)
    copy_into_live(built, a.apply)
    if a.apply:
        s = snapshot(LIVE)
        s["backup"] = str(backup) if backup else None
        s["built_from"] = str(built)
        out = LAB / "reports" / f"ow_postinstall_snapshot_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(s, indent=1))
        print(f"snapshot -> {out}")
        print("NEXT: in GameHub set Overwatch to GPTK 3.0-3 + Wine Proton 10, play, quit, then run diff_ow.py")


if __name__ == "__main__":
    main()
