# looks/: the global look model

W1 synthesis, 2026-10-30, written after the critic pass over `G1.md`..`G5.md`.

**What is authoritative**
1. `targets.json` holds every number that is checked, for all 41 bonfires plus the transition pairs. `tools/check_looks.py` reads it.
2. This README holds the rules every area follows: the lever model, the sun classes, fog, exposure, measurement, transitions, the validator facts, and the proposed `spec/global.toml` changes.
3. `G1.md`..`G5.md` hold the per-area reasoning, row tables and lever recipes. Each one opens with a correction block that points back here.

The section 10 proposals are applied in `spec/global.toml` and `tools/validate.py`.

**The target, in one sentence.** Bring back the per-area mood of PTDE (stronger casts, deeper shade, less flat fill, characters that sit in the scene) using DSR's own renderer plus the real-time sun, with no PTDE assets or values, and without re-grading FromSoftware's art direction.

---

## 1. What "PTDE moods, DSR fidelity" means in numbers

These are the measurable version of the mood. All deltas are against the baseline (section 6).

| Mood dimension | Global rule | Numbers |
|---|---|---|
| Darkness | Most sources remember PTDE darker. Darken modestly, and only where a source or the art intent asks for it. | Darkened views: EV Δ between −0.05 and −0.50 at most. Protected views hold within about ±0.15–0.25. No bonfire view goes below −0.50. |
| The one inversion | In Blighttown, DSR is the version that crushes to black. Lift the floor there without brightening the mood. | crush must fall by 1–2 points or more (aim 1–6); exposure at most +0.30 |
| Form and separation | Get it from less flat fill, never from contrast. Cut `colA_d` more than `colA_u`. | spread `log2((p90+1)/(p10+1))` +0.10..+0.60 where asked (Darkroot, Township). Contrast moves only in Blighttown, and only down. |
| Characters | The strongest cross-source finding: DSR characters carry a sourceless glow. | char-crop EV −0.10..−0.45 (Archives, Anor interior). d_char_env −0.05..−0.50 (G3). The sun gives modelling back in daylight. |
| Colour cast | Casts are small, signed, and only where sourced. | "Hold" means \|Δa\|, \|Δb\| ≤ 0.004–0.008. A sourced shift is at most 0.014 (Painted b). Colour resting only on fromsoftserve is halved. Anor Londo warmth is held. |
| Haze | PTDE's "haze" was bloom and DoF glow, not fog density. | Fog density holds within ±10%, except the Burg (thinner, ×0.8–0.9). Darkroot's milky scatter comes down (Scatter 0 sunA ×0.75–0.90). |
| Saturation | No re-grade. | 0.95–1.10× typical. Never add saturation where it is already the group maximum (sanctuary_garden 0.73). |
| Highlights | Never blow out more. Pull back only where asked. | Clip may rise at most +0.5..+1.0 points. anor_entrance must *fall* by at least 1 point. crystal_cave may fall by up to 10. |
| Shadows | Readable dark, never more crush in views that are already crushed. | Crush may rise +0.3..+3 points depending on the view. Dark views (ev < −5.5) get p50 floors. No ToneMap darkening in crushed views. |

**Direction of each sourced colour move**

| Area | Move | Limits |
|---|---|---|
| Burg, Parish, Sunlight Altar | less yellow | b −0.002..−0.008; Parish down to −0.012 |
| Firelink | hold or slightly deepen the green; less red/yellow | a ≤ +0.002, b ≤ +0.001 |
| Painted World | cooler blue-green | b −0.004..−0.014 |
| Darkroot Garden | greener | a −0.002..−0.009 |
| Darkroot Basin | less warm, as a side effect of the shared rows | |
| Everywhere else | held | |

## 2. Levers: which lever makes which mood

Sensitivities come from `data/sweep_sensitivity.json`, measured at parish, firelink, anor_interior and archives_balcony. They are per doubling unless stated.

