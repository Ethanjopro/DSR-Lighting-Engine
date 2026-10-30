# DSR Lighting Engine (working title)

A lighting overhaul for Dark Souls Remastered (PC, DX11). It retunes the game's per-area lighting data (DrawParam) and upgrades the renderer's shaders. The goal is to keep the original release's area moods while lighting them with a modern model.

The full plan is in [docs/PLAN.md](docs/PLAN.md).

## What the default install changes
Only loose, client-side visual files:
- `param/DrawParam/a10..a18_DrawParam.parambnd.dcx`
- `shader/*.shaderbnd.dcx`

It never touches GameParam, map layout (MSB), events, saves, the executable, or any DLL. That puts it in the same category as texture mods. Online safety is still not guaranteed.

## Repository layout
| Path | Contents |
|---|---|
| `vanilla/` | Pristine copies of the files we change. Only `manifest.sha256` is committed. |
| `vanilla_src/` | Shader source recovered from the game's own debug data. Git-ignored and never distributed. |
| `data/` | Vanilla tables, live-row maps and locations extracted by script. |
| `spec/` | The mod's lighting changes, written relative to vanilla. |
| `looks/` | Per-area look bibles. |
| `shaders/` | Shader hooks, features, filter rewrites and the build. |
| `tools/` | Extract, build, validate, install/uninstall and capture scripts. |
| `dist/` | Build output, laid out like the game folder. |

## Compatibility
- Works alongside DSR-TPUP texture packs and UI mods.
- Conflicts with other DrawParam or shader mods, such as DSR Restored Lighting, DSR Shader Overhaul and Visual Overhaul.
