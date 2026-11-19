# Personal taste questions

Look differences from the plain game that are a matter of taste rather than bugs. They are tabled: nothing here blocks a commit or a release, and nothing changes until someone decides it should. When one is decided, move it to **Decided** with the answer.

Add a new entry when a look review finds something that could reasonably go either way, with the next free number. Brightness numbers are median 8-bit luma of the frame, from the DLL's back-buffer dumps. Before-and-after images are in `captures/f4/cmp/<view>.png` (git-ignored, left is the plain game).

The numbers match the owner's private taste picks page, which shows every option for each question side by side in game (shots in `captures/taste`, `taste_w`, `taste_w2`, `taste_hdr` and `taste_hdr2`, git-ignored). The option settings are `engine/variants/taste_*.ini`. Alternate sun tables for 2 and 3 were built into `dist/_taste/<name>` and ruled out as the cause; questions 1, 2 and 3 all turn on `lightreach.reach`.

## Open

| # | Question | What changed | What would change it |
|---|---|---|---|
| 18 | How long should the ease between two maps' values be? | The picks for 1, 2, 3, 5 and 6 now apply only in their maps. Walking from one map into another eases from one map's values to the next over 1.5 s; after a load they switch at once. Not yet judged in game. | `[maps] blend_seconds` in `dsrle.ini`, 1.5 now. |
| 19 | Should Darkroot Basin get the Garden's values? | The picks for 3 and 5 were judged in the Garden (`darkroot_garden`). The Garden and the Basin are one map to the game (m12_00), so the Basin's own geometry gets `lightreach.reach = 1` and `lighting.point_gain = 1.3` too. At the `darkroot_basin` bonfire, though, every drawn piece in both sessions was the Valley of Drakes' (m16_00), so that spot keeps the global values. Not yet judged in game. | Nothing today: the engine finds maps, not parts of one. Keeping the Basin on the global values would need a way to tell the two apart, such as the camera box considered for the Depths and the Burg. |
| 20 | Is Darkroot Garden right now that its shadows and haze use its own values? | Beta 8 fixed a fault: in Darkroot the sun shadow maps, the contact shadows and the light in the air were drawn with the sun direction and fog of whichever area had been on screen before, because the forest's pieces use shaders the engine did not read those values from. They now come from Darkroot's own draws. At the `darkroot_garden` bonfire the ruin is a little darker (mean 8-bit level of the frame 36.7 to 34.0), the lit edges on the rubble are gone, and the streak of light in the doorway is an even blue haze. At the `sanctuary_garden` bonfire the warm sun patches on the ground are a little weaker. The Basin bonfire and every other bonfire stayed within shot-to-shot noise. The picks for 3 and 5 were made on the old picture. Before and after: `captures/b8_review/darkroot_beta7_left_beta8_right.png`. Not yet judged in game, and not seen out in the forest. | `[map.m12_00]` values in `dsrle.ini` for the haze (`volumetrics.haze_density`, `volumetrics.sun`) or the torches (`lighting.point_gain`, 1.3 there now). The direction itself is the area's own and should stay. |
| 21 | Should the engine write on the screen at all, and for how long? | Beta 8 shows one line at the top of the screen: the version for 8 s when the game starts, "LIGHTING ENGINE OFF" or "ON" for 2 s at F10, and a 10 s warning if the game's Ambient Occlusion setting is off, which turns the engine off. The text is 42 pixels tall at 2160 lines. Not yet judged in game. | `[engine] notices = 0` hides them all. The times and the size are in `engine/src/engine.cpp` and `engine/src/osd.cpp`. |

The next new question is 22.

## Decided

All 17 were decided by the user on 2026-11-02 on the taste picks page. Ten keep the current look. The five that change need some settings to differ by map, because the picks for 1, 2, 3 and 5 pull the same global torch settings different ways and 6 should not change the haze elsewhere.

### Main