| Want | Lever | What it does / limits | Don't |
|---|---|---|---|
| Darker, with more form | LightBank `colA_d` (cut more), `colA_u` (cut less), `envDif_colA` | ambient ×2 = +0.58 (firelink) to +2.05 EV (parish). envDif ×2 = +0.51..+1.18 EV. c7/c8 scale as colA^2.2, so ×0.85 gives ×0.70 fill. | darken through the exposure curve |
| Recover exposure after a fill cut | ToneMap `grayKeyValue` up to ×1.15 | ×1.5 = +0.46..+1.03 EV and up to +8.7 clip (balcony) | push it in clip-heavy views |
| Cooler cast | ToneCorrect `brightnessB` up and `brightnessR` down together, G held | brightnessB ×1.03 ≈ b −0.002..−0.004 (a +0.001). brightnessR ×0.97 ≈ a −0.002. | B alone, which drifts purple in the shade |
| Cast in the distance / sky | FogBank `colR/G/B` (abs 40); Scatter `sunR/G/B` | distance and sky only | — |
| Cast in the fill | LightBank `colR/G/B_u/d` (abs 40) | mirror s_LightBank | — |
| Warmer light on characters | sun tint `colR/G/B_s` (abs 40, so the warmest is about 255/215/215) | follows the row's baked key (section 4) | a warm grade on the whole frame |
| Greener | ToneCorrect `brightnessR` down; Scatter sunG up, sunR/B down | Darkroot only | — |
| Less milky haze | Scatter `sunA` down, `blendCoef` down | scatter ×2 = +0.27..+0.74 EV, sat −0.16 (parish), clip +2.5 (firelink) | fog density as a "PTDE haze" lever |
| Glow | ToneMap `bloomMul` ×1.0–1.4, `lightShaftPower` ≤ 0.03 | bloom ×2 ≈ 0 EV, clip +0.5 (firelink) | bloom or shafts where clip must fall |
| Highlights back | ToneCorrect `brightness` down (rows > 1.0); `grayKey` down within the floor | Anor entrance: brightness ×0.88–0.93 ≈ −0.15..−0.25 EV | `maxAdapredLum`: ×0.5 measured 0.00..+0.10 EV, a near-null lever |
| Character de-glow | char-row `envDif_colA`, `envSpc_colA` (not lockstepped); `colA_u/d` (needs P3/P4) | envSpc never below ×0.6: the vanilla EnvSpec per-draw gain can turn metal black | — |
| Longer shadows and sun reach | ShadowBank `endDist` ≤ 2×, scaling `fadeBeginDist`/`fadeDist` with it | ×2 ≈ −0.12 EV (firelink). Needs a shimmer check. | ShadowBank angles (immutable) |
| Contrast | ToneCorrect `contrastRGB`, \|Δ\| ≤ 0.03 | +0.10 = −0.5..−2.9 EV, crush +21 (parish) | anything but Blighttown's −0.02..−0.03 on all channels |

**Never touched:**
- directional `colA_0..2` (ignored by the 421b shaders; band ±20%);
- light and shadow rotations;
- cubemap IDs;
- ToneCorrect `hue` (a global rotation);
- fog distances;
- PointLightBank (the player lantern is out of scope for v1).

## 3. Exposure and adaptation philosophy

**Mood darkness comes from light, not from the exposure curve.** Exposure is roughly `grayKey / clamp(lum, minAdaptedLum, maxAdapredLum)`. If the fill drops, auto-exposure keeps working and the scene keeps its gradient. If the curve is lowered instead, dark scenes go flat and crush. ToneMap therefore has two jobs only: recovering exposure after a fill cut (grayKey up), and recovering highlights (grayKey down, within the floor).

**Readability floor** (`spec/global.toml`). `log2(cap_vanilla/cap_new) × 2.8 ≤ 1 EV`, where cap = grayKey/minAdaptedLum. So the new cap must be ≥ 0.78× vanilla: minAdaptedLum at most ×1.28 alone, or grayKey at least ×0.78 alone. The sweep shows why the gain is there: minAdaptedLum ×3 measured −1.0..−4.4 EV.

**Shared ToneMap rows move whole areas.**

| Row | Shared by |
|---|---|
| a10 row 0 | all three m10 maps |
| a12 row 0 | Darkroot, Royal Wood, Battle of Stoicism |
| a12 rows 11/17/20 | the DLC; 17 and 20 include the Chasm |
| a13 row 0 | all of m13 |
| a15 row 0 | Sen's and Anor Londo |
| a17 row 0 | Archives and Crystal Cave |
| a18 row 0 | Kiln and Asylum |

On these rows minAdaptedLum is a last resort only.

**Locked exposure.** Darkroot's ToneMap 0 runs min 0.11 against max 0.13, so every fill, scatter and grayKey edit lands about 1:1. Step it in ×0.05 increments and change one lever per round.

**Crushed views** (new_londo_ingward, kiln_gwyn, asylum_2, oolacile_dungeon, the Tomb, Demon Ruins 3, Izalith 2) get no ToneMap darkening at all. minAdaptedLum may only go down (×0.9–1.0) to lift.

## 4. Sun model

The sun is the real-time key on draws without a lightmap: characters, objects, props.
- **Carrier.** `c6 = (col_s/255 × colA_s/100)^2.2` per channel, from LightBank (`data/probe_constants.json`).
- **Radiance.** 2 × c6, with the largest channel capped at 0.6, which is reached at colA_s 58.
- **Direction.** ShadowBank lightDegRot (c73), the same as the shadow maps.
- **Fade.** It fades out with the shadow range.

### 4.1 Classes

| class | radiance (white) | LightBank colA_s **today** | s_LightBank today | colA_s **after P2** | use |
|---|---|---|---|---|---|
| off | 0 | 0 | 0 | 0 | interiors, caves, night, every row not named for a sun |
| low | 0.031–0.095 | 15–25 | 30–50 (2×, ±10%) | ~25 | overcast or veiled-sun exteriors |
| mid | 0.141–0.174 today; 0.266 at 40 | 30–33 | 60 (band max) | ~40 | partly lit exteriors |
| high | 0.435–0.537 | 50–55 | **left at vanilla 100** | ~55 | clear low sun: anor_entrance only |
| spec-only | none, once shader gate S1 exists | > 60 in vanilla, untouched | vanilla | same | water and lava specular rows |

