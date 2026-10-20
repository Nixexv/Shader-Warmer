# Bundled binaries (Overwatch engine)

| файл | что это | откуда |
|---|---|---|
| `dxbc2dxil` | DXBC → DXIL через `libdxilconv` из D3DMetal | `clang++ -arch x86_64 -std=c++17 -O2 dxbc2dxil.cpp` |
| `stage2metallib` | DXIL → metallib + рефлексия через приватный `libmetalirconverter` из D3DMetal | `clang++ -arch x86_64 -std=c++17 -O2 stage2metallib.cpp -L"$RES" -lmetalirconverter -Wl,-rpath,@executable_path/gptk_res` |
| `warmgen_ow.exe` | прогрев через сам D3DMetal: запасной путь и калибровка | `x86_64-w64-mingw32-gcc -O2 warmgen2.c -o warmgen_ow.exe -ld3d11 -ldxgi -luuid` |
| `libxxh3.dylib` + `xxh3_secret.bin` | XXH3-64/128 — контрольные суммы записей кэша и ShaderID | `clang -O3 -shared -o libxxh3.dylib xxh3.c` |
| `owshaderdump/` | извлекатель шейдеров игры (самодостаточный .NET, ~82 МБ) | `dotnet publish -c Release -r osx-arm64 --self-contained true -p:PublishSingleFile=true` |

`owshaderdump/` **не хранится в git** из-за размера: он собирается из исходников лаборатории и едет
в релизе внутри `.app`. Без него движок Overwatch не сможет достать шейдеры игры.

`stage2metallib` ищет `libmetalirconverter.dylib` по ссылке `gptk_res` рядом с собой — её создаёт
`ow_warm.py` в рабочей папке, поэтому GPTK пользователя может лежать где угодно.
