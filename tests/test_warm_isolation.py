"""Warms must keep their GPU code out of the Metal cache the Wine games share."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gptk_pick  # noqa: E402

PLIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
    <key>CFBundleIdentifier</key>
    <string>org.winehq.wine</string>
</dict></plist>
"""


def build_loader(path: Path, with_id: bool = True):
    """A tiny signed Mach-O that carries (or not) Wine's bundle id, like GameHub's loaders."""
    src = path.with_suffix(".c")
    src.write_text("int main(void){return 0;}\n")
    plist = path.with_suffix(".plist")
    plist.write_bytes(PLIST if with_id else PLIST.replace(b"org.winehq.wine", b"com.example.xyz"))
    subprocess.run(["clang", str(src), "-o", str(path), "-sectcreate", "__TEXT", "__info_plist", str(plist)],
                   check=True, capture_output=True)
    src.unlink()
    plist.unlink()


def fake_install(root: Path, with_id: bool = True) -> Path:
    inst = root / "wine_installations/10000099"
    (inst / "bin").mkdir(parents=True)
    (inst / "lib/wine/x86_64-unix").mkdir(parents=True)
    (inst / "lib/wine/x86_64-unix/ntdll.so").write_bytes(b"not a loader")
    (inst / "prefix").mkdir()
    (inst / "prefix/system.reg").write_text("big template prefix")
    build_loader(inst / "bin/wine", with_id)
    return inst


class WarmWineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = gptk_pick._WARM_WINES
        gptk_pick._WARM_WINES = self.root / "clones"
        gptk_pick._warm_wine_memo.clear()

    def tearDown(self):
        gptk_pick._WARM_WINES = self._saved
        gptk_pick._warm_wine_memo.clear()
        self.tmp.cleanup()

    def test_clone_carries_its_own_id_and_leaves_the_install_alone(self):
        inst = fake_install(self.root)
        before = (inst / "bin/wine").read_bytes()
        got = gptk_pick.warm_wine(inst / "bin/wine")
        self.assertEqual(got, self.root / "clones/10000099/bin/wine")
        data = got.read_bytes()
        self.assertIn(b"<string>org.winehq.warm</string>", data)
        self.assertNotIn(b"<string>org.winehq.wine</string>", data)
        self.assertEqual(subprocess.run(["codesign", "-v", str(got)], capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run([str(got)]).returncode, 0)
        self.assertEqual((inst / "bin/wine").read_bytes(), before)
        self.assertTrue((got.parent.parent / "lib/wine/x86_64-unix/ntdll.so").exists())
        self.assertFalse((got.parent.parent / "prefix").exists())

    def test_clone_is_reused_until_the_install_changes(self):
        inst = fake_install(self.root)
        got = gptk_pick.warm_wine(inst / "bin/wine")
        ino = got.stat().st_ino
        gptk_pick._warm_wine_memo.clear()
        self.assertEqual(gptk_pick.warm_wine(inst / "bin/wine").stat().st_ino, ino)
        build_loader(inst / "bin/wine")                 # GameHub updated this Wine
        os.utime(inst / "bin/wine", ns=(1, 1))
        gptk_pick._warm_wine_memo.clear()
        self.assertNotEqual(gptk_pick.warm_wine(inst / "bin/wine").stat().st_ino, ino)

    def test_install_without_the_stock_id_is_used_as_is(self):
        inst = fake_install(self.root, with_id=False)
        self.assertEqual(gptk_pick.warm_wine(inst / "bin/wine"), inst / "bin/wine")
        self.assertFalse((self.root / "clones").exists())


if __name__ == "__main__":
    unittest.main()