**Why "today" differs from the intended low ~25, mid ~40, high ~55.** Most live rows are 50 in LightBank and 100 in s_LightBank: 103 slot-0 rows with map-piece, object, character or collision users. Two rules then interact:
- `validate.py` strips the `s_` prefix, so the `colA_s max = 60` band also binds s_LightBank.
- Same-ID lockstep makes s_ follow the LightBank ratio within ±10%.

The result:
- **0–33 is reachable.** s_ goes to 2× the value, which stays ≤ 60.
- **46–55 is reachable.** s_ stays untouched at 100, because ratio 1.0 is within 10% of ×0.92..×1.10.
- **34–45 and 56–60 fail.** That dead zone is why mid tops out at 33.
- Painted a11 row 10 (s_ 140) is the exception: it reaches 0–23 and 46–55 only.

Proposal P2 removes the dead zone.

### 4.2 Class per view

Classes are assigned by sky condition, not per author:
- **low:** burg, parish_altar, painted_entrance, painted, darkroot_garden (trees row 4 only), sanctuary_garden, oolacile_township, ash_lake, sens, asylum_1.
- **mid:** firelink, sens_bridge, oolacile_sanctuary, kiln_entrance.
- **high:** anor_entrance only.
- **off:** everything else. darkroot_basin is "off" by view only: it shares every row with the garden, so verify that the character crop gains ≤ +0.05 EV between a sun build and sun off.

### 4.3 Sun rules

1. **Tint follows the row's baked key** (`colR/G/B_0`), clamped to abs 40 of the vanilla `col_s` (white):
   - Burg row 10 has key 255/205/175, so the sun is 255/215–235/215–225.
   - Firelink's cool key gives 240–255/250–255/255.
   - Anor row 30 is warm.
   - Oolacile Township's dark-red key (155/55/55) cannot be followed within the band, so it gets neutral-warm 255/225–235/220–230. Never cool there.
2. **Reach equals the shadow range.** The sun fades with ShadowBank `endDist`/fade, so at Firelink, Parish, the Anor interior and Kiln/Asylum row 0 it is a 10 m bubble. Firelink's mid class is paired with endDist 10 → 18–20, with the fade fields scaled.
3. **Direction check.** Compare the shadow-light direction with the scatter sun direction:
   - They agree at Burg (2°), Painted (17°), Firelink (21°), Darkroot (21°), Kiln/Asylum (4.5°) and Ash Lake (0°).
   - They disagree at Parish (35°), Anor entrance (35.5°), Anor row 11 (37.5°) and Sen's (27°).
   - Where the sun disc is in frame (anor_entrance, kiln_entrance): if the character rim light visibly disagrees with the disc, fall back to tint-only at the vanilla 50.
4. **Density leak.** A ShadowBank densityRatio below 100 lets about (1 − density) of the sun through full shadow:
   - Darkroot 40, Depths 60, Burg 70, Parish/Ash Lake 80, Painted/Anor 85.
   - "Low as seen" is unreliable there unless the build uses `DSRLE_SUN_SHADOW_RENORM`.
5. **Snow is excluded** from the sun (`data/sun_ab.json`: +1.7 clip points on Painted World snow).
6. **The bonfire camera barely sees the sun.** It looks at the character's back, and the sun adds only +0.05..+0.2 EV there. Character targets that rely on the sun refilling a fill cut need sun-facing captures for the mid and high views (harness request H1).
7. **Every row needs a decision.** Every live LightBank row with 0 < vanilla colA_s ≤ 60 (138 rows, both a15 slots included) needs an explicit sun decision in its area spec: keep, set, or 0 (P9). Left alone, it renders vanilla 50 as a 0.435 sun.
8. **Spec-only rows** are vanilla colA_s > 60. They are water or lava settings, not sun intent:

   | Area | Rows (vanilla colA_s) |
   |---|---|
   | a10 | 2 (100), 3 (200), 4 (300) |
   | a12 | 0 (300), 1 (300), 4 (70), 36 (80) |
   | a13 | 15 (100) |
   | a14 | 0, 1, 2, 4–8, 12, 15–17, 21–23, 64 (all 100) |
   | a16 | 0 (200) |
   | a18 | 17, 18 (100) |

   **Rule:** leave them vanilla, and have the shader skip the sun where the carrier is above the cap (S1). Water and lava keep their specular, and the caves get no sun with no edit at all. **Interim, until S1 exists:** no build ships with the sun shader on. A row may be zeroed only after a water/lava A/B shows its sheen survives.
9. **0 → nonzero needs a declaration.** Turning the sun on from vanilla 0 (Ash Lake 18, Oolacile char 14) passes the same-ID check today only through a comparison quirk: `inf > inf` is false. The same edit fails on declared character rows. Treat it as needing an explicit declaration (P3).

## 5. Fog and scatter philosophy

