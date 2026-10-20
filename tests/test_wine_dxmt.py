import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dxmt_fix as core
from wine_dxmt import WineFix, identity


class WineEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='Другой Mac Wine ');self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve();self.prefix=self.home/'Bottles/Steam игры'
        (self.prefix/'drive_c').mkdir(parents=True)
        (self.prefix/'user.reg').write_text('WINE REGISTRY Version 2\n\n[Environment]\n"CUSTOM"="keep"\n')
        (self.prefix/'cxbottle.conf').write_text('[Bottle]\n"WineArch"="win64"\n')
        self.wine=self.home/'Runtime/bin/wine';self.wine.parent.mkdir(parents=True)
        self.wine.write_text('wine');self.wine.chmod(0o755)
        (self.wine.parent/'wineserver').write_text('server')
        unix=self.wine.parent.parent/'lib/wine/x86_64-unix';unix.mkdir(parents=True)
        (unix/'ntdll.so').write_bytes(b'original native runtime');(unix/'wine').write_bytes(b'loader')
        windows=unix.with_name('x86_64-windows');windows.mkdir();(windows/'d3d11.dll').write_bytes(b'vendor renderer')
        self.assets=self.home/'assets';dll=self.assets/'wine/x86_64-windows/d3d11.dll';dll.parent.mkdir(parents=True);dll.write_bytes(b'patched')
        (self.assets/'manifest.json').write_text(json.dumps({'version':'test','files':{'wine/x86_64-windows/d3d11.dll':hashlib.sha256(b'patched').hexdigest()}}))
        self.patch=patch.object(core,'PATCH_SHA',hashlib.sha256(b'patched').hexdigest());self.patch.start();self.addCleanup(self.patch.stop)
        self.game=self.home/'Games/Test';self.game.mkdir(parents=True)
        pe=bytearray(256);pe[:2]=b'MZ';pe[60:64]=(64).to_bytes(4,'little');pe[64:70]=b'PE\0\0\x64\x86';pe[100:110]=b'd3d11.dll\0'
        (self.game/'Game.exe').write_bytes(pe)
        self.fix=self.make()

    def make(self,kind='crossover',prefix=None):
        f=WineFix(prefix or self.prefix,self.wine,kind,home=self.home,assets=self.assets)
        f.core.platform_probe=lambda: {'system':'Darwin','machine':'arm64','macos':'15.0','rosetta':True}
        f.core.processes=lambda: [];f.probe=lambda: None
        return f

    def snapshot(self,root):
        return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def test_crossover_persists_settings_and_restores_prefix_and_vendor(self):
        prefix=self.snapshot(self.prefix);vendor=self.snapshot(self.wine.parent.parent)
        self.fix.apply(self.game);self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])
        self.assertEqual(self.snapshot(self.wine.parent.parent),vendor)
        self.assertIn('"CX_GRAPHICS_BACKEND" = "dxmt"',self.fix.conf.read_text())
        self.assertEqual(self.fix.registry.read_bytes(),prefix["user.reg"])
        self.fix.apply(self.game,restoring=True)
        self.assertFalse(self.fix.detect(self.game)['dxmt_installed'])
        self.assertEqual(self.snapshot(self.prefix),prefix);self.assertEqual(self.snapshot(self.wine.parent.parent),vendor)

    def test_other_wine_uses_the_same_explicit_private_loader(self):
        f=self.make('wine');f.apply(self.game)
        self.assertTrue(f.detect(self.game)['dxmt_installed'])
        self.assertTrue(f.command(self.game/'Game.exe')[0].startswith(str(f.state/'runtime')))
        self.assertEqual((f.state/'runtime/lib/wine/x86_64-windows/d3d11.dll').read_bytes(),b'patched')
        self.assertEqual((self.wine.parent.parent/'lib/wine/x86_64-windows/d3d11.dll').read_bytes(),b'vendor renderer')

    def test_failed_probe_does_not_activate_or_touch_prefix(self):
        before=self.snapshot(self.prefix)
        self.fix.probe=lambda: (_ for _ in ()).throw(RuntimeError('incompatible ABI'))
        with self.assertRaisesRegex(RuntimeError,'ABI'):self.fix.apply(self.game)
        self.assertEqual(self.snapshot(self.prefix),before);self.assertFalse(self.fix.record.exists())

    def test_record_write_failure_does_not_change_prefix(self):
        before=self.snapshot(self.prefix);real=core.atomic
        def fail(path,data):
            if Path(path)==self.fix.record:raise OSError('disk full')
            real(path,data)
        with patch.object(core,'atomic',side_effect=fail):
            with self.assertRaisesRegex(OSError,'disk full'):self.fix.apply(self.game)
        self.assertEqual(self.snapshot(self.prefix),before);self.assertFalse(self.fix.record.exists())

    def test_shared_bottle_is_one_prepared_environment(self):
        self.fix.apply(self.game)
        other=self.home/'Games/Second';other.mkdir();(other/'Second.exe').write_bytes((self.game/'Game.exe').read_bytes())
        self.fix.apply(other);self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])
        self.fix.apply(other,restoring=True);self.assertFalse(self.fix.detect(self.game)['dxmt_installed'])

    def test_identity_is_canonical_and_independent_of_game(self):
        self.assertEqual(identity(self.prefix),self.fix.state.name)
        self.assertEqual(identity(self.prefix),identity(self.prefix/'..'/self.prefix.name))

    def test_symlink_registry_is_rejected(self):
        reg=self.prefix/'user.reg';other=self.home/'outside.reg';reg.rename(other);reg.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError,'обычными'):self.fix.apply(self.game)

    def test_gamehub_prefix_cannot_use_generic_installer(self):
        p=self.home/'Library/Application Support/com.gamemac.www/wine-engine/containers/virtual_containers/11'
        p.mkdir(parents=True);(p/'drive_c').mkdir();(p/'user.reg').write_text('registry')
        with self.assertRaisesRegex(RuntimeError,'GameHub'):self.make('wine',p).apply(self.game)

    def test_foreign_record_is_rejected(self):
        self.fix.apply(self.game);saved=self.fix.saved();saved['prefix']='/another/Mac';core.write_json(self.fix.record,saved)
        with self.assertRaisesRegex(RuntimeError,'другому'):self.fix.apply(self.game,restoring=True)

    def test_running_windows_apps_prevent_runtime_rebuild(self):
        self.fix.core.processes=lambda:[['123','steam.exe']]
        with self.assertRaisesRegex(RuntimeError,'Windows'):self.fix.apply(self.game)
        self.assertFalse(self.fix.record.exists())

    def test_launch_checks_active_environment_and_game_executable(self):
        with self.assertRaisesRegex(RuntimeError,'Сначала'):self.fix.launch(self.game,self.game/'Game.exe')
        self.fix.apply(self.game)
        with self.assertRaisesRegex(RuntimeError,'внутри'):self.fix.launch(self.game,self.home/'Outside.exe')
        with patch('wine_dxmt.subprocess.Popen') as launch:
            self.fix.launch(self.game,self.game/'Game.exe','-novid')
            args,kw=launch.call_args
            self.assertIn('-novid',args[0]);self.assertEqual(kw['env']['WINEPREFIX'],str(self.prefix));self.assertTrue(kw['start_new_session'])
            self.assertTrue(args[0][0].startswith(str(self.fix.state/'runtime')))

    def test_changed_source_runtime_requires_disable_and_rebuild(self):
        self.fix.apply(self.game)
        original=self.wine.parent.parent/'lib/wine/x86_64-unix/ntdll.so';original.write_bytes(b'updated native runtime')
        self.assertFalse(self.fix.detect(self.game)['dxmt_installed'])
        with self.assertRaisesRegex(RuntimeError,'Среда изменилась'):self.fix.apply(self.game)
        self.fix.apply(self.game,restoring=True);self.fix.apply(self.game)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])

    def test_disable_works_after_wine_was_removed(self):
        self.fix.apply(self.game);self.wine.unlink()
        self.assertTrue(self.fix.detect(self.game)['dxmt_backup'])
        self.fix.apply(self.game,restoring=True);self.assertFalse(self.fix.saved()['active'])

    def test_foreign_wine_environment_does_not_leak_into_launch(self):
        with patch.dict(os.environ,{'WINEPREFIX':'/foreign','CX_ROOT':'/foreign','DXMT_ASYNC_PSO':'0','DYLD_LIBRARY_PATH':'/foreign'}):
            env=self.fix.environment()
        self.assertEqual(env['WINEPREFIX'],str(self.prefix));self.assertEqual(env['DXMT_ASYNC_PSO'],'1')
        self.assertNotIn('CX_ROOT',env);self.assertNotIn('DYLD_LIBRARY_PATH',env)

    def test_corrupted_private_dll_blocks_launch_but_allows_disable(self):
        self.fix.apply(self.game);(self.fix.state/'runtime/lib/wine/x86_64-windows/d3d11.dll').write_bytes(b'bad')
        self.assertFalse(self.fix.detect(self.game)['dxmt_installed'])
        with self.assertRaisesRegex(RuntimeError,'Сначала'):self.fix.launch(self.game,self.game/'Game.exe')
        self.fix.apply(self.game,restoring=True);self.assertFalse(self.fix.saved()['active'])

    def test_wine_activation_keeps_prefix_unchanged(self):
        f=self.make('wine');before=self.snapshot(self.prefix)
        f.apply(self.game);self.assertEqual(self.snapshot(self.prefix),before)
        f.apply(self.game,restoring=True);self.assertEqual(self.snapshot(self.prefix),before)

    def test_restore_preserves_unrelated_concurrent_config_edits(self):
        self.fix.conf.write_text('[EnvironmentVariables]\n"CX_GRAPHICS_BACKEND" = "d3dmetal"\n"DXMT_PREWARM" = "0"\n"OTHER" = "keep"\n')
        self.fix.apply(self.game)
        with self.fix.conf.open('a') as out:out.write('"NEW_OPTION" = "new"\n')
        self.fix.apply(self.game,restoring=True)
        text=self.fix.conf.read_text()
        self.assertIn('"NEW_OPTION" = "new"',text);self.assertIn('"OTHER" = "keep"',text)
        self.assertIn('"CX_GRAPHICS_BACKEND" = "d3dmetal"',text)
        self.assertIn('"DXMT_PREWARM" = "0"',text);self.assertNotIn('"CX_ROOT"',text)

    def test_native_backup_precedes_configuration_write(self):
        real=core.atomic
        def observe(path,data):
            if Path(path)==self.fix.conf:
                saved=self.fix.saved();self.assertTrue(saved['pending']);self.assertFalse(saved['active'])
                self.assertEqual(saved['native']['before'],self.fix.conf.read_text())
            real(path,data)
        with patch.object(core,'atomic',side_effect=observe):self.fix.apply(self.game)

    def test_failed_activation_record_rolls_back_native_settings(self):
        before=self.fix.conf.read_bytes();real=core.atomic;failed=False
        def fail_once(path,data):
            nonlocal failed
            if Path(path)==self.fix.record and json.loads(data).get('active') and not failed:
                failed=True;raise OSError('activation record failed')
            real(path,data)
        with patch.object(core,'atomic',side_effect=fail_once):
            with self.assertRaisesRegex(OSError,'activation record'):self.fix.apply(self.game)
        self.assertEqual(self.fix.conf.read_bytes(),before);self.assertFalse(self.fix.saved()['active'])

    def test_pending_transaction_can_be_restored_after_restart(self):
        before=self.fix.conf.read_bytes();self.fix.apply(self.game)
        saved=self.fix.saved();saved.update(active=False,pending=True);core.write_json(self.fix.record,saved)
        f=self.make();self.assertFalse(f.detect(self.game)['dxmt_installed'])
        with self.assertRaisesRegex(RuntimeError,'прервалась'):f.apply(self.game)
        f.apply(self.game,restoring=True);self.assertEqual(f.conf.read_bytes(),before)

    def test_crossovers_backend_change_is_detected(self):
        self.fix.apply(self.game)
        self.fix.conf.write_text(self.fix.conf.read_text().replace('"dxmt"','"d3dmetal"'))
        result=self.fix.detect(self.game)
        self.assertFalse(result['dxmt_installed']);self.assertTrue(result['runtime_error'])
        with self.assertRaisesRegex(RuntimeError,'сначала отключи'):self.fix.apply(self.game)
        self.fix.apply(self.game,restoring=True)

    def test_reapply_keeps_original_graphics_backup(self):
        self.fix.conf.write_text('[EnvironmentVariables]\n"CX_GRAPHICS_BACKEND" = "dxvk"\n')
        before=self.fix.conf.read_bytes();self.fix.apply(self.game);self.fix.apply(self.game)
        self.fix.apply(self.game,restoring=True);self.assertEqual(self.fix.conf.read_bytes(),before)

    def test_crossover_backend_is_private_and_corruption_is_detected(self):
        self.fix.apply(self.game)
        backend=self.fix.state/'runtime/lib/dxmt/x86_64-windows/d3d11.dll'
        self.assertEqual(backend.read_bytes(),b'patched');backend.write_bytes(b'damaged')
        self.assertFalse(self.fix.detect(self.game)['dxmt_installed'])
        self.fix.apply(self.game,restoring=True)

    def test_failed_reapply_retains_active_native_configuration(self):
        self.fix.apply(self.game);before=self.fix.conf.read_bytes();real=core.atomic;failed=False
        def fail_once(path,data):
            nonlocal failed
            if Path(path)==self.fix.record and json.loads(data).get('active') and not failed:
                failed=True;raise OSError('reapply failed')
            real(path,data)
        with patch.object(core,'atomic',side_effect=fail_once):
            with self.assertRaisesRegex(OSError,'reapply failed'):self.fix.apply(self.game)
        self.assertEqual(self.fix.conf.read_bytes(),before)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])

if __name__=='__main__':unittest.main()
