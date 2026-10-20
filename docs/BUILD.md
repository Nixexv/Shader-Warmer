# Сборка и проверки

Пользователю готового DMG не нужны Python, Homebrew или Xcode. Версии и SHA-256 компонентов: `runtime/python.json` и `dxmt_async/manifest.json`.

## GUI

Нужны macOS, Apple Silicon, Xcode Command Line Tools и Python 3.12+. Python runtime скачивается при первой сборке и проверяется по SHA-256.

```sh
cd gui
bash build_app.sh
bash make_dmg.sh
```

Выход: `gui/build/Shader Warmer.app` и `gui/build/DeadlockWarmer.dmg`.

Готовые DXMT DLL и нативные вспомогательные бинарники хранятся в репозитории. Это сборка приложения с этими компонентами, а не полная сборка каждого бинарника из исходников.

## Дополнительный Overwatch-прогрев

Готовое приложение включает `ow/bin/owshaderdump/`. Эта большая вспомогательная программа не хранится в git, и её отдельный исходный проект сюда не включён. Для воспроизведения этого дополнительного workflow нужен такой же helper в `ow/bin/owshaderdump/` перед сборкой. Без него основной DXMT Async работает, но извлечение шейдеров в GPTK-прогреве Overwatch недоступно. Сборка без helper не полностью идентична опубликованному DMG.

## Тесты

Из корня репозитория:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B tools/verify_portability.py "gui/build/Shader Warmer.app"
```

Часть тестов использует macOS, Swift/clang и codesign. Проверка переносимости переносит приложение в путь с пробелами и кириллицей, использует пустую домашнюю папку и системный PATH, проверяет зависимости, контрольные суммы DXMT и подпись. Она не заменяет запуск на каждом Mac и в каждой игре.

`gui/sign_app.sh` пробует локальную самоподпись и может использовать ad-hoc как запасной вариант. Это не Developer ID и не notarization Apple. Секретные ключи и keychain разработчика в репозиторий не входят.

Нативные конвертеры: `tools/build_native.sh`; zstd: `tools/build_zstd.py`; D3D11 probe: команда из `gui/README.md`. Изменения DXMT: `dxmt_async/patches/`, базовая версия v0.80. GPTK/D3DMetal для дополнительных конвертеров берутся из установки пользователя и не входят в репозиторий.
