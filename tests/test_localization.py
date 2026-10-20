"""Exercise the actual Swift localizer and the release's translation resource."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_translations_keep_arguments_and_are_english(self):
        pairs = json.loads((ROOT / 'gui/Resources/Localizations.json').read_text())
        self.assertGreater(len(pairs), 300)
        for source, translation in pairs.items():
            with self.subTest(source=source):
                self.assertTrue(translation.strip())
                self.assertFalse(re.search('[А-Яа-яЁё]', translation))
                self.assertEqual(sorted(re.findall(r'\{\d+\}', source)),
                                 sorted(re.findall(r'\{\d+\}', translation)))

    def test_build_ships_language_resources(self):
        import plistlib
        info = plistlib.loads((ROOT / 'gui/Info.plist').read_bytes())
        self.assertEqual(info['CFBundleDevelopmentRegion'], 'en')
        self.assertEqual(info['CFBundleLocalizations'], ['en', 'ru'])
        script = (ROOT / 'gui/build_app.sh').read_text()
        self.assertIn('Sources/Localization.swift', script)
        self.assertIn('cp Resources/Localizations.json', script)


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'macOS Swift localizer')
class RuntimeTests(unittest.TestCase):
    def test_language_fallback_interpolation_and_engine_errors(self):
        harness = r'''
import Foundation
@main struct Check {
    static func main() {
        let catalog = URL(fileURLWithPath: CommandLine.arguments[1])
        let english = AppLocalizer(preferences: ["en-GB", "ru"], catalogURL: catalog)
        let russian = AppLocalizer(preferences: ["ru-Cyrl-RU", "en"], catalogURL: catalog)
        for preferences in [["fr-FR"], ["ja-JP"], []] {
            assert(AppLocalizer.language(for: preferences) == "en")
        }
        assert(AppLocalizer.language(for: ["de-DE", "ru_RU"]) == "ru")
        assert(english.text("Мои игры") == "My games")
        assert(russian.text("Мои игры") == "Мои игры")
        let path = "/Volumes/Игры {1}/Deadlock"
        let detail: LocalizedText = "Контейнер: \(path)"
        assert(english.text(detail) == "Prefix: " + path)
        assert(russian.text(detail) == "Контейнер: " + path)
        assert(english.text("\(3) из \(12)") == "3 of 12")
        assert(english.message("Закрой Deadlock перед изменением настроек") == "Close Deadlock before changing settings")
        assert(english.message("Нет прав на запись или диск доступен только для чтения: " + path) == "Permission denied or read-only drive: " + path)
        assert(english.message("Недостаточно места для отдельной Wine-среды: нужно около 512 МБ") == "Not enough space for a private Wine environment: about 512 MB required")
        assert(english.message("Async включён · запускай из CrossOver") == "Async enabled · launch from CrossOver")
        assert(english.message("GameHub: настройки игры: файл не найден") == "GameHub: game settings: file not found")
        assert(russian.message("Async включён · запускай из CrossOver") == "Async включён · запускай из CrossOver")
        assert(english.message("unknown diagnostic " + path) == "unknown diagnostic " + path)
        assert(english.message("GameHub · Wine Proton 11 · DXMT v0.80") == "GameHub · Wine Proton 11 · DXMT v0.80")
        // Verify macOS per-process preferred language selection, not just injected unit fixtures.
        assert(L10n.current.language == CommandLine.arguments[2])
        print("Localization checks passed")
    }
}
'''
        with tempfile.TemporaryDirectory(prefix='Localizer Кириллица ') as tmp:
            base = Path(tmp)
            source = base / 'Harness.swift'
            source.write_text(harness)
            binary = base / 'localizer'
            subprocess.run(['swiftc', '-module-cache-path', str(base / 'Cache'),
                            str(ROOT / 'gui/Sources/Localization.swift'), str(source),
                            '-o', str(binary)], check=True, capture_output=True, text=True)
            for preference, expected in [('en-GB', 'en'), ('ru-RU', 'ru'), ('fr-FR', 'en')]:
                result = subprocess.run([str(binary), str(ROOT / 'gui/Resources/Localizations.json'),
                                         expected, '-AppleLanguages', f'({preference})'],
                                        check=True, capture_output=True, text=True)
                self.assertIn('Localization checks passed', result.stdout)
