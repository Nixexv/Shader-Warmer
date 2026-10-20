#!/usr/bin/env python3
"""Прогрев шейдерного кэша Unity-игры (D3D11 через D3DMetal).

Три шага:

  1 extract    шейдеры достаются прямо из ассетов игры (свой разбор + LZ4), секунды
  2 build      каждый шейдер рисуется один раз на класс render target через D3DMetal;
               классы берутся из кэша, который игра написала сама
  3 install    слияние с текущим кэшем игры и установка с бэкапом

Игру нужно один раз запустить до прогрева: без её собственного кэша неизвестно, какие классы
render target она использует.

  unity_warm.py --game <папка игры> [--detect] [--json] [--dry-run] [--gptk PATH] [--wine PATH]
"""
from __future__ import annotations
import argparse, collections, datetime, hashlib, json, os, re, shutil, struct, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "ow"))
sys.path.insert(0, str(HERE / "unity"))
import owplan, merge_ns  # noqa: E402
sys.path.insert(0, str(HERE))
import gptk_pick  # noqa: E402
import metal_cache  # noqa: E402
import unity_dxbc, unity_assets  # noqa: E402

GH = Path.home() / "Library/Application Support/com.gamemac.www/wine-engine"
APPDIR = Path.home() / "Library/Application Support/ShaderWarmer"
LOGDIR = Path.home() / "Library/Logs/ShaderWarmer"
WARMGEN = HERE / "ow/bin/warmgen_ow.exe"
DXBC2DXIL = HERE / "ow/bin/dxbc2dxil"
DEFAULT_CLASSES = [(0, 1, 0, 0), (0, 1, 0, 1)]
STALL_SECONDS = 4          # the converter does ~500 shaders/s; 4 s without progress means a hang
MIN_FREE_GB = 4
HINT = 'xattr -dr com.apple.quarantine "/Applications/Shader Warmer.app"'

# Metal links varyings by name AND type: a pixel shader input must be written by the vertex shader
# with the same component type and at least the same components, or pipeline creation fails.  D3D is
# laxer, so the plain name/register match owplan uses for Overwatch is not enough here.
owplan.SYSTEM_INPUTS = {"SV_ISFRONTFACE", "SV_SAMPLEINDEX", "SV_COVERAGE", "SV_VERTEXID"}


def _sig_out(vs):
    return frozenset((e[0], e[1], e[2], e[3], e[4]) for e in vs["out"])


def _sig_in(ps):
    return frozenset((e[0], e[1], e[2], e[3], e[4]) for e in ps["in"] if e[0] not in owplan.SYSTEM_INPUTS)


def _covers(self, out_sig, in_sig):
    k = (out_sig, in_sig)
    if k not in self.compat:
        have = {(n, si): (reg, ct, m) for n, si, reg, ct, m in out_sig}
        ok = True
        for n, si, reg, ct, m in in_sig:
            got = have.get((n, si))
            if got is None or got[0] != reg or got[1] != ct or (m & ~got[2]):
                ok = False
                break
        self.compat[k] = ok
    return self.compat[k]


owplan.Planner.sig_out = staticmethod(_sig_out)
owplan.Planner.sig_in = staticmethod(_sig_in)
owplan.Planner.covers = _covers


