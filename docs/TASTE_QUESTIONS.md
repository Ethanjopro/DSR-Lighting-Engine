# Personal taste questions

Look differences from the plain game that are a matter of taste rather than bugs. They are tabled: nothing here blocks a commit or a release, and nothing changes until someone decides it should. When one is decided, move it to **Decided** with the answer.

Add a new entry when a look review finds something that could reasonably go either way. Brightness numbers are median 8-bit luma of the frame, from the DLL's back-buffer dumps. Before-and-after images are in `captures/f4/cmp/<view>.png` (git-ignored, left is the plain game).

## Open

### Main

| Question | What changed | Measured | What would change it |
|---|---|---|---|
| Should the Depths keep the blue-black void at the end of its hall? | Torches now light the hall to the far wall in amber, with a warm haze. It looks good, but the void is gone. | View `depths`: 21 plain, 55 now | `lightreach.reach` and `lighting.point_gain` in `dsrle.ini`, or `volumetrics.light_density` for the haze. These are global, so a change affects every area. |
| Should the Burg and Parish stay closer to their cool grey-green? | The sun turns the walls a warm amber-tan, and unlit wall keeps the old olive, so shadow edges can read as colour boundaries. | `burg`: 26 plain, 44 now. `parish`: 26 plain, 41 now | Sun colour and strength for the Burg and Parish rows in `spec/sun_rows.toml`, rebuilt with `tools/build_v4.py` |
| Should Darkroot Garden's mossy stone stay grey-green? | Sunlit stone turns a saturated orange against the area's blue-green palette. | `darkroot_garden`: 36 plain, 47 now. One stone face went from about (54,63,54) to (122,88,52) | Sun colour for the Darkroot rows in `spec/sun_rows.toml` |
| Should Anor Londo's interior walls be as bright as in the plain game? | In one view the walls are dimmer and the floor brighter. Nothing is crushed to black. | `anor_interior_t1`: 41 plain, 32 now | Not traced. Start with `lighting.fill_probe_sun` and `lighting.fill_hemi_sun`, then the Anor interior rows in `spec/sun_rows.toml`. |

### Smaller

| Question | What changed | What would change it |
|---|---|---|
| Is the Darkroot bonfire's warmth too strong? | The stones near the bonfire may read too orange. | `lighting.point_gain` |
| Is the haze on Gwynevere's stairs acceptable? | The stairs look slightly veiled. | `volumetrics.haze_density`, `volumetrics.light_density` |
| Should the Kiln entrance's shadows stay inkier? | The rock and background are lifted and a little grey in `kiln_entrance_t1`. Its near-black share went from 44% to 15%. | `composite.dark_exposure` |
| Are the Oolacile dungeon's side walls too hot? | Already harsh in the plain game, they are now paler. The brighter wall also makes a hard black shape behind the character stand out. That shape is an opening, not a shadow: the stone courses break around it, chains hang in front of it, and the plain game has it too. | `lighting.point_gain` |
| Is the tan patch on the wall at Sen's bridge acceptable? | A small speckled warm patch sits on a cool wall in `sens_bridge_t1`. It may be real sun or a light leak. | Check in game first |
| Should Sen's darkest foliage gaps be blacker? | Gaps between leaves are very dark teal rather than black, and the spears at the Sen's bonfire are also very dark teal. | `composite.black_floor` |
| What should HDR paper white be? | 200 nits is a starting value, not judged on a screen. It sets how bright everything below the highlights and the UI looks. | `hdr.paper_white` |
| How orange should fire stay in HDR? | With headroom for red and not for green, flames drift from orange toward red. 0.5 keeps half the SDR hue. The same blend leaves a broad, slightly pinker band around Anor's brightest clouds, so far seen only in stretched stills. Judge both on the screen. | `hdr.warm_hue` |
| How strong should the new ambient be? | Corners and the bases of walls get a little more light, and pale stone keeps more in creases. The change is deliberately subtle. | `lighting.bent_normal`, `lighting.ao_bounce` |
| Should the Blighttown tunnel stay fully green? | The left wall drifts toward khaki in `blighttown_swamp_t1`. | Point light colour is not a setting, so `lighting.point_gain` is the nearest control |
| Is the railing's shadow on the Parish wall too heavy? | In `parish`, the bonfire throws the railing onto the wall above it as a wide dark band and one post. It is a real shadow: it goes with `pointshadow.enabled = 0` and its far end meets the rail at the wall. | `[pointshadow]` settings. These are global. |
| Should Asylum's shaded upper wall keep more texture? | The wall in `asylum_1` goes flatter than in the plain game. Dark 32 px blocks (no channel above code 12, HUD areas left out) go from 0 to 29, the largest rise of the 20 views compared (`f2/off` against `release_b5`). | `composite.black_floor`, `lighting.fill_probe_sun`, `lighting.fill_hemi_sun` |
| How black should Ash Lake's near water be? | The plain game's near water is nearly black. It is now a faint grey-green: median 8,14,16 against 0,0,2 (`release_b5` against `h1/off`, x 2900-3800, y 1250-1500), and 1,5,8 with volumetrics off (`w3/novol`), so most of the lift is light in the air. | `[volumetrics]` settings. `volumetrics.sun` is the likely one, not measured alone. |

## Decided

Nothing yet.
