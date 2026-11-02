# Personal taste questions

Look differences from the plain game that are a matter of taste rather than bugs. They are tabled: nothing here blocks a commit or a release, and nothing changes until someone decides it should. When one is decided, move it to **Decided** with the answer.

Add a new entry when a look review finds something that could reasonably go either way, with the next free number. Brightness numbers are median 8-bit luma of the frame, from the DLL's back-buffer dumps. Before-and-after images are in `captures/f4/cmp/<view>.png` (git-ignored, left is the plain game).

The numbers match the owner's private taste picks page, which shows every option for each question side by side in game (shots in `captures/taste`, `taste_w`, `taste_w2`, `taste_hdr` and `taste_hdr2`, git-ignored). The option settings are `engine/variants/taste_*.ini`. Alternate sun tables for 2 and 3 were built into `dist/_taste/<name>` and ruled out as the cause; questions 1, 2 and 3 all turn on `lightreach.reach`.

## Open

### Main

| # | Question | What changed | Measured | What would change it |
|---|---|---|---|---|
| 1 | Should the Depths keep the blue-black void at the end of its hall? | Torches now light the hall to the far wall in amber, with a warm haze. It looks good, but the void is gone. | View `depths`: 21 plain, 55 now | `lightreach.reach` and `lighting.point_gain` in `dsrle.ini`, or `volumetrics.light_density` for the haze. These are global, so a change affects every area. |
| 2 | Should the Burg and Parish stay closer to their cool grey-green? | The walls by the bonfires turn a warm amber-tan, and unlit wall keeps the old olive, so shadow edges can read as colour boundaries. It is the bonfire's light, not the sun: a neutral or cool sun table moves the Burg wall's blue-to-red ratio from 0.52 to 0.53, and `lightreach.reach = 1` takes it to 1.14 (plain 1.51). The vanilla DrawParam with the engine on changes it as little. | `burg`: 26 plain, 44 now. `parish`: 26 plain, 41 now | `lightreach.reach`, then `lighting.point_gain`. Global, and shared with 1 and 3. |
| 3 | Should Darkroot Garden's mossy stone stay grey-green? | The stone around the bonfire turns a saturated orange against the area's blue-green palette. As in the Burg it is the bonfire's reach: `lightreach.reach = 1` gives a blue-to-red ratio of 1.88 (plain 2.04, now 0.57), and greener or cooler sun tables leave it orange. | `darkroot_garden`: 36 plain, 47 now. One stone face went from about (54,63,54) to (122,88,52) | `lightreach.reach`, then `lighting.point_gain`. Global, and shared with 1 and 2. |
| 4 | Should Anor Londo's interior walls be as bright as in the plain game? | In one view the walls are dimmer and the floor brighter. Nothing is crushed to black. The room has no sun, so the sun fills do nothing here. Indoor fill at 1.6 brings the wall's median back to the plain game's (36.8 against 36.9, 30.5 now). AO and `lighting.enabled` hardly move it. | `anor_interior_t1`: 41 plain, 32 now | `lighting.fill_probe_dark`, `lighting.fill_hemi_dark`. Global for unlit rooms and night. |

### Smaller

| # | Question | What changed | What would change it |
|---|---|---|---|
| 5 | Is the Darkroot bonfire's warmth too strong? | The stones near the bonfire may read too orange. | `lighting.point_gain` |
| 6 | Is the haze on Gwynevere's stairs acceptable? | The stairs look slightly veiled. | `volumetrics.haze_density`, `volumetrics.light_density` |
| 7 | Should the Kiln entrance's shadows stay inkier? | The rock and background are lifted and a little grey in `kiln_entrance_t1`. Its near-black share went from 44% to 15%. | `composite.dark_exposure` |
| 8 | Are the Oolacile dungeon's side walls too hot? | Already harsh in the plain game, they are now paler. The brighter wall also makes a hard black shape behind the character stand out. That shape is an opening, not a shadow: the stone courses break around it, chains hang in front of it, and the plain game has it too. | `lighting.point_gain` |
| 9 | Is the tan patch on the wall at Sen's bridge acceptable? | A small speckled warm patch sits on a cool wall at the right edge of `sens_bridge_t1`. It is sunlight through the gaps above, not a leak: the plain game has a similar patch, and it stays with `ssgi.enabled = 0`. | `lighting.sun_gain`, `[ssgi]` |
| 10 | Should Sen's darkest foliage gaps be blacker? | Gaps between leaves are very dark teal rather than black, and the spears at the Sen's bonfire are also very dark teal. | `composite.black_floor` |
| 11 | What should HDR paper white be? | 200 nits is a starting value, not judged on a screen. It sets how bright everything below the highlights and the UI looks. | `hdr.paper_white` |
| 12 | How orange should fire stay in HDR? | SDR clips the brightest part of a flame to yellow; with HDR headroom it comes out a deeper orange, and `warm_hue` pulls it back toward the SDR colour (0.5 is halfway). The same pull acts on Anor's brightest sky, which SDR also clips to plain yellow. At 0 fire is deep orange and the sky gold, at 0.5 the sky leans slightly yellow-green, and at 1 fire is yellow and the sky lime (`captures/taste_hdr`, `captures/taste_hdr2`). Judge both on the screen. | `hdr.warm_hue`. Keeping both would need the blend limited to fire-like pixels, a code change. |
| 13 | How strong should the new ambient be? | Corners and the bases of walls get a little more light, and pale stone keeps more in creases. The change is deliberately subtle. | `lighting.bent_normal`, `lighting.ao_bounce` |
| 14 | Should the Blighttown tunnel stay fully green? | The left wall drifts toward khaki in `blighttown_swamp_t1`. | Point light colour is not a setting, so `lighting.point_gain` is the nearest control |
| 15 | Is the railing's shadow on the Parish wall too heavy? | In `parish`, the bonfire throws the railing onto the wall above it as a wide dark band and one post. It is a real shadow: it goes with `pointshadow.enabled = 0` and its far end meets the rail at the wall. | `[pointshadow]` settings. These are global. |
| 16 | Should Asylum's shaded upper wall keep more texture? | The wall in `asylum_1` goes flatter than in the plain game. Dark 32 px blocks (no channel above code 12, HUD areas left out) go from 0 to 29, the largest rise of the 20 views compared (`f2/off` against `release_b5`). | `composite.black_floor`, `lighting.fill_probe_sun`, `lighting.fill_hemi_sun` |
| 17 | How black should Ash Lake's near water be? | The plain game's near water is nearly black. It is now a faint grey-green: median 8,14,16 against 0,0,2 (`release_b5` against `h1/off`, x 2900-3800, y 1250-1500), and 1,5,8 with volumetrics off (`w3/novol`), so most of the lift is light in the air. | `[volumetrics]` settings. `volumetrics.sun` is the likely one, not measured alone. |

## Decided

Nothing yet.
