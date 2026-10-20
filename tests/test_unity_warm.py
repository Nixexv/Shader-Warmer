import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unity_warm


class UnityWarmTests(unittest.TestCase):
    def test_gpu_family_detection_covers_m1_through_newer_chips(self):
        self.assertEqual(unity_warm.gpu_family_of(Path("MTLGPUFamilyApple7_0")), 1007)
        self.assertEqual(unity_warm.gpu_family_of(Path("MTLGPUFamilyApple10_0")), 1010)


if __name__ == "__main__":
    unittest.main()