- **Fog density holds** (degRotW within ±10%) everywhere except the Burg. There, fog becomes thinner (60 → 48–54) and cooler-blue, never purple.
- **No fog lift in dark places.** A grey veil kills the void in the Tomb, the Depths corridor, the Chasm and the Abyss.
- **Fog colour moves only toward a sourced cast:**
  - Burg: cooler blue.
  - Firelink: slightly cooler-green.
  - Painted World: cooler, after colour sign-off.
  - Oolacile Township: toward blue-black, at most −6 per channel.
- **Fog rows with degRotW 0 cannot move** under rel bands: a10 row 4, a12 row 0, a17 row 3.
- **Fog distances** (`fogBeginZ`, `fogEndZ`, `degRotZ`) are frozen by policy.
- **Scatter is the sky and sun haze.**
  - Darkroot: sunA ×0.75–0.90, with a greener sun colour.
  - Anor entrance: the warming (sunG/sunB) is deferred to a signed-off round 2.
  - Everywhere else it stays unchanged.
  - The cyan sky and god rays at Firelink stay vanilla.

## 6. Measurement rules (how `targets.json` is checked)

**Baseline**
- Guardrails are measured against the **re-baseline**: frozen shaders + vanilla DrawParam (PLAN's "freeze shaders + re-baseline"), suggested at `captures/rebaseline/vanilla`.
- The Phase-0 captures (`captures/baseline_vanilla/vanilla`) are only for the user-facing before/after, and are the default until the re-baseline exists.
- Against the re-baseline, a "sun off" view can read slightly negative on the character crop, because vanilla colA_s leaks a sun there (anor_interior +0.25 EV in sun1). The char ranges allow for that.

**Noise**
- EV: ±0.10 outdoors; 0.03 (parish) and 0.08 (firelink) on repeats; bonfire flicker moves the character crop by up to 0.18 EV.
- Oklab: 0.0015. Saturation: 0.013.
- Judge views with a bonfire in or near frame on the mean of 2 captures (`--repeat`).
- Clip repeat noise under auto-exposure has not been measured. Three checks have no allowance at `clip_max_delta = 0`: crystal_cave, archives_elevator and oolacile_dungeon. They may need about 0.1 point of slack after the first tuned capture.

**Exposure metric**
- Normally `ev` (log2 of the geometric-mean luminance).
- Where crush is above 30% (blighttown_bridge, demon_ruins_3, izalith_2, asylum_2, oolacile_dungeon), the geometric mean over-reacts to lifted blacks: a pixel going from level 0 to 2 moves its log term by more than 1 EV. There the exposure check is `log2(lin(p50_new)/lin(p50_base))` against the same `ev_delta` range, and the raw ev is reported as advisory.
- The Tomb uses p99 plus the character crop, with ev, sat and Oklab not enforced (crush 84–93%).

**Floors**
- Explicit p90/p50 floors from G1 and G4.
- G5: p90 ≥ vanilla − 10.
- Derived p50 floors for the other dark views (ev < −5.5): the level at (ev lower bound − 0.15 EV).

**Crops** (`--crops`)
- CHAR = x 1650–2210, y 880–1720. This stops above the rest-at-bonfire prompt. The sun_ab box (560×1180) includes the prompt, so its numbers are slightly diluted.
- ENV = the frame minus the HUD masks, the prompt box and the x 1600–2300 column.
- Both use the mean linear Y.
- Not valid where a flame sits in the box (blighttown_bridge), and noisy where the bonfire dominates (firelink, parish).

**Sign-off gates.** The first capture of these moves goes to the user even when it passes:
- colour moves that rest on unverified single sources: Burg/Parish cooling, Painted blue-green, Darkroot green, Firelink green;
- the Blighttown lift;
- any optional experiment (e.g. the lava underlight, which is not in v1).

## 7. Transitions

**Rule.** At a walkable boundary the exposure jump may grow by at most 0.3 EV over vanilla: |ΔA − ΔB| ≤ 0.3 on each view's exposure metric. Within the Burg/Parish family, the b shifts must also share one sign (noise 0.0015).

| Pair | Constraint (ΔA − ΔB) | Why |
|---|---|---|
| burg / parish / parish_altar | ±0.3 each | shared rows 10/11/15 |
| burg − firelink | ±0.3 | aqueduct |
| depths − burg | ≥ −0.3 | Lower Burg → Depths |
| depths_dragon − blighttown_bridge | ±0.3 | Depths → Blighttown (up to 0.85 apart if unpaired) |
| parish − darkroot_garden | ±0.3 | already harsh in vanilla |
| darkroot_garden − darkroot_basin | ±0.3 | shared rows |
| blighttown_swamp − quelaag | ±0.3 | up to 0.55 apart if unpaired |
| quelaag − demon_ruins_1 | ±0.3 | |
| demon_ruins_3 − izalith_1 | ±0.3 | |
| sens_bridge − anor_entrance | ±0.3 | shared ToneMap/Shadow 0 |
| anor_interior − anor_entrance | ≥ −0.3 | the outdoor-to-indoor drop (vanilla 1.59 EV) may grow by at most 0.3 |
| archives_balcony − crystal_cave | ±0.3 | a17 ToneMap 0 serves place 1701 |
| painted_entrance − anor_interior | ±0.3, warn | load transition |
| great_hollow − ash_lake | ±0.3, warn | |

**Views with no baseline.** Capture these before any shared-row edit ships (`targets.json` `_pending`):
- Royal Wood and Battle of Stoicism (a12 row 0);
- Chasm of the Abyss (a12 ToneMap 17/20; must stay pitch black);
- Valley of Drakes (Basin and New Londo transitions);
- Lower Burg/Capra (a10 row 36);
- the Firelink Abyss entrance (a10 rows 24/29);
- dark Anor Londo (`<slug>_dark`).

Setting colA_s to 0 on slot-1 rows reproduces vanilla exactly, so that one edit can ship before the dark baseline.

## 8. Shared rows: cross-area traps (verified in `data/live_rows.json`)

| Rows | Users | Consequence |
|---|---|---|
| a10 row 0, all banks | m10_00/01/02; LightBank 0 has 294 characters | probe before touching |
| a10 Fog 11, Scatter 11, LightBank 16 | m10_01 *and* m10_02: Scatter 11 has 18 pieces and 365 objects; Light 16 has 363 objects | Parish edits move Firelink. ToneCorrect 11 is m10_01 only. |
| a12 row 0, all banks | Darkroot, plus m12_01 places 1212/1215; 418 characters | Darkroot edits bleed into Royal Wood and Battle of Stoicism |
| a12 ShadowBank 20 | places 1211–1216 | not Township-local: no endDist change for one view |
| a12 ToneMap 17 / 20 | 1213+1214 / 1211–1216 | the Chasm stays black: no lift, no darkening |
| a13 row 0 | all of m13 (949 Light users) | ToneMap/ToneCorrect 0 stay vanilla; LightBank 0 only colA_d and sun |
| a13 LightBank 18 | m13_01 and m13_02 (place 1321) | the Ash Lake sun is capped at 20–25; check for Tomb users |
| a14 row 0 | both m14 maps; 383 characters plus the swamp | spec-only under S1 |
| a15 row 0 (all banks); Fog/Scatter 10 | Sen's and Anor Londo | cross-check after every edit |
| a15 slot 1 rows equal to slot 0 | dark Anor Londo | mirrored by validate.py |
| a16 row 0 | 108 characters, 21 objects, the flood water | spec-only under S1 |
| a17 ToneMap 0 | Archives and Crystal Cave | pair check |
| a18 row 0 (all banks) | Kiln and Asylum | edits must pass both areas |

## 9. Validator facts the bibles rely on (checked against `tools/validate.py`, 2026-10-30)

- **colA_s reachability.** See section 4.1. The census of live rows (LightBank, s_LightBank):

  | (LightBank, s_LightBank) | rows |
  |---|---|
  | 50, 100 | 103 |
  | 0, 100 | 77 |
  | 100, 100 | 18 |
  | 50, 50 | 6 |
  | 20, 100 | 6 |
  | everything else | 1–3 each |

  Every live row can be set to 0.
- **Zero or tiny s_ fields quantise the LightBank field.** An unchanged s_ field reads as ×1.0, so the LightBank field can only move within ±10%, plus whatever ratios the small integer can express:

  | Field | Reachable values |
  |---|---|
  | a12 row 10 colA_u/d (s_ 0) | 46–49 / 51–55; a cut is at most ×0.92 |
  | a12 row 0 colA_u (s_ 5) | 36 (×0.9) unreachable |
  | a17 row 18 colA_u | 55–59 |
  | a17 row 24 colA_u / colA_d | 137–149 / 91–99 |
  | a18 row 36 colA_u | 46–49 |
  | a13 row 0 colA_u | 37–39 |
  | a15 row 30 colA_u | 19, 21–22 |
  | a15 row 21 colA_u | 10–11, 14, 16, 19–21 |
  | a10 row 3 colA_d | 16 or 17; 18 (×0.9) unreachable |

  Route these through `envDif_colA`/`envSpc_colA`, or adopt P3.
- **0 → nonzero.** It passes same-ID lockstep by accident (`inf > inf` is false) and fails declared char-row lockstep. See P3.
- **No-op edits are a warning, not an error.** G4's "rejects" overstated this.
- **No band at all, so only paramdef limits apply** (P1, P5–P8):
  - all of LightScatteringBank except the sun angles;
  - FogBank `fogBeginZ`, `fogEndZ`, `degRotZ`;
  - ShadowBank `densityRatio`, `colR/G/B`, `beginDist`, `calibulateFar`, `fadeBeginDist`, `fadeDist`, `persedDepthOffset`, `gradFactor`, `shadowVolumeDepth`;
  - ToneMap `bloomBegin`, `bloomBeginFar`, `bloomNearDist`, `bloomFarDist`, `adaptSpeed`, `lightShaftBegin`, `lightShaftAttenRate`, `inverseToneMapMul`;
  - ToneCorrect `hue`, `sfxMultiplier`;
  - LightBank `envDif_colR/G/B`, `envSpc_colR/G/B`;
  - PointLightBank and LensFlareExBank (not immutable either).

## 10. Proposed changes to `spec/global.toml`

**Status (2026-10-30): applied** in `spec/global.toml`, `tools/build.py` and `tools/validate.py`. P2 is `exact_bank = true` on the colA_s band. P3(c) is `from_zero = true` on an edit. P4 is `char_rel` on a `[[lockstep]]` declaration. P9 accepts a colA_s edit or a `[[sun_keep]]` entry. ShadowBank has no `gradFactor` field, so P5 omits it. The one-bank s_LightBank capture that P2 asks for is still pending.

| # | Change | Reason |
|---|---|---|
| P1 | **LightScatteringBank band:** `sunA` rel [0.7, 1.3]; `sunR/G/B` abs 40; `blendCoef` abs 10 (paramdef max 100). `distanceMul`, `lsHGg`, `lsBetaRay`, `lsBetaMie`, `reflectanceR/G/B/A` immutable until swept. | Unbanded today. G2 and G5 used different self-imposed limits (0.7–1.3 vs 0.7–1.4, blend ±10 vs ±15). Scatter ×2 already costs +2.5 clip at Firelink. |
| P2 | **colA_s band on LightBank only.** s_LightBank colA_s keeps same-ID lockstep but gets no `max`. This needs a band-scope key, e.g. `banks = ["LightBank"]`, because validate.py strips `s_`. | c6 is read from LightBank: vanilla c6 = 0.2176 = 0.5^2.2 where LightBank is 50 and s_ is 100 (`data/probe_constants.json`). Soulstruct's PTDE DrawParam module labels s_LightBank as an unused debug baked-light bank. The band on s_ creates the 34–45 / 56–60 dead zone. Confirm first with one capture that changes only s_LightBank colA_s at one bonfire. |
| P3 | **Lockstep repair:** (a) skip s_ fields that are 0 in vanilla; (b) accept a tiny s_ field when it equals `round(s_old × r)` (quantisation-aware); (c) a LightBank field going 0 → nonzero must be declared in the area spec, and is otherwise an error. | Today (a) and (b) freeze or quantise the Archives and Darkroot character de-glow and a13 row 0, and (c) passes only through an `inf > inf` comparison. |
| P4 | **Character-row policy:** keep declared char ↔ map pairs, but allow a per-declaration asymmetric tolerance `char_rel = [0.65, 1.10]` relative to the map ratio. colA_s is excluded from char lockstep, because the sun is per-row intent. | The strongest PTDE finding is over-lit characters (Depths 9, Archives 11/24, Anor 31/33, Sen's 10, Firelink 25/27). The critic's alternative (leave char rows undeclared, guarded only by the 0.6–1.4 band) also works. Bounded declarations keep "characters may be dimmed relative to the scene, never boosted" explicit. |
| P5 | **ShadowBank:** `fadeBeginDist` and `fadeDist` must scale with `endDist` (same ratio ±10%). `densityRatio`, `beginDist`, `calibulateFar`, `persedDepthOffset`, `gradFactor`, `shadowVolumeDepth`, `colR/G/B` immutable until swept. | The sun fades with the shadow fade. Density sets the sun leak. Today all of these are unbanded. |
| P6 | **FogBank:** `fogBeginZ`, `fogEndZ`, `degRotZ` immutable. | Every bible holds fog distances. |
| P7 | **ToneMap:** `adaptSpeed`, `bloomBegin`, `bloomBeginFar`, `bloomNearDist`, `bloomFarDist`, `lightShaftBegin`, `lightShaftAttenRate`, `inverseToneMapMul` immutable. | Unswept. `inverseToneMapMul` couples to the Filter shader. |
| P8 | **ToneCorrect** `hue` and `sfxMultiplier` immutable. **LightBank** `envDif_colR/G/B` and `envSpc_colR/G/B` abs 40. **PointLightBank** and **LensFlareExBank** immutable. | hue is a global rotation. The cubemap tints are unbanded. The lantern is out of scope for v1. |
| P9 | **Sun decision rule:** every live LightBank row with 0 < vanilla colA_s ≤ 60 needs an explicit sun entry in its area spec (validator WARN when missing). Rows > 60 stay vanilla unless the spec says otherwise. | 138 rows would otherwise render a vanilla 0.435 sun unintentionally. |
| P10 | **`[transitions] max_extra_jump_ev = 0.3`** and **`[guardrails]`** mirroring PLAN (ev ±0.3, clip +1, crush +3) as defaults that `targets.json` overrides per view. | One place for the numbers that `check_looks.py` and the bibles share. |

## 11. Requests outside DrawParam

- **S1 (shader): spec-only gate.** Skip the DIRECT sun where the largest c6 channel is above about 0.33, i.e. vanilla colA_s above about 60. This keeps the water and lava specular, removes 13 of G3's 15 a14 zero-edits (only rows 14 and 28 still need one), and settles the Darkroot (300) and New Londo (200) carrier question with no edits. It is a prerequisite for shipping the sun.
- **S2 (shader): evaluate `DSRLE_SUN_SHADOW_RENORM`** on rows with densityRatio < 100 (Darkroot 40).
- **H1 (harness): sun-facing captures** for the mid and high views (anor_entrance, firelink, sens_bridge, kiln_entrance, oolacile_sanctuary). The bonfire camera sees the character's back.
- **H2 (harness): a CHAR/ENV crop in StatsJson** (`<slug>.crops.json`), so `--crops` does not need the PNGs.
- **H3 (harness):** two captures per bonfire view, and the guard views listed in section 7.
- **Probes:**
  - one-row tint/probe captures to confirm the bonfire → row tables (every bible lists rows as "probable");
  - the one-bank s_LightBank capture for P2;
  - the live ToneMap row at blighttown_bridge (rows 1/2/3).

## 12. How the critic's review was applied

**Accepted**
- B2, zero/tiny s_ fields: verified, with the exact reachable sets in section 9.
- B3: spec decisions are needed for colA_s, zero s_ fields, character rows and a Scatter band (P1–P4).
- B4: the spec-only carrier rule plus the sun decision rule (S1, P9).
- B5: the re-baseline is the guardrail baseline.
- Section 2: classes by sky condition (parish_altar and firelink come down), the tint-from-key rule, reach, the direction check (angles re-computed and confirmed), the density leak, and sun-facing captures.
- Section 3: every missing pair.
- Section 4: evidence marked by status; fromsoftserve-only colour halved (Firelink, Darkroot, Painted tightened to −0.004..−0.014); colour sign-off; the darker-vs-Blighttown tension stated.
- Section 5:
  - maxAdapredLum is near-null (sweep-confirmed);
  - the Anor clip lever is ToneCorrect brightness, with a quantified clip fall;
  - no bloom or shaft increase at Anor;
  - Darkroot is capped at three levers;
  - Painted b is tightened;
  - dark views get p10/p50 floors.
- Section 6, per bonfire: all items, with the modifications below.

**Modified**
- **B1 reachability.** The dead zone is 34–45 and 56–60, not 34–49 and 51–58. LightBank 46–55 passes with s_ left at 100, so "high" is 50–55, not only "50 untouched". anor_entrance may therefore use 51–55; tint-only at 50 remains its fallback if the direction check fails.
- **The (50,100) row count** is 103 live rows (map/object/character/collision users), not about 290.
- **a12 row 10 colA_u/d** reaches 46–49 / 51–55, not 46–47.
- **Character-row policy.** A bounded asymmetric declaration (P4) is recommended over plain undeclared rows. Both are listed.
- **blighttown_bridge.** The +0.3 ceiling is adopted (−0.10..+0.30), but checked on the median (p50 EV), because G3's argument that the geometric mean over-reacts at 36% crush stands. The raw ev is advisory. blighttown_swamp also follows PLAN's +0.3.
- **The evidence file** is section 13 here, not a separate `docs/ptde_evidence.md`: one audited list next to the targets.
- **Burg colour** has two independent (both unverified) sources, so it is moderated rather than halved: b −0.002..−0.008, fog colR 43–48 / colB 75–80, degRotW 48–54. The grade and scatter cooling wait for round 2 if b has not moved.

**Rejected**
- The claim that 51–58 is unreachable. 51–55 is reachable; only 56–60 is not.
- "Leave colA_s at 50 and change hue only" as anor_entrance's *default*. It stays the fallback.

## 13. Evidence ledger

Everything here is paraphrase; no PTDE assets were used.

**Status codes**
- **confirmed**: the W1 critic fetched the page (2026-10-30).
- **unverified**: it returned 403 or is a video without a transcript. Treat it as single-source, not false.
- **not re-checked**: only the original sweep read it.

| Claim (paraphrase) | Used for | Source | Status |
|---|---|---|---|
| DSR characters show a whitish, sourceless sheen in dark areas; Quelaag's fire and Blighttown torches lead the light | char de-glow everywhere; Quelaag | https://steamcommunity.com/app/570940/discussions/0/1694922345929612208 | confirmed (says nothing about Firelink) |
| DSR crushes blacks in a few areas, including the Painted World basement | Painted "no added crush"; Blighttown | https://steamcommunity.com/app/570940/discussions/0/1697174779846747538/ | confirmed; the thread never names PTDE, and several posters disagree |
| PTDE's Asylum looked flat and shadowless under open sky; the same poster rates DSR generally stronger and doubts the comparison video | Asylum keeps DSR's modelling | https://steamcommunity.com/app/570940/discussions/0/595136540368201208 | confirmed |
| The Burg reads clearer in DSR; PTDE was very dark overall | Burg darkens only slightly; global darker | https://steamcommunity.com/app/570940/discussions/0/3559414588261385698/ | confirmed |
| New Londo is better in DSR; the original Sen's had starker contrast and baked lighting | New Londo hold; Sen's darker and more shadowed | https://www.resetera.com/threads/dark-souls-prepare-to-die-vs-remastered-which-is-the-definitive-version.332714/page-2 | confirmed |
| Blighttown is darker in DSR than in PTDE on PC with a mod | Blighttown lift | https://www.thesixthaxis.com/2018/05/23/is-dark-souls-remastered-the-definitive-version-of-a-modern-classic/ | confirmed |
| DSR kept the murk | Blighttown and Darkroot counterweight | https://www.pushsquare.com/reviews/ps4/dark_souls_remastered | confirmed |
| Burg/Parish indoor fill too strong; Firelink characters lit flatly; god-ray boost reverted; Darkroot hazier than PTDE/Switch; Oolacile kept DSR values; Archives characters over-lit; New Londo needs DSR's compensation; raw PTDE values too dark in the Asylum and library; Anor characters cut, specular kept; Quelaag lantern off; Demon Ruins envDif cut then ambient re-raised; PTDE values broke Izalith; the EnvSpec gain caveat | most G1–G5 relationships | https://www.nexusmods.com/darksoulsremastered/mods/1423?tab=logs (and ?tab=posts) | unverified (403) |
| Burg shade blue-violet; Firelink shadow range too short; Depths torches and fog strong; character glow cuts (Painted, Oolacile, Archives, Crystal Cave, Asylum sewers, Sen's bridge); lava bloom for range | char glow, Firelink shadows, lava bloom | https://www.nexusmods.com/darksoulsremastered/mods/711?tab=logs | unverified (403); not PTDE-targeted |
| Vanilla Burg/Parish/Firelink look yellow-filtered next to PTDE | Burg/Parish/Firelink less yellow | https://www.nexusmods.com/darksoulsremastered/mods/558?tab=posts | unverified (403) |
| PTDE Burg fog bluer and weaker; Firelink green-blue with lower red; Painted blue-green; Darkroot very green and dark; Sen's interior dark and lantern-dependent; Anor redder/orange (unfinished build); Catacombs already close; Tomb dark mainly through the player light; harsh area thresholds | colour directions (halved), Sen's, Catacombs, Tomb, transitions | https://www.youtube.com/watch?v=XGr1hLiXYOU , https://www.youtube.com/watch?v=DfgvdVZsRT8 , https://www.youtube.com/watch?v=gzOuW0reUhk , https://www.youtube.com/watch?v=fjHRqm-quvQ , https://www.youtube.com/watch?v=HPYHawwsgKQ | unverified (video). Structurally weak: PTDE *values* rendered by DSR, which shows what the numbers do in DSR, not how PTDE looked |
| Missing PTDE bloom in a cell scene | Asylum interior bloom | https://www.resetera.com/threads/dark-souls-vs-remastered-1h40m-compilation-by-crowbcat.61450/ | not re-checked |
| Gothic mood intact in DSR previews; flat grey impression; lost prison depth | counterweights | http://www.gamebanshee.com/k4uqh , https://destructoid.com/?p=213867 | not re-checked |
| Blighttown easier to see in DSR; a PTDE-forum recollection of a white light in Seath's room | Blighttown counterweight; Crystal Cave hold | https://steamcommunity.com/app/570940/discussions/0/592887534192760008 , https://steamcommunity.com/app/211420/discussions/0/1693795812298458260/ | not re-checked |
| Contested Anor Londo warmth; Anor Londo washed out (both versions) | Anor hue held | https://www.youtube.com/watch?v=Hnhhx3RHaI4 , https://github.com/mooofin/DS_AnorLondoLightFix | unverified (video) / not re-checked |
| DSR reworked lighting, bloom, AO and materials | renderer context | https://www.youtube.com/watch?v=WtI4SS1_reU | unverified (video) |

**The tension, stated once.** Most sources remember PTDE as darker, so most views darken a little. Blighttown is the exception: there the reported problem is DSR crushing to black. Its lift is contested (it depends on the display, and some players find DSR easier to see), which is why it is gated on the user's sign-off.

## 14. Checker

```
.venv\Scripts\python.exe tools\check_looks.py captures\<run>                      # every variant under the run
.venv\Scripts\python.exe tools\check_looks.py captures\<run> --variant tuned --crops
.venv\Scripts\python.exe tools\check_looks.py captures\r1\tuned captures\r2\tuned --repeat --group G1
.venv\Scripts\python.exe tools\check_looks.py captures\<run> --baseline captures\rebaseline\vanilla --json out.json
```

**Output**
- Per bonfire: PASS / WARN / FAIL / SKIP, with each failing check's value and range, plus a sign-off note where one is required.
- Then the pair and same-sign checks.
- Exit code 1 on any FAIL. A missing capture is SKIP.

**Self-test.** The baseline checked against itself (no `--crops`) fails exactly the 12 views whose targets require a move: burg, parish, parish_altar, painted_entrance, painted, darkroot_garden, darkroot_basin, oolacile_township, blighttown_bridge, blighttown_swamp, anor_entrance and anor_interior. Every other view passes.

With `--crops`, the views whose character crop must drop fail as well: depths_dragon, great_hollow, izalith_2, archives_elevator and archives_balcony.

The PNG reader handles all five PNG row filters (checked on a synthetic image). The game's captures use filter 0 only.
