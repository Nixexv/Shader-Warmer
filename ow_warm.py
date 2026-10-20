#!/usr/bin/env python3
"""Прогрев шейдерного кэша Overwatch (GPTK 3.0-3 + Wine Proton 10).

Четыре шага, все на машине пользователя:

  1 extract    шейдеры игры из её же файлов (10 параллельных процессов, ~70 с)
  2 calibrate  короткий прогон через D3DMetal: он даёт version.bin и настоящие записи этой
               машины; те же записи собираются офлайн и сверяются байт в байт
  3 build      весь кэш собирается офлайн (DXBC->DXIL->metallib), ~5 мин
  4 install    слияние с текущим кэшем игры и установка с проверенным бэкапом

Если калибровка не сошлась (другое семейство GPU, другие настройки компилятора), быстрый путь
не используется: кэш строится медленным, но всегда верным способом — прогоном через D3DMetal.

  ow_warm.py [--detect] [--json] [--dry-run] [--game PATH] [--gptk PATH] [--wine PATH]
"""
from __future__ import annotations
import argparse, datetime, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OW = HERE / "ow"
sys.path.insert(0, str(OW))
sys.path.insert(0, str(HERE))
import owplan, merge_ns, install_ow as inst  # noqa: E402
import gptk_pick, gamehub_wine  # noqa: E402
import metal_cache  # noqa: E402

GH = Path.home() / "Library/Application Support/com.gamemac.www/wine-engine"
APPDIR = Path.home() / "Library/Application Support/OverwatchWarmer"
LOGDIR = Path.home() / "Library/Logs/OverwatchWarmer"
EXE = "Overwatch.exe"
HINT = 'xattr -dr com.apple.quarantine "/Applications/Shader Warmer.app"'
WANT_GPTK = "gptk-3.0-3"          # Overwatch only runs on this one
OVERWATCH_STEAM_APP_ID = "2357570"
MIN_FREE_GB = 14


