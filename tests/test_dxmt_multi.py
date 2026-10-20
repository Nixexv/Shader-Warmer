import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import dxmt_fix as d
import dxmt_game
import test_dxmt_fix as fixtures


class MultiGameTests(unittest.TestCase):
    setUp = fixtures.DXMTFixTests.setUp
    put = fixtures.DXMTFixTests.put

    def add_game(self, name='Another Game', app_id='648800', prefix='22', mode='unity', evidence=True):
        home = Path(self.temp.name)
        game = home / 'GameHub/steamapps/common' / name
        executable = self.pe64 if evidence else self.pe64.replace(b'd3d11.dll\0', b'd3d12.dll\0')
        self.put(game / (name + '.exe'), executable)
        if mode == 'unity':
            self.put(game / (name + '_Data/globalgamemanagers'), b'unity')
        if mode == 'unreal':
            (game / 'Engine').mkdir()
        self.put(home / ('GameHub/steamapps/appmanifest_' + app_id + '.acf'),
                 ('"AppState" { "appid" "' + app_id + '" "installdir" "' + name + '" }').encode())
        store = self.fix.root / 'gamehub/game_container_store.json'
        document = json.loads(store.read_text())
        document['bindings'].append({'platform': 'steam', 'platform_app_id': app_id,
            'game_path': str(home / 'GameHub'), 'virtual_container_id': prefix, 'game_name': name})
        store.write_text(json.dumps(document))
        registry = self.fix.engine / 'containers/virtual_containers' / prefix / 'user.reg'
        if not registry.exists():
            self.put(registry, b'WINE REGISTRY Version 2\n\n[Environment]\n"KEEP"="other game"\n')
            (registry.parent / 'drive_c').mkdir()
        settings = self.settings.with_name(app_id + '.json')
        document = copy.deepcopy(self.doc)
        document['key']['platform_app_id'] = app_id
        document['settings']['graphics_stack'] = {'kind': 'gptk', 'component_id': 'original-gptk', 'component_config': {'name': 'gptk-3.0-3'}}
        document['settings']['start_parameters'] = '-novid -force-vulkan' if mode == 'unity' else '-novid'
        self.put(settings, json.dumps(document).encode())
        return game, registry, settings

    def test_two_games_enable_then_restore_in_any_order(self):
        other, registry, settings = self.add_game()
        original_other = json.loads(settings.read_text())['settings']
        original_registry = registry.read_text()
        self.fix.apply(self.game)
        deadlock_record = self.fix.record
        deadlock_registry = self.registry.read_bytes()
        self.fix.apply(other)
        other_record = self.fix.record
        self.assertNotEqual(deadlock_record, other_record)
        self.assertEqual(json.loads(settings.read_text())['settings']['start_parameters'], '-novid -force-d3d11')
        self.assertEqual(self.registry.read_bytes(), deadlock_registry)
        self.assertTrue(self.fix.detect(other)['dxmt_installed'])
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])
        self.fix.apply(self.game, restoring=True)
        self.assertTrue(self.fix.detect(other)['dxmt_installed'])
        self.assertEqual((self.fix.component / 'wine/x86_64-windows/d3d11.dll').read_bytes(), self.patch_bytes)
        self.fix.apply(other, restoring=True)
        self.assertEqual(registry.read_text().rstrip(), original_registry.rstrip())
        restored = json.loads(settings.read_text())['settings']
        for key in d.SETTINGS_KEYS:
            self.assertEqual(restored.get(key), original_other.get(key))
        self.assertEqual((self.fix.component / 'wine/x86_64-windows/d3d11.dll').read_bytes(), b'original vendor DLL')

    def test_second_game_can_be_first_install(self):
        other, _, _ = self.add_game()
        self.fix.apply(other)
        self.fix.apply(self.game)
        self.fix.apply(other, restoring=True)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])
        self.fix.apply(self.game, restoring=True)
        self.assertEqual((self.fix.component / 'wine/x86_64-windows/d3d11.dll').read_bytes(), b'original vendor DLL')

    def test_existing_144_deadlock_backup_migrates_without_seed(self):
        self.fix.apply(self.game)
        for name in ('component-backup.json', 'backup-record.json', 'installation.seed.json'):
            (self.fix.state / name).unlink(missing_ok=True)
        other, _, _ = self.add_game()
        self.fix.apply(other)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])

    def test_same_prefix_is_rejected_without_changing_second_game(self):
        other, _, settings = self.add_game(prefix='11')
        self.fix.apply(self.game)
        before = settings.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'отдельный контейнер'):
            self.fix.apply(other)
        self.assertEqual(settings.read_bytes(), before)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])

    def test_32bit_game_is_rejected(self):
        other, _, settings = self.add_game()
        pe = bytearray(self.pe64); pe[68:70] = b'\x4c\x01'
        (other / (other.name + '.exe')).write_bytes(pe)
        self.put(other / 'UE4PrereqSetup_x64.exe', self.pe64)
        before = settings.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '32-битные'):
            self.fix.apply(other)
        self.assertEqual(settings.read_bytes(), before)

    def test_unknown_dx11_requires_confirmation_and_remembers_it(self):
        other, _, settings = self.add_game(mode='generic', evidence=False)
        before = settings.read_bytes()
        result = self.fix.detect(other)
        self.assertTrue(result['dxmt_requires_confirmation'])
        self.assertFalse(result['dxmt_available'])
        with self.assertRaisesRegex(RuntimeError, 'Подтверди'):
            self.fix.apply(other)
        self.assertEqual(settings.read_bytes(), before)
        self.fix.dx11_confirmed = True
        self.fix.apply(other)
        self.fix.dx11_confirmed = False
        self.assertTrue(self.fix.detect(other)['dxmt_available'])
        self.assertEqual(json.loads(settings.read_text())['settings']['start_parameters'], '-novid')

    def test_generic_game_does_not_receive_unknown_dx11_flag(self):
        other, _, settings = self.add_game(mode='generic')
        document = json.loads(settings.read_text())
        args = r'--profile "C:\My Games\Profile" -novid'
        document['settings']['start_parameters'] = args
        settings.write_text(json.dumps(document))
        self.fix.apply(other)
        self.assertEqual(json.loads(settings.read_text())['settings']['start_parameters'], args)

    def test_generic_game_explicit_dx12_is_rejected(self):
        other, _, settings = self.add_game(mode='generic')
        document = json.loads(settings.read_text()); document['settings']['start_parameters'] = '-dx12'
        settings.write_text(json.dumps(document))
        with self.assertRaisesRegex(RuntimeError, 'другой API'):
            self.fix.apply(other)
        self.assertFalse(self.fix.backup.exists())

    def test_unreal_uses_dx11_switch(self):
        other, _, settings = self.add_game(mode='unreal')
        document = json.loads(settings.read_text()); document['settings']['start_parameters'] = '-dx12 -windowed'
        settings.write_text(json.dumps(document))
        self.fix.apply(other)
        self.assertEqual(json.loads(settings.read_text())['settings']['start_parameters'], '-windowed -dx11')

    def test_other_game_partial_install_keeps_first_game_active(self):
        other, registry, settings = self.add_game()
        self.fix.apply(self.game)
        before = settings.read_bytes(), registry.read_bytes()
        real_atomic = d.atomic
        failed = False
        def fail_once(path, data):
            nonlocal failed
            if Path(path) == settings and not failed:
                failed = True
                raise OSError('simulated write failure')
            real_atomic(path, data)
        with patch.object(d, 'atomic', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.fix.apply(other)
        self.assertEqual((settings.read_bytes(), registry.read_bytes()), before)
        self.assertTrue(self.fix.detect(self.game)['dxmt_installed'])

    def test_pending_transaction_recovers_even_when_different_game_selected(self):
        other, _, _ = self.add_game()
        ctx = self.fix.context(self.game)
        self.fix.state.mkdir(parents=True)
        self.fix.prepare_record(ctx)
        self.fix.begin_transaction(ctx, self.fix.component_bytes(), self.fix.package()['files'])
        self.settings.write_bytes(b'truncated-json')
        pending = self.fix.detect(self.game)
        self.assertTrue(pending['dxmt_available'])
        self.assertIsNone(pending['runtime_error'])
        self.fix.apply(other)
        self.assertIsInstance(json.loads(self.settings.read_text()), dict)
        self.assertFalse((self.fix.state / 'transaction').exists())

    def test_local_game_matches_exact_local_id(self):
        game = Path(self.temp.name) / 'Local Игра'
        exe = game / 'Play.exe'; self.put(exe, self.pe64)
        prefix = self.fix.engine / 'containers/virtual_containers/44'
        self.put(prefix / 'user.reg', b'WINE REGISTRY Version 2\n')
        (prefix / 'drive_c').mkdir()
        store = self.fix.root / 'gamehub/game_container_store.json'
        doc = json.loads(store.read_text()); doc['bindings'].append({'platform': 'local', 'local_game_id': 'local-specific',
            'game_path': str(exe), 'virtual_container_id': '44'})
        store.write_text(json.dumps(doc))
        document = copy.deepcopy(self.doc); document['key'] = {'kind': 'local', 'local_game_id': 'local-specific'}
        document['settings']['start_parameters'] = ''
        self.put(self.settings.with_name('local.json'), json.dumps(document).encode())
        self.fix.apply(game)
        self.assertTrue(self.fix.detect(game)['dxmt_installed'])
        self.assertTrue(self.fix.record.name.startswith('local-'))

    def test_running_second_game_blocks_mutation(self):
        other, _, settings = self.add_game()
        self.fix.processes = lambda: [['42', r'C:\games\Another Game.exe']]
        before = settings.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Закрой Another Game'):
            self.fix.apply(other)
        self.assertEqual(settings.read_bytes(), before)

    def test_last_restore_removes_only_files_added_by_package(self):
        name = 'wine/x86_64-windows/extra.dll'
        self.put(self.fix.assets / name, b'package-only')
        manifest = self.fix.assets / 'manifest.json'
        doc = json.loads(manifest.read_text()); doc['files'][name] = hashlib.sha256(b'package-only').hexdigest()
        manifest.write_text(json.dumps(doc))
        self.fix.apply(self.game)
        self.assertTrue((self.fix.component / name).exists())
        self.fix.apply(self.game, restoring=True)
        self.assertFalse((self.fix.component / name).exists())

    def test_corrupt_other_active_record_blocks_global_restore(self):
        other, _, _ = self.add_game()
        self.fix.apply(self.game)
        self.fix.apply(other)
        self.fix.record.write_text('[]')
        with self.assertRaisesRegex(RuntimeError, 'неподдерживаемый формат'):
            self.fix.apply(self.game, restoring=True)
        self.assertEqual((self.fix.component / 'wine/x86_64-windows/d3d11.dll').read_bytes(), self.patch_bytes)
