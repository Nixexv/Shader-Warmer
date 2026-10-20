#!/usr/bin/env python3
"""Warm Deadlock's D3DMetal shader cache for every hero — without launching the game.

Pipeline, all on the user's own machine:
  1. extract every shader (DXBC) from the installed game's VPKs  (extract_shaders.py)
  2. build a warm plan for all shaders and render-target classes  (gen_plan.py)
  3. replay it through a tiny generator named after the game so D3DMetal builds and
     persists the cache on the user's own GPU and driver.

Nothing prebuilt is shipped: shaders come from the user's install (so they match the
installed game version) and every cache record is produced locally.

    python3 warm.py            # do it
    python3 warm.py --dry-run  # detect runtime + game, extract, build plan, but don't warm
    python3 warm.py --keep     # leave the work directory behind for inspection
"""
import argparse, json, os, re, shutil, signal, struct, subprocess, sys, time
from pathlib import Path


class CancelledRun(Exception):
    pass


def _cancel_run(_signum, _frame):
    raise CancelledRun()


_cancel_scratch = None


def cleanup_cancelled_run():
    """Drop the scratch namespaces after the GUI cancels a run.

    The warm never writes into the game's cache before the final renames (and the GUI blocks
    cancel during those), so a cancel has nothing to restore — the game's cache is as it was.
    """
    global _cancel_scratch
    for scratch in (_cancel_scratch or []):
        shutil.rmtree(scratch, ignore_errors=True)
    _cancel_scratch = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gptk_pick  # noqa: E402
import metal_cache  # noqa: E402

HERE = Path(__file__).resolve().parent
HOME = Path.home()
GAMEHUB = HOME / "Library/Application Support/com.gamemac.www/wine-engine"
EXE = "deadlock.exe"
CHUNK = 2000
GAME_GLOBS = [
    "GameHub/steamapps/common/Deadlock/game",
    "Library/Application Support/com.gamemac.www/wine-engine/containers/virtual_containers/*/drive_c/Program Files (x86)/Steam/steamapps/common/Deadlock/game",
]


def load_config(path):
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, _, v = line.partition("=")
                if v.strip():
                    out[k.strip()] = v.strip()
    return out


def find_game(cfg):
    if cfg.get("game"):
        return Path(cfg["game"])
    for g in GAME_GLOBS:
        for p in sorted(HOME.glob(g)):
            if (p / "citadel/shaders_pc_dir.vpk").exists():
                return p
    # a library on an external drive (/Volumes/T5/steamGames) is only known to GameHub's bindings
    for lib in gptk_pick.steam_libraries():
        p = lib / "steamapps/common/Deadlock/game"
        if gptk_pick.probe(p / "citadel/shaders_pc_dir.vpk"):
            return p
    return None


def private_prefix(wine_bin: Path, fallback: Path) -> Path:
    """A throwaway copy of the Wine installation's own template prefix.

    Warming inside the game's container would run wine in the player's real prefix — its
    registry, and a wineboot auto-update that can take ten minutes.  The Unity and Overwatch
    engines have always used a private clone; Deadlock now does the same.  It lives beside the
    work dir, so the one-time wine first boot is paid once, not every warm.
    """
    template = wine_bin.parent.parent / "prefix"
    if not (template / "system.reg").exists():
        return fallback
    pfx = Path.home() / "Library/Caches/DeadlockWarmer" / ("prefix_" + wine_bin.parent.parent.name)
    if not pfx.exists():
        pfx.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-c", "-R", str(template), str(pfx)], check=True)
        (pfx / ".update-timestamp").write_text("disable")
    dos = pfx / "dosdevices"
    dos.mkdir(exist_ok=True)
    for drive, target in (("c:", "../drive_c"), ("z:", "/")):
        if not (dos / drive).is_symlink():
            (dos / drive).symlink_to(target)
    return pfx


def candidates(cfg):
    wines = sorted(GAMEHUB.glob("containers/wine_installations/*/bin/wine"))
    gptks = [p for p in sorted(GAMEHUB.glob("downloads/gptk-*")) if (p / "external/D3DMetal.framework").is_dir()]
    prefixes = [p for p in sorted(GAMEHUB.glob("containers/virtual_containers/*")) if (p / "user.reg").is_file()]
    if cfg.get("wine"):
        wines = [Path(cfg["wine"])]
    if cfg.get("gptk"):
        gptks = [Path(cfg["gptk"])]
    if cfg.get("prefix"):
        prefixes = [Path(cfg["prefix"])]
    if not (wines and gptks and prefixes):
        return [], "Окружение GPTK не найдено под GameHub — заполни config.txt"
    # prefer the GPTK that wrote the game's cache, then the one GameHub configured, then gptk-4.0-2
    want = wanted_gptk()
    gptks.sort(key=lambda p: (0 if want and p == want else 1, 0 if "gptk-4.0-2" in str(p) else 1))
    return [(w, g, x) for g in gptks for w in wines for x in prefixes], None


def game_gptk():
    """The GPTK that wrote the game's own cache — the only build whose records the game will load.

    GameHub lets every game pick its own graphics stack, so the newest installed GPTK is not
    necessarily the one Deadlock runs under; version.bin settles it.
    """
    gptks = [p for p in sorted(GAMEHUB.glob("downloads/gptk-*")) if (p / "external/D3DMetal.framework").is_dir()]
    fam = family_dir(cache_root_for(EXE))
    return gptk_pick.gptk_for_cache(gptks, fam) if fam.exists() else None


