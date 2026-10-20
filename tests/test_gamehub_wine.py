import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamehub_wine


class GameHubWineTests(unittest.TestCase):
    def fixture(self, selected="110"):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name) / "wine-engine"
        settings_dir = root.parent / "gamehub/game-settings"
        settings_dir.mkdir(parents=True)
        for ident, major in (("100", "10.0"), ("110", "11.0")):
            directory = root / "containers/wine_installations" / ident
            (directory / "bin").mkdir(parents=True)
            (directory / "bin/wine").write_text("")
            (directory / "manifest.json").write_text(json.dumps({
                "architecture": "x86_64", "metadata": {"proton_version": major}}))
        config = {"schema_version": 2, "settings_revision": 3, "runtime_config_revision": 4,
                  "key": {"platform_app_id": "2357570"}, "settings": {
                      "compatibility_layer": selected,
                      "compatibility_layer_config": {"id": int(selected), "name": "old"}}}
        (settings_dir / "overwatch.json").write_text(json.dumps(config))
        return temp, root, settings_dir / "overwatch.json"

    def test_switches_overwatch_to_proton_10_and_keeps_backup(self):
        temp, root, path = self.fixture()
        with temp:
            result = gamehub_wine.ensure_game_wine(root, "2357570", "10")
            updated = json.loads(path.read_text())
            self.assertTrue(result["changed"])
            self.assertEqual(updated["settings"]["compatibility_layer"], "100")
            self.assertEqual(updated["settings_revision"], 4)
            self.assertTrue(path.with_suffix(".json.shader-warmer-backup").exists())

    def test_matching_setting_is_left_unchanged(self):
        temp, root, path = self.fixture(selected="100")
        with temp:
            before = path.read_bytes()
            result = gamehub_wine.ensure_game_wine(root, "2357570", "10")
            self.assertFalse(result["changed"])
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
