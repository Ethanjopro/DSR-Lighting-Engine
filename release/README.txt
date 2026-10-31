DSR LIGHTING ENGINE
A lighting overhaul for DARK SOULS: REMASTERED (PC, Steam, DX11)

What it is
----------
DSR Lighting Engine brings back the moods of the original Dark Souls in Remastered: deeper shade,
less flat fill light, characters that sit in the scene instead of glowing. It also adds a modern
lighting model on top of FromSoftware's art direction. It retunes every area by hand and upgrades
the game's own shaders. It contains no assets or values from the original release, and it does not
re-grade the game's colours.

What it changes
---------------
* Per-area lighting (all areas, DLC included): the ambient fill, light colours, fog, haze,
  exposure and shadow range are retuned area by area against measured targets.
* Real-time sun: characters, enemies and objects (anything without baked lighting) are lit by the
  sun from the same direction the shadows fall. Interiors, caves and night areas have it switched
  off, and snow, water and lava keep their vanilla look.
* Soft shadows: 16-sample rotated shadow filtering instead of the blocky 3x3 grid, with a smooth
  blend between shadow detail levels.
* Specular occlusion: reflections no longer glow out of cracks and dark corners.
* Smoother torch and bonfire light: the bright ring at the edge of point lights is gone, and their
  reach and brightness stay within a few percent of vanilla.
* Crisper anti-aliasing: the game's TAA uses a sharper (Catmull-Rom) history and gentle
  contrast-adaptive sharpening. Fine detail comes back at high resolutions without halos.
  Strong edges are left alone, and the sharpening cannot build up from frame to frame.

Files
-----
The mod replaces only loose, client-side visual files:
  param\DrawParam\a10..a18_DrawParam.parambnd.dcx
  shader\FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx
  shader\FRPG_Filter_DX11.shaderbnd.dcx
It never touches the executable, DLLs, GameParam, maps, events or saves, and it injects nothing.

Install
-------
1. Close the game.
2. Double-click install.bat (or run: powershell -ExecutionPolicy Bypass -File install.ps1).
   The game folder is found through Steam. If it is somewhere unusual, run
   install.ps1 -GameDir "X:\...\DARK SOULS REMASTERED".
   Every replaced file is backed up first, to <game>\DSRLE_backup.
   If a file belongs to another mod, the installer stops without changing anything.
   install.ps1 -Force replaces it anyway, and the backup keeps the other mod's file.

Uninstall
---------
Double-click uninstall.bat. It restores every backed-up file and checks its hash.
If the backup is gone, use Steam > Dark Souls Remastered > Properties > Installed Files >
Verify integrity of game files.

Compatibility
-------------
* Works with texture packs (e.g. DSR-TPUP) and UI mods.
* Does NOT work with other mods that replace DrawParam or shader files, e.g. DSR Restored Lighting,
  Visual Overhaul, DSR Shader Overhaul or similar lighting mods. Uninstall those first.
* ReShade can run on top, but keep its colour effects subtle. The mod already sharpens, so skip
  ReShade sharpening.
* The TAA improvements apply when the in-game anti-aliasing option is on (the default).

Online play
-----------
This mod changes only visual files on your PC, in the same category as texture mods. It does not
change game data that the servers check, and nothing in it touches your save. Even so, no mod
can guarantee you won't be flagged online. Use it online at your own risk; playing offline is the
safe choice.

Performance
-----------
The shader upgrades add work to each pixel. On a modern GPU the cost was not measurable at
3840x2160 and 60 fps. On older GPUs, expect a small drop.

Credits
-------
Original work. No PTDE files or values were used. The moods were reconstructed from public
comparisons of the two releases. Thanks to the Dark Souls modding community and its tools
(Soulstruct, Paramdex, DSR-Gadget).
