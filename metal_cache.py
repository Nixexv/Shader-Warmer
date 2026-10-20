"""macOS's own cache of compiled GPU code — the one Wine games share — and how to clean it.

Metal keeps GPU code it compiled for an app under <user cache>/<bundle id>/com.apple.metal/<gpu>/:
functions.data holds the code, functions.list indexes it, and functionsN.* are shards other
processes of the same app write to.  Wine's loader says org.winehq.wine, so every Wine game
shares one such cache.  The format, as read from real caches:

  functions.list   u32 magic | 32-byte cache id | u32 count | u32 0 |
                   count x { u64 size | u64 offset | 32-byte key | u64 flags }
  functions.data   entries at 64-byte-aligned offsets, each
                   { sha256(code) | 32-byte key | code }

The key does not depend on the app, so the GPU code a warm compiles under its own bundle id
(org.winehq.warm) has the same keys as the copies earlier warms left in the shared cache.  That
is what makes the cleanup possible: drop exactly those, keep what the games compiled themselves.
Every entry carries its own sha256, so a copied entry stays valid wherever it lands, and one that
is not what the index says is a miss for Metal (it recompiles), never wrong code.
"""
from __future__ import annotations

import hashlib
import mmap
import os
import struct
import subprocess
from pathlib import Path

HEADER = 44
ENTRY = 56
ALIGN = 64

KEYS_FILE = Path.home() / "Library/Application Support/ShaderWarmer/warm_gpu_keys.bin"


