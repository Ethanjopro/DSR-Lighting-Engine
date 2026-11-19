DSR LIGHTING ENGINE
A lighting overhaul for DARK SOULS: REMASTERED (PC, Steam, DX11)

What it is
----------
DSR Lighting Engine replaces how Dark Souls: Remastered lights its world, in the spirit of DS2 Lighting
Engine: a real sun with cast shadows, torches and fires that light rooms and throw moving shadows, light
in the air, reflections and bounce light. Each area keeps its palette and mood. The lighting itself is
reimagined, so this is a large change on purpose. Press F10 in game to compare with the plain game.
When the game starts, a line at the top of the screen names the version for a few seconds.
The game's Ambient Occlusion setting must be on; see Compatibility.

What it changes
---------------
* Real sun: every area open to the sky has a sun with its own colour and strength, and world geometry
  casts real-time shadows out to 350 m. Interiors, caves and night areas have none.
* Living fires and torches: lights reach further and fall off like real flames, and the nearest 8 cast
  real-time shadows from the world, characters and objects.
* Ambient occlusion and contact shadows: a new AO pass in place of the game's, with sun contact shadows.
  Ambient light follows the open part of each surface, and light bounces inside creases on pale materials.
* Light in the air: sun shafts, a glow around lights and a faint haze in each area's fog colour.
* Reflections: screen-space reflections on smooth surfaces and water, replacing the probe where a ray hits.
* Bounce light: lit surfaces colour what faces them, using what is on screen.
* Foliage: light passes through leaves, so plants no longer read as flat dark shapes.
* Image: wider bloom from the whole scene, a smooth highlight roll-off, filmic contrast that leaves near
  black alone, a lift in very dark scenes and dithering. The game's TAA uses a sharper history with gentle
  contrast-adaptive sharpening.
* Native HDR: on an HDR display the game outputs real HDR, with highlights up to the display's peak and
  the UI at paper white, in place of Windows Auto HDR. On an SDR display nothing changes.
* Per-area lighting: all areas, DLC included, are retuned for this model.

Adjusting it
------------
dsrle\dsrle.ini holds every setting, and the game picks up changes while it runs. Add these at the bottom
of the file; a later value wins.

Closer to the original look:
  [lightreach]
  reach = 1.25
  [lighting]
  point_gain = 1.2
  sun_gain = 9
  fill_probe_sun = 0.8
  fill_hemi_sun = 0.8
  [volumetrics]
  sun = 0.00004
  light_density = 0.002
  [bloom]
  strength = 0.04

Stronger still:
  [lightreach]
  reach = 2.5
  [lighting]
  sun_gain = 18
  fill_probe_sun = 0.5
  fill_hemi_sun = 0.5
  [volumetrics]
  sun = 0.00016
  light_density = 0.006
  [bloom]
  strength = 0.12

Too dark or too bright (these are the defaults):
  [composite]
  exposure = 1          ; the whole image
  dark_exposure = 1.35  ; very dark scenes only; 1 = the game's own limit
  film_shadow = 1.12    ; shadow contrast; 1 = none
  black_floor = 0.03    ; the darkest pixels fade to black under this instead of clipping; 0 = hard clip

HDR, on an HDR display only. Restart the game after changing enabled:
  [hdr]
  enabled = 1        ; 0 = SDR, which hands the image back to Windows Auto HDR if you use it
  paper_white = 200  ; nits of SDR white. Raise it if the game looks dim beside your desktop, lower it if too bright
  peak_nits = 0      ; nits the highlights reach; 0 = what the display reports
  warm_hue = 0.5     ; how much of its usual colour fire keeps above paper white; 0 lets it go redder
In HDR, F10 compares with the plain game shown at paper white, not with Auto HDR's version of it.
HDR highlights need the game's anti-aliasing set to TAA or off. With FXAA the image is fine but stays at
paper white, and dsrle\dsrle.log says so.

Single features turn off with enabled = 0 under [pointshadow], [lightreach], [reflect], [ssgi],
[volumetrics], [bloom], [sunshadow] or [hdr].

Quality, for a slower GPU or one with little video memory:
  [engine]
  quality = 0   ; 0 = picked from the video memory: under 3 GB low, under 5 GB medium, otherwise high
                ; 1 = low, 2 = medium, 3 = high
High uses every setting as written. Medium halves the sun and torch shadow maps in each direction, runs
a lighter ambient occlusion, shorter reflection rays and bounce light at a quarter of the screen's size.
Low also drops reflections and bounce light, and shadows 4 lights in place of 8 with fewer objects.
The two lower levels are limits: a setting of yours that is already lighter stays. dsrle\dsrle.log
names the graphics card, its video memory and the level in use, and the line at the start names a level
below high.

