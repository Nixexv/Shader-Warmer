import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import warm


class CancelTests(unittest.TestCase):
    def test_cancel_drops_scratch_and_leaves_the_game_cache_alone(self):
        """The warm runs in scratch namespaces; a cancel must clear them and nothing else."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "deadlock.exe"
            live.mkdir()
            (live / "stage_cache.bin").write_text("the player's cache")
            scratch = [root / "deadlock_warm_1_0.exe", root / "deadlock_warm_1_1.exe"]
            for d in scratch:
                d.mkdir()
                (d / "stage_cache.bin").write_text("half a warm")
            warm._cancel_scratch = scratch

            warm.cleanup_cancelled_run()
            self.assertFalse(any(d.exists() for d in scratch))
            self.assertEqual((live / "stage_cache.bin").read_text(), "the player's cache")
            self.assertIsNone(warm._cancel_scratch)


if __name__ == "__main__":
    unittest.main()