| # | Question | What changed | Measured | Decision |
|---|---|---|---|---|
| 1 | Should the Depths keep the blue-black void at the end of its hall? | Torches now light the hall to the far wall in amber, with a warm haze. It looks good, but the void is gone. | View `depths`: 21 plain, 55 now | **D, the game's own reach**: `lightreach.reach = 1` in the Depths only. On collisions whose rows the Depths shares with the Burg (30 of its 85, possibly around the Lower Burg door; not yet located), the global values stay. |
| 2 | Should the Burg and Parish stay closer to their cool grey-green? | The walls by the bonfires turn a warm amber-tan, and unlit wall keeps the old olive, so shadow edges can read as colour boundaries. It is the bonfire's light, not the sun: a neutral or cool sun table moves the Burg wall's blue-to-red ratio from 0.52 to 0.53, and `lightreach.reach = 1` takes it to 1.14 (plain 1.51). The vanilla DrawParam with the engine on changes it as little. | `burg`: 26 plain, 44 now. `parish`: 26 plain, 41 now | **E, dimmer at full reach**: `lighting.point_gain = 1` in the Burg and Parish only, reach stays 1.8. |
| 3 | Should Darkroot Garden's mossy stone stay grey-green? | The stone around the bonfire turns a saturated orange against the area's blue-green palette. As in the Burg it is the bonfire's reach: `lightreach.reach = 1` gives a blue-to-red ratio of 1.88 (plain 2.04, now 0.57), and greener or cooler sun tables leave it orange. | `darkroot_garden`: 36 plain, 47 now. One stone face went from about (54,63,54) to (122,88,52) | **D, the game's own reach**: `lightreach.reach = 1` in Darkroot (map m12_00, which holds both the Garden and the Basin; see 19). With 5, Darkroot also gets `point_gain = 1.3`. |
| 4 | Should Anor Londo's interior walls be as bright as in the plain game? | In one view the walls are dimmer and the floor brighter. Nothing is crushed to black. The room has no sun, so the sun fills do nothing here. Indoor fill at 1.6 brings the wall's median back to the plain game's (36.8 against 36.9, 30.5 now). AO and `lighting.enabled` hardly move it. | `anor_interior_t1`: 41 plain, 32 now | **B, keep the current look.** |

### Smaller

| # | Question | What changed | Decision |
|---|---|---|---|
| 5 | Is the Darkroot bonfire's warmth too strong? | The stones near the bonfire may read too orange. | **C, a little weaker**: `lighting.point_gain = 1.3` in Darkroot (m12_00, Garden and Basin; see 19). |
| 6 | Is the haze on Gwynevere's stairs acceptable? | The stairs look slightly veiled. | **E, half of both**: `volumetrics.haze_density = 0.000025`, `light_density = 0.0015`, `max_glow = 0.025` in Anor Londo only. |
| 7 | Should the Kiln entrance's shadows stay inkier? | The rock and background are lifted and a little grey in `kiln_entrance_t1`. Its near-black share went from 44% to 15%. | **B, keep.** |
| 8 | Are the Oolacile dungeon's side walls too hot? | Already harsh in the plain game, they are now paler. The black shape behind the character is an opening in the wall, not a shadow: the plain game has it too. | **B, keep**: point lights stay at 1.6 there. |
| 9 | Is the tan patch on the wall at Sen's bridge acceptable? | A small speckled warm patch sits on a cool wall at the right edge of `sens_bridge_t1`. It is sunlight through the gaps above, not a leak: the plain game has a similar patch, and it stays with `ssgi.enabled = 0`. | **B, keep.** |
| 10 | Should Sen's darkest foliage gaps be blacker? | Gaps between leaves are very dark teal rather than black, and the spears at the Sen's bonfire are also very dark teal. | **B, keep.** |
| 11 | What should HDR paper white be? | 200 nits was a starting value, not judged on a screen. It sets how bright everything below the highlights and the UI looks. | **C, keep 200 nits** (judged from the page's simulation). |
| 12 | How orange should fire stay in HDR? | SDR clips the brightest part of a flame to yellow; with HDR headroom it comes out a deeper orange, and `warm_hue` pulls it back toward the SDR colour (0.5 is halfway). The same pull acts on Anor's brightest sky, which SDR also clips to plain yellow. At 0 fire is deep orange and the sky gold, at 0.5 the sky leans slightly yellow-green, and at 1 fire is yellow and the sky lime (`captures/taste_hdr`, `captures/taste_hdr2`). | **C, keep 0.5** (judged from the page's simulation). |
| 13 | How strong should the new ambient be? | Corners and the bases of walls get a little more light, and pale stone keeps more in creases. The change is deliberately subtle. | **C, keep.** |
| 14 | Should the Blighttown tunnel stay fully green? | The left wall drifts toward khaki in `blighttown_swamp_t1`. | **B, keep**: point lights stay at 1.6 there. |
| 15 | Is the railing's shadow on the Parish wall too heavy? | In `parish`, the bonfire throws the railing onto the wall above it as a wide dark band and one post. It is a real shadow. | **B, keep.** |
| 16 | Should Asylum's shaded upper wall keep more texture? | The wall in `asylum_1` goes flatter than in the plain game. Dark 32 px blocks (no channel above code 12, HUD areas left out) go from 0 to 29, the largest rise of the 20 views compared (`f2/off` against `release_b5`). | **B, keep.** |
| 17 | How black should Ash Lake's near water be? | The plain game's near water is nearly black. It is now a faint grey-green: median 8,14,16 against 0,0,2 (`release_b5` against `h1/off`, x 2900-3800, y 1250-1500), and 1,5,8 with volumetrics off (`w3/novol`), so most of the lift is light in the air. | **B, keep.** |
