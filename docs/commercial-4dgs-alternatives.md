# Survey of commercially usable sparse-view 4DGS alternatives (2026-10-15)

**Background**: 4C4D won the baseline test (see [report-baseline.md](report-baseline.md)), but it cannot be used commercially, for two reasons:
1. It bundles Inria's `diff-gaussian-rasterization` rasterizer, whose license says: "THE USER CANNOT USE, EXPLOIT OR DISTRIBUTE THE SOFTWARE FOR COMMERCIAL PURPOSES";
2. The MASt3R used for initialization is CC BY-NC-SA.

This document surveys commercially usable options that can replace these two parts.

> License information comes from each repo's LICENSE, README and model card (links at the end). **This is not legal advice.** Entries marked "unverified" are ones where we could not find the original license file.

## 1. Rasterizer / training framework

| Name | Code license | Commercial use? | Dynamic scenes? |
|---|---|---|---|
| **[gsplat](https://github.com/nerfstudio-project/gsplat)** (nerfstudio) | Apache-2.0 | ✅ | No ready-made 4D trainer, but `rasterization()` accepts precomputed 3D covariances. As long as the 4D Gaussians are sliced into 3D by time in PyTorch, each frame can be handed to it for rendering. Actively maintained |
| [Brush](https://github.com/ArthurBrussee/brush) | Apache-2.0 | ✅ | Can only play back dynamic results, not train them |
| [OpenSplat](https://github.com/pierotofy/OpenSplat) | AGPL-3.0 | ⚠️ copyleft license | Static only |
| [LichtFeld Studio](https://github.com/MrNeRF/LichtFeld-Studio) | GPL-3.0+ | ⚠️ copyleft license | Static only |
| [Taichi 3DGS](https://github.com/wanmeihuali/taichi_3d_gaussian_splatting) | Apache-2.0 | ✅ | Static only, personal project |

## 2. Dynamic (4D) Gaussian methods

| Name | Code license | Bundled rasterizer | Commercial use (as is)? | Notes |
|---|---|---|---|---|
| 4C4D | MIT | Inria-derived | ❌ | The core idea (Neural Decaying Function) can be reimplemented from the paper |
| **[4DGS](https://github.com/fudan-zvg/4d-gaussian-splatting)** (ICLR'24, the basis of 4C4D) | MIT + Inria license file | Inria-derived | ❌, but **best suited for porting** | The renderer has a path that "slices 4D into 3D in PyTorch, then hands it to the rasterizer", which can connect directly to gsplat. The Inria-derived files in the repo need to be checked one by one |
| [4D-GS](https://github.com/hustvl/4DGaussians) (CVPR'24) | Apache-2.0 | Submodule is an Inria fork | ❌ | Supports multi-view; the deformation network could also be ported to gsplat |
| [Spacetime Gaussians](https://github.com/oppo-us-research/SpacetimeGaussians) | MIT | Inria | ❌ | |
| [Dynamic 3D Gaussians](https://github.com/JonathonLuiten/Dynamic3DGaussians) | MIT (except `external.py`) | Inria-derived | ❌ | README states commercial use requires a license from Inria; targets Panoptic's dense cameras |
| [Ex4DGS](https://github.com/juno181/Ex4DGS) | CC BY-NC 4.0 | — | ❌ | |
| [GC-4DGS](https://github.com/liyw420/GC-4DGS) (sparse input, 2025) | MIT + Inria | Inria-derived | ❌ | The Depth-Anything-V2-Large used for initialization is also non-commercial |
| [Shape of Motion](https://github.com/vye16/shape-of-motion) | MIT | **gsplat** | ✅ | Monocular, not multi-view; useful as a reference for "doing 4D on gsplat" |
| Swift4D, Deformable-3DGS | Unverified | Inria / unverified | ❌ or uncertain | |

**We found no ready-made multi-view 4DGS repo that is both commercially usable and built on gsplat.**

## 3. Initialization alternatives to MASt3R (known camera poses)

| Name | Code | Weights | Commercial use? | Notes |
|---|---|---|---|---|
| **[MapAnything](https://github.com/facebookresearch/map-anything)** (Meta) | Apache-2.0 | `facebook/map-anything-apache`: Apache-2.0 (the default `map-anything` is CC-BY-NC) | ✅ (apache weights) | **Accepts known intrinsics and poses as input**, outputs a dense point cloud, can export COLMAP format. The closest drop-in replacement for MASt3R |
| [COLMAP](https://colmap.github.io) triangulation with known poses | BSD | — | ✅ | With 4 views the points are very sparse; needs a learned matcher to densify |
| [LightGlue](https://github.com/cvg/LightGlue) + DISK / ALIKED | Apache-2.0 | DISK Apache-2.0; ALIKED BSD-3 | ✅ | Match, then triangulate with known poses — same approach as our current MASt3R script |
| [RoMa](https://github.com/Parskatt/RoMa) | MIT | Weights license **unverified** | Code ✅, weights to be confirmed | Dense matcher |
| [VGGT](https://github.com/facebookresearch/vggt) | VGGT License | `VGGT-1B-Commercial`: commercial use allowed, but military use prohibited; requires an application | ⚠️ conditional | Can't take known poses as input; output must be aligned to the calibrated cameras afterwards |
| Depth Anything V2 | Apache-2.0 | **Only the Small version is Apache-2.0**; Base/Large/Giant are CC-BY-NC | Small ✅ | Can serve as a depth prior |
| SuperPoint | Non-commercial research | Same | ❌ | Be careful not to pick it when using hloc / LightGlue |
| DUSt3R / MASt3R | CC BY-NC-SA | Training data includes non-commercial datasets | ❌ | |

## 4. Recommendation

There is no ready-made commercial version of 4C4D; we would need to **port it ourselves, an estimated 1–2 weeks of engineering**:

1. **Rasterizer**: switch to **gsplat** (Apache-2.0). Start from the model logic of fudan 4DGS: slice the 4D Gaussians into 3D by time in PyTorch, then hand them to `gsplat.rasterization()`. Reimplement 4C4D's "opacity decay" idea from the paper; do not copy Inria-licensed files.
2. **Initialization**: use **MapAnything's Apache weights**, feeding in the known intrinsics and poses to produce a dense point cloud. Fallback: match with LightGlue + ALIKED, then triangulate with COLMAP under known poses.
3. **Avoid**: SuperPoint, DUSt3R/MASt3R, the Base/Large versions of Depth Anything V2, and any code that includes Inria's `diff-gaussian-rasterization` or `simple-knn`. Before release, check every `LICENSE_gaussian_splatting.md` in the repo.
4. **Three things to confirm before use**: the license of the RoMa weights; the full terms of VGGT-1B-Commercial; whether the MIT-labeled files in 4C4D contain Inria-derived code.

After porting, retest on the same Panoptic data with the same evaluation protocol to confirm image quality is no worse than 4C4D (PSNR 21.7).

## Sources

gsplat [LICENSE](https://raw.githubusercontent.com/nerfstudio-project/gsplat/main/LICENSE) · [rasterization API](https://docs.gsplat.studio/main/apis/rasterization.html) ·
Inria [LICENSE](https://raw.githubusercontent.com/graphdeco-inria/diff-gaussian-rasterization/main/LICENSE.md) ·
fudan 4DGS [LICENSE_gaussian_splatting](https://raw.githubusercontent.com/fudan-zvg/4d-gaussian-splatting/main/LICENSE_gaussian_splatting.md) · [renderer](https://raw.githubusercontent.com/fudan-zvg/4d-gaussian-splatting/main/gaussian_renderer/__init__.py) ·
4D-GS [.gitmodules](https://raw.githubusercontent.com/hustvl/4DGaussians/master/.gitmodules) ·
Ex4DGS [LICENSE](https://raw.githubusercontent.com/juno181/Ex4DGS/main/LICENSE.txt) ·
VGGT [LICENSE](https://raw.githubusercontent.com/facebookresearch/vggt/main/LICENSE.txt) · [VGGT-1B-Commercial](https://huggingface.co/facebook/VGGT-1B-Commercial) ·
SuperPoint [LICENSE](https://raw.githubusercontent.com/magicleap/SuperPointPretrainedNetwork/master/LICENSE) ·
[COLMAP license](https://colmap.github.io/license.html) · plus the home pages of each repo in the tables above