The lines at the top of the screen (the version at the start, F10's off and on, and a warning when the
engine has to turn itself off) go away with:
  [engine]
  notices = 0

Per-map settings
----------------
Four maps have their own values for a few settings, and there they win over the same settings in
dsrle\dsrle.ini. Walking from one map into another eases between their values over 1.5 s. These
defaults are in dsrle\maps.ini, which every install replaces:
  Depths                      [lightreach] reach = 1: torches and fires keep the game's own reach
  Undead Burg and Parish      [lighting] point_gain = 1.0: dimmer torches and fires
  Darkroot Garden and Basin   [lightreach] reach = 1 and [lighting] point_gain = 1.3
  Anor Londo                  [volumetrics] haze_density = 0.000025, light_density = 0.0015 and
                              max_glow = 0.025: half the haze and glow in the air
In some parts of the Depths and the Burg the engine cannot yet tell the two maps apart, and there the
values in dsrle.ini stay. They also stay while the view shows two maps' worlds and neither has [maps]
c73_share (0.75) of what is drawn.
Only these four maps can have values of their own. Yours go in dsrle.ini, in a section named after the
map: m10_00 Depths, m10_01 Undead Burg and Parish, m12_00 Darkroot Garden and Basin or m15_01 Anor
Londo. Each key carries its section name, and a key there wins over maps.ini:
  [map.m10_01]
  lighting.point_gain = 1.2
To use the values in dsrle.ini everywhere:
  [maps]
  enabled = 0
If the DrawParam files are not this mod's, per-map values stay off and dsrle\dsrle.log says so.
With [maps] log = 1, dsrle\dsrle.log gets a line every 60 frames on what the detection sees. In it,
c73s= counts the drawn world's draws by sun direction, as group:draws for up to four groups, c73miss=
counts the draws with a direction the engine does not know, and why=mixed marks a frame where no
direction had c73_share of the draws.

Files
-----
This mod is code that runs inside the game process. d3d11.dll sits in the game folder, so the game
loads it in place of the Windows graphics DLL; it loads the real one from System32 and intercepts the
game's graphics calls. It is not signed, so an antivirus may flag it.

Added to the game folder:
  d3d11.dll
  dsrle\shaders.pak            replacement shaders, swapped in while the game runs
  dsrle\hlsl\*.hlsl            the engine's own passes, compiled while the game runs
  dsrle\shader_names.tsv       tells the engine which game shader is which
  dsrle\dsrle.ini              settings; the game picks up changes to this file while it runs
  dsrle\dsrle.ini.default      this version's default settings
  dsrle\maps.ini               per-map settings; every install replaces it
  dsrle\maps_detect.txt        tells the engine which map is which
The engine also writes its log, dsrle\dsrle.log, in that folder.
Replaced, after a backup:
  param\DrawParam\a10..a18_DrawParam.parambnd.dcx
The game's shader archives stay as they are on disk, and they must be the vanilla ones: replacements
are matched to the vanilla shaders by checksum. The mod does not change the executable, GameParam,
maps, events or saves on disk.

