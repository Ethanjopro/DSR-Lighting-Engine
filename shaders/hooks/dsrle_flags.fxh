//==============================================================================
// dsrle_flags.fxh -- compile-time variant flag bits passed to the DSRLE hooks.
// Original DSRLE code. Macros only (no declarations).
//
// The hook argument `flags` is (DSRLE_VARIANT_FLAGS | per-function bits).
// dsrle_variant.fxh sets DSRLE_VARIANT_FLAGS once per base #include, using the
// macros the wrapper or variant pattern defined for that include
// (WITH_LightMap, WITH_ShadowMap, WITH_BumpMap, POINT_LIGHT_0, ...).
// Each base call site adds DSRLE_VF_SPC / DSRLE_VF_MUL for its entry point.
//==============================================================================
#ifndef DSRLE_FLAGS_FXH
#define DSRLE_FLAGS_FXH

// light / shadow inputs
#define DSRLE_VF_LIT          0x00000001u  // light map (lightmap arg is a real sample)
#define DSRLE_VF_SDW          0x00000002u  // "Sdw": shadow lookup from the VS light-space position (In.VtxLit)
#define DSRLE_VF_CSD          0x00000004u  // "Csd": cascaded shadow lookup in the PS (gFC_ShadowMapMtxArray, t7)
#define DSRLE_VF_NCS          0x00000008u  // "Ncs": water/snow single (non-cascaded) PS lookup
#define DSRLE_VF_SHADOWED     (DSRLE_VF_SDW | DSRLE_VF_CSD | DSRLE_VF_NCS)
// material / mesh features
#define DSRLE_VF_BMP          0x00000010u  // normal map
#define DSRLE_VF_MUL          0x00000020u  // two-layer (vertex-alpha blended) material
#define DSRLE_VF_SPC          0x00000040u  // PBL / specular map present (else pblTexData is the constant (1,0,0,1))
#define DSRLE_VF_PNTS         0x00000080u  // "PntS": second clustered light list
#define DSRLE_VF_GHOST        0x00000100u  // Gst ghost path (ghost light + tint)
#define DSRLE_VF_TRANSLUCENT  0x00000200u  // FS_TRANSLUCENT inverse-tone-mapped output (Gst, Sfx)
#define DSRLE_VF_SUBSURF      0x00000400u  // FS_SUBSURF
#define DSRLE_VF_ENVLERP      0x00000800u  // two-probe blend really compiled in
#define DSRLE_VF_PARALLAX     0x00001000u  // WITH_Parallax runtime POM branch
// family (wrapper)
#define DSRLE_VF_FAM_PHN      0x00010000u
#define DSRLE_VF_FAM_GST      0x00020000u
#define DSRLE_VF_FAM_SFX      0x00040000u
#define DSRLE_VF_FAM_WATER    0x00080000u
#define DSRLE_VF_FAM_SNOW     0x00100000u
// base (fragment body)
#define DSRLE_VF_BASE_HEMENV  0x01000000u
#define DSRLE_VF_BASE_NON     0x02000000u
#define DSRLE_VF_BASE_FACEEYE 0x04000000u
#define DSRLE_VF_BASE_WATER   0x08000000u
#define DSRLE_VF_BASE_SNOW    0x10000000u

#endif //DSRLE_FLAGS_FXH
