"""Cleaning the warms' GPU code out of the Metal cache the Wine games share."""
import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import metal_cache as mc  # noqa: E402

CACHE_ID = bytes(range(32))


def program(kind: str, n: int) -> bytes:
    """Stand-in GPU code: what matters is the segment name the cleanup looks for."""
    return b"\xcf\xfa\xed\xfe" + b"\0" * 60 + ("__" + kind).encode() + bytes([n]) * (200 + n)


def key(n: int) -> bytes:
    return hashlib.sha256(b"key%d" % n).digest()


def write_shard(lst: Path, entries, journal=()):
    """entries: [(key, code)]; journal: [(key, code or None for a removal)] appended past the count."""
    data = bytearray()
    rows = []
    for k, code in list(entries) + [j for j in journal if j[1] is not None]:
        off = len(data)
        blob = hashlib.sha256(code).digest() + k + code
        data += blob + b"\0" * (-len(blob) % 64)
        rows.append((k, len(blob), off))
    base = rows[:len(entries)]
    tail, extra = rows[len(entries):], []
    it = iter(tail)
    for k, code in journal:
        if code is None:
            extra.append(struct.pack("<QQ", 0, 0) + k + struct.pack("<Q", 1))
        else:
            k2, size, off = next(it)
            extra.append(struct.pack("<QQ", size, off) + k2 + struct.pack("<Q", 0))
    head = struct.pack("<I", 0x18273645) + CACHE_ID + struct.pack("<II", len(base), 0)
    lst.write_bytes(head + b"".join(struct.pack("<QQ", s, o) + k + struct.pack("<Q", 0) for k, s, o in base)
                    + b"".join(extra))
    lst.with_suffix(".data").write_bytes(bytes(data) + b"\0" * (-len(data) % 4096))


class MetalCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "org.winehq.wine/com.apple.metal/16777235_355"
        self.dir.mkdir(parents=True)
        self.saved = (mc.KEYS_FILE, mc.app_dirs, mc.wine_running)
        mc.KEYS_FILE = Path(self.tmp.name) / "warm_gpu_keys.bin"
        mc.app_dirs = lambda bundle_id: [self.dir]
        mc.wine_running = lambda: False

    def tearDown(self):
        mc.KEYS_FILE, mc.app_dirs, mc.wine_running = self.saved
        self.tmp.cleanup()

    def test_journal_adds_and_removals_count(self):
        write_shard(self.dir / "functions.list", [(key(1), program("fragment", 1)), (key(2), program("vertex", 2))],
                    journal=[(key(3), program("fragment", 3)), (key(1), None)])
        keys = [e[2] for e in mc.read_list(self.dir / "functions.list")[1]]
        self.assertEqual(sorted(keys), sorted([key(2), key(3)]))

    def test_only_warm_fragments_leave_and_survivors_stay_valid(self):
        game = [(key(i), program("fragment", i)) for i in range(10, 20)]
        warm_frag = [(key(i), program("fragment", i)) for i in range(20, 30)]
        shared_vertex = [(key(i), program("vertex", i)) for i in range(30, 33)]
        write_shard(self.dir / "functions.list", game + warm_frag + shared_vertex)
        mc.KEYS_FILE.write_bytes(b"".join(sorted(k for k, _ in warm_frag)))
        # a key the warm also has, but which a game filed in its own shard: it is in use, keep it
        write_shard(self.dir / "functions11.list", [(key(20), program("fragment", 20))])
        before = (self.dir / "functions11.data").read_bytes()
        rep = mc.clean_shared()
        self.assertEqual(rep["dropped"], 9)
        _, entries = mc.read_list(self.dir / "functions.list")
        left = {e[2] for e in entries}
        self.assertEqual(left, {k for k, _ in game + shared_vertex} | {key(20)})
        with open(self.dir / "functions.data", "rb") as f:
            m = f.read()
        for size, off, k, _ in entries:
            self.assertTrue(mc._valid(m, size, off, k))
            self.assertEqual(off % 64, 0)
        self.assertEqual((self.dir / "functions11.data").read_bytes(), before)   # game shards untouched
        self.assertEqual(struct.unpack_from("<I", (self.dir / "functions.list").read_bytes(), 36)[0], len(entries))

    def test_stale_entries_go_but_nothing_is_rewritten_without_warm_leftovers(self):
        write_shard(self.dir / "functions.list", [(key(1), program("fragment", 1))])
        mc.KEYS_FILE.write_bytes(key(99))
        before = (self.dir / "functions.list").read_bytes()
        self.assertEqual(mc.clean_shared()["dropped"], 0)
        self.assertEqual((self.dir / "functions.list").read_bytes(), before)

    def test_running_wine_leaves_the_cache_alone(self):
        write_shard(self.dir / "functions.list", [(key(1), program("fragment", 1))])
        mc.KEYS_FILE.write_bytes(key(1))
        mc.wine_running = lambda: True
        self.assertEqual(mc.clean_shared()["dropped"], 0)
        self.assertEqual(len(mc.read_list(self.dir / "functions.list")[1]), 1)

    def test_dry_run_reports_without_writing(self):
        write_shard(self.dir / "functions.list", [(key(1), program("fragment", 1)), (key(2), program("fragment", 2))])
        mc.KEYS_FILE.write_bytes(key(1))
        before = (self.dir / "functions.list").read_bytes()
        self.assertEqual(mc.clean_shared(dry=True)["dropped"], 1)
        self.assertEqual((self.dir / "functions.list").read_bytes(), before)

    def test_warm_keys_are_fragment_programs_only(self):
        write_shard(self.dir / "functions.list", [(key(1), program("fragment", 1)), (key(2), program("vertex", 2)),
                                                  (key(3), program("compute", 3))])
        self.assertEqual(mc.fragment_keys([self.dir]), {key(1)})
        self.assertEqual(mc.remember_warm_keys(), 1)
        self.assertEqual(mc.load_warm_keys(), {key(1)})

    def _two_files(self, main_n=1200, game_n=1100):
        main = [(key(i), program("fragment", i % 200)) for i in range(main_n)]
        game = [(key(10000 + i), program("vertex", i % 200)) for i in range(game_n)]
        write_shard(self.dir / "functions.list", main)
        write_shard(self.dir / "functions11.list", game)
        return main, game

    def test_main_file_moves_into_the_file_the_game_reads(self):
        import os
        main, game = self._two_files()
        junk = [k for k, _ in main[:50]]
        mc.KEYS_FILE.write_bytes(b"".join(sorted(junk)))
        os.utime(self.dir / "functions.data", (1000, 1000))
        os.utime(self.dir / "functions11.data", (2000, 2000))
        rep = mc.consolidate()
        self.assertEqual(rep["moved"], 1150)
        g = {e[2] for e in mc.read_list(self.dir / "functions11.list")[1]}
        self.assertEqual(g, {k for k, _ in game} | {k for k, _ in main[50:]})
        self.assertEqual(mc.read_list(self.dir / "functions.list")[1], [])
        with open(self.dir / "functions11.data", "rb") as f:
            m = f.read()
        for size, off, k, _ in mc.read_list(self.dir / "functions11.list")[1]:
            self.assertTrue(mc._valid(m, size, off, k))
        self.assertEqual(mc.consolidate()["moved"], 0)            # nothing left to move

    def test_nothing_moves_when_the_game_uses_the_main_file(self):
        import os
        self._two_files()
        os.utime(self.dir / "functions.data", (3000, 3000))
        os.utime(self.dir / "functions11.data", (2000, 2000))
        before = (self.dir / "functions.list").read_bytes()
        self.assertEqual(mc.consolidate()["moved"], 0)
        self.assertEqual((self.dir / "functions.list").read_bytes(), before)

    def test_game_file_stays_under_its_cap(self):
        import os
        self._two_files()
        os.utime(self.dir / "functions.data", (1000, 1000))
        os.utime(self.dir / "functions11.data", (2000, 2000))
        saved = mc.GAME_SHARD_CAP
        mc.GAME_SHARD_CAP = (self.dir / "functions11.data").stat().st_size + 200 * 1024
        try:
            rep = mc.consolidate()
        finally:
            mc.GAME_SHARD_CAP = saved
        self.assertGreater(rep["moved"], 0)
        left = len(mc.read_list(self.dir / "functions.list")[1])
        self.assertEqual(rep["moved"] + left, 1200)                # what did not fit stays where it was


if __name__ == "__main__":
    unittest.main()
