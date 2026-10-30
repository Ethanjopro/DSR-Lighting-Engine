//==============================================================================
// dsrle_variant.fxh -- (re)computes DSRLE_VARIANT_FLAGS for the current base include.
// Original DSRLE code. No include guard: each fragment base includes it once per
// feature combination, after it has set DSRLE_BASE_FLAGS (a DSRLE_VF_BASE_* bit).
// Macros only.
//==============================================================================
#include "dsrle_flags.fxh"

#undef DSRLE_VF__LIT
#undef DSRLE_VF__SHD
#undef DSRLE_VF__BMP
#undef DSRLE_VF__PNT
#undef DSRLE_VF__GST
#undef DSRLE_VF__TRN
#undef DSRLE_VF__SSS
#undef DSRLE_VF__LRP
#undef DSRLE_VF__PLX
#undef DSRLE_VF__FAM
#undef DSRLE_VARIANT_FLAGS

#ifdef WITH_LightMap
	#define DSRLE_VF__LIT DSRLE_VF_LIT
#else
	#define DSRLE_VF__LIT 0u
#endif

#ifdef WITH_ShadowMap
	#if (WITH_ShadowMap == CalcLispPos_VS)
		#define DSRLE_VF__SHD DSRLE_VF_SDW
	#elif (WITH_ShadowMap == CalcLispPos_PS_NoCsd)
		#define DSRLE_VF__SHD DSRLE_VF_NCS
	#else
		#define DSRLE_VF__SHD DSRLE_VF_CSD
	#endif
#else
	#define DSRLE_VF__SHD 0u
#endif

#ifdef WITH_BumpMap
	#define DSRLE_VF__BMP DSRLE_VF_BMP
#else
	#define DSRLE_VF__BMP 0u
#endif

#if (POINT_LIGHT_0 > POINT_LIGHT_TYPE_None)
	#define DSRLE_VF__PNT DSRLE_VF_PNTS
#else
	#define DSRLE_VF__PNT 0u
#endif

#ifdef WITH_GhostMap
	#define DSRLE_VF__GST DSRLE_VF_GHOST
#else
	#define DSRLE_VF__GST 0u
#endif

#ifdef FS_TRANSLUCENT
	#define DSRLE_VF__TRN DSRLE_VF_TRANSLUCENT
#else
	#define DSRLE_VF__TRN 0u
#endif

#ifdef FS_SUBSURF
	#define DSRLE_VF__SSS DSRLE_VF_SUBSURF
#else
	#define DSRLE_VF__SSS 0u
#endif

#ifdef FRPG_421B_ENVLERP_PROBES
	#define DSRLE_VF__LRP DSRLE_VF_ENVLERP
#else
	#define DSRLE_VF__LRP 0u
#endif

#ifdef WITH_Parallax
	#define DSRLE_VF__PLX DSRLE_VF_PARALLAX
#else
	#define DSRLE_VF__PLX 0u
#endif

#if defined(FRPG_FAMILY_PHN)
	#define DSRLE_VF__FAM DSRLE_VF_FAM_PHN
#elif defined(FRPG_FAMILY_GST)
	#define DSRLE_VF__FAM DSRLE_VF_FAM_GST
#elif defined(FRPG_FAMILY_SFX)
	#define DSRLE_VF__FAM DSRLE_VF_FAM_SFX
#elif defined(FRPG_FAMILY_WATER)
	#define DSRLE_VF__FAM DSRLE_VF_FAM_WATER
#elif defined(FRPG_FAMILY_SNOW)
	#define DSRLE_VF__FAM DSRLE_VF_FAM_SNOW
#else
	#define DSRLE_VF__FAM 0u
#endif

#ifndef DSRLE_BASE_FLAGS
	#define DSRLE_BASE_FLAGS 0u
#endif

#define DSRLE_VARIANT_FLAGS (DSRLE_VF__LIT | DSRLE_VF__SHD | DSRLE_VF__BMP | DSRLE_VF__PNT | DSRLE_VF__GST | DSRLE_VF__TRN | DSRLE_VF__SSS | DSRLE_VF__LRP | DSRLE_VF__PLX | DSRLE_VF__FAM | DSRLE_BASE_FLAGS)
