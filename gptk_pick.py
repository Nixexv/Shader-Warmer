"""What this Mac's runtime can do: which GPTK runs a game, which GPU family it writes for, where
the Steam libraries are, and whether our x86_64 helpers can run at all.

D3DMetal stamps version.bin with a build fingerprint, and the first 8 bytes of that stamp are a
constant living inside the D3DMetal binary itself.  So the cache the game wrote on its own launch
names the GPTK that wrote it.  This matters because a cache built under another GPTK is useless:
D3DMetal throws away a cache whose version.bin differs, and merge_ns refuses to mix the two — so a
warm run under the wrong GPTK is thrown away at the very end, after all the work.
"""
from __future__ import annotations

import json
import mmap
import re
import subprocess
import sys
import traceback
from pathlib import Path

GAMEHUB = Path.home() / "Library/Application Support/com.gamemac.www"


def probe(path: Path, what: str = "exists") -> bool:
    """Path.exists()/is_dir() that survive a folder macOS will not let us into.

    On Python 3.9 both RAISE PermissionError instead of returning False, and an external drive
    the app has not been allowed onto (the grant is lost whenever an ad-hoc build changes) is
    exactly such a folder — one unreadable library used to take the whole --detect down.
    """
    try:
        return getattr(Path(path), what)()
    except OSError:
        return False


def cache_stamp(family: Path | None) -> bytes | None:
    """The build fingerprint of whatever D3DMetal wrote this cache."""
    if family is None:
        return None
    v = Path(family) / "version.bin"
    try:
        data = v.read_bytes()
    except OSError:
        return None
    return data[:8] if len(data) >= 8 else None


def _d3dmetal(gptk: Path) -> Path | None:
    for rel in ("external/D3DMetal.framework/Versions/A/D3DMetal",
                "external/D3DMetal.framework/D3DMetal"):
        p = Path(gptk) / rel
        if p.is_file():
            return p
    return None


def writes_stamp(gptk: Path, stamp: bytes) -> bool:
    """True when this GPTK's D3DMetal carries that fingerprint."""
    binary = _d3dmetal(gptk)
    if binary is None or not stamp:
        return False
    try:
        with binary.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            return m.find(stamp) != -1
    except (OSError, ValueError):
        return False


def gptk_for_cache(gptks, family: Path | None) -> Path | None:
    """The installed GPTK that wrote this cache, or None when nothing matches.

    None is not proof of trouble: a macOS upgrade can restamp the cache, and the game may simply
    never have run.  Callers warn and keep their own choice instead of refusing to work.
    """
    stamp = cache_stamp(family)
    if not stamp:
        return None
    for g in gptks:
        if writes_stamp(Path(g), stamp):
            return Path(g)
    return None


# MTLGPUFamilyApple<N> per Apple's Metal docs: A14/M1 = 7, A15-A16/M2 = 8, A17/M3/M4 = 9,
# A19/M5 = 10.  Only a fallback: the app asks Metal itself and passes SHADER_WARMER_GPU_FAMILY.
_CHIP_FAMILY = ((" M1", 7), (" M2", 8), (" M3", 9), (" M4", 9), (" M5", 10))


def family_dir_name(d3dm_root: Path | None = None) -> str:
    """The MTLGPUFamilyApple<N>_0 directory D3DMetal writes on THIS Mac.

    Defaulting to Apple10 sends a cache to a folder an M1 never reads, so ask the machine: reuse
    the family any existing namespace already has, and otherwise derive it from the chip.
    """
    import os
    told = os.environ.get("SHADER_WARMER_GPU_FAMILY", "")     # the app asked Metal directly
    if re.fullmatch(r"MTLGPUFamilyApple\d+_0", told):
        return told
    if d3dm_root is not None:
        for cache in sorted(Path(d3dm_root).glob("*/shaders.cache")):
            found = sorted(cache.glob("MTLGPUFamilyApple*_0"))
            if found:
                return found[0].name
    try:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True, timeout=10).stdout
    except Exception:
        chip = ""
    for needle, family in _CHIP_FAMILY:
        if needle in " " + chip:
            return f"MTLGPUFamilyApple{family}_0"
    # A chip newer than this list: guessing a number would point at a folder nothing reads, and
    # D3DMetal does not bump the family every generation anyway (M3 and M4 are both Apple9).
    return "MTLGPUFamilyApple10_0"


