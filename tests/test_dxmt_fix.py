import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import shutil
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dxmt_fix as d


class DXMTFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='Other Mac Кириллица ')
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        self.fix = d.Fix(home, home / 'assets')
        self.fix.platform_probe = lambda: {'system': 'Darwin', 'machine': 'arm64', 'macos': '15.0', 'rosetta': True}
        self.patch_bytes = b'tested patched DLL'
        self.sha = patch.object(d, 'PATCH_SHA', hashlib.sha256(self.patch_bytes).hexdigest())
        self.sha.start()
        self.addCleanup(self.sha.stop)
        self.fix.processes = lambda: []
        self.fix.stop_idle_server = lambda ctx: None
        self.game = home / 'GameHub/steamapps/common/Deadlock/game'
        pe = bytearray(256); pe[:2] = b'MZ'; pe[60:64] = (64).to_bytes(4, 'little')
        pe[64:70] = b'PE\0\0\x64\x86'; pe[100:110] = b'd3d11.dll\0'
        self.pe64 = bytes(pe)
        self.put(self.game / 'bin/win64/deadlock.exe', self.pe64)
        self.registry = self.fix.engine / 'containers/virtual_containers/11/user.reg'
        self.put(self.registry, b'WINE REGISTRY Version 2\n\n[Environment]\n"KEEP"="value"\n"DXMT_PREWARM"="old"\n\n[Other]\n"Unrelated"="1"\n')
        (self.registry.parent / 'drive_c').mkdir()
        store = {'bindings': [{'platform': 'steam', 'platform_app_id': '1422450',
                  'game_path': str(home / 'GameHub'), 'virtual_container_id': '11'}]}
        self.put(self.fix.root / 'gamehub/game_container_store.json', json.dumps(store).encode())
        wine = self.fix.engine / 'containers/wine_installations/73'
        self.put(wine / 'bin/wine', b'wine')
        self.put(wine / 'bin/wineserver', b'wineserver')
        (wine / 'bin/wine').chmod(0o755)
        (wine / 'bin/wineserver').chmod(0o755)
        self.put(wine / 'manifest.json', json.dumps({'architecture': 'x86_64',
                   'metadata': {'proton_version': '11.0'}}).encode())
        self.settings = self.fix.root / 'gamehub/game-settings/deadlock.json'
        self.doc = {'settings_revision': 5, 'runtime_config_revision': 3,
            'key': {'platform_app_id': '1422450'}, 'settings': {
            'compatibility_layer': '73', 'start_parameters': '-novid -vulkan', 'language': 'ru',
            'graphics_stack': {'kind': 'dxmt', 'component_id': '163',
                'component_config': {'name': 'dxmt-v0.80', 'id': 163}},
            'runtime_graphics_profiles': {'rosetta_x64': {'molten_vk': 'keep'}},
            'dxmt_experimental_dx12_support': True}}
        self.put(self.settings, json.dumps(self.doc).encode())
        self.put(self.fix.component / 'manifest.json', b'{"id":"original"}')
        self.put(self.fix.component / 'wine/x86_64-windows/d3d11.dll', b'original vendor DLL')
        self.put(self.fix.component / 'wine/x86_64-unix/winemetal.so', b'original vendor metal')
        self.put(self.fix.assets / 'wine/x86_64-windows/d3d11.dll', self.patch_bytes)
        self.put(self.fix.assets / 'wine/x86_64-unix/winemetal.so', b'matching metal')
        files = {str(p.relative_to(self.fix.assets)): d.digest(p)
                 for p in (self.fix.assets / 'wine').rglob('*') if p.is_file()}
        self.put(self.fix.assets / 'manifest.json', json.dumps({'version': 'test', 'files': files}).encode())

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_null_launch_arguments_are_rejected_before_wine_or_backup(self):
        self.doc['settings']['start_parameters'] = None
        self.settings.write_text(json.dumps(self.doc))
        with self.assertRaisesRegex(RuntimeError, 'формат параметров'):
            self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists())

    def test_malformed_graphics_stack_is_reported_by_detect(self):
        self.doc['settings']['graphics_stack'] = 'unexpected schema'
        self.settings.write_text(json.dumps(self.doc))
        result = self.fix.detect(self.game)
        self.assertFalse(result['dxmt_available'])
        self.assertIn('формат параметров', result['runtime_error'])

    def test_upgrade_retains_existing_log_location_until_reapply(self):
        self.fix.apply(self.game)
        record = json.loads(self.fix.record.read_text())
        record['log_path'] = str(self.fix.log_dir / 'deadlock-old-session.txt')
        self.fix.record.write_text(json.dumps(record))
        self.assertEqual(Path(self.fix.detect(self.game)['dxmt_log_directory']), self.fix.log_dir.resolve())

    def test_apply_detect_restore_preserves_unrelated_settings_and_registry(self):
        original_registry = self.registry.read_text()
        self.fix.apply(self.game)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])
        self.assertEqual((self.fix.component / 'wine/x86_64-windows/d3d11.dll').read_bytes(), self.patch_bytes)
        self.assertEqual(json.loads(self.settings.read_text())['settings']['start_parameters'], '-novid -dx11')
        doc = json.loads(self.settings.read_text())
        doc['settings']['language'] = 'en'
        doc['settings']['runtime_graphics_profiles']['rosetta_x64']['molten_vk'] = 'new user value'
        self.settings.write_text(json.dumps(doc))
        self.fix.apply(self.game, restoring=True)
        settings = json.loads(self.settings.read_text())['settings']
        self.assertEqual(settings['language'], 'en')
        self.assertEqual(settings['start_parameters'], '-novid -vulkan')
        self.assertEqual(settings['runtime_graphics_profiles']['rosetta_x64'], {'molten_vk': 'new user value'})
        self.assertEqual(d.env_snapshot(self.registry.read_text()), d.env_snapshot(original_registry))
        self.assertIn('"KEEP"="value"', self.registry.read_text())
        self.assertIn('[Other]', self.registry.read_text())
        self.assertEqual((self.fix.component / 'wine/x86_64-windows/d3d11.dll').read_bytes(), b'original vendor DLL')

    def test_reapply_keeps_original_backup(self):
        self.fix.apply(self.game)
        before = (self.fix.backup / 'wine/x86_64-windows/d3d11.dll').read_bytes()
        self.fix.apply(self.game)
        self.assertEqual((self.fix.backup / 'wine/x86_64-windows/d3d11.dll').read_bytes(), before)

    def test_running_game_blocks_mutation(self):
        self.fix.processes = lambda: [['42', 'C:\\game\\deadlock.exe']]
        before = self.settings.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Закрой Deadlock'):
            self.fix.apply(self.game)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse(self.fix.backup.exists())

    def test_package_corruption_never_changes_live_files(self):
        self.put(self.fix.assets / 'wine/x86_64-windows/d3d11.dll', b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'Повреждены'):
            self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists())

    def test_wrong_game_copy_is_rejected(self):
        other = Path(self.temp.name) / 'other/game'
        self.put(other / 'bin/win64/deadlock.exe', b'game')
        self.assertIn('другая копия', self.fix.detect(other)['runtime_error'])

    def test_partial_install_rolls_back_every_target(self):
        old_settings = self.settings.read_bytes()
        old_registry = self.registry.read_bytes()
        real_atomic = d.atomic
        failed = False
        def fail_once(path, data):
            nonlocal failed
            if Path(path) == self.settings and not failed:
                failed = True
                raise OSError('disk error')
            real_atomic(path, data)
        with patch.object(d, 'atomic', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'disk error'):
                self.fix.apply(self.game)
        self.assertEqual(self.settings.read_bytes(), old_settings)
        self.assertEqual(self.registry.read_bytes(), old_registry)
        self.assertEqual((self.fix.component / 'wine/x86_64-windows/d3d11.dll').read_bytes(), b'original vendor DLL')
        self.assertFalse(json.loads(self.fix.record.read_text())['active'])

    def test_durable_journal_recovers_interrupted_install(self):
        ctx = self.fix.context(self.game)
        self.fix.state.mkdir(parents=True)
        self.fix.prepare_record(ctx)
        old_settings = self.settings.read_bytes()
        self.fix.begin_transaction(ctx, self.fix.component_bytes(), self.fix.package()['files'])
        self.put(self.settings, b'partial')
        self.put(self.fix.component / 'wine/x86_64-windows/d3d11.dll', b'partial')
        self.fix.recover_transaction()
        self.assertEqual(self.settings.read_bytes(), old_settings)
        self.assertEqual((self.fix.component / 'wine/x86_64-windows/d3d11.dll').read_bytes(), b'original vendor DLL')
        self.assertFalse((self.fix.state / 'transaction').exists())

    def test_corrupt_backup_prevents_restore(self):
        self.fix.apply(self.game)
        self.put(self.fix.backup / 'wine/x86_64-windows/d3d11.dll', b'bad backup')
        with self.assertRaisesRegex(RuntimeError, 'повреждена'):
            self.fix.apply(self.game, restoring=True)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])

    def test_already_stopped_wineserver_is_success(self):
        ctx = self.fix.context(self.game)
        responses = [subprocess.CompletedProcess([], 1, b'', b''), subprocess.CompletedProcess([], 0, b'', b'')]
        with patch.object(d.subprocess, 'run', side_effect=responses) as run:
            d.Fix.stop_idle_server(self.fix, ctx)
        self.assertEqual(run.call_count, 2)

    def test_wineserver_diagnostic_is_not_ignored(self):
        ctx = self.fix.context(self.game)
        with patch.object(d.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'error')):
            with self.assertRaisesRegex(RuntimeError, 'Не удалось остановить'):
                d.Fix.stop_idle_server(self.fix, ctx)

    def test_external_library_and_arbitrary_container_and_wine_ids(self):
        external = Path(self.temp.name) / 'Внешний диск #1/Steam Library'
        shutil.move(str(Path(self.temp.name) / 'GameHub'), external)
        self.game = external / 'steamapps/common/Deadlock/game'
        store = self.fix.root / 'gamehub/game_container_store.json'
        doc = json.loads(store.read_text());doc['bindings'][0].update(game_path=str(external), virtual_container_id='958')
        store.write_text(json.dumps(doc))
        old_prefix = self.registry.parent
        new_prefix = old_prefix.with_name('958');old_prefix.rename(new_prefix);self.registry=new_prefix/'user.reg'
        old_wine = self.fix.engine / 'containers/wine_installations/73';old_wine.rename(old_wine.with_name('201234'))
        doc = json.loads(self.settings.read_text());doc['settings']['compatibility_layer']='201234';self.settings.write_text(json.dumps(doc))
        self.fix.apply(self.game)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])
        self.fix.apply(self.game, restoring=True)
        self.assertEqual((self.fix.component/'wine/x86_64-windows/d3d11.dll').read_bytes(),b'original vendor DLL')

    def test_frame_log_needs_neither_z_drive_nor_ascii_home(self):
        self.fix.apply(self.game)
        values = dict((json.loads(line.strip().split('=',1)[0]),json.loads(line.strip().split('=',1)[1]))
            for line in d.env_snapshot(self.registry.read_text()))
        self.assertTrue(values['DXMT_FRAMETIME_LOG'].startswith('C:\\ShaderWarmer\\Logs\\'))
        self.assertTrue(values['DXMT_FRAMETIME_LOG'].isascii())
        self.assertFalse((self.registry.parent/'dosdevices/z:').exists())

    def test_old_macos_blocks_before_backup(self):
        self.fix.platform_probe=lambda:dict(system='Darwin',machine='arm64',macos='14.7',rosetta=True)
        with self.assertRaisesRegex(RuntimeError,'macOS 15'):self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists())

    def test_missing_rosetta_blocks_before_backup(self):
        self.fix.platform_probe=lambda:dict(system='Darwin',machine='arm64',macos='15.0',rosetta=False)
        with self.assertRaisesRegex(RuntimeError,'Rosetta 2'):self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists())

    def test_intel_is_explicitly_unsupported(self):
        self.fix.platform_probe=lambda:dict(system='Darwin',machine='x86_64',macos='15.0',rosetta=True)
        self.assertIn('Apple Silicon',self.fix.detect(self.game)['runtime_error'])

    def test_missing_gamehub_is_explained_without_mutating_component(self):
        (self.fix.root/'gamehub/game_container_store.json').unlink()
        self.assertIn('GameHub',self.fix.detect(self.game)['runtime_error'])
        self.assertFalse(self.fix.backup.exists())

    def test_incomplete_wine_is_rejected(self):
        (self.fix.engine/'containers/wine_installations/73/bin/wineserver').unlink()
        with self.assertRaisesRegex(RuntimeError,'Wine Proton 11 неполна'):self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists())

    def test_no_free_space_keeps_live_data_unchanged(self):
        before=self.registry.read_bytes()
        with patch.object(d.shutil,'disk_usage',return_value=shutil._ntuple_diskusage(100,100,0)):
            with self.assertRaisesRegex(RuntimeError,'Недостаточно места'):self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists());self.assertEqual(self.registry.read_bytes(),before)

    def test_readonly_volume_keeps_live_data_unchanged(self):
        with patch.object(d.tempfile,'TemporaryFile',side_effect=PermissionError()):
            with self.assertRaisesRegex(RuntimeError,'Нет прав на запись'):self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists())

    def test_malformed_gamehub_profiles_are_rejected_before_backup(self):
        doc=json.loads(self.settings.read_text());doc['settings']['runtime_graphics_profiles']=[]
        self.settings.write_text(json.dumps(doc))
        with self.assertRaisesRegex(RuntimeError,'графических профилей'):self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists())

    def test_absolute_asset_paths_are_rejected(self):
        manifest=self.fix.assets/'manifest.json';doc=json.loads(manifest.read_text());doc['files']['/tmp/unrelated']='bad'
        manifest.write_text(json.dumps(doc))
        with self.assertRaisesRegex(RuntimeError,'Недопустимый путь'):self.fix.apply(self.game)
        self.assertFalse(self.fix.backup.exists())

    def test_backup_commit_interruption_recovers_from_seed(self):
        ctx=self.fix.context(self.game);self.fix.state.mkdir(parents=True);self.fix.prepare_record(ctx)
        self.fix.record.unlink()
        self.fix.apply(self.game)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])

    def test_unrelated_settings_files_can_be_invalid(self):
        self.put(self.settings.with_name('broken.json'),b'[]')
        self.fix.apply(self.game)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])

    def test_backup_from_other_user_is_rejected(self):
        self.fix.apply(self.game);record=json.loads(self.fix.record.read_text());record['prefix']='/Users/someone-else/prefix'
        self.fix.record.write_text(json.dumps(record))
        self.assertIn('другого Mac',self.fix.detect(self.game)['runtime_error'])

    def test_corrupted_state_is_reported_without_traceback(self):
        self.fix.state.mkdir(parents=True);self.fix.record.write_text('[]')
        self.assertIn('неподдерживаемый формат',self.fix.detect(self.game)['runtime_error'])

    def test_restore_remains_available_on_older_os(self):
        self.fix.apply(self.game)
        self.fix.platform_probe=lambda:dict(system='Darwin',machine='arm64',macos='14.7',rosetta=True)
        self.assertTrue(self.fix.detect(self.game)['dxmt_backup'])
        self.fix.apply(self.game,restoring=True)
        self.assertEqual((self.fix.component/'wine/x86_64-windows/d3d11.dll').read_bytes(),b'original vendor DLL')


if __name__ == '__main__':
    unittest.main()