# ---------------------------------------------------------------- environment
def d3dm_root() -> Path:
    out = subprocess.run(["getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True, text=True).stdout.strip()
    return Path(out) / "d3dm"


def unity_data_dir(game: Path):
    for d in sorted(game.glob("*_Data")):
        if (d / "globalgamemanagers").exists():
            return d
    return None


def game_exe(game: Path):
    data = unity_data_dir(game)
    if data is None:
        return None
    exe = game / (data.name[:-5] + ".exe")          # "<name>_Data" -> "<name>.exe"
    if exe.exists():
        return exe.name
    others = [p.name for p in game.glob("*.exe") if "crashhandler" not in p.name.lower()]
    return others[0] if others else None


def list_gptks():
    return [{"path": str(d), "version": d.name.replace("gptk-", "")}
            for d in sorted((GH / "downloads").glob("gptk-*")) if (d / "external/D3DMetal.framework").exists()]


def find_wine(gptk: Path, override=None):
    """GPTK 4.x ships with Proton 11, 3.x with Proton 10 — pick the Wine the launcher pairs it with."""
    if override:
        return Path(override)
    want = "11" if gptk and gptk.name.startswith("gptk-4") else "10"
    best = None
    for d in sorted((GH / "containers/wine_installations").glob("*")):
        man, wine = d / "manifest.json", d / "bin/wine"
        if not man.exists() or not wine.exists():
            continue
        try:
            m = json.loads(man.read_text())
        except Exception:
            continue
        if m.get("architecture") != "x86_64":
            continue
        if m.get("metadata", {}).get("proton_version", "").startswith(want):
            best = d
    return best


def free_gb(path: Path) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 2 ** 30


def prefix_for(wine: Path, work: Path) -> Path:
    pfx = work / "prefix"
    if not pfx.exists():
        subprocess.run(["cp", "-c", "-R", str(wine / "prefix"), str(pfx)], check=True)
        (pfx / ".update-timestamp").write_text("disable")   # or wineboot spends 10+ min updating
    dos = pfx / "dosdevices"
    dos.mkdir(exist_ok=True)
    for drive, target in (("c:", "../drive_c"), ("z:", "/")):
        if not (dos / drive).is_symlink():
            (dos / drive).symlink_to(target)
    return pfx


# ---------------------------------------------------------------- cache
def stage_records(path: Path):
    if not path.exists() or path.stat().st_size < 24:
        return
    import mmap
    f = path.open("rb")
    m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    end = struct.unpack_from("<QQQ", m, 0)[1]
    off = 24
    while off < end:
        framed, payload = struct.unpack_from("<QQ", m, off)
        st, _, _, ks = struct.unpack_from("<4I", m, off + 24)
        yield st, bytes(m[off + 24 + 0x28:off + 24 + 0x28 + ks]), bytes(m[off + 24:off + 24 + payload])
        off += framed


def family_dir(cache: Path):
    fams = sorted(cache.glob("MTLGPUFamily*_0")) if cache.exists() else []
    return fams[0] if fams else None


def gpu_family_of(family: Path) -> int:
    """D3DMetal family number (Apple7=M1, Apple8=M2, Apple9=M3, Apple10=M4/M5)."""
    match = re.search(r"MTLGPUFamilyApple(\d+)_", family.name)
    return 1000 + int(match.group(1)) if match else 1010


def learn_classes(family: Path):
    c = collections.Counter()
    for st, key, _ in stage_records(family / "stage_cache.bin") or ():
        if st == 2:
            c[(key[24], key[25], key[26], key[27])] += 1
    return [cls for cls, _ in c.most_common()], {str(k): v for k, v in c.items()}


def classes_for(ps, classes):
    tmask, imask = owplan.ps_shape(ps)
    out = []
    for dsb, dfb, intrt, un8 in classes:
        if (un8 | intrt) & ~tmask or intrt & ~imask:
            continue
        out.append((dsb, dfb, intrt, un8))
    return out or [c for c in DEFAULT_CLASSES if not (c[3] & ~tmask)] or [(0, 1, 0, 0)]


# ---------------------------------------------------------------- steps
def extract(data: Path, out: Path, emit, log):
    shutil.rmtree(out, ignore_errors=True)
    t0 = time.time()

    def on_file(name, done, total, found):
        el = time.time() - t0
        emit("progress", units_done=done, units_total=total, percent=round(100 * done / total, 1),
             elapsed=round(el), eta=round((total - done) / (done / el)) if el and done else 0,
             rate=round(found / el) if el else 0)
        print(f"  {name}: {found} шейдеров", flush=True)
    for attempt in (1, 2):
        try:
            summary = unity_dxbc.run(data, out, on_file)
            break
        except OSError as e:
            log(f"extract   : unreadable (try {attempt}) {e!r}")
            if attempt == 1:        # give a drive that is still waking up one more try
                time.sleep(5)
                continue
            emit("error", reason="game_unreadable", path=str(e.filename or data))
            print(gptk_pick.unreadable_message(e, data))
            raise SystemExit(1)
    log("extract   : " + json.dumps(summary))
    return summary


def usable_shaders(corpus: Path, res: Path, work: Path, emit, log):
    """Only what D3DMetal can compile: games ship feature-level 9.x shaders that its own converter
    rejects — and hangs on some — and one of those takes the whole warm down."""
    cache = work / "usable.json"
    if cache.exists():
        return set(json.loads(cache.read_text()))
    files = [p for sub in ("vs", "ps", "cs") for p in sorted((corpus / sub).glob("*.dxbc"))]
    tmp = work / "conv"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    def shard(batch, tag, seen):
        """Convert a shard, resuming past whatever the converter hangs on.  It prints a JOB line
        before each shader, so a timeout tells us exactly which one to skip."""
        start = 0
        while start < len(batch):
            todo = batch[start:]
            jobs = work / f"conv_{tag}.tsv"
            jobs.write_text("".join(f"{p}\t{tmp / (p.stem + '.dxil')}\n" for p in todo))
            out = work / f"conv_{tag}.log"
            # The converter names every shader before it starts on it, so a hang shows up as a JOB
            # line that stops advancing — no need to wait out a timeout proportional to the batch.
            with out.open("w") as fo:
                proc = subprocess.Popen(["arch", "-x86_64", str(DXBC2DXIL), str(res), str(jobs)],
                                        stdout=fo, stderr=subprocess.DEVNULL)
                hung = False
                last_seen, last_move = -1, time.time()
                while proc.poll() is None:
                    time.sleep(0.25)
                    try:
                        n = out.read_text().count("JOB ")
                    except OSError:
                        n = last_seen
                    if n != last_seen:
                        last_seen, last_move = n, time.time()
                    elif time.time() - last_move > STALL_SECONDS:
                        proc.kill()
                        proc.wait()
                        hung = True
                        break
            # The converter both hangs and crashes on shaders it cannot take.  Either way its last
            # JOB line names the one it died on: skip that one and carry on with the rest.
            lines = [l for l in out.read_text().splitlines() if l.startswith("JOB ")]
            stuck = int(lines[-1].split()[1]) if lines else 0
            if not hung and stuck >= len(todo) - 1:
                start = len(batch)
            else:
                log(f"convert   : {'hang' if hung else 'crash'} on {todo[stuck].name}, skipping")
                start += stuck + 1
            jobs.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
        seen[0] += len(batch)
        el = time.time() - t_check
        emit("progress", units_done=seen[0], units_total=len(files),
             percent=round(100 * seen[0] / len(files), 1), elapsed=round(el),
             eta=round((len(files) - seen[0]) / (seen[0] / el)) if el and seen[0] else 0,
             rate=round(seen[0] / el) if el else 0)
        print(f"  проверено {seen[0]}/{len(files)} шейдеров", flush=True)
        return [p for p in batch if (tmp / (p.stem + ".dxil")).exists()]

    from concurrent.futures import ThreadPoolExecutor
    shards = [files[i::8] for i in range(8)]
    seen = [0]
    t_check = time.time()
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda t: shard(t[1], f"{t[0]}", seen), enumerate(shards)))
    ok = {p.name for r in results for p in r}
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"usable    : {len(ok)}/{len(files)}")
    cache.write_text(json.dumps(sorted(ok)))
    return ok