def rosetta_ok() -> bool:
    """The DXBC and metallib converters ship as x86_64 binaries, so without Rosetta they cannot
    start at all — and the failure they produce says nothing about why."""
    try:
        return subprocess.run(["arch", "-x86_64", "/usr/bin/true"],
                              capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def quarantined(root: Path) -> list[str]:
    """Files under root still carrying the download quarantine flag."""
    out = subprocess.run(["xattr", "-r", str(root)], capture_output=True, text=True).stdout
    return [line.rsplit(": ", 1)[0] for line in out.splitlines() if line.endswith(": com.apple.quarantine")]


def clear_quarantine(root: Path) -> list[str]:
    """Strip the quarantine flag from our own bundled helpers; return whatever keeps it.

    Opening the app with "Open Anyway" approves the app, not the x86_64 converters inside it, and
    Gatekeeper SIGKILLs a quarantined unsigned binary the moment it is exec'd — the converters
    die with exit 137 and the warm reports "0 usable shaders" with nothing to say why.
    """
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(root)], capture_output=True)
    return quarantined(root)


def steam_libraries() -> list[Path]:
    """Every Steam library a game could live in, external drives included.

    GameHub records each game's library root in its container bindings, which is the only place
    an external library such as /Volumes/T5/steamGames is written down.
    """
    roots = [Path.home() / "GameHub"]
    try:
        store = json.loads((GAMEHUB / "gamehub/game_container_store.json").read_text())
        roots += [Path(b["game_path"]) for b in store.get("bindings", []) if b.get("game_path")]
    except Exception:
        pass
    try:
        roots += sorted((GAMEHUB / "wine-engine/containers/virtual_containers").glob("*/drive_c/Program Files (x86)/Steam"))
    except OSError:
        pass
    for pattern in ("*/steamapps", "*/*/steamapps"):     # libraries GameHub has not bound yet
        try:    # glob swallows PermissionError, not EIO from a drive that dropped off or is failing
            roots += [p.parent for p in Path("/Volumes").glob(pattern)]
        except OSError:
            pass
    seen, out = set(), []
    for r in roots:
        if r not in seen and probe(r / "steamapps", "is_dir"):
            seen.add(r)
            out.append(r)
    return out


def steam_appid(game_dir: Path) -> str | None:
    """The Steam app id of an installed game, from the appmanifest next to its folder."""
    game_dir = Path(game_dir)
    for d in (game_dir, *game_dir.parents):             # Deadlock's root is <lib>/common/Deadlock/game
        if d.parent.name == "common" and d.parent.parent.name == "steamapps":
            for acf in d.parent.parent.glob("appmanifest_*.acf"):
                try:
                    text = acf.read_text(errors="ignore")
                except OSError:
                    continue
                if re.search(r'"installdir"\s+"%s"' % re.escape(d.name), text, re.I):
                    m = re.search(r'"appid"\s+"(\d+)"', text)
                    return m.group(1) if m else None
            return None
    return None


