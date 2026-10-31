DSR LIGHTING ENGINE
A lighting overhaul for DARK SOULS: REMASTERED (PC, Steam, DX11)

What it is
----------
DSR Lighting Engine brings back the moods of the original Dark Souls in Remastered: deeper shade,
less flat fill light, characters that sit in the scene instead of glowing. It also adds a modern
lighting model on top of FromSoftware's art direction. It retunes the areas one by one and
upgrades the game's own shaders. It contains no assets or values from the original release.
Colour grading is touched only lightly: small saturation, contrast and brightness trims in five
areas keep colours close to vanilla after the lighting changes, plus a slightly cooler grade in
the Painted World.

What it changes
---------------
* Per-area lighting (all areas, DLC included): ambient fill, light colours and the sun are retuned
  area by area against measured targets. Exposure, bloom, Darkroot's haze and shadow range are
  adjusted where an area needed it.
* Real-time sun: characters, enemies and objects (anything without baked lighting) are lit by the
  sun from the same direction the shadows fall. Interiors, caves and night areas have it switched
  off. Snow and lava keep their vanilla look, and so does most water. The water in the Painted
  World basement, Pinwheel's room and the Asylum corridor and sewer loses its vanilla sheen,
  because its light row is shared with indoor meshes that must not get a sun.
* Soft shadows: 16-sample rotated shadow filtering instead of the blocky 3x3 grid, with a smooth
  blend between shadow detail levels.
* Specular occlusion: reflections no longer glow out of cracks and dark corners.
* Smoother torch and bonfire light: the bright ring at the edge of point lights is gone. Light reach
  is unchanged; brightness is up to 15% higher right next to the source and about 5% lower where
  the ring used to be.
* Crisper anti-aliasing: the game's TAA uses a sharper (Catmull-Rom) history and gentle
  contrast-adaptive sharpening. Fine detail comes back at high resolutions without halos, and the
  sharpening cannot build up from frame to frame. Standing still, fine texture can shimmer very
  slightly more than in vanilla.

Files
-----
The mod replaces only loose, client-side visual files:
  param\DrawParam\a10..a18_DrawParam.parambnd.dcx
  shader\FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx
  shader\FRPG_Filter_DX11.shaderbnd.dcx
It never touches the executable, DLLs, GameParam, maps, events or saves, and it injects nothing.

Install
-------
1. Close the game and extract the zip. Keep the extracted folder: its uninstall.bat removes the mod
   (a later version's uninstall.bat works too).
2. Double-click install.bat.
   The game folder is found through Steam. If it is somewhere unusual, run from a command prompt
   in this folder:   install.bat -GameDir "X:\...\DARK SOULS REMASTERED"
   Every replaced file is backed up first, to <game>\DSRLE_backup.
   If a file belongs to another mod, the installer stops without changing anything.
   install.bat -Force replaces it anyway, and the backup keeps the other mod's file.

Uninstall
---------
Double-click uninstall.bat. It restores every backed-up file and checks its hash.
If another mod replaced one of the files after you installed, uninstall.bat leaves that file alone
and says so; uninstall.bat -Force puts the original back over it anyway.
If the backup is gone, use Steam > Dark Souls Remastered > Properties > Installed Files >
Verify integrity of game files.

Compatibility
-------------
* Works with texture packs (e.g. DSR-TPUP) and UI mods.
* Does NOT work with other mods that replace DrawParam or shader files, e.g. DSR Restored Lighting,
  Visual Overhaul, DSR Shader Overhaul or similar lighting mods. Uninstall those first.
* ReShade can run on top, but keep its colour effects subtle. The mod already sharpens, so skip
  ReShade sharpening.
* The anti-aliasing improvements apply to the game's TAA pass (tested with anti-aliasing on). With
  anti-aliasing off they do nothing.

Online play
-----------
This mod changes only lighting tables and shaders on your PC, in the same category as texture
mods. It does not touch GameParam, the executable or your save. Even so, no mod can guarantee you
won't be flagged online. Use it online at your own risk; playing offline is the safe choice.

Performance
-----------
The shader upgrades add work to each pixel. On an RTX 5090 at 3840x2160 and 60 fps (the game's
frame cap) the cost was within measurement noise. On older GPUs, expect a small drop.

Credits
-------
Original work. No PTDE files or values were used. The moods were reconstructed from public
comparisons of the two releases. Thanks to the Dark Souls modding community and its tools
(Soulstruct, Paramdex, DSR-Gadget).
