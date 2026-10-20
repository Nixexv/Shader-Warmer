import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ow_warm


class OverwatchWarmTests(unittest.TestCase):
    def test_gpu_calibration_can_enable_real_draws_for_apple7(self):
        env = ow_warm.wine_env(Path("/wine"), Path("/gptk"), Path("/prefix"),
                               draw_vertices=3, dispatch=1)
        self.assertEqual(env["WARMGEN_DRAW_VERTICES"], "3")
        self.assertEqual(env["WARMGEN_DISPATCH"], "1")

    def test_default_warm_remains_no_gpu(self):
        env = ow_warm.wine_env(Path("/wine"), Path("/gptk"), Path("/prefix"))
        self.assertEqual(env["WARMGEN_DRAW_VERTICES"], "0")
        self.assertEqual(env["WARMGEN_DISPATCH"], "0")


if __name__ == "__main__":
    unittest.main()