# ---------------------------------------------------------------- detection
def d3dm_root() -> Path:
    out = subprocess.run(["getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True, text=True).stdout.strip()
    return Path(out) / "d3dm"


def family_dir(cache: Path) -> Path | None:
    """d3dm/<exe>/shaders.cache/MTLGPUFamilyAppleN_0 — the one the game reads."""
    fams = sorted(cache.glob("MTLGPUFamily*_0")) if cache.exists() else []
    return fams[0] if fams else None


def gpu_family_of(fam: Path) -> int:
    m = re.search(r"MTLGPUFamilyApple(\d+)_", fam.name)
    return 1000 + int(m.group(1)) if m else 1010


def find_game(override=None) -> Path | None:
    cands = [Path(override)] if override else []
    cands += [Path.home() / "GameHub/steamapps/common/Overwatch"]
    try:        # glob swallows PermissionError but not EIO: a drive that dropped off must not kill --detect
        cands += [p / "Overwatch" for p in Path("/Volumes").glob("*/*/steamapps/common")]
    except OSError:
        pass
    cands += [lib / "steamapps/common/Overwatch" for lib in gptk_pick.steam_libraries()]
    for c in cands:
        for d in (c, c / "Overwatch"):
            # probe(): a library on a drive macOS has not let us onto must not crash --detect
            if gptk_pick.probe(d / "data/.build.config") and gptk_pick.probe(d / EXE):
                return d
    return None


def game_build(game: Path) -> str:
    for line in (game / "data/.build.config").read_text(errors="ignore").splitlines():
        if line.startswith("build-number"):
            return line.split("=", 1)[1].strip()
    return "unknown"


def list_gptks():
    """Every GPTK GameHub has downloaded, for the settings dropdown."""
    out = []
    for d in sorted((GH / "downloads").glob("gptk-*")):
        if (d / "external/D3DMetal.framework").exists():
            out.append({"path": str(d), "version": d.name.replace("gptk-", "")})
    return out


def stamp_path() -> Path:
    return APPDIR / "last_warm.json"


def read_stamp() -> dict:
    try:
        return json.loads(stamp_path().read_text())
    except Exception:
        return {}


def find_gptk(override=None, live_family: Path | None = None) -> Path | None:
    if override:
        return Path(override)
    # Overwatch normally runs on 3.0-3, but the cache the game itself wrote is the authority:
    # D3DMetal stamps version.bin per build and drops a cache stamped by another one, so warming
    # under the wrong GPTK produces records the game will never load.
    # GameHub's per-game setting says what Overwatch launches with next; the cache stamp only says
    # what ran last.  Prefer the setting, then the stamp, then the build Overwatch is known to need.
    configured = gptk_pick.gamehub_gptk(OVERWATCH_STEAM_APP_ID)
    if configured:
        return configured
    matched = gptk_pick.gptk_for_cache([Path(g["path"]) for g in list_gptks()], live_family)
    if matched:
        return matched
    p = GH / "downloads" / WANT_GPTK
    return p if (p / "external/D3DMetal.framework").exists() else None


def find_wine(override=None) -> Path | None:
    """Wine Proton 10 — the build Overwatch runs on."""
    if override:
        return Path(override)
    best = None
    for d in sorted((GH / "containers/wine_installations").glob("*")):
        man = d / "manifest.json"
        if not man.exists() or not (d / "bin/wine").exists():
            continue
        try:
            m = json.loads(man.read_text())
        except Exception:
            continue
        if m.get("metadata", {}).get("proton_version", "").startswith("10") and m.get("architecture") == "x86_64":
            best = d
    return best


def game_running() -> bool:
    return subprocess.run(["pgrep", "-if", r"win64.Overwatch\.exe|Overwatch\.exe"], capture_output=True).returncode == 0


def ram_gb() -> int:
    """How much memory this Mac has — the finished cache has to fit in it at game launch."""
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=10).stdout
        return int(int(out.strip()) / 2 ** 30)
    except Exception:
        return 0


def free_gb(path: Path) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 2 ** 30


# ---------------------------------------------------------------- wine runtime
def prefix_for(wine: Path, work: Path) -> Path:
    pfx = work / "prefix"
    if not pfx.exists():
        subprocess.run(["cp", "-c", "-R", str(wine / "prefix"), str(pfx)], check=True)
        # GameHub's own containers say "disable" here; without it wineboot spends 10+ minutes
        # re-running wine.inf on every fresh prefix.
        (pfx / ".update-timestamp").write_text("disable")
    dos = pfx / "dosdevices"
    dos.mkdir(exist_ok=True)
    for drive, target in (("c:", "../drive_c"), ("z:", "/")):
        if not (dos / drive).is_symlink():
            (dos / drive).symlink_to(target)
    return pfx


def wine_env(wine: Path, gptk: Path, prefix: Path, draw_vertices=0, dispatch=0) -> dict:
    e = dict(os.environ)
    e.update({"WINEPREFIX": str(prefix), "WINEDLLPATH": str(gptk / "wine"),
              "WINE_GPTK_LIBD3DSHARED_PATH": str(gptk / "external/libd3dshared.dylib"),
              "WINEARCH": "win64", "WINEMSYNC": "1", "WINEDEBUG": "-all", "D3DM_NO_WINDOW": "1",
              "MEDIACONV_BLANK_AUDIO_FILE": str(wine / "share/media/blank.ptna"),
              "MEDIACONV_BLANK_VIDEO_FILE": str(wine / "share/media/blank.mkv"),
              "WARMGEN_DRAW_VERTICES": str(draw_vertices), "WARMGEN_DISPATCH": str(dispatch)})
    return e


def run_warmgen(wine, gptk, prefix, plan_dir, exe_name, work, first=0, count=0, with_cs=True,
                timeout=7200, progress_cb=None, draw_vertices=0, dispatch=0):
    """One warmgen process; D3DMetal writes d3dm/<exe_name>/ itself."""
    wine = gptk_pick.warm_wine(wine / "bin/wine").parent.parent   # the games' GPU-code cache stays untouched
    exe = work / exe_name
    shutil.copy2(OW / "bin/warmgen_ow.exe", exe)
    env = wine_env(wine, gptk, prefix, draw_vertices, dispatch)
    env.update({"WARMGEN_FIRST": str(first), "WARMGEN_CS": "1" if with_cs else "0"})
    if count > 0:                      # 0 would tell warmgen to draw nothing; unset means "all"
        env["WARMGEN_COUNT"] = str(count)
    out_lines = []
    p = subprocess.Popen([str(wine / "bin/wine"), str(exe), "Z:" + str(plan_dir.resolve()).replace("/", "\\"), "40"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    t0 = time.time()
    for line in p.stdout:
        out_lines.append(line)
        if progress_cb and line.startswith("PROGRESS "):
            parts = line.split()
            done, total = parts[1].split("/")
            progress_cb(int(done) - first, int(total) - first)
        if time.time() - t0 > timeout:
            p.kill()
            break
    p.wait()
    subprocess.run([str(wine / "bin/wineserver"), "-w"], env=env, capture_output=True, timeout=600)
    exe.unlink(missing_ok=True)
    text = "".join(out_lines)
    stats = {l.split()[0]: l.split()[1] for l in text.splitlines() if len(l.split()) == 2 and l.split()[0].isupper()}
    return {"ok": "WARMGEN_OK" in text, "rc": p.returncode, "recorded": stats.get("RECORDED"),
            "skipped": stats.get("SKIPPED"), "cs": stats.get("CS_DISPATCHED"), "text": text}


def prepare_tools(gptk: Path, work: Path) -> Path:
    """stage2metallib finds D3DMetal's private libmetalirconverter through a `gptk_res` symlink next
    to itself, so it works wherever the user's GPTK lives.  The app bundle is read-only, so the
    binary is copied into the writable work dir and the symlink is made there."""
    tools = work / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    harness = tools / "stage2metallib"
    if not harness.exists():
        shutil.copy2(OW / "bin/stage2metallib", harness)
    link = tools / "gptk_res"
    target = gptk / "external/D3DMetal.framework/Versions/A/Resources"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    return harness


# ---------------------------------------------------------------- steps
def extract(game: Path, out: Path, shards: int, emit, log) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    dump = OW / "bin/owshaderdump/owshaderdump"
    procs = []
    for i in range(shards):
        env = dict(os.environ, OWDUMP_SHARD=str(i), OWDUMP_SHARDS=str(shards))
        procs.append(subprocess.Popen([str(dump), str(game), str(out)], env=env,
                                      stdout=(out / f"shard_{i}.log").open("w"), stderr=subprocess.STDOUT))
    for i, p in enumerate(procs):
        p.wait()
        emit("progress", units_done=i + 1, units_total=shards, percent=round(100 * (i + 1) / shards, 1))
    rows, seen = [], set()
    for f in sorted(out.glob("index_*.jsonl")):
        for l in f.open():
            r = json.loads(l)
            if r["sha"] not in seen:
                seen.add(r["sha"])
                rows.append(r)
    (out / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    counts = {}
    for r in rows:
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
    log(f"extract   : {len(rows)} shaders {counts}")
    return {"shaders": len(rows), "stages": counts}


def mini_corpus(corpus: Path, work: Path, n_ps=900, n_vs=150, n_cs=20) -> Path:
    """Shaders of each stage for the calibration round-trip — about 2000 recipes on purpose.

    The same lesson as the Deadlock smoke test: on an M1 a tiny D3DMetal run can exit before any
    of its records reach the disk (a 90-recipe calibration came back with 0 records twice in a row,
    with and without real draws), while a run of a couple thousand recipes persists fine.  An empty
    reference sent Overwatch down the hour-long fallback for nothing.
    """
    mini = work / "calib_corpus"
    shutil.rmtree(mini, ignore_errors=True)
    for sub, n in (("ps", n_ps), ("vs", n_vs), ("cs", n_cs)):
        (mini / sub).mkdir(parents=True)
        for p in sorted((corpus / sub).glob("*.dxbc"))[:n]:
            (mini / sub / p.name).symlink_to(p.resolve())
    return mini


def calibrate(wine, gptk, prefix, corpus, work, harness, emit, log):
    """Warm a handful of shaders through D3DMetal, then mint the same keys offline and compare."""
    mini = mini_corpus(corpus, work)
    plan_dir = work / "calib_plan"
    shutil.rmtree(plan_dir, ignore_errors=True)
    vs_list = [owplan.load(p) for p in sorted((mini / "vs").glob("*.dxbc"))]
    ps_list = [owplan.load(p) for p in sorted((mini / "ps").glob("*.dxbc"))]
    pl = owplan.Planner(vs_list, ps_list)
    for ps in ps_list:
        pl.ps(ps, owplan.shape_classes(owplan.ps_shape(ps), "top"))
    for vs in vs_list:
        pl.vs(vs)
    owplan.write_plan(plan_dir, pl.recipes, sorted((mini / "cs").glob("*.dxbc")), {"calibration": True})

    ns_name = f"owwarmcheck_{os.getpid()}.exe"
    ns_parent = d3dm_root() / ns_name / "shaders.cache"
    shutil.rmtree(ns_parent.parent, ignore_errors=True)
    draw = gptk_pick.known_draw_mode(gptk_pick.family_dir_name(d3dm_root()), gptk) or 0
    r = run_warmgen(wine, gptk, prefix, plan_dir, ns_name, work, timeout=900,
                    draw_vertices=draw, dispatch=(1 if draw else 0))
    fam = family_dir(ns_parent)
    if not r["ok"] or fam is None:
        return None, {"ok": False, "why": "warmgen", "detail": r["text"][-400:]}
    family = gpu_family_of(fam)
    stage_records = inst.arena(fam / "stage_cache.bin")[1] if (fam / "stage_cache.bin").exists() else 0
    log(f"calibrate : Draw({draw}) family={family} stage_records={stage_records} "
        f"reported={r.get('recorded')} cs={r.get('cs')}")

    # Empty means either this GPU + GPTK keeps nothing from zero-vertex draws (an M1 under 3.0-3),
    # or a short run exited before D3DMetal flushed — seen on an M1 even with real draws.  Either
    # way give it a second run, with real draws.  (1.31 skipped this once the mode was remembered.)
    if stage_records == 0:
        draw = 3
        log(f"calibrate : GPU family {family} empty-cache retry with Draw(3)/Dispatch(1)")
        shutil.rmtree(ns_parent.parent, ignore_errors=True)
        r = run_warmgen(wine, gptk, prefix, plan_dir, ns_name, work, timeout=900,
                        draw_vertices=3, dispatch=1)
        fam = family_dir(ns_parent)
        if not r["ok"] or fam is None:
            return None, {"ok": False, "why": "apple7_gpu_retry", "detail": r["text"][-400:]}
        family = gpu_family_of(fam)
        stage_records = inst.arena(fam / "stage_cache.bin")[1] if (fam / "stage_cache.bin").exists() else 0
        log(f"calibrate : Draw(3)/Dispatch(1) family={family} stage_records={stage_records} "
            f"reported={r.get('recorded')} cs={r.get('cs')}")
    out = work / "calib_minted"
    shutil.rmtree(out, ignore_errors=True)
    res = gptk / "external/D3DMetal.framework/Versions/A/Resources"
    p = subprocess.run([sys.executable, str(OW / "mint_all.py"), "--corpus", str(mini), "--out", str(out),
                        "--res", str(res), "--gpu-family", str(family), "--version-bin", str(fam / "version.bin"),
                        "--work", str(work / "calib_mint_work"), "--workers", "4", "--batch", "200",
                        "--harness", str(harness),
                        "--classes", "top", "--reference", str(fam)], capture_output=True, text=True)
    log("calibrate : " + p.stdout.strip().replace("\n", " ")[-600:])
    result = work / "calib_mint_work/result.json"
    if p.returncode != 0 or not result.exists():
        return None, {"ok": False, "why": "mint", "detail": (p.stdout + p.stderr)[-400:]}
    report = json.loads(result.read_text())
    chk = report.get("reference_check") or {}
    ok = bool(chk) and chk.get("sampled") and chk["identical"] == chk["sampled"]
    if stage_records:
        gptk_pick.remember_draw_mode(fam.name, gptk, draw)
    info = {"ok": bool(ok), "gpu_family": family, "family_dir": fam.name, "draw": draw,
            "records": chk.get("sampled"), "identical": chk.get("identical")}
    return (fam if ok else None), info


def build_offline(corpus, work, res, family, version_bin, harness, emit, log):
    out = work / "minted"
    shutil.rmtree(out, ignore_errors=True)
    p = subprocess.Popen([sys.executable, str(OW / "mint_all.py"), "--corpus", str(corpus), "--out", str(out),
                          "--res", str(res), "--gpu-family", str(family), "--version-bin", str(version_bin),
                          "--work", str(work / "mint_work"), "--classes", "ready", "--harness", str(harness)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0, last = time.time(), 0
    total_batches = None
    for line in p.stdout:
        line = line.rstrip()
        if line.startswith("keys:") or line.startswith("corpus:") or line.startswith("dxil:"):
            log("build     : " + line)
        m = re.match(r"\s+compiled (\d+)/(\d+) batches, (\d+) keys/s", line)
        if m:
            done, total, rate = int(m.group(1)), int(m.group(2)), int(m.group(3))
            total_batches = total
            el = time.time() - t0
            eta = round((total - done) / (done / el)) if done and el else 0
            emit("progress", units_done=done, units_total=total, percent=round(100 * done / total, 1),
                 elapsed=round(el), eta=eta, rate=rate)
            last = done
    p.wait()
    if p.returncode != 0 or not (out / "stage_cache.bin").exists():
        return None
    return out


def build_via_warmgen(corpus, work, wine, gptk, prefix, emit, log, draw=0):
    """Fallback: let D3DMetal build the cache itself (slow but always correct)."""
    plan_dir = work / "plan"
    shutil.rmtree(plan_dir, ignore_errors=True)
    vs_list = [owplan.load(p) for p in sorted((corpus / "vs").glob("*.dxbc"))]
    ps_list = [owplan.load(p) for p in sorted((corpus / "ps").glob("*.dxbc"))]
    pl = owplan.Planner(vs_list, ps_list)
    for ps in ps_list:
        pl.ps(ps, owplan.shape_classes(owplan.ps_shape(ps), "top"))
    for vs in vs_list:
        pl.vs(vs)
    cs = sorted((corpus / "cs").glob("*.dxbc"))
    owplan.write_plan(plan_dir, pl.recipes, cs, {"recipes": len(pl.recipes)})
    total = len(pl.recipes)
    emit("plan_ready", recipes=total, compute=len(cs), chunks=(total + 1999) // 2000)
    # Every part in its own namespace: reopening a cache another process just wrote is what
    # made D3DMetal declare caches corrupted on an M1.  Parts are merged offline at the end.
    for stale in d3dm_root().glob("owwarmbuild_*.exe"):
        shutil.rmtree(stale, ignore_errors=True)
    t0 = time.time()
    builts, family_name = [], None
    # 2000 per process: past a few thousand pipelines in one process the GPU driver hits its
    # compiled-variants limit and the rest never reach macOS's Metal shader cache (in-game stutter)
    for i, first in enumerate(range(0, total, 2000)):
        count = min(2000, total - first)
        ns_name = f"owwarmbuild_{os.getpid()}_{i}.exe"
        ns_parent = d3dm_root() / ns_name / "shaders.cache"

        def cb(done, _tot, first=first):
            el = time.time() - t0
            all_done = first + done
            emit("progress", units_done=all_done, units_total=total,
                 percent=round(100 * all_done / total, 1), elapsed=round(el),
                 eta=round((total - all_done) / (all_done / el)) if all_done and el else 0,
                 rate=round(all_done / el) if el else 0)
        for attempt in (1, 2):
            shutil.rmtree(ns_parent.parent, ignore_errors=True)
            r = run_warmgen(wine, gptk, prefix, plan_dir, ns_name, work, first=first, count=count,
                            with_cs=(first + count >= total), progress_cb=cb,
                            draw_vertices=draw, dispatch=(1 if draw else 0))
            fam = family_dir(ns_parent)
            got = inst.arena(fam / "stage_cache.bin")[1] if fam and (fam / "stage_cache.bin").exists() else 0
            log(f"warmgen   : part {i} draw={draw} ok={r['ok']} recorded={r['recorded']} "
                f"skipped={r['skipped']} records={got}")
            if got == 0 and attempt == 1:
                draw = 3        # nothing kept: zero-vertex draws unsupported here, or an early exit
                continue
            break
        if not got:
            return None, None
        builts.append(fam)
        family_name = fam.name
    gptk_pick.remember_draw_mode(family_name, gptk, draw)
    out = work / "fallback_merged"
    shutil.rmtree(out, ignore_errors=True)
    merge_ns.merge(builts, out, with_bytecode=False)
    for fam in builts:
        shutil.rmtree(fam.parent.parent, ignore_errors=True)
    return out, family_name


def main() -> int:
    ap = argparse.ArgumentParser(description="прогрев шейдерного кэша Overwatch")
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--shards", type=int, default=10)
    ap.add_argument("--keep", action="store_true", help="не удалять рабочие файлы")
    for k in ("game", "gptk", "wine"):
        ap.add_argument(f"--{k}")
    a = ap.parse_args()

    def emit(event, **kw):
        if a.json:
            print("@@ " + json.dumps({"event": event, **kw}), flush=True)

    # --detect is polled by the GUI; giving each poll its own log file buries the real runs.
    logpath, logf = None, None
    if not a.detect:
        LOGDIR.mkdir(parents=True, exist_ok=True)
        logpath = LOGDIR / ("warm_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
        logf = logpath.open("a", encoding="utf-8")

    def log(msg):
        if logf:
            logf.write(msg + "\n")
            logf.flush()

    game = find_game(a.game)
    live = d3dm_root() / EXE / "shaders.cache"
    fam_live = family_dir(live)
    gptk, wine = find_gptk(a.gptk, fam_live), find_wine(a.wine)
    counts = {}
    if fam_live:
        for f in ("stage_cache.bin", "pipeline_cache.bin"):
            counts[f.split("_")[0]] = inst.arena(fam_live / f)[1] if (fam_live / f).exists() else 0
    build = game_build(game) if game else None
    stamp = read_stamp()
    info = {"game": str(game) if game else None, "build": build,
            "gptk": str(gptk) if gptk else None,
            "gptk_version": gptk.name.replace("gptk-", "") if gptk else None,
            "gptks": list_gptks(),
            "wine": str(wine) if wine else None, "cache": str(fam_live) if fam_live else None,
            "cache_shaders": counts.get("stage", 0), "cache_pipelines": counts.get("pipeline", 0),
            "cache_stale": bool(build and stamp.get("build") and stamp["build"] != build),
            "game_running": game_running(), "free_gb": round(free_gb(Path.home()), 1),
            "ram_gb": ram_gb(),
            "ready": bool(game and gptk and wine)}
    # A cache warmed by 1.21-1.35 can carry pipelines pointing at the wrong shaders (a black
    # screen on GPTK 4).  Check it once and drop them — never while the game has it open.
    info["repaired"] = 0
    if fam_live and not info["game_running"]:
        try:
            info["repaired"] = merge_ns.repair_once(fam_live, APPDIR / "work")
        except Exception as e:
            log("repair    : failed %r" % (e,))
        if info["repaired"]:
            log("repair    : dropped %d pipelines with wrong stage pointers" % info["repaired"])
    if a.detect:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    log("detect    : " + json.dumps(info, ensure_ascii=False))

    wine_switch = None
    if not a.wine and not info["game_running"]:
        try:
            wine_switch = gamehub_wine.ensure_game_wine(GH, OVERWATCH_STEAM_APP_ID, "10")
            wine = Path(wine_switch["path"])
            info["wine"] = str(wine)
            log("gamehub   : Wine Proton 10 " + ("selected automatically" if wine_switch["changed"] else "already selected"))
        except Exception as exc:
            wine = None
            info["wine"] = None
            log("gamehub   : cannot select Wine Proton 10: " + str(exc))

    emit("stage", n=0, of=4, name="preflight")
    # Gatekeeper kills our quarantined x86_64 helpers on exec; clear the flag before relying on them
    quarantine_left = gptk_pick.clear_quarantine(HERE)
    dropped = gptk_pick.drop_legacy_backups(d3dm_root())   # left by older versions of this app
    if dropped:
        log("cleanup   : removed old backups " + ", ".join(dropped))
    checks = [("Overwatch найден", bool(game), str(game or "")),
              ("GPTK " + (gptk.name.replace("gptk-", "") if gptk else WANT_GPTK.replace("gptk-", "")),
               bool(gptk), str(gptk or "")),
              ("Wine Proton 10", bool(wine),
               (("выбран автоматически · " if wine_switch and wine_switch["changed"] else "") + str(wine))
               if wine else "не установлен — скачай Wine Proton 10 в GameHub (Настройки → Wine)"),
              ("карантин снят", not quarantine_left,
               "" if not quarantine_left else f"{len(quarantine_left)} файлов — выполни: {HINT}"),
              ("игра закрыта", not info["game_running"], ""),
              # the DXBC and metallib converters are x86_64 binaries run through `arch -x86_64`
              ("Rosetta", gptk_pick.rosetta_ok(), "нужна для конвертеров"),
              # the cache lives in the system cache dir, not next to the game
              (f"свободно ≥ {MIN_FREE_GB} ГБ на системном диске",
               info["free_gb"] >= MIN_FREE_GB, f"{info['free_gb']} ГБ")]
    for name, ok, detail in checks:
        emit("check", name=name, ok=ok, detail=detail)
        log(f"check     : {name} ok={ok} {detail}")
    if not all(ok for _, ok, _ in checks):
        emit("error", reason="preflight_failed")
        print("ОШИБКА: не пройдены проверки перед прогревом (см. лог)")
        return 1

    if info["ram_gb"] and info["ram_gb"] < 24:
        # D3DMetal loads the whole cache at launch — measured at ~5 ГБ for the full roster
        note = ("памяти %d ГБ: полный кэш Overwatch весит около 5 ГБ и целиком грузится в память "
                "при старте игры, так что выигрыш может съесть подкачка" % info["ram_gb"])
        print("ВНИМАНИЕ: " + note)
        emit("log", note=note)
        log("warn      : " + note)

    work = APPDIR / "work"
    work.mkdir(parents=True, exist_ok=True)
    prefix = prefix_for(wine, work)
    harness = prepare_tools(gptk, work)
    build = game_build(game)
    corpus = APPDIR / f"corpus_{build}"

    emit("stage", n=1, of=4, name="extract")
    print("1/4 достаю шейдеры игры…")
    if (corpus / "index.jsonl").exists():
        log("extract   : corpus for build %s already present" % build)
    else:
        ex = extract(game, corpus, a.shards, emit, log)
        if ex["shaders"] < 1000:
            emit("error", reason="extract_failed", shaders=ex["shaders"])
            print("ОШИБКА: не удалось достать шейдеры игры")
            return 1

    emit("stage", n=2, of=4, name="calibrate")
    print("2/4 сверяю сборку с D3DMetal этой машины…")
    fam, cal = calibrate(wine, gptk, prefix, corpus, work, harness, emit, log)
    emit("check", name="сверка с D3DMetal", ok=bool(cal.get("ok")),
         detail=f"{cal.get('identical')}/{cal.get('records')} записей совпали" if cal.get("records") else str(cal.get("why", "")))
    log("calibrate : " + json.dumps(cal, ensure_ascii=False))

    emit("stage", n=3, of=4, name="build")
    if fam is not None:
        print("3/4 собираю кэш (быстрый офлайн-путь, ~5 мин)…")
        built = build_offline(corpus, work, gptk / "external/D3DMetal.framework/Versions/A/Resources",
                              cal["gpu_family"], fam / "version.bin", harness, emit, log)
        family_name = cal["family_dir"]
    else:
        print("3/4 сверка не сошлась — собираю кэш через D3DMetal (это долго, ~50 мин)…")
        emit("log", note="fallback_warmgen")
        built, family_name = build_via_warmgen(corpus, work, wine, gptk, prefix, emit, log,
                                               draw=cal.get("draw", 0))
    if built is None:
        emit("error", reason="build_failed")
        print("ОШИБКА: не удалось собрать кэш")
        return 1

    emit("stage", n=4, of=4, name="install")
    print("4/4 объединяю с текущим кэшем и устанавливаю…")
    final = work / "final"
    shutil.rmtree(final, ignore_errors=True)
    inputs = ([fam_live] if fam_live and (fam_live / "stage_cache.bin").exists() else []) + [built]
    try:
        rep = merge_ns.merge(inputs, final, with_bytecode=False)
    except merge_ns.VersionMismatch:
        # version.bin carries the D3DMetal build AND the macOS version, so a macOS update between
        # playing and warming makes the game's own cache unmergeable.  The game drops such a cache
        # on its next launch anyway — install the fresh one instead of losing the whole warm.
        shutil.rmtree(final, ignore_errors=True)
        rep = merge_ns.merge([built], final, with_bytecode=False)
        log("merge     : previous cache carries a different version.bin — dropped as stale")
        emit("log", note="прежний кэш был от другой версии GPTK/macOS — отброшен")
    log("merge     : " + json.dumps(rep))
    stage_records_n = rep["stage_cache.bin"]["records"]

    if a.dry_run:
        emit("dry_run_ok", shaders=stage_records_n)
        print(f"ГОТОВО (dry-run): собран кэш на {stage_records_n} записей, установка пропущена")
        if not a.keep:
            for d in (work / "mint_work", final, built):
                shutil.rmtree(d, ignore_errors=True)
        return 0

    target = live / (family_name or gptk_pick.family_dir_name(d3dm_root()))
    inst.LIVE_PARENT = live
    inst.LIVE = target
    # No backup copy: the fresh cache is a union with the old one, so it already holds every record
    # the game had.  The copy cost ~5 GB of disk on every warm and was never cleaned up.  The
    # install itself is safe to interrupt: each file is cloned aside and renamed into place.
    inst.copy_into_live(final, True)
    log(f"install   : {stage_records_n} records -> {target}")
    metal_cache.after_warm(log)
    if not a.keep:
        shutil.rmtree(work / "mint_work", ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        shutil.rmtree(built, ignore_errors=True)
    stamp_path().write_text(json.dumps({"build": build, "shaders": stage_records_n,
                                        "time": datetime.datetime.now().isoformat(timespec="seconds")}))
    emit("done", shaders=stage_records_n, added=stage_records_n - info["cache_shaders"],
         pipelines=rep["pipeline_cache.bin"]["records"])
    print(f"\nГОТОВО. В кэше {stage_records_n} записей. Запускай игру ({gptk.name}).")
    return 0


if __name__ == "__main__":
    try:
        os.setpgrp()  # the GUI cancels Python, Wine and offline compilers together
    except OSError:
        pass
    sys.exit(gptk_pick.detect_guard(main, LOGDIR))