def build_plan(corpus: Path, out: Path, classes, usable):
    keep = (lambda p: p.name in usable)
    vs_list = [owplan.load(p) for p in sorted((corpus / "vs").glob("*.dxbc")) if keep(p)]
    ps_list = [owplan.load(p) for p in sorted((corpus / "ps").glob("*.dxbc")) if keep(p)]
    cs = [p for p in sorted((corpus / "cs").glob("*.dxbc")) if keep(p)]
    pl = owplan.Planner(vs_list, ps_list)
    miss_ps = sum(0 if pl.ps(ps, classes_for(ps, classes)) else 1 for ps in ps_list)
    ps_draws = len(pl.recipes)
    miss_vs = sum(0 if pl.vs(vs) else 1 for vs in vs_list)
    stats = {"ps_shaders": len(ps_list), "ps_draws": ps_draws, "ps_without_carrier": miss_ps,
             "vs_shaders": len(vs_list), "vs_draws": len(pl.recipes) - ps_draws,
             "vs_without_partner": miss_vs, "cs": len(cs)}
    owplan.write_plan(out, pl.recipes, cs, stats)
    return stats


def run_warmgen(wine, gptk, prefix, plan_dir, exe_name, work, first, count, with_cs, timeout=3600,
                progress_cb=None, draw_vertices=0, dispatch=0):
    wine = gptk_pick.warm_wine(wine / "bin/wine").parent.parent   # the games' GPU-code cache stays untouched
    exe = work / exe_name
    shutil.copy2(WARMGEN, exe)
    env = dict(os.environ)
    env.update({"WINEPREFIX": str(prefix), "WINEDLLPATH": str(gptk / "wine"),
                "WINE_GPTK_LIBD3DSHARED_PATH": str(gptk / "external/libd3dshared.dylib"),
                "WINEARCH": "win64", "WINEMSYNC": "1", "WINEDEBUG": "-all", "D3DM_NO_WINDOW": "1",
                "MEDIACONV_BLANK_AUDIO_FILE": str(wine / "share/media/blank.ptna"),
                "MEDIACONV_BLANK_VIDEO_FILE": str(wine / "share/media/blank.mkv"),
                "WARMGEN_DRAW_VERTICES": str(draw_vertices), "WARMGEN_DISPATCH": str(dispatch),
                "WARMGEN_FIRST": str(first), "WARMGEN_CS": "1" if with_cs else "0"})
    if count > 0:
        env["WARMGEN_COUNT"] = str(count)
    t = time.time()
    lines = []
    p = subprocess.Popen([str(wine / "bin/wine"), str(exe), "Z:" + str(plan_dir.resolve()).replace("/", "\\"), "40"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in p.stdout:                      # warmgen is line buffered: report every batch
        lines.append(line)
        if progress_cb and line.startswith("PROGRESS "):
            done_total = line.split()[1]
            progress_cb(int(done_total.split("/")[0]))
        if time.time() - t > timeout:
            p.kill()
            break
    p.wait()
    subprocess.run([str(wine / "bin/wineserver"), "-w"], env=env, capture_output=True, timeout=600)
    exe.unlink(missing_ok=True)
    text = "".join(lines)
    (work / f"warmgen_{first:06d}.log").write_text(text)
    return {"rc": p.returncode, "ok_log": "WARMGEN_OK" in text, "seconds": round(time.time() - t, 1)}


def scan(game: Path, data, exe, gptk, live, shaders, emit, log) -> int:
    """Is this game warmable?  Reads the shipped assets only: the game is never launched and no
    cache is touched, so it can be run on a game that has just been installed."""
    print(f"игра      : {game.name}")
    if data is None or exe is None:
        print("вердикт   : НЕ ПОДХОДИТ — это не Unity-игра (нет папки *_Data с ассетами)")
        emit("scan", unity=False, verdict="not_unity")
        return 1
    version = ""
    try:
        version = unity_assets.parse(data / "globalgamemanagers")["unity"]
    except Exception:
        pass
    # Addressables ship their shaders inside .bundle archives; the carver opens those too.
    bundles = sum(1 for p in data.rglob("*.bundle"))
    print(f"движок    : Unity {version or '?'}   ассетов: {sum(1 for _ in data.rglob('*.assets'))}"
          + (f"   .bundle: {bundles}" if bundles else ""))
    if gptk is None:
        print("вердикт   : НЕ ПРОВЕРИТЬ — не найдена GPTK")
        emit("scan", verdict="no_gptk")
        return 1

    work = APPDIR / ("scan_" + hashlib.sha256(str(game).encode()).hexdigest()[:12])
    work.mkdir(parents=True, exist_ok=True)
    corpus = work / "corpus"
    res = gptk / "external/D3DMetal.framework/Versions/A/Resources"
    print("читаю ассеты…")
    summary = extract(data, corpus, emit, log)
    found = summary["unique"]
    if found < 10:
        print(f"вердикт   : НЕ ПОДХОДИТ — в ассетах нашлось всего {found} шейдеров "
              "(сборка не под DirectX или шейдеры лежат не в ассетах)")
        emit("scan", unity=True, shaders=found, verdict="no_shaders")
        shutil.rmtree(work, ignore_errors=True)
        return 1
    print(f"шейдеров  : {found} (" + ", ".join(f"{k}: {v}" for k, v in sorted(summary["stages"].items())) + ")")
    print("проверяю, что их принимает конвертер D3DMetal…")
    usable = usable_shaders(corpus, res, work, emit, log)
    share = round(100 * len(usable) / found)
    print(f"пригодно  : {len(usable)} из {found} ({share}%)")
    played = shaders > 0
    print("кэш игры  : " + (f"{shaders} записей — прогревать можно прямо сейчас"
                            if played else "пуст — сначала один раз запусти игру"))
    verdict = "ok" if share >= 40 else "poor"
    print("вердикт   : " + ("ПОДХОДИТ" if verdict == "ok" else
                            "СОМНИТЕЛЬНО — конвертер принимает слишком мало шейдеров")
          + (", но сперва один запуск игры" if verdict == "ok" and not played else ""))
    if bundles:
        print(f"примечание: {bundles} файлов .bundle (Addressables) тоже прочитаны")
    emit("scan", unity=True, unity_version=version, shaders=found, usable=len(usable),
         share=share, bundles=bundles, played=played, verdict=verdict)
    log(f"scan      : {game.name} unity={version} found={found} usable={len(usable)} verdict={verdict}")
    shutil.rmtree(work / "conv", ignore_errors=True)
    return 0 if verdict == "ok" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="прогрев шейдерного кэша Unity-игры")
    ap.add_argument("--game", required=True)
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--scan", action="store_true",
                    help="совместима ли игра: читает ассеты, не запускает игру и не трогает кэш")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gptk")
    ap.add_argument("--wine")
    # 2000 recipes per warmgen process: one process over thousands of pipelines runs the GPU driver
    # past its compiled-variants limit and the rest never reach macOS's Metal shader cache (in-game
    # stutter).  Deadlock proved 2000 safe; 12 000 in one process is not.
    ap.add_argument("--chunk", type=int, default=2000)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    def emit(event, **kw):
        if a.json:
            print("@@ " + json.dumps({"event": event, **kw}), flush=True)

    # --detect is polled by the GUI; giving each poll its own log file buries the real runs.
    logf = None
    if not a.detect:
        LOGDIR.mkdir(parents=True, exist_ok=True)
        logf = (LOGDIR / ("unity_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")).open("a")

    def log(msg):
        if logf:
            logf.write(msg + "\n")
            logf.flush()

    game = Path(a.game)
    data = unity_data_dir(game)
    exe = game_exe(game)
    gptks = list_gptks()
    live_parent = (d3dm_root() / exe / "shaders.cache") if exe else None
    live = family_dir(live_parent) if live_parent else None
    # GameHub gives different games different GPTKs, and a cache built under the wrong one is dead
    # weight: the game drops a cache whose version.bin differs.  The cache the game wrote on its own
    # first launch says which GPTK it runs under, so trust that over the newest installed one.
    matched = gptk_pick.gptk_for_cache([Path(g["path"]) for g in gptks], live)
    # What GameHub will launch the game with NEXT beats what wrote the cache LAST: if the player
    # switched the GPTK since, a warm under the stamped one would be thrown away on launch.
    configured = gptk_pick.gamehub_gptk(gptk_pick.steam_appid(game))
    wanted = configured or matched
    gptk = Path(a.gptk) if a.gptk else (Path(gptks[-1]["path"]) if gptks else None)
    gptk_note = ""
    if wanted and gptk and wanted != gptk:
        gptk_note = f"игра запускается под {wanted.name}, а не {gptk.name} — переключаюсь"
        gptk = wanted
    if configured and matched and configured != matched:
        gptk_note += ("; " if gptk_note else "") + (f"кэш игры записан под {matched.name}, а в GameHub уже "
                                                    f"{configured.name} — прежний кэш будет отброшен")
    elif live is not None and not wanted:
        gptk_note = "не удалось сверить версию GPTK с кэшем игры"
    wine = find_wine(gptk, a.wine)
    classes, hist = learn_classes(live) if live else ([], {})
    shaders = sum(1 for _ in stage_records(live / "stage_cache.bin")) if live else 0

    info = {"game": str(game), "data": str(data) if data else None, "exe": exe,
            "gptk": str(gptk) if gptk else None,
            "gptk_version": gptk.name.replace("gptk-", "") if gptk else None, "gptks": gptks,
            "gptk_matched": matched.name if matched else None, "gptk_note": gptk_note,
            "wine": str(wine) if wine else None, "cache": str(live) if live else None,
            "cache_shaders": shaders, "classes": hist,
            "game_running": subprocess.run(["pgrep", "-if", exe or "@@none@@"], capture_output=True).returncode == 0,
            "free_gb": round(free_gb(Path.home()), 1),
            # the game's own cache is what tells us which render-target classes it uses
            "needs_play": not bool(classes),
            "runtime_error": None if (data and exe and gptk and wine) else "не найден рантайм или файлы игры",
            "ready": bool(data and exe and gptk and wine and classes)}
    # A cache warmed by 1.21-1.35 can carry pipelines pointing at the wrong shaders (a black
    # screen on GPTK 4).  Check it once and drop them — never while the game has it open.
    info["repaired"] = 0
    if live and not info["game_running"]:
        try:
            info["repaired"] = merge_ns.repair_once(live, APPDIR / "work")
        except Exception as e:
            log("repair    : failed %r" % (e,))
        if info["repaired"]:
            log("repair    : dropped %d pipelines with wrong stage pointers" % info["repaired"])
    if a.detect:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    log("detect    : " + json.dumps(info, ensure_ascii=False))

    if a.scan:
        return scan(game, data, exe, gptk, live, shaders, emit, log)

    emit("stage", n=0, of=3, name="preflight")
    # Gatekeeper kills our quarantined x86_64 helpers on exec; clear the flag before relying on them
    quarantine_left = gptk_pick.clear_quarantine(HERE)
    dropped = gptk_pick.drop_legacy_backups(d3dm_root())   # left by older versions of this app
    if dropped:
        log("cleanup   : removed old backups " + ", ".join(dropped))
    checks = [("игра найдена", bool(data and exe), exe or ""),
              ("GPTK", bool(gptk), (gptk.name if gptk else "") + (f" · {gptk_note}" if gptk_note else "")),
              ("Wine", bool(wine), wine.name if wine else ""),
              ("игра закрыта", not info["game_running"], ""),
              ("кэш игры (нужен один запуск)", bool(classes), f"{shaders} записей" if shaders else "пусто"),
              # the DXBC converter is an x86_64 binary run through `arch -x86_64`
              ("Rosetta", gptk_pick.rosetta_ok(), "нужна для конвертера"),
              ("карантин снят", not quarantine_left,
               "" if not quarantine_left else f"{len(quarantine_left)} файлов — выполни: {HINT}"),
              (f"свободно ≥ {MIN_FREE_GB} ГБ на системном диске",
               info["free_gb"] >= MIN_FREE_GB, f"{info['free_gb']} ГБ")]
    for name, ok, detail in checks:
        emit("check", name=name, ok=ok, detail=detail)
        log(f"check     : {name} ok={ok} {detail}")
    if not all(ok for _, ok, _ in checks):
        emit("error", reason="preflight_failed")
        print("ОШИБКА: не пройдены проверки (см. лог)")
        return 1
    if gptk_note:
        print("GPTK: " + gptk_note)
    # M1/Apple7 may persist no stage records for Draw(0).  Its real draws are chunked and use the
    # render-target classes learned from this exact game's own cache.
    fam_name = live.name if live else gptk_pick.family_dir_name(d3dm_root())
    draw = gptk_pick.known_draw_mode(fam_name, gptk) or 0   # the first part tells if 0 works here
    emit("preflight_ok", gpu=bool(draw))

    work = APPDIR / ("work_" + hashlib.sha256(str(game).encode()).hexdigest()[:12])
    work.mkdir(parents=True, exist_ok=True)
    corpus = work / "corpus"
    res = gptk / "external/D3DMetal.framework/Versions/A/Resources"

    emit("stage", n=1, of=3, name="extract")
    print("1/3 достаю шейдеры игры…")
    summary = extract(data, corpus, emit, log)
    emit("plan_ready", recipes=summary["unique"], compute=summary["stages"].get("cs", 0), chunks=1)
    if summary["unique"] < 10:
        emit("error", reason="extract_failed", shaders=summary["unique"])
        print("ОШИБКА: не удалось достать шейдеры из ассетов игры")
        return 1

    emit("stage", n=2, of=3, name="build")
    print("2/3 проверяю шейдеры и прогреваю…")
    usable = usable_shaders(corpus, res, work, emit, log)
    plan_dir = work / "plan"
    shutil.rmtree(plan_dir, ignore_errors=True)
    stats = build_plan(corpus, plan_dir, classes, usable)
    log("plan      : " + json.dumps(stats))
    total = sum(1 for l in (plan_dir / "prewarm_plans.txt").open() if l.startswith("RECIPE "))

    # Every part warms into its own namespace: a process reopening a cache another one just wrote
    # is what made D3DMetal declare caches corrupted and wipe them on an M1.
    ns_base = f"warmbuild_{exe.replace(' ', '_')}"
    for stale in d3dm_root().glob(ns_base + "*"):
        shutil.rmtree(stale, ignore_errors=True)
    prefix = prefix_for(wine, work)
    t0 = time.time()

    def report(done_now):
        el = time.time() - t0
        emit("progress", units_done=done_now, units_total=total,
             percent=round(100 * done_now / total, 1), elapsed=round(el),
             eta=round((total - done_now) / (done_now / el)) if el and done_now else 0,
             rate=round(done_now / el) if el else 0)

    builts, scratch = [], []
    for i, first in enumerate(range(0, total, a.chunk)):
        count = min(a.chunk, total - first)
        ns_name = f"{ns_base}_{i}"
        ns_parent = d3dm_root() / ns_name / "shaders.cache"
        scratch.append(ns_parent.parent)
        for attempt in (1, 2):
            shutil.rmtree(ns_parent.parent, ignore_errors=True)
            r = run_warmgen(wine, gptk, prefix, plan_dir, ns_name, work, first, count,
                            first + count >= total, progress_cb=report,
                            draw_vertices=(3 if draw else 0), dispatch=(1 if draw else 0))
            fam = family_dir(ns_parent)
            got = sum(1 for _ in stage_records(fam / "stage_cache.bin")) if fam else 0
            log(f"warmgen   : part {i} draw={draw} {json.dumps(r)} records={got}")
            if got == 0 and attempt == 1:
                # nothing kept: zero-vertex draws unsupported on this GPU + GPTK, or the process
                # exited before D3DMetal flushed — one more run, with real draws
                draw = 3
                log("warmgen   : part persisted nothing — retrying with real draws")
                continue
            break
        report(min(first + count, total))
        print(f"  прогрето {min(first + count, total)}/{total}", flush=True)
        if got:
            builts.append(fam)
        elif not r["ok_log"]:
            emit("error", reason="warm_failed", chunk=first)
            print("ОШИБКА: прогрев не удался")
            for d in scratch:
                shutil.rmtree(d, ignore_errors=True)
            return 1
    if not builts:
        emit("error", reason="warm_failed")
        return 1
    gptk_pick.remember_draw_mode(fam_name, gptk, draw)

    emit("stage", n=3, of=3, name="install")
    same = collections.Counter()
    for st, key, payload in stage_records(live / "stage_cache.bin") or ():
        if st in (1, 2, 8):
            same["game"] += 1
    final = work / "final"
    shutil.rmtree(final, ignore_errors=True)
    stale = False
    try:
        rep = merge_ns.merge([live] + builts, final, with_bytecode=False)
    except merge_ns.VersionMismatch:
        # version.bin carries the D3DMetal build AND the macOS version, so a macOS update between
        # playing and warming makes the game's own records unmergeable.  The warm ran under the
        # GPTK the game uses, so the fresh cache is the good one — the game would drop the old
        # records on its next launch anyway.
        stale = True
        shutil.rmtree(final, ignore_errors=True)
        rep = merge_ns.merge(builts, final, with_bytecode=False)
        log(f"merge     : version.bin mismatch, previous cache dropped as stale (live={live})")
        print("прежний кэш игры был от другой версии GPTK/macOS — отброшен")
        emit("log", note="прежний кэш был от другой версии GPTK/macOS — отброшен")
    records = rep["stage_cache.bin"]["records"]
    log("merge     : " + json.dumps(rep))
    if a.dry_run:
        emit("dry_run_ok", shaders=records)
        print(f"ГОТОВО (dry-run): собран кэш на {records} записей, установка пропущена")
        return 0

    # No backup copy: the fresh cache is a union with the old one, so it already holds every record
    # the game had, and the per-warm copy piled up in the work dir, never cleaned.
    for f in ("bytecode_cache.bin", "pipeline_cache.bin", "rootsignature_cache.bin", "stage_cache.bin", "version.bin"):
        # Never expose a half-copied arena if the disk fills up or the app is closed mid-install.
        temporary = live / (f + ".installing")
        shutil.copy2(final / f, temporary)
        os.replace(temporary, live / f)
    log(f"install   : {records} records -> {live}")
    metal_cache.after_warm(log)
    if not a.keep:
        shutil.rmtree(final, ignore_errors=True)
        for d in scratch:
            shutil.rmtree(d, ignore_errors=True)
    emit("done", shaders=records, added=records - shaders, stale_dropped=stale,
         pipelines=rep["pipeline_cache.bin"]["records"])
    print(f"\nГОТОВО. В кэше {records} записей. Запускай игру.")
    return 0


if __name__ == "__main__":
    try:
        os.setpgrp()  # the GUI cancels Python, Wine and converters together
    except OSError:
        pass
    sys.exit(gptk_pick.detect_guard(main, LOGDIR))