def wanted_gptk():
    return game_gptk() or configured_gptk()


def gptk_mismatch(gptk_path):
    want = wanted_gptk()
    if want:
        return Path(gptk_path).resolve() != Path(want).resolve()
    return gptk_version(gptk_path) != "4.0-2"


def cache_root_for(exe):
    try:
        root = subprocess.run(["getconf", "DARWIN_USER_CACHE_DIR"], text=True, capture_output=True, timeout=10).stdout.strip()
    except Exception:
        root = ""
    return Path(root or "/private/var/folders") / "d3dm" / exe / "shaders.cache"


def gptk_version(gptk_path):
    m = re.search(r"gptk-([0-9.\-]+)", str(gptk_path))
    return m.group(1) if m else "?"


def configured_gptk():
    """The GPTK GameHub has set for Deadlock (app 1422450), if any."""
    for p in sorted((GAMEHUB.parent / "gamehub/game-settings").glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        if d.get("key", {}).get("platform_app_id") == "1422450":
            name = d.get("settings", {}).get("graphics_stack", {}).get("component_config", {}).get("name")
            if name:
                hit = list(GAMEHUB.glob(f"downloads/{name}"))
                return hit[0] if hit else None
    return None


def family_dir(cache):
    """The MTLGPUFamilyApple<N>_0 folder D3DMetal uses for THIS Mac's GPU (N differs per chip:
    M1=7, M2=8, M3=9, M4/M5=10). Pick the existing one; otherwise ask the machine — assuming
    Apple10 would point an M1 at a folder its D3DMetal never reads."""
    if cache and cache.exists():
        found = sorted(cache.glob("MTLGPUFamilyApple*_0"))
        if found:
            return found[0]
    return cache / gptk_pick.family_dir_name(cache_root_for(EXE).parent.parent if cache else None)


def cache_root():
    try:
        root = subprocess.run(["getconf", "DARWIN_USER_CACHE_DIR"], text=True, capture_output=True, timeout=10).stdout.strip()
    except Exception:
        root = ""
    return Path(root or "/private/var/folders") / "d3dm" / EXE / "shaders.cache"


def records(path):
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if len(data) < 24:
        return 0
    end = struct.unpack_from("<Q", data, 8)[0]
    if not 24 <= end <= len(data):
        return 0
    off, n = 24, 0
    while off + 32 <= end:
        framed, payload = struct.unpack_from("<QQ", data, off)
        if framed == 0 or not (payload + 32 <= framed <= payload + 39) or off + framed > end:
            break
        n += 1
        off += framed
    return n


def cache_counts(cache):
    """Records in the arenas that matter. stage_cache is the one that removes the
    first-use shader-compile stall (a stage record alone: 0.24 ms vs 8.6 ms cold);
    pipeline_cache is thin bookkeeping and does NOT persist from a zero-vertex draw on
    some GPUs — Apple7/M1 keeps ~0 graphics pipelines yet a full 1.4 GB stage_cache.
    So progress and success are judged by stage records, not pipelines."""
    fam = family_dir(cache)
    return {name: records(fam / (name + "_cache.bin")) for name in ("stage", "pipeline")}


def stage_size_mb(cache):
    """Cheap growth indicator for the per-chunk log: stat() the stage arena, no read."""
    try:
        return (family_dir(cache) / "stage_cache.bin").stat().st_size / 2**20
    except OSError:
        return 0.0


STAMP_FILE = Path.home() / "Library/Application Support/DeadlockWarmer/warm_stamp.json"


def game_fingerprint(game):
    """Size+mtime of the game's shader VPKs — changes when the game is updated, so we can tell
    a warmed cache has gone stale after a patch."""
    if not game:
        return ""
    parts = []
    for vpk in sorted(Path(game).glob("*/shaders_pc_dir.vpk")):
        try:
            st = vpk.stat()
            parts.append("%s:%d:%d" % (vpk.parent.name, st.st_size, int(st.st_mtime)))
        except OSError:
            pass
    return "|".join(parts)


def read_stamps():
    try:
        return json.loads(STAMP_FILE.read_text())
    except Exception:
        return {}


def write_stamp(game, shaders):
    try:
        d = read_stamps()
        d[str(game)] = {"fp": game_fingerprint(game), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "shaders": shaders}
        STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STAMP_FILE.write_text(json.dumps(d))
    except Exception:
        pass


def cache_fingerprint(cache):
    """version.bin signature + size + inode of the arenas — so a reset (D3DMetal recreating
    files, or the version signature changing) is visible in the per-chunk log."""
    fam = family_dir(cache)
    try:
        vb = fam / "version.bin"
        sig = vb.read_bytes()[:8].hex()
        ino = fam.stat().st_ino
        return "ver=%s dir_ino=%d" % (sig, ino)
    except OSError:
        return "ver=none"


def start_oslog(logdir):
    """Stream D3DMetal's OWN unified-log messages about the shader cache into a file, so a
    reset shows its reason verbatim: 'Clearing shader cache' preceded by 'Cache version file
    ... mismatch' or 'Failed to parse <arena> cache'. These are os_log (%{darwin.errno}d),
    not stderr, which is why the warmgen stderr never showed them. Best-effort; needs no sudo."""
    import datetime as _dt
    try:
        path = logdir / ("d3dmetal_oslog_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
        # Capture the WHOLE D3DMetal subsystem so the reset REASON logged next to
        # "Clearing shader cache" is caught, plus any cache message from elsewhere.
        pred = ('subsystem == "D3DMetal" OR ' + ' OR '.join('eventMessage CONTAINS[c] "%s"' % s for s in
                ("shader cache", "Cache version", "Cache mismatch", "parse stage cache",
                 "parse pipeline cache", "parse bytecode cache")))
        f = open(path, "w", encoding="utf-8")
        p = subprocess.Popen(["log", "stream", "--style", "compact", "--info",
                              "--predicate", pred], stdout=f, stderr=subprocess.STDOUT)
        return p, f, path
    except Exception:
        return None, None, None


def stop_oslog(p, f):
    if p:
        try:
            p.terminate(); p.wait(timeout=5)
        except Exception:
            try: p.kill()
            except Exception: pass
    if f:
        try: f.close()
        except Exception: pass


def winpath(p):
    return "Z:" + str(Path(p).resolve()).replace("/", "\\")



def run_warmgen(wine, gptk, prefix, plan_dir, exe_name, work, cache, progress_cb=None, timeout=7200, log=None,
                draw_vertices=0, dispatch=0):
    """Run one warmgen process. Returns dict(ok, stray, recorded, skipped, cs, rc). Logs stderr/tail.
    draw_vertices/dispatch=0 = no-GPU (Apple10 persists from that); 3/1 = real GPU draws, needed on
    Apple7/M1 where a zero-vertex draw persists nothing (at the cost of the old GPU-hang risk)."""
    import re as _re, threading
    wine = gptk_pick.warm_wine(wine, log)     # keeps the warm's GPU code out of the games' cache
    gen = work / exe_name
    shutil.copy2(HERE / "warmgen.exe", gen)
    env = os.environ.copy()
    env.update({"WINEPREFIX": str(prefix), "WINEDLLPATH": str(gptk / "wine"),
                "WINE_GPTK_LIBD3DSHARED_PATH": str(gptk / "external/libd3dshared.dylib"),
                "WINEARCH": "win64", "WINEMSYNC": "1", "WINEDEBUG": "-all",
                "D3DM_NO_WINDOW": "1", "WARMGEN_DRAW_VERTICES": str(draw_vertices),
                "WARMGEN_DISPATCH": str(dispatch)})
    fams_before = sorted(p.name for p in cache.iterdir()) if (cache and cache.exists()) else []
    proc = subprocess.Popen([str(wine), str(gen), winpath(plan_dir), "40"],
                            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    res = {"ok": False, "recorded": 0, "skipped": 0, "cs": 0, "rc": None, "stray": []}
    err_tail = []
    # D3DMetal may print the cache-reset reason to stderr (it uses both stderr and os_log);
    # surface any such line immediately, not just in the tail window.
    hit = _re.compile(r"cach|shader cache|version file|parse (?:stage|pipeline|bytecode)|clearing", _re.I)
    def drain_err():
        for e in proc.stderr:
            e = e.rstrip()
            err_tail.append(e)
            if len(err_tail) > 40:
                err_tail.pop(0)
            if log and hit.search(e):
                log("   warmgen stderr HIT: " + e)
    t = threading.Thread(target=drain_err, daemon=True); t.start()
    gfx_total = 0  # graphics recipe count for this run, so CS progress continues past it
    for ln in proc.stdout:
        m = _re.match(r"(CS_)?PROGRESS (\d+)/(\d+)", ln)
        if m and progress_cb:
            progress_cb(int(m.group(2)) + (gfx_total if m.group(1) else 0))
        elif ln.startswith("RECIPES "):
            gfx_total = int(ln.split()[1])
            if log: log(f"   warmgen: {ln.rstrip()}")
        elif ln.startswith("RECORDED "):
            res["recorded"] = int(ln.split()[1])
        elif ln.startswith("SKIPPED "):
            res["skipped"] = int(ln.split()[1])
        elif ln.startswith("CS_DISPATCHED "):
            res["cs"] = int(ln.split()[1])
        elif "WARMGEN_OK" in ln:
            res["ok"] = True
        elif log and (ln.startswith("DEVICE_OK") or "D3D11CreateDevice" in ln):
            log(f"   warmgen: {ln.rstrip()}")
    try:
        res["rc"] = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        if log: log("   warmgen: TIMEOUT")
        return res
    t.join(timeout=5)
    subprocess.run([str(wine.parent / "wineserver"), "-w"], env=env, capture_output=True, timeout=600)
    fams_after = sorted(p.name for p in cache.iterdir()) if (cache and cache.exists()) else []
    res["stray"] = [f for f in fams_after if f.startswith("MTLGPUFamily") and f not in fams_before and not f.endswith("_0")]
    if log and err_tail:
        log("   warmgen stderr (хвост):")
        for e in err_tail[-20:]:
            log("     " + e)
    return res


def main():
    global _cancel_scratch
    ap = argparse.ArgumentParser(description="прогрев шейдерного кэша Deadlock (все герои)")
    ap.add_argument("--config", default=str(HERE / "config.txt"))
    ap.add_argument("--classes", choices=("top5", "all", "full"), default="top5")
    ap.add_argument("--keep", action="store_true", help="не удалять рабочую папку после прогрева")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--detect", action="store_true", help="print detected paths as JSON and exit")
    ap.add_argument("--json", action="store_true", help="emit machine-readable @@ status lines")
    for k in ("game", "gptk", "wine", "prefix"):
        ap.add_argument(f"--{k}", help="override auto-detection")
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    for k in ("game", "gptk", "wine", "prefix"):
        v = getattr(args, k)
        if v:
            cfg[k] = v

    def emit(event, **kw):
        if args.json:
            print("@@ " + json.dumps({"event": event, **kw}), flush=True)

    import datetime
    logdir = Path.home() / "Library/Logs/DeadlockWarmer"
    logpath, logf = None, None
    try:
        # --detect is polled by the GUI; a log file per poll buries the real runs.
        if not args.detect:
            logdir.mkdir(parents=True, exist_ok=True)
            logpath = logdir / ("warm_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
            logf = open(logpath, "a", encoding="utf-8")
    except Exception:
        logpath, logf = None, None

    def log(msg):
        if logf:
            logf.write(msg + "\n"); logf.flush()

    def sysval(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return "?"

    if logpath:
        emit("log", path=str(logpath))
    log("=== Deadlock Shader Warmer log ===")
    log("time      : " + datetime.datetime.now().isoformat())
    log("chip      : " + sysval("sysctl", "-n", "machdep.cpu.brand_string"))
    log("macOS     : " + sysval("sw_vers", "-productVersion") + " (" + sysval("sw_vers", "-buildVersion") + ")")
    log("python    : " + sys.version.split()[0] + " @ " + sys.executable)
    log("args      : classes=%s keep=%s dry_run=%s" % (args.classes, args.keep, args.dry_run))

    # Match the real game binary under win64\ however it was launched — requiring "-steam" missed
    # other launches, and installing into a cache the running game holds open gets it overwritten.
    # Our own warmgen copies are named deadlock_warm_<pid>.exe / deadlock_warmcheck_<pid>.exe and
    # live in the work dir, so they cannot match.
    running = subprocess.run(["pgrep", "-f", r"win64.deadlock\.exe"],
                             capture_output=True).returncode == 0
    game = find_game(cfg)
    combos, err = candidates(cfg)
    combo0 = combos[0] if combos else None
    cache = cache_root()
    counts = cache_counts(cache)
    # A cache warmed by 1.21-1.35 can carry pipelines pointing at the wrong shaders (a black
    # screen on GPTK 4).  Check it once and drop them — never while the game has it open.
    repaired = 0
    if not running and family_dir(cache).exists():
        sys.path.insert(0, str(HERE / "ow"))
        import merge_ns
        try:
            repaired = merge_ns.repair_once(family_dir(cache), Path.home() / "Library/Caches/DeadlockWarmer")
        except Exception as e:
            log("repair    : failed %r" % (e,))
        if repaired:
            log("repair    : dropped %d pipelines with wrong stage pointers" % repaired)
            counts = cache_counts(cache)
    have = counts["stage"]

    log("game      : " + (str(game) if game else "NOT FOUND"))
    if combos:
        w0, g0v, x0 = combos[0]
        log("wine      : " + str(w0))
        log("gptk      : " + str(g0v) + "  (version " + gptk_version(g0v) + ")")
        log("prefix    : " + str(x0))
    else:
        log("runtime   : NONE (" + str(err) + ")")
    log("cache dir : " + str(cache))
    log("family    : " + family_dir(cache).name +
        "  (stage now: %d, pipeline: %d)" % (counts["stage"], counts["pipeline"]))
    if args.detect:
        # every distinct GPTK found (combos are pre-sorted: game's configured / 4.0-2 first),
        # so the GUI can offer them in a dropdown and the user can pick another one.
        gptk_list, seen = [], set()
        for _, g, _ in combos:
            if str(g) not in seen:
                seen.add(str(g))
                gptk_list.append({"path": str(g), "version": gptk_version(g)})
        info = {
            "deadlock_running": running,
            "game": str(game) if game else None,
            "gptk": str(combo0[1]) if combo0 else None,
            "gptk_version": gptk_version(combo0[1]) if combo0 else None,
            "gptks": gptk_list,
            "wine": str(combo0[0]) if combo0 else None,
            "prefix": str(combo0[2]) if combo0 else None,
            "runtime_error": err,
            "cache_shaders": counts["stage"],
            "cache_pipelines": counts["pipeline"],
            "cache_stale": bool(counts["stage"] > 0 and game and (prev := read_stamps().get(str(game)))
                                and prev.get("fp") and prev["fp"] != game_fingerprint(game)),
            "ready": bool(game and combo0 and not running),
        }
        info["repaired"] = repaired
        print(json.dumps(info, ensure_ascii=False))
        return 0

    print("Deadlock shader warmer — все герои\n")
    if running:
        emit("error", reason="deadlock_running")
        print("ОСТАНОВЛЕНО: Deadlock запущен. Закрой игру и повтори.")
        return 2
    if not game:
        emit("error", reason="game_not_found")
        print("ОСТАНОВЛЕНО: установка Deadlock не найдена. Впиши путь game= в config.txt")
        return 2
    print(f"игра   : {game}")
    if err:
        emit("error", reason="runtime_not_found")
        print(f"ОСТАНОВЛЕНО: {err}")
        return 2

    # Work in a writable per-user cache dir, NOT inside the app bundle — a warm extracts
    # ~300 MB of shaders, and HERE is the read-only .app when run from /Applications.
    work = Path.home() / "Library/Caches/DeadlockWarmer/work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    dxbc = work / "dxbc"

    emit("stage", n=1, of=5, name="extract")
    print("1/5 извлекаю шейдеры из игры...")
    r = subprocess.run([sys.executable, str(HERE / "extract_shaders.py"), "--game", str(game), "--out", str(dxbc)],
                       capture_output=True, text=True)
    if r.returncode == 3:           # the disk could not read the game: give a waking drive one more try
        log("extract   : unreadable, retrying in 5 s — " + r.stdout.strip()[:200])
        time.sleep(5)
        r = subprocess.run([sys.executable, str(HERE / "extract_shaders.py"), "--game", str(game), "--out", str(dxbc)],
                           capture_output=True, text=True)
    print("   " + (r.stdout.strip() or r.stderr.strip()))
    log("extract   : rc=%d  %s" % (r.returncode, (r.stdout.strip() or r.stderr.strip())[:200]))
    if r.returncode != 0:
        log("extract stderr: " + r.stderr.strip()[-500:])
        emit("error", reason="game_unreadable" if r.returncode == 3 else "extract_failed")
        return 1

    emit("stage", n=2, of=5, name="plan")
    print("2/5 строю план прогрева...")
    plan_dir = work / "plan"
    r = subprocess.run([sys.executable, str(HERE / "gen_plan.py"), "--corpus", str(dxbc),
                        "--layouts", str(HERE / "layout_library.json"), "--out", str(plan_dir), "--classes", args.classes],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout + r.stderr)
        emit("error", reason="plan_failed")
        return 1
    plan_text = (plan_dir / "prewarm_plans.txt").read_text()
    graphics, _, cs = plan_text.partition("\nCS ")
    blocks = re.split(r"\n(?=RECIPE )", graphics.strip())
    cs_lines = [l for l in ("CS " + cs).splitlines() if l.startswith("CS ")] if cs else []
    total_units = len(blocks) + len(cs_lines)
    print(f"   {len(blocks)} рецептов + {len(cs_lines)} compute")
    log("plan      : %d recipes + %d compute" % (len(blocks), len(cs_lines)))
    emit("plan_ready", recipes=len(blocks), compute=len(cs_lines), chunks=1)

    cache = cache_root()
    before = cache_counts(cache)["stage"]
    print(f"кеш    : {cache}\n   сейчас в нём: {before} шейдеров")

    # ---- preflight: verify everything, then a real micro-run, before touching the live cache ----
    emit("stage", n=0, of=5, name="preflight")
    print("\nПроверка перед запуском...")

    def check(name, ok, detail=""):
        emit("check", name=name, ok=bool(ok), detail=detail)
        print(f"   [{'OK' if ok else '!!'}] {name}" + (f" — {detail}" if detail else ""))
        log("check     : [%s] %s%s" % ("OK" if ok else "!!", name, (" — " + detail) if detail else ""))
        return bool(ok)

    checks_ok = True
    checks_ok &= check("Игровые VPK читаются", any(game.glob("*/shaders_pc_dir.vpk")), str(game))
    g0 = combos[0][1]
    for lib in ("external/D3DMetal.framework/Versions/A/D3DMetal",
                "external/D3DMetal.framework/Versions/A/Resources/libmetalirconverter.dylib",
                "external/D3DMetal.framework/Versions/A/Resources/libdxilconv.dylib"):
        checks_ok &= check(f"GPTK: {Path(lib).name}", (g0 / lib).exists())
    want_g = wanted_gptk()
    checks_ok &= check("GPTK версия " + (gptk_version(want_g) if want_g else "4.0-2"),
                       not gptk_mismatch(g0), gptk_version(g0))
    checks_ok &= check("Wine запускается", os.access(str(combos[0][0]), os.X_OK))
    checks_ok &= check("Python для извлечения", bool(sys.executable))
    # Gatekeeper kills quarantined helpers on exec; clear the flag on everything we ship
    quarantine_left = gptk_pick.clear_quarantine(HERE)
    dropped = gptk_pick.drop_legacy_backups(cache_root_for(EXE).parent.parent)   # left by older versions of this app
    if dropped:
        log("cleanup   : removed old backups " + ", ".join(dropped))
    checks_ok &= check("Карантин снят", not quarantine_left,
                       "" if not quarantine_left else
                       f"{len(quarantine_left)} файлов — выполни: " + 'xattr -dr com.apple.quarantine "/Applications/Shader Warmer.app"')
    free_gb = shutil.disk_usage(cache.parent.parent).free / 2**30
    # The warm fills a scratch namespace, merges it with the live cache into a work dir, and only
    # then swaps the result in, while the backup clone keeps the old blocks alive — so the peak
    # is roughly two caches on top of the existing one.  Measured: 1.3 GB scratch + 1.7 GB merged.
    live_gb = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file()) / 2**30 if cache.exists() else 0
    need_gb = max(5.0, round(2 * live_gb + 2, 1))
    checks_ok &= check("Место на диске", free_gb >= need_gb,
                       f"{free_gb:.1f} ГБ свободно, нужно {need_gb:.0f}")

    if not checks_ok:
        emit("error", reason="preflight_failed")
        print("\nОСТАНОВЛЕНО: не все проверки пройдены. Живой кэш не тронут.")
        shutil.rmtree(work, ignore_errors=True)
        return 2

    # micro-run: replay 4 recipes into a throwaway namespace to prove the whole chain works
    smoke_name = f"deadlock_warmcheck_{os.getpid()}.exe"
    smoke_ns = cache_root_for(smoke_name)
    shutil.rmtree(smoke_ns.parent, ignore_errors=True)
    smoke_plan = work / "smoke"
    smoke_plan.mkdir(parents=True, exist_ok=True)
    (smoke_plan / "prewarm_plans.txt").write_text("\n".join(blocks[:4]) + "\n")
    # Warm ONLY under the game's own GPTK version. A different version (e.g. 3.0-3)
    # signs version.bin with the wrong signature and the game throws the whole cache
    # away on launch — so never fall back across versions, only across wine/prefix.
    want_ver = gptk_version(combos[0][1])
    # The prefix is replaced by a private clone at use time, so combos differing only in the
    # game's prefix are the same combo — try each wine/GPTK pair once.
    same_ver, seen_pair = [], set()
    for w, g, x in (c for c in combos if gptk_version(c[1]) == want_ver):
        if (w, g) not in seen_pair:
            seen_pair.add((w, g))
            same_ver.append((w, g, x))
    working = None
    warm_draw = 0  # no-GPU by default (Apple10/M4/M5 persist from Draw(0))
    confirmed = False   # the smoke actually persisted a stage record (nice, but flaky at 4-recipe scale)
    # Pass the smoke on CHAIN success (device up, recipes ran, no stray). stage>0 only CONFIRMS
    # persistence — on Apple7/M1 four tiny records often don't flush before the process exits, so
    # requiring it wrongly blocked the whole warm. The real warm (2000/chunk) flushes fine, and
    # snapshot-best + never-regress protect the cache if a run turns out weak.
    # start from the draw mode already measured for this GPU + GPTK, if any
    known = gptk_pick.known_draw_mode(gptk_pick.family_dir_name(cache.parent.parent), same_ver[0][1]) \
        if same_ver else None
    for draw in ((3, 0) if known == 3 else (0, 3)):
        for wine, gptk, prefix in same_ver:
            prefix = private_prefix(wine, prefix)   # never the player's own game prefix
            log("smoke try : gptk=%s prefix=%s draw=%d" % (gptk.name, prefix.name, draw))
            r = run_warmgen(wine, gptk, prefix, smoke_plan, smoke_name, work, smoke_ns, timeout=120, log=log,
                            draw_vertices=draw, dispatch=(1 if draw else 0))
            c = cache_counts(smoke_ns)
            log("smoke res : ok=%s rc=%s recorded=%s skipped=%s stray=%s stage=%d pipeline=%d draw=%d" %
                (r["ok"], r["rc"], r["recorded"], r["skipped"], r["stray"], c["stage"], c["pipeline"], draw))
            if r["ok"] and not r["stray"]:
                working, warm_draw, confirmed = (wine, gptk, prefix), draw, c["stage"] > 0
                if confirmed:
                    break
            shutil.rmtree(smoke_ns.parent, ignore_errors=True)
        if confirmed:
            break
        # draw=0 chain works — don't escalate to GPU draws (hang risk) just because the tiny
        # smoke didn't flush; no-GPU warms fine on this GPU in a real run.
        if working and draw == 0:
            break
    shutil.rmtree(smoke_ns.parent, ignore_errors=True)
    if not check("Пробный прогон", working is not None,
                 ("цепочка Wine->GPTK->D3DMetal работает" + (" (GPU)" if warm_draw else "")
                  + ("" if confirmed else " — кэш подтвердится при прогреве"))
                 if working else "прогрев не запустился"):
        emit("error", reason="smoke_failed")
        print("\nОСТАНОВЛЕНО: пробный прогон не удался. Живой кэш не тронут.")
        shutil.rmtree(work, ignore_errors=True)
        return 2
    log("warm mode : %s%s" % ("GPU draws (draw=3, dispatch=1)" if warm_draw else "no-GPU (draw=0)",
                              "" if confirmed else "  (smoke persisted 0 — chain ok, proceeding)"))
    emit("preflight_ok", gpu=bool(warm_draw))
    print("Все проверки пройдены.\n")

    if args.dry_run:
        w, g, x = working
        emit("dry_run_ok", gptk=g.name, gptk_version=gptk_version(g), gpu=bool(warm_draw))
        print(f"окружение: wine {w.parent.parent.name}, {g.name}, prefix {x.name}"
              + (" (GPU-прогрев)" if warm_draw else ""))
        print("--dry-run: прогрев не запускался")
        shutil.rmtree(work, ignore_errors=True)
        return 0

    # Warm into a throwaway namespace, never into the cache the game reads.  D3DMetal decides on
    # its own when a cache looks inconsistent and wipes it — "Shader cache corrupted" / "Clearing
    # shader cache", logged a dozen times mid-warm on an M1 — and when the cache it wipes is the
    # game's own, the warm takes the game's records down with it.  Warming aside also means a
    # cancelled or crashed run costs nothing: the live cache is touched once, at install.
    # How much of the plan one warmgen process may take: 2000 recipes, what the first versions used
    # and what kept play smooth.  One process over the whole plan runs the GPU driver past its limit
    # ("AGX: exceeded compiled variants footprint limit" — in every single-process warm since 19.09,
    # never in the 24-chunk ones; 12 000 recipes in one process trips it, 2000 does not), and the
    # pipelines past that point never reach macOS's own Metal shader cache, so they compile in-game
    # and every hero stutters on first appearance.  Each part gets its own namespace — reopening one
    # cache from several processes is what made D3DMetal wipe caches on an M1 — and the parts are
    # merged offline at the end, with the pipelines' stage pointers carried along.
    per_part = CHUNK
    if os.environ.get("DEADLOCK_WARM_PART", "").isdigit():   # force another size, e.g. to test
        per_part = max(1, int(os.environ["DEADLOCK_WARM_PART"]))
    starts = list(range(0, len(blocks), per_part)) or [0]
    ns_names = [f"deadlock_warm_{os.getpid()}_{i}.exe" for i in range(len(starts))]
    ns_caches = [cache_root_for(n) for n in ns_names]
    for stale_ns in cache.parent.parent.glob("deadlock_warm_*.exe"):     # left by a killed run
        shutil.rmtree(stale_ns, ignore_errors=True)
    _cancel_scratch = [c.parent for c in ns_caches]

    def drop_scratch():
        for c in ns_caches:
            shutil.rmtree(c.parent, ignore_errors=True)

    emit("stage", n=3, of=5, name="warm", chunks=len(starts))
    print("3/5 прогреваю... (10-20 мин, окна не будет — это нормально)"
          + (f"\n   {len(starts)} частей по {per_part} рецептов" if len(starts) > 1 else "") + "\n")
    log("warm plan : %d part(s) of up to %d recipes" % (len(starts), per_part))
    wine, gptk, prefix = working
    try:
        (Path.home() / "Library/Application Support/DeadlockWarmer/cache_cap_mb.txt").unlink()
    except OSError:
        pass

    def clone(src, dst):
        shutil.rmtree(dst, ignore_errors=True)
        subprocess.run(["cp", "-c", "-R", str(src), str(dst)], capture_output=True)

    oslog_p, oslog_f, oslog_path = start_oslog(logdir)   # capture the reason if it still resets
    if oslog_path:
        log("d3dmetal oslog: " + oslog_path.name)

    t0 = time.time()
    def progress(units):
        el = time.time() - t0
        rate = units / el if el > 0 else 0
        eta = round((total_units - units) / rate) if rate > 0 else 0
        emit("progress", units_done=units, units_total=total_units,
             percent=round(100 * units / total_units, 1),
             elapsed=round(el), eta=eta, rate=round(rate))

    builts, failed_parts, done_units = [], [], 0
    try:
        for i, first in enumerate(starts):
            part = blocks[first:first + per_part]
            pdir = plan_dir / f"part{i}"
            pdir.mkdir(parents=True, exist_ok=True)
            if len(starts) == 1:
                text = plan_text                             # the proven single pass, byte for byte
            else:                                            # compute rides with the first part
                text = "\n".join(part) + "\n" + ("\n".join(cs_lines) + "\n" if i == 0 and cs_lines else "")
            (pdir / "prewarm_plans.txt").write_text(text)
            units_here = len(part) + (len(cs_lines) if i == 0 else 0)
            for attempt in (1, 2):              # a part that dies once usually passes in a fresh process
                shutil.rmtree(ns_caches[i].parent, ignore_errors=True)
                r = run_warmgen(wine, gptk, prefix, pdir, ns_names[i], work, ns_caches[i],
                                progress_cb=lambda n, base=done_units: progress(base + n), log=log,
                                draw_vertices=warm_draw, dispatch=(1 if warm_draw else 0),
                                timeout=(7200 if warm_draw else 5400))
                fam = family_dir(ns_caches[i])
                got = records(fam / "stage_cache.bin") if (fam / "stage_cache.bin").exists() else 0
                ok = r["ok"] and got > 0
                log("warm part : %d/%d try %d ok=%s rc=%s recorded=%s skipped=%s cs=%s stage=%.0fMB %s" %
                    (i + 1, len(starts), attempt, r["ok"], r["rc"], r["recorded"], r["skipped"], r["cs"],
                     stage_size_mb(ns_caches[i]), cache_fingerprint(ns_caches[i])))
                if ok or r["stray"]:
                    break
                if r["ok"] and not got and warm_draw == 0:
                    # this GPU + GPTK keeps nothing from zero-vertex draws: replay with real ones
                    warm_draw = 3
                    log("warm part : zero-vertex draws persisted nothing — switching to real draws")
            if r["stray"]:
                emit("error", reason="stray_family_dir", dirs=r["stray"])
                log("ERROR     : stray dirs " + str(r["stray"]))
                drop_scratch()
                print(f"\nОШИБКА: записи ушли в {r['stray']}. Останавливаюсь. Кэш игры не тронут.")
                return 1
            if ok:
                builts.append(fam)
            else:
                failed_parts.append(i + 1)
            done_units += units_here
            progress(done_units)
            if len(starts) > 1:
                print(f"   часть {i + 1}/{len(starts)}: " + ("готово" if ok else "не прогрелась"), flush=True)
    finally:
        stop_oslog(oslog_p, oslog_f)
    if not builts:
        emit("error", reason="warm_failed")
        log("ERROR     : no part produced a cache")
        drop_scratch()
        print("\nОШИБКА: прогрев не удался. Кэш игры не тронут.")
        return 1
    gptk_pick.remember_draw_mode(builts[0].name, gptk, warm_draw)
    if failed_parts:
        log("WARN      : parts %s failed twice — installing the rest" % failed_parts)

    # Union the fresh warm with whatever the game had accumulated — the warm goes first, so its
    # records win any key collision.  Picking whichever file was bigger used to lose the whole
    # warm after a game patch: the old cache is bigger precisely because it is full of records for
    # shaders the patch replaced.
    sys.path.insert(0, str(HERE / "ow"))
    import merge_ns
    emit("stage", n=4, of=5, name="build")
    print("4/5 собираю итоговый кэш…", flush=True)
    live_fam = family_dir(cache) if cache.exists() else None
    if live_fam is not None and not (live_fam / "stage_cache.bin").exists():
        live_fam = None
    out = work / "merged"
    shutil.rmtree(out, ignore_errors=True)
    stale = False
    try:
        rep = merge_ns.merge(builts + ([live_fam] if live_fam else []), out, with_bytecode=True)
    except merge_ns.VersionMismatch:
        # The game's own cache was written by a different D3DMetal or a different macOS (the build
        # and the OS version both sit in version.bin).  The game will throw it away on its next
        # launch anyway, so install the fresh one alone instead of losing the whole warm.
        stale = True
        shutil.rmtree(out, ignore_errors=True)
        rep = merge_ns.merge(builts, out, with_bytecode=True)
        log("MERGE     : previous cache carries a different version.bin — dropped as stale")

    # No backup copy: the merged cache is a union with the old one, so it already holds every record
    # the game had — the per-warm clone only pinned another 1.9 GB of blocks on disk.  The install is
    # a handful of renames on one volume, and the GUI blocks cancel while it runs.
    emit("stage", n=5, of=5, name="install")
    print("5/5 устанавливаю готовый кэш…", flush=True)
    target = cache / builts[0].name    # the family dir THIS Mac uses, whatever the chip
    target.mkdir(parents=True, exist_ok=True)
    # Rename rather than copy: the work dir and the cache sit on the same volume, and a copy would
    # need room for a third full cache at the worst possible moment.
    drop_scratch()
    for f in ("bytecode_cache.bin", "pipeline_cache.bin", "rootsignature_cache.bin",
              "stage_cache.bin", "version.bin"):
        shutil.move(str(out / f), str(target / f))
    shutil.rmtree(out, ignore_errors=True)
    st = rep["stage_cache.bin"]
    merged_ok = bool(live_fam) and not stale
    # with several parts the duplicate count also covers records the parts shared among themselves
    shared = (" (%d общих записей)" % st["duplicates_dropped"]) if len(builts) == 1 else ""
    merged_note = ((" — объединено с прежним кэшем" + shared) if merged_ok
                   else (" — прежний кэш был от другой версии GPTK/macOS, отброшен" if stale else ""))
    if failed_parts:
        merged_note += "; части %s не прогрелись — повтори прогрев позже" % ", ".join(map(str, failed_parts))
    log("MERGE     : warmed + previous -> %d stage, %d pipeline (%d ключей совпало)%s" %
        (st["records"], rep["pipeline_cache.bin"]["records"], st["duplicates_dropped"],
         " [stale dropped]" if stale else ""))

    metal_cache.after_warm(log)
    fin = cache_counts(cache)
    after = fin["stage"]
    write_stamp(game, after)   # remember the game's shader fingerprint so we can flag a stale cache after a patch
    log("DONE      : stage %d -> %d (+%d), pipeline %d%s in %.1f min" %
        (before, after, after - before, fin["pipeline"],
         " (merged)" if merged_ok else (" (stale dropped)" if stale else ""),
         (time.time()-t0)/60))
    emit("done", shaders=after, added=after-before, pipelines=fin["pipeline"],
         merged=merged_ok, stale_dropped=stale, minutes=round((time.time()-t0)/60, 1))
    tail = merged_note
    print(f"\nГОТОВО за {(time.time()-t0)/60:.0f} мин. В кеше {after} шейдеров (+{after-before}){tail}. Запускай игру.")
    _cancel_scratch = None
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        os.setpgrp()  # let the GUI cancel Python, Wine and every compiler as one process tree
    except OSError:
        pass
    signal.signal(signal.SIGTERM, _cancel_run)
    try:
        sys.exit(gptk_pick.detect_guard(main, Path.home() / "Library/Logs/DeadlockWarmer"))
    except CancelledRun:
        cleanup_cancelled_run()
        print("Прогрев отменён. Кэш игры не тронут.", flush=True)
        sys.exit(130)
