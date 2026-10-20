"""The offline compiler must get the option set D3DMetal itself uses on each GPU family."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ow"))
import stage_records  # noqa: E402


def opts(family):
    return dict(o.split("=", 1) for o in stage_records.options_for(family))


class CompilerOptionTests(unittest.TestCase):
    def test_apple10_is_the_captured_set_byte_for_byte(self):
        """M5 calibrates 93/93 with these; they must not move."""
        self.assertEqual(stage_records.options_for(1010), stage_records.DEFAULT_OPTIONS)

    def test_family_dependent_options(self):
        # (family, compat, shaderdetect, yield) as D3DMCompiler::D3DMCompiler derives them
        for family, compat, detect, yld in ((1007, "0x1BF", "0", "0"), (1008, "0x1BF", "1", "0"),
                                             (1009, "0x1BF", "1", "1"), (1010, "0x19F", "1", "1")):
            with self.subTest(family=family):
                o = opts(family)
                self.assertEqual((o["compat"], o["shaderdetect"], o["yield"], o["gpufamily"]),
                                 (compat, detect, yld, str(family)))
                self.assertEqual(o["yieldmode"], "3" if yld == "1" else "0")


if __name__ == "__main__":
    unittest.main()
