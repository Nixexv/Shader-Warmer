# Third-party components

The root MIT license applies to Shader Warmer's own code and changes. It does not replace the licenses of third-party components, games or runtimes.

| Component | Location / notice |
| --- | --- |
| DXMT v0.80 | MIT; [full notice](dxmt_async/LICENSE-DXMT), [upstream](https://github.com/3Shain/dxmt). Binaries and checksum manifest are in `dxmt_async/`; changes are provided as source patches. |
| zstd 1.5.7 | BSD license; [full notice](lib/LICENSE-zstd). Bundled as `lib/libzstd.dylib`. |
| CPython and bundled dependencies | Included in the app under `Contents/Resources/python/`, with `lib/python3.13/LICENSE.txt` and dependency notices. Provenance: `runtime/python.json`. |

The optional Overwatch cache workflow includes a prebuilt extraction helper in the app. Its separate source project is not included here; see [build limitations](docs/BUILD.md). The root MIT license does not relicense third-party contents embedded in prebuilt helpers.

GameHub, CrossOver, Wine engines, GPTK/D3DMetal and games are supplied by the user's installation. Shader Warmer does not include CrossOver, GPTK/D3DMetal or game installations. Artwork and names belong to their respective owners; artwork is read locally or optionally requested from Steam's CDN.

Shader Warmer is an independent project and is not an official product of Apple, Valve, CodeWeavers or GameHub.