def user_cache() -> Path:
    return Path(subprocess.run(["getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True,
                               text=True).stdout.strip())


def app_dirs(bundle_id: str) -> list[Path]:
    """The per-GPU folders of one app's cache (one per GPU/driver pair)."""
    base = user_cache() / bundle_id / "com.apple.metal"
    try:
        return sorted(d for d in base.iterdir() if d.is_dir() and (d / "functions.list").exists())
    except OSError:
        return []


def read_list(path: Path):
    """(header bytes, [(size, offset, key, flags), ...]) — the index as Metal sees it.

    The header counts the merged part; past it is a journal the last process appended, flags 0
    adding an entry and 1 removing one (an eviction).  The next process to open the cache folds
    the journal in; until then both parts count."""
    b = Path(path).read_bytes()
    if len(b) < HEADER:
        raise ValueError(f"{path}: too short")
    total = (len(b) - HEADER) // ENTRY
    count = min(struct.unpack_from("<I", b, 36)[0], total)
    index = {}
    for i in range(total):
        o = HEADER + ENTRY * i
        size, off = struct.unpack_from("<QQ", b, o)
        key, flags = b[o + 16:o + 48], struct.unpack_from("<Q", b, o + 48)[0]
        if i >= count and flags == 1:
            index.pop(key, None)
        else:
            index[key] = (size, off, key, 0 if i >= count else flags)
    return b[:HEADER], list(index.values())


def keys_in(dirs) -> set[bytes]:
    keys = set()
    for d in dirs:
        for lst in Path(d).glob("functions*.list"):
            try:
                keys.update(e[2] for e in read_list(lst)[1])
            except (OSError, ValueError):
                pass
    return keys


def load_warm_keys() -> set[bytes]:
    try:
        b = KEYS_FILE.read_bytes()
    except OSError:
        return set()
    return {b[i:i + 32] for i in range(0, len(b) - len(b) % 32, 32)}


def fragment_keys(dirs) -> set[bytes]:
    """Keys of the fragment programs in these caches.  Only those depend on the render targets
    and blending a pipeline draws with, so only those differ between a warm and a game: the
    vertex programs come from the game's real input layouts and compute has no render state at
    all — a game compiles exactly what the warm did, and those stay."""
    keys = set()
    for d in dirs:
        for lst in Path(d).glob("functions*.list"):
            data = lst.with_suffix(".data")
            try:
                entries = read_list(lst)[1]
                with open(data, "rb") as f:
                    m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                    for size, off, key, _flags in entries:
                        if off + 64 < len(m) and b"__fragment" in m[off + 64:off + min(size, 0x400)]:
                            keys.add(key)
            except (OSError, ValueError):
                pass
    return keys


def remember_warm_keys(bundle_id: str = "org.winehq.warm") -> int:
    """Add the fragment programs a warm compiled to the list we keep across warms.
    The warm's own cache is capped too and forgets old entries; this list does not."""
    known = load_warm_keys()
    keys = known | fragment_keys(app_dirs(bundle_id))
    if len(keys) == len(known):
        return len(keys)
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_bytes(b"".join(sorted(keys)))
    tmp.replace(KEYS_FILE)
    return len(keys)


def _valid(m, size, off, key) -> bool:
    if size < 64 or off + size > len(m):
        return False
    return m[off + 32:off + 64] == key and hashlib.sha256(m[off + 64:off + size]).digest() == m[off:off + 32]


def _write_shard(lst: Path, header: bytes, entries) -> None:
    """Write a shard from (source mmap, size, offset, key, flags) entries, packed from offset 0.
    The index goes last: if anything stops in between, old entries point into the new data, fail
    their key/sha check and are simply compiled again."""
    data = lst.with_suffix(".data")
    new_data, new_list = data.with_name(data.name + ".sw"), lst.with_name(lst.name + ".sw")
    index, pos = [], 0
    with open(new_data, "wb") as out:
        for m, size, off, key, flags in entries:
            out.write(m[off:off + size])
            pad = -size % ALIGN
            out.write(b"\0" * pad)
            index.append(struct.pack("<QQ", size, pos) + key + struct.pack("<Q", flags))
            pos += size + pad
        out.truncate(pos + (-pos % 4096))
        out.flush()
        os.fsync(out.fileno())
    hdr = bytearray(header)
    struct.pack_into("<II", hdr, 36, len(index), 0)
    with open(new_list, "wb") as out:
        out.write(bytes(hdr) + b"".join(index))
        out.flush()
        os.fsync(out.fileno())
    os.replace(new_data, data)
    os.replace(new_list, lst)


def compact_shard(lst: Path, drop: set, keep: set = frozenset(), dry: bool = False) -> dict:
    """Rewrite one shard without the entries whose key is in `drop` (unless also in `keep`),
    and without entries that no longer hold what the index says.  Survivors are packed from
    offset 0 in index order, so the freed space is real and Metal stops evicting."""
    data = lst.with_suffix(".data")
    header, entries = read_list(lst)
    rep = {"entries": len(entries), "dropped": 0, "invalid": 0, "mb_before": data.stat().st_size >> 20}
    if not any(e[2] in drop and e[2] not in keep for e in entries):
        rep["mb_after"] = rep["mb_before"]
        return rep                                  # the common case: nothing of ours in there
    with open(data, "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if data.stat().st_size else b""
        kept = []
        for size, off, key, flags in entries:
            if key in drop and key not in keep:
                rep["dropped"] += 1
            elif not _valid(m, size, off, key):
                rep["invalid"] += 1
            else:
                kept.append((size, off, key, flags))
        if not rep["dropped"] or dry:
            if not rep["dropped"]:
                rep["invalid"] = 0                  # nothing rewritten, nothing removed
            rep["kept"] = len(kept)
            rep["mb_after"] = (sum(e[0] + (-e[0] % ALIGN) for e in kept) >> 20) if rep["dropped"] else rep["mb_before"]
            return rep
        _write_shard(lst, header, [(m, size, off, key, flags) for size, off, key, flags in kept])
    rep["kept"] = len(kept)
    rep["mb_after"] = data.stat().st_size >> 20
    return rep


def wine_running() -> bool:
    """A Wine process alive keeps its own copy of the index and writes it back on exit.
    Our own warm clones (org.winehq.warm) never touch the games' cache and do not count."""
    r = subprocess.run(["pgrep", "-fl", r"wineserver|wine64-preloader|wine-preloader|\.exe( |$)"],
                       capture_output=True, text=True)
    return any(l.strip() and "/ShaderWarmer/wine/" not in l for l in r.stdout.splitlines())


def clean_shared(log=None, bundle_id: str = "org.winehq.wine", dry: bool = False) -> dict:
    """Take the warms' fragment programs out of the cache the Wine games share and keep
    everything the games compiled themselves.

    Only the main file is rewritten: warms ran alone and filed their code there, and it is the
    one at the size cap.  A game shares its session with Steam, so its code mostly lands in the
    numbered shards; anything found in one of those is kept in the main file as well.
    Skipped while any Wine process runs — the next warm tries again."""
    say = log or (lambda _m: None)
    drop = load_warm_keys()
    total = {"dropped": 0, "invalid": 0, "mb_before": 0, "mb_after": 0}
    if not drop:
        return total
    if not dry and wine_running():
        say("metal     : a Wine process is running — shared GPU-code cache left for the next warm")
        return total
    for d in app_dirs(bundle_id):
        keep = set()
        for lst in d.glob("functions*.list"):
            if lst.name != "functions.list":
                try:
                    keep.update(e[2] for e in read_list(lst)[1])
                except (OSError, ValueError):
                    pass
        try:
            rep = compact_shard(d / "functions.list", drop, keep, dry)
        except (OSError, ValueError) as e:
            say("metal     : %s skipped (%s)" % (d.name, e))
            continue
        for k in total:
            total[k] += rep.get(k, 0)
    if total["dropped"]:
        say("metal     : shared GPU-code cache %d -> %d MB (%d warm fragment programs out, %d stale)" %
            (total["mb_before"], total["mb_after"], total["dropped"], total["invalid"]))
    else:
        say("metal     : shared GPU-code cache holds no warm leftovers")
    return total


GAME_SHARD_CAP = 900 << 20      # stay well under the ~1.1 GB a single file was seen to stop at


def consolidate(log=None, bundle_id: str = "org.winehq.wine", dry: bool = False) -> dict:
    """Move what the main file holds into the file the game uses now.

    Each Metal process locks one file of the cache for its lifetime and looks only in that one.
    Under GameHub, Steam's Wine processes start first and take the main file and the next ten, so
    the game gets functions11 and never sees the main file — where it filed its shaders back when
    it was the one holding it (423 of 1375 variants recompiled in two matches were sitting there).
    The game's file is the numbered one written most recently; nothing moves when the main file
    was written after it, which is where a game that holds the main file itself keeps its code.
    The warms' fragment programs stay behind, and the total size of the cache does not change."""
    say = log or (lambda _m: None)
    rep = {"moved": 0, "mb": 0}
    if not dry and wine_running():
        return rep
    drop = load_warm_keys()
    for d in app_dirs(bundle_id):
        main = d / "functions.list"
        try:
            main_t = main.with_suffix(".data").stat().st_mtime
            shards = []
            for lst in d.glob("functions*.list"):
                if lst != main:
                    n = len(read_list(lst)[1])
                    if n >= 1000:
                        shards.append((lst.with_suffix(".data").stat().st_mtime, n, lst))
            if not shards:
                continue
            t_mtime, _n, target = max(shards)
            if t_mtime <= main_t:
                continue
            m_header, m_entries = read_list(main)
            t_header, t_entries = read_list(target)
        except (OSError, ValueError) as e:
            say("metal     : %s not consolidated (%s)" % (d.name, e))
            continue
        have = {e[2] for e in t_entries}
        moving = [e for e in m_entries if e[2] not in have and e[2] not in drop]
        if len(moving) < 1000:
            continue
        with open(main.with_suffix(".data"), "rb") as fm, open(target.with_suffix(".data"), "rb") as ft:
            mm = mmap.mmap(fm.fileno(), 0, access=mmap.ACCESS_READ)
            tm = mmap.mmap(ft.fileno(), 0, access=mmap.ACCESS_READ)
            out = [(tm, s_, o, k, f) for s_, o, k, f in t_entries if _valid(tm, s_, o, k)]
            size = sum(x[1] + (-x[1] % ALIGN) for x in out)
            moved = []
            for s_, o, k, f in moving:
                if size + s_ > GAME_SHARD_CAP:
                    break
                if _valid(mm, s_, o, k):
                    moved.append((mm, s_, o, k, 0))
                    size += s_ + (-s_ % ALIGN)
            moved_keys = {x[3] for x in moved}
            left = [(mm, s_, o, k, f) for s_, o, k, f in m_entries if k not in moved_keys and k not in drop
                    and k not in have and _valid(mm, s_, o, k)]
            rep["moved"] += len(moved)
            rep["mb"] += sum(x[1] for x in moved) >> 20
            if dry:
                continue
            _write_shard(target, t_header, out + moved)    # copies first: a stop in between only duplicates
            _write_shard(main, m_header, left)
        say("metal     : %d programs (%d MB) moved from the main file into %s, the one the game reads" %
            (len(moved), sum(x[1] for x in moved) >> 20, target.name))
    return rep


def after_warm(log=None) -> None:
    """Remember the isolated warm's keys without mutating the game's GPU-code files.
    Key overlap is not evidence that a game program is unused. Never fail a warm over diagnostics."""
    say = log or (lambda _m: None)
    try:
        n = remember_warm_keys()
        say("metal     : %d warm fragment programs known" % n)
        # A key seen in a warm can also be compiled and used by the game. Neither key
        # membership nor a shard's mtime proves provenance or which shard the next game
        # will lock. Do not remove or relocate the game's GPU code automatically.
        say("metal     : game GPU-code cache preserved (warm keys do not prove unused code)")
    except Exception as e:
        say("metal     : key inventory skipped (%s: %s)" % (type(e).__name__, e))
