import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'ow'), str(ROOT / 'tests')]
import diagnostics
import gptk_pick
import metal_cache
from test_metal_cache import key, program, write_shard
from test_merge_pointers import namespace, stage, sid


class GameGpuCacheTests(unittest.TestCase):
    def test_after_warm_preserves_a_fragment_the_game_also_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            main, game = d / 'functions.list', d / 'functions11.list'
            # A real game program has exactly the same key as the program in the warm.
            write_shard(main, [(key(1), program('fragment', 1))])
            write_shard(game, [(key(2), program('vertex', 2))])
            keys = d / 'warm_gpu_keys.bin'
            keys.write_bytes(key(1))
            before = {p: p.read_bytes() for p in (main, game, main.with_suffix('.data'), game.with_suffix('.data'))}
            with patch.object(metal_cache, 'KEYS_FILE', keys), \
                 patch.object(metal_cache, 'app_dirs', return_value=[d]), \
                 patch.object(metal_cache, 'wine_running', return_value=False):
                metal_cache.after_warm()
            self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_detect_has_no_automatic_gpu_cache_mutations(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sys, 'argv', ['warm.py', '--detect']), \
             patch.object(metal_cache, 'clean_shared', side_effect=AssertionError('deleted GPU code')), \
             patch.object(metal_cache, 'consolidate', side_effect=AssertionError('relocated GPU code')):
            self.assertEqual(gptk_pick.detect_guard(lambda: 0, Path(tmp)), 0)


class DiagnosticTests(unittest.TestCase):
    def test_dxmt_logs_from_prefix_without_z_drive(self):
        with tempfile.TemporaryDirectory(prefix='Другой Mac ') as tmp:
            home = Path(tmp)
            logs = home / 'Library/Application Support/com.gamemac.www/wine-engine/containers/virtual_containers/958/drive_c/ShaderWarmer/Logs'
            logs.mkdir(parents=True)
            frame = logs / 'deadlock-session.txt'
            frame.write_text('16.7\n')
            record = home / 'Library/Application Support/ShaderWarmer/dxmt-async/installation.json'
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({'log_path': str(frame)}))
            out = home / 'export'
            out.mkdir()
            with patch.object(diagnostics.Path, 'home', return_value=home):
                self.assertEqual(diagnostics.export_dxmt_logs(out), [frame.name])
            self.assertEqual((out / 'dxmt' / frame.name).read_text(), '16.7\n')

    def test_dxmt_export_rejects_foreign_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            private = home / 'deadlock-private.txt'
            private.write_text('must not export')
            record = home / 'Library/Application Support/ShaderWarmer/dxmt-async/installation.json'
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({'log_path': str(private)}))
            with patch.object(diagnostics.Path, 'home', return_value=home):
                self.assertEqual(diagnostics.export_dxmt_logs(home), [])
            self.assertFalse((home / 'dxmt').exists())

    def test_inventory_records_fingerprints_without_changing_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fam = namespace(root / 'd3dm/deadlock.exe/shaders.cache/MTLGPUFamilyApple7_0',
                            [stage(1, 1, sid(1)), stage(2, 1, sid(2))], [])
            before = {f: f.read_bytes() for f in fam.iterdir()}
            report = diagnostics.cache_inventory(root)['deadlock.exe'][fam.name]
            self.assertEqual(report['stage_cache.bin']['records'], 2)
            self.assertEqual(report['stage_cache.bin']['per_stage'], {1: 1, 2: 1})
            self.assertFalse(report['stage_cache.bin']['changed_during_read'])
            self.assertEqual({f: f.read_bytes() for f in before}, before)
            # Adding a shader is visible in the report, even if the allocated arena size is unchanged.
            other = namespace(root / 'other', [stage(1, 1, sid(1)), stage(2, 1, sid(3))], [])
            self.assertNotEqual(report['stage_cache.bin']['ordered_keys_sha256'],
                                diagnostics.arena_info(other / 'stage_cache.bin')['ordered_keys_sha256'])

    def test_broken_cache_is_reported_without_repairing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'stage_cache.bin'
            original = struct.pack('<3Q', 65536, 999999, 999999) + b'\0' * (65536 - 24)
            f.write_bytes(original)
            self.assertIn('error', diagnostics.arena_info(f))
            self.assertEqual(f.read_bytes(), original)

    def test_runtime_report_only_exports_the_game_runtime_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            d = home / 'Library/Application Support/com.gamemac.www/gamehub/game-settings'
            d.mkdir(parents=True)
            (d / 'deadlock.json').write_text(json.dumps({'key': {'platform_app_id': '1422450'},
                'settings': {'compatibility_layer': '10000033', 'graphics_stack': {'name': 'gptk-4.0-2'},
                             'account_token': 'must not export'}}))
            report = diagnostics.runtime_choices(home)
            self.assertEqual(report[0]['wine'], '10000033')
            self.assertNotIn('must not export', json.dumps(report))


if __name__ == '__main__':
    unittest.main()

class WineDiagnosticTests(unittest.TestCase):
    def test_export_selected_wine_prefix_and_private_launch_logs(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);prefix=home/'Library/Application Support/CrossOver/Bottles/Test'
            logs=prefix/'drive_c/ShaderWarmer/Logs';logs.mkdir(parents=True);(logs/'dxmt.txt').write_text('frame\n')
            key=hashlib.sha256(str(prefix.resolve()).encode()).hexdigest()[:24]
            state=home/'Library/Application Support/ShaderWarmer/wine-environments'/key
            (state/'logs').mkdir(parents=True);(state/'logs/launch.log').write_text('launch\n')
            (state/'installation.json').write_text(json.dumps({'schema':2,'prefix':str(prefix)}))
            out=home/'export';out.mkdir()
            with patch.object(diagnostics.Path,'home',return_value=home): result=diagnostics.export_dxmt_logs(out)
            self.assertEqual(len(result),2)
            self.assertEqual((out/'dxmt'/('wine-'+key+'-dxmt.txt')).read_text(),'frame\n')

    def test_export_does_not_follow_wine_log_symlink(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);prefix=home/'Library/Application Support/CrossOver/Bottles/Test'
            logs=prefix/'drive_c/ShaderWarmer/Logs';logs.mkdir(parents=True)
            secret=home/'private.txt';secret.write_text('private');(logs/'dxmt.txt').symlink_to(secret)
            key=hashlib.sha256(str(prefix.resolve()).encode()).hexdigest()[:24]
            state=home/'Library/Application Support/ShaderWarmer/wine-environments'/key;state.mkdir(parents=True)
            (state/'installation.json').write_text(json.dumps({'schema':2,'prefix':str(prefix)}))
            with patch.object(diagnostics.Path,'home',return_value=home):self.assertEqual(diagnostics.export_dxmt_logs(home),[])
