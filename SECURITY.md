# Security

DSR Lighting Engine is a hobby mod kept by one person. This page says what the mod does on your PC, what counts as a security problem in it, and how to report one.

## What runs on your PC

- **A DLL inside the game.** `d3d11.dll` sits in the game folder, so Dark Souls Remastered loads it in place of the Windows graphics library. It is not signed, and an antivirus may flag it for that reason. It loads the real `d3d11.dll`, `dxgi.dll` and the shader compiler from `System32` by full path and hooks the game's Direct3D calls.
- **No network code.** The DLL links against `kernel32`, `user32`, `bcrypt` and Direct3D only, and its source calls no network or process-launching API.
- **Files it reads.** From the `dsrle` folder beside it: `dsrle.ini`, `maps.ini`, `shaders.pak`, `shader_names.tsv`, `maps_detect.txt` and the HLSL files in `dsrle\hlsl`, which it compiles while the game runs.
- **Files it writes.** `dsrle\dsrle.log`, and only on request: frame traces in `dsrle\trace`, copies of unknown shaders in `dsrle\unknown` (`[dev] dump_unknown`), and a screenshot at the path written in `dsrle\trigger_shot`.
- **The installer.** `install.bat` and `uninstall.bat` run PowerShell scripts with `-ExecutionPolicy Bypass`. They copy files into the game folder, replace the DrawParam files after backing them up to `DSRLE_backup`, and check every file against the SHA-256 in `manifest.json`.

Anyone who can write to the game folder can already change what the game runs, so the files above are trusted as much as the game's own. The trigger files are test hooks: a program that can create `dsrle\trigger_shot` can make the game write an image file wherever the game itself may write.

## Online play

This project makes no claim about online play, anti-cheat or bans. The mod is code running in the game process and it replaces game data files. Play offline; see `release/README.txt`.

## Supported versions

Only the newest version in this repository gets fixes.

## What to report

- A crash or memory error in the DLL that a file in the `dsrle` folder, a save or the game's own data can trigger.
- The installer or uninstaller deleting, overwriting or failing to restore a file it should not touch.
- Anything that lets the mod's files run code or write outside the game folder beyond what is described above.
- A published build whose files do not match the source in this repository.

Not security problems: the look of the game, performance, conflicts with other mods, and bugs in the game itself. Open a normal issue for those.

## How to report

Use GitHub's private reporting: the **Security** tab of this repository, then **Report a vulnerability**. If that button is not there, open an issue that says only that you have a security report, with no details, and a private channel will be arranged.

Please include the mod version, which is near the top of `dsrle\dsrle.log`, what you did, and what happened. No response time is promised.
