"""A stale cache must never cost a user the whole warm, and the cache must land where the Mac reads it."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ow"))
import gptk_pick  # noqa: E402
import merge_ns  # noqa: E402

FILES = ("stage_cache.bin", "pipeline_cache.bin", "rootsignature_cache.bin", "bytecode_cache.bin")


def namespace(where: Path, version: bytes) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        merge_ns.ArenaWriter(where / name).close()
    (where / "version.bin").write_bytes(version)
    return where


class CacheMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_version_mismatch_raises_a_catchable_error(self):
        """SystemExit here used to tear the process down after an hour of warming."""
        fresh = namespace(self.tmp / "fresh", b"D3DMetal-A")
        stale = namespace(self.tmp / "stale", b"D3DMetal-B")
        with self.assertRaises(merge_ns.VersionMismatch):
            merge_ns.merge([fresh, stale], self.tmp / "out", with_bytecode=False)
        self.assertIsInstance(merge_ns.VersionMismatch(), ValueError)

    def test_fresh_cache_alone_still_merges(self):
        """What every engine falls back to once the game's own cache turns out to be stale."""
        fresh = namespace(self.tmp / "fresh", b"D3DMetal-A")
        rep = merge_ns.merge([fresh], self.tmp / "out", with_bytecode=False)
        self.assertEqual(rep["stage_cache.bin"]["records"], 0)
        self.assertEqual((self.tmp / "out/version.bin").read_bytes(), b"D3DMetal-A")

    def test_matching_versions_merge(self):
        same = [namespace(self.tmp / "a", b"same"), namespace(self.tmp / "b", b"same")]
        rep = merge_ns.merge(same, self.tmp / "out", with_bytecode=False)
        self.assertIn("stage_cache.bin", rep)


class GpuFamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_family_comes_from_an_existing_namespace(self):
        """An M1 writes Apple7; assuming Apple10 would install into a folder it never reads."""
        (self.tmp / "game.exe/shaders.cache/MTLGPUFamilyApple7_0").mkdir(parents=True)
        self.assertEqual(gptk_pick.family_dir_name(self.tmp), "MTLGPUFamilyApple7_0")

    def test_family_falls_back_to_the_chip(self):
        name = gptk_pick.family_dir_name(self.tmp)       # empty root: ask the machine
        self.assertRegex(name, r"^MTLGPUFamilyApple\d+_0$")

    def test_no_cache_means_no_gptk_guess(self):
        self.assertIsNone(gptk_pick.gptk_for_cache([], self.tmp / "nope"))


if __name__ == "__main__":
    unittest.main()
