"""Execute the actual Swift scanner against large and shared-prefix libraries."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'macOS Swift scanner')
class GameScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='Shader Scanner ')
        cls.base=Path(cls.temp.name)
        sources=Path(__file__).resolve().parents[1]/'gui/Sources'
        model=(sources/'Model.swift').read_text()
        start=model.index('    struct Game:');end=model.index('\n    @Published var phase',start)
        stub='import Foundation\nenum WarmerModel {\n enum GameEngine: String { case deadlock, overwatch, unity }\n'+model[start:end]+'\n}\n'
        stub+='struct LauncherProfile { let prefix: String; let id: String; var title: String { id } }\n'
        stub+='''@main struct Check {
 static func main() throws {
  let prefix=CommandLine.arguments[1]
  let profiles=CommandLine.arguments.dropFirst(2).map { LauncherProfile(prefix: prefix, id: $0) }
  print(String(data: try JSONEncoder().encode(GameScanner.scanProfiles(profiles)),encoding: .utf8)!)
 }
}
'''
        source=cls.base/'Harness.swift';source.write_text(stub)
        cls.binary=cls.base/'scanner'
        subprocess.run(['swiftc','-module-cache-path',str(cls.base/'Cache'),str(sources/'GameScanner.swift'),str(source),'-o',str(cls.binary)],check=True,capture_output=True,text=True)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def setUp(self):
        self.fixture=tempfile.TemporaryDirectory(dir=self.base);self.addCleanup(self.fixture.cleanup)
        self.prefix=Path(self.fixture.name)
        self.common=self.prefix/'drive_c/Program Files (x86)/Steam/steamapps/common'
        self.common.mkdir(parents=True)

    def scan(self,*ids):
        return json.loads(subprocess.check_output([str(self.binary),str(self.prefix),*(ids or ('Steam',))],text=True))

    def deadlock(self):
        game=self.common/'Deadlock';(game/'game/citadel').mkdir(parents=True)
        (game/'game/citadel/shaders_pc_dir.vpk').touch()
        for i in range(600):(game/f'asset-{i}.dat').touch()
        (game/'game/bin/win64').mkdir(parents=True);(game/'game/bin/win64/deadlock.exe').touch()
        return game

    def test_large_deadlock_is_found_before_bounded_executable_search(self):
        game=self.deadlock();found=self.scan()
        self.assertEqual(len(found),1);self.assertEqual(found[0]['path'],str(game/'game'))
        self.assertEqual(found[0]['engine'],'deadlock');self.assertEqual(found[0]['profileID'],'Steam')

    def test_same_installation_remains_selectable_in_multiple_launchers(self):
        self.deadlock();found=self.scan('CrossOver Steam','Other Wine')
        self.assertEqual({x['profileID'] for x in found},{'CrossOver Steam','Other Wine'})

    def test_non_windows_folders_are_ignored(self):
        (self.common/'Native Game').mkdir();(self.common/'Native Game/readme.txt').touch()
        (self.common/'Windows Game').mkdir();(self.common/'Windows Game/Game.exe').touch()
        self.assertEqual([x['name'] for x in self.scan()],['Windows Game'])

if __name__=='__main__':unittest.main()