def gamehub_gptk(appid: str | None) -> Path | None:
    """The GPTK GameHub will launch this game with next time — its per-game graphics setting.

    The cache stamp only says which GPTK ran last; if the player switched it since, warming under
    the stamped one builds a cache the game will throw away.
    """
    if not appid:
        return None
    for f in sorted((GAMEHUB / "gamehub/game-settings").glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if str(d.get("key", {}).get("platform_app_id")) != str(appid):
            continue
        name = d.get("settings", {}).get("graphics_stack", {}).get("component_config", {}).get("name")
        path = GAMEHUB / "wine-engine/downloads" / name if name else None
        return path if path and probe(path / "external/D3DMetal.framework") else None
    return None


def drop_legacy_backups(d3dm_root: Path | None) -> list[str]:
    """Delete the per-warm cache copies older versions of this app left behind.

    Every warm used to copy the game's cache aside first — a full 5 GB copy for Overwatch, never
    cleaned up.  The warm now merges into a superset of the old cache, so those copies protect
    nothing; this only removes directories the app itself created.
    """
    support = Path.home() / "Library/Application Support"
    victims = [support / "OverwatchWarmer/live_cache_backups"]
    victims += sorted(support.glob("ShaderWarmer/work_*/backup_*"))
    if d3dm_root is not None:
        victims += sorted(Path(d3dm_root).glob("deadlock.exe.backup.*"))
    removed = []
    for v in victims:
        if v.exists():
            subprocess.run(["rm", "-rf", str(v)], capture_output=True)
            removed.append(v.name)
    return removed


def detect_guard(main, logdir: Path) -> int:
    """Run an engine's main(); if a --detect run crashes, answer with JSON anyway.

    The GUI parses --detect's stdout, and a crash there used to leave it with no answer at all —
    it then skipped the game scan too, so every game vanished from the window.  The traceback goes
    to the engine's log dir, which "Собрать логи" collects.
    """
    try:
        return main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:
        if "--detect" not in sys.argv:
            raise
        try:
            Path(logdir).mkdir(parents=True, exist_ok=True)
            (Path(logdir) / "detect_error.log").write_text(traceback.format_exc())
        except OSError:
            pass
        print(json.dumps({"runtime_error": f"опрос системы упал: {type(e).__name__}: {e}", "ready": False},
                         ensure_ascii=False), flush=True)
        return 0


_DRAW_MODES = Path.home() / "Library/Application Support/ShaderWarmer/draw_modes.json"


def _draw_key(family_name: str, gptk: Path) -> str:
    return f"{family_name}|{Path(gptk).name}"


def known_draw_mode(family_name: str, gptk: Path) -> int | None:
    """0 (no GPU work, fast) or 3 (a real 3-vertex draw), as found earlier on this Mac.

    Whether D3DMetal persists a pipeline for a zero-vertex draw depends on the GPU *and* the
    GPTK build: on an M1 it does under GPTK 4.0-2 (Deadlock warmed +40k records that way) but not
    under 3.0-3 (Overwatch's calibration came back empty).  So it is measured, never assumed
    from the chip, and the answer is kept per GPU family + GPTK.
    """
    try:
        return json.loads(_DRAW_MODES.read_text()).get(_draw_key(family_name, gptk))
    except (OSError, ValueError):
        return None


def remember_draw_mode(family_name: str, gptk: Path, draw: int) -> None:
    try:
        known = json.loads(_DRAW_MODES.read_text()) if _DRAW_MODES.exists() else {}
    except (OSError, ValueError):
        known = {}
    known[_draw_key(family_name, gptk)] = int(draw)
    try:
        _DRAW_MODES.parent.mkdir(parents=True, exist_ok=True)
        _DRAW_MODES.write_text(json.dumps(known, indent=1))
    except OSError:
        pass


def volume_of(path: Path) -> tuple[str, str] | None:
    """(mount point, filesystem) of the disk a path really lives on.

    GameHub links games kept on an external drive into its containers, so the path alone names
    the internal disk; an error about it should name the drive that actually failed.
    """
    import os
    real = os.path.realpath(str(path))
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    best = None
    for line in out.splitlines():
        m = re.match(r"^\S+ on (.+) \(([^,)]+)", line)
        if not m:
            continue
        mp, fs = m.group(1), m.group(2)
        if (real == mp or real.startswith(mp.rstrip("/") + "/")) and (best is None or len(mp) > len(best[0])):
            best = (mp, fs)
    return best


def unreadable_message(e: OSError, where: Path) -> str:
    """One sentence for a game the disk cannot read, naming the drive when it is an external one."""
    import errno
    why = {errno.EIO: "диск не отвечает (ошибка ввода-вывода)", errno.EPERM: "macOS не даёт доступ",
           errno.EACCES: "нет прав на чтение"}.get(e.errno, e.strerror or str(e))
    vol = volume_of(where)
    if vol and vol[0].startswith("/Volumes/"):
        disk = f"внешнем диске {vol[0][len('/Volumes/'):]} ({vol[1]})"
        return (f"ОШИБКА: игра лежит на {disk} — {why}. Безопасно извлеки диск и подключи заново; "
                "если не поможет — «Первая помощь» для него в Дисковой утилите.")
    return f"ОШИБКА: не читаются файлы игры — {why} ({e.filename or where})."


# macOS keeps the GPU code it compiles for a Metal app under <user cache>/<bundle id>/com.apple.metal,
# and Wine's loader carries the bundle id org.winehq.wine, so every Wine game shares one such cache.
# It is capped at about 1 GB; once full, each new entry evicts others — the most recent ones
# included (measured: 370 new pipelines pushed out 290 of the 293 compiled just before).  A warm
# draws into its own render targets, never the game's, so the GPU code it compiles is code no game
# asks for; warms under the stock loader filled that shared cache with it, and from then on every
# session's own compiles evicted the previous session's and each hero stuttered again.  Warms now
# run a clone of the Wine install whose loader carries its own id, which keeps their GPU code apart.
_WINE_ID = b"<string>org.winehq.wine</string>"
_WARM_ID = b"<string>org.winehq.warm</string>"     # same length: nothing in the loader moves
_WARM_WINES = Path.home() / "Library/Caches/ShaderWarmer/wine"
_warm_wine_memo: dict = {}


def _loaders(inst: Path) -> list[Path]:
    """The Wine loaders of an install (bin/wine, lib/wine/*-unix/wine64, …) that carry the stock id."""
    found = []
    for d in [inst / "bin"] + sorted((inst / "lib/wine").glob("*-unix")):
        try:
            names = sorted(p for p in d.iterdir() if p.name.startswith("wine") and "." not in p.name)
        except OSError:
            continue
        for p in names:
            try:
                if p.is_file() and not p.is_symlink() and p.stat().st_size < 64 << 20 \
                        and _WINE_ID in p.read_bytes():
                    found.append(p)
            except OSError:
                pass
    return found


def warm_wine(wine: Path, log=None) -> Path:
    """The Wine loader to warm with: `wine` itself, or its clone that files GPU code under
    org.winehq.warm.  The clone is an APFS copy made once per Wine install (instant, shares the
    disk blocks) and rebuilt when GameHub updates the install.  Anything unusual — a Wine.app
    bundle, a loader without the stock id, a failed signature — falls back to the stock loader."""
    wine = Path(wine)
    if wine in _warm_wine_memo:
        return _warm_wine_memo[wine]
    say = log or (lambda _m: None)
    result = wine
    try:
        inst = wine.parent.parent
        loaders = _loaders(inst)
        if loaders and not any(p.suffix == ".app" for p in inst.iterdir()):
            src = {str(p.relative_to(inst)): [p.stat().st_size, p.stat().st_mtime_ns] for p in loaders}
            dst = _WARM_WINES / inst.name
            stamp = dst / ".source.json"
            fresh = False
            try:
                fresh = json.loads(stamp.read_text()) == {"install": str(inst), "loaders": src}
            except (OSError, ValueError):
                pass
            if not fresh:
                import shutil
                shutil.rmtree(dst, ignore_errors=True)
                dst.mkdir(parents=True)
                for entry in inst.iterdir():
                    if entry.name == "prefix":          # the template prefix stays where it is
                        continue
                    if subprocess.run(["cp", "-c", "-R", "-p", str(entry), str(dst)], capture_output=True).returncode:
                        subprocess.run(["cp", "-R", "-p", str(entry), str(dst)], check=True, capture_output=True)
                for rel in src:
                    p = dst / rel
                    data = p.read_bytes().replace(_WINE_ID, _WARM_ID)
                    tmp = p.with_name(p.name + ".sw")
                    tmp.write_bytes(data)
                    tmp.chmod(p.stat().st_mode)
                    tmp.replace(p)
                    subprocess.run(["codesign", "-f", "-s", "-", str(p)], check=True, capture_output=True)
                stamp.write_text(json.dumps({"install": str(inst), "loaders": src}))
                say("warm wine : cloned %s -> %s (GPU code under org.winehq.warm)" % (inst.name, dst))
            result = dst / wine.relative_to(inst)
            say("warm wine : %s" % result)
        else:
            say("warm wine : %s has no stock-id loader — warming with it as is" % inst.name)
    except Exception as e:                              # never let this stop a warm
        say("warm wine : clone failed (%s: %s) — warming with the stock loader" % (type(e).__name__, e))
        result = wine
    _warm_wine_memo[wine] = result
    return result