Install
-------
1. Close the game and extract the zip. Keep the extracted folder: its uninstall.bat removes the mod
   (a later version's uninstall.bat works too).
2. Double-click install.bat.
   The game folder is found through Steam. If it is somewhere unusual, run from a command prompt
   in this folder:   install.bat -GameDir "X:\...\DARK SOULS REMASTERED"
   Leave off any backslash at the end of that path. With one, a -Force after it is not seen, and the
   installer stops and says so.
   Every replaced file is backed up first, to <game>\DSRLE_backup.

The installer refuses to run while the game is running. It stops without changing anything if:
* The game folder has a d3d11.dll that is not this mod's. It names the file. install.bat -Force backs
  that file up and replaces it, and uninstall.bat puts it back. The other mod will not work meanwhile.
  If an earlier -Force had already backed up a different d3d11.dll, the newer one takes its place in
  the backup, and the installer says so.
* A DrawParam file belongs to another mod. install.bat -Force replaces it anyway, and the backup keeps
  the other mod's file. The exception is a mod that wrote over this mod's file after it was installed:
  -Force overwrites that file without a backup, and uninstall.bat brings back the vanilla one. The
  installer says so first.
* FRPG_FlverPBL_fpo_DX11 or FRPG_Filter_DX11 in the shader folder is not vanilla. Uninstall the mod
  that changed it, or use Steam's Verify integrity of game files, then install again.
  install.bat -Force installs anyway, but the shader replacements will not apply to that archive.
* The install record, DSRLE_backup\install.json, is damaged. See Uninstall below.

Updating from DSR Lighting Engine 1.0: just run install.bat. 1.0 replaced the shader archives, so the
installer puts the vanilla ones back from 1.0's backup and says so.
Reinstalling or updating replaces dsrle\dsrle.ini with the new version's if you never changed it. If
you did, your file is kept and the new version's is written beside it as dsrle.ini.default. Either way a
setting that is not in your file uses this version's default, which is built into d3d11.dll, so a
dsrle.ini can be just the lines you changed.
dsrle\maps.ini is always replaced, so keep your own per-map values in dsrle.ini.

Uninstall
---------
Close the game and double-click uninstall.bat. It works on an install by any earlier version, 1.0
included.
* d3d11.dll is removed if it is the one this mod installed. If the installer backed up another mod's
  d3d11.dll, that file is put back. A d3d11.dll that is not this mod's is not deleted. The one exception:
  after an install that was interrupted while copying d3d11.dll and never finished, whatever file has
  that name cannot be told from the half-copied one, and is removed.
* The dsrle folder is deleted with everything in it: your settings, logs and any traces.
* Every backed-up file is restored and its hash checked.
If another mod replaced one of the files after you installed, uninstall.bat leaves that file alone
and says so; uninstall.bat -Force puts the original back over it anyway.
If a backed-up file is missing or damaged, uninstall.bat names it:
* A DrawParam or shader file: use Steam > Dark Souls Remastered > Properties > Installed Files >
  Verify integrity of game files, then run uninstall.bat again. If it then says the file was changed,
  the lost backup held another mod's file: uninstall.bat -Force finishes, and you can reinstall that mod.
* Another mod's d3d11.dll: this mod's d3d11.dll is still removed. Reinstall the other mod for its file.
If the whole DSRLE_backup folder is gone, or its install record is damaged, the scripts stop. Use the
same Verify for the DrawParam and shader files. Steam leaves files it does not know about, so also
delete d3d11.dll, the dsrle folder and DSRLE_backup from the game folder by hand. If DSRLE_backup holds a
d3d11.dll, it is another mod's file that this mod replaced: copy it back to the game folder first.

Compatibility
-------------
* d3d11.dll name clash: PvP Overhaul (Dark Souls 1 Overhaul) and the OmniRoll standalone also ship as
  d3d11.dll in the game folder. Only one file can have that name and this mod does not chain to another,
  so they cannot be used together with it. The same goes for ReShade installed as d3d11.dll.
* Does NOT work with other mods that replace DrawParam or shader files, e.g. DSR Restored Lighting,
  Visual Overhaul, DSR Shader Overhaul or similar lighting mods. Uninstall those first.
* Texture packs (e.g. DSR-TPUP) and UI mods do not share any files with this mod.
* ReShade under another name (dxgi.dll) has not been tested with this version. If you try it, keep its
  colour effects subtle. The mod already sharpens, so skip ReShade sharpening.
* A game update that changes the shader archives makes the shader replacements stop applying until
  this mod is updated. Nothing breaks: those shaders simply stay vanilla. Native HDR also depends on a
  shader in the game's executable. If an update changes it the image looks washed out and dsrle\dsrle.log
  says so; set [hdr] enabled = 0 until this mod is updated.
* Ambient Occlusion must be on in the game's own settings: the engine's passes run where the game's
  ambient occlusion does. With it off the engine turns itself off after two seconds, says so at the top
  of the screen and in dsrle\dsrle.log, and the game looks as it does without the mod until the setting
  is turned back on.
* The anti-aliasing improvements apply to the game's TAA pass (tested with anti-aliasing on). With
  anti-aliasing off they do nothing.
* With native HDR on, overlays such as Steam's draw for an SDR screen and may look dim. If Windows HDR is
  turned off while the game runs, the image falls back to fit an SDR screen until the game restarts.

Online play
-----------
This mod is code running in the game process, and it also replaces DrawParam files on disk. It makes
no safety claim of any kind. What the game's anti-tamper and online checks look for is not publicly
documented, so nobody can tell you this is safe, and you could be flagged or banned.
Online use is at your own risk. Playing offline is recommended.
The mod does not stop the game from connecting: going offline is up to you, through the game's own
settings.

Performance
-----------
On an RTX 5090 at 3840x2160 the engine took about 4 ms of GPU time per frame on average across all
bonfires, staying at the game's 60 fps cap. By its buffer sizes it uses roughly 1 GB of video memory at
the high quality level and about half that at the two lower ones. At five places the whole frame took
1.3 ms of GPU time without the engine, 4.5 ms at high, 3.8 ms at medium and 2.7 ms at low.
No slower GPU has been tested, and the two lower levels were only checked on that card. See Quality
under Adjusting it.

Credits
-------
Original work. No PTDE files or values were used. Thanks to the Dark Souls modding community and its tools
(Soulstruct, Paramdex, DSR-Gadget).
