# 可商用的稀疏视角 4DGS 替代方案调研（2026-10-15）

**背景**：基线测试中 4C4D 胜出（见 [report-baseline.md](report-baseline.md)），但它不能商用，原因有两个：
1. 内含 Inria 的 `diff-gaussian-rasterization` 光栅化器，许可原文："THE USER CANNOT USE, EXPLOIT OR DISTRIBUTE THE SOFTWARE FOR COMMERCIAL PURPOSES"；
2. 初始化用的 MASt3R 是 CC BY-NC-SA。

本文调研能替换这两部分的可商用方案。

> 许可信息来自各仓库的 LICENSE、README 和模型卡（链接见文末）。**这不是法律意见**。标"未核实"的条目没有找到原始许可文件。

## 1. 光栅化器 / 训练框架

| 名称 | 代码许可 | 能否商用 | 能否做动态场景 |
|---|---|---|---|
| **[gsplat](https://github.com/nerfstudio-project/gsplat)**（nerfstudio） | Apache-2.0 | ✅ | 没有现成的 4D 训练器，但 `rasterization()` 接受预先算好的 3D 协方差。只要在 PyTorch 里把 4D 高斯按时间切成 3D，就能逐帧交给它渲染。维护活跃 |
| [Brush](https://github.com/ArthurBrussee/brush) | Apache-2.0 | ✅ | 只能播放动态结果，不能训练 |
| [OpenSplat](https://github.com/pierotofy/OpenSplat) | AGPL-3.0 | ⚠️ 传染性许可 | 只支持静态 |
| [LichtFeld Studio](https://github.com/MrNeRF/LichtFeld-Studio) | GPL-3.0+ | ⚠️ 传染性许可 | 只支持静态 |
| [Taichi 3DGS](https://github.com/wanmeihuali/taichi_3d_gaussian_splatting) | Apache-2.0 | ✅ | 只支持静态，个人项目 |

## 2. 动态（4D）高斯方法

| 名称 | 代码许可 | 内含光栅化器 | 能否商用（原样） | 说明 |
|---|---|---|---|---|
| 4C4D | MIT | Inria 衍生 | ❌ | 核心思路（Neural Decaying Function）可以按论文重写 |
| **[4DGS](https://github.com/fudan-zvg/4d-gaussian-splatting)**（ICLR'24，4C4D 的基础） | MIT + Inria 许可文件 | Inria 衍生 | ❌，但**最适合移植** | 渲染器有一条"在 PyTorch 里把 4D 切成 3D 再交给光栅化器"的路径，可以直接对接 gsplat。仓库里的 Inria 衍生文件需要逐个排查 |
| [4D-GS](https://github.com/hustvl/4DGaussians)（CVPR'24） | Apache-2.0 | 子模块是 Inria 的分支 | ❌ | 支持多视角；变形网络也可以移植到 gsplat |
| [Spacetime Gaussians](https://github.com/oppo-us-research/SpacetimeGaussians) | MIT | Inria | ❌ | |
| [Dynamic 3D Gaussians](https://github.com/JonathonLuiten/Dynamic3DGaussians) | MIT（`external.py` 除外） | Inria 衍生 | ❌ | README 写明商用需取得 Inria 许可；针对 Panoptic 的密集机位 |
| [Ex4DGS](https://github.com/juno181/Ex4DGS) | CC BY-NC 4.0 | — | ❌ | |
| [GC-4DGS](https://github.com/liyw420/GC-4DGS)（稀疏输入，2025） | MIT + Inria | Inria 衍生 | ❌ | 初始化用到的 Depth-Anything-V2-Large 也是非商业 |
| [Shape of Motion](https://github.com/vye16/shape-of-motion) | MIT | **gsplat** | ✅ | 单目，不是多视角；可以参考"在 gsplat 上做 4D"的写法 |
| Swift4D、Deformable-3DGS | 未核实 | Inria / 未核实 | ❌ 或不确定 | |

**没有找到现成的、既能商用又基于 gsplat 的多视角 4DGS 仓库。**

## 3. 替换 MASt3R 的初始化方案（已知相机位姿）

| 名称 | 代码 | 权重 | 能否商用 | 说明 |
|---|---|---|---|---|
| **[MapAnything](https://github.com/facebookresearch/map-anything)**（Meta） | Apache-2.0 | `facebook/map-anything-apache`：Apache-2.0（默认的 `map-anything` 是 CC-BY-NC） | ✅（apache 版权重） | **可以输入已知内参和位姿**，输出稠密点云，能导出 COLMAP 格式。最接近 MASt3R 的直接替代 |
| [COLMAP](https://colmap.github.io) 已知位姿三角化 | BSD | — | ✅ | 4 个视角下点很稀疏，需要配合学习型匹配器加密 |
| [LightGlue](https://github.com/cvg/LightGlue) + DISK / ALIKED | Apache-2.0 | DISK Apache-2.0；ALIKED BSD-3 | ✅ | 匹配后用已知位姿三角化，和我们现在的 MASt3R 脚本思路一样 |
| [RoMa](https://github.com/Parskatt/RoMa) | MIT | 权重许可**未核实** | 代码 ✅，权重待确认 | 稠密匹配器 |
| [VGGT](https://github.com/facebookresearch/vggt) | VGGT License | `VGGT-1B-Commercial`：可商用，但禁止军用，需要申请 | ⚠️ 有条件 | 不能输入已知位姿，输出要再对齐到标定相机 |
| Depth Anything V2 | Apache-2.0 | **只有 Small 版是 Apache-2.0**；Base/Large/Giant 是 CC-BY-NC | Small ✅ | 可以作深度先验 |
| SuperPoint | 非商业研究 | 同左 | ❌ | 用 hloc / LightGlue 时注意不要选它 |
| DUSt3R / MASt3R | CC BY-NC-SA | 训练数据含非商业数据集 | ❌ | |

## 4. 建议

没有现成可用的商用版 4C4D，需要**自己移植，估计 1–2 周工程量**：

1. **光栅化器**：换成 **gsplat**（Apache-2.0）。以 fudan 4DGS 的模型逻辑为基础：在 PyTorch 里把 4D 高斯按时间切成 3D，再交给 `gsplat.rasterization()`。4C4D 的"透明度衰减"思路按论文重写，不要复制 Inria 许可的文件。
2. **初始化**：用 **MapAnything 的 Apache 版权重**，输入已知的内参和位姿，生成稠密点云。备选方案：LightGlue + ALIKED 匹配后，用 COLMAP 在已知位姿下三角化。
3. **避开**：SuperPoint、DUSt3R/MASt3R、Depth Anything V2 的 Base/Large 版，以及任何包含 Inria `diff-gaussian-rasterization` 或 `simple-knn` 的代码。发布前逐个检查仓库里的 `LICENSE_gaussian_splatting.md`。
4. **用之前还要确认三件事**：RoMa 权重的许可；VGGT-1B-Commercial 的完整条款；4C4D 里标 MIT 的文件是否含有 Inria 衍生代码。

移植完成后，用同一套 Panoptic 数据和评估口径复测，确认画质不低于 4C4D（PSNR 21.7）。

## 来源

gsplat [LICENSE](https://raw.githubusercontent.com/nerfstudio-project/gsplat/main/LICENSE) · [rasterization API](https://docs.gsplat.studio/main/apis/rasterization.html) ·
Inria [LICENSE](https://raw.githubusercontent.com/graphdeco-inria/diff-gaussian-rasterization/main/LICENSE.md) ·
fudan 4DGS [LICENSE_gaussian_splatting](https://raw.githubusercontent.com/fudan-zvg/4d-gaussian-splatting/main/LICENSE_gaussian_splatting.md) · [renderer](https://raw.githubusercontent.com/fudan-zvg/4d-gaussian-splatting/main/gaussian_renderer/__init__.py) ·
4D-GS [.gitmodules](https://raw.githubusercontent.com/hustvl/4DGaussians/master/.gitmodules) ·
Ex4DGS [LICENSE](https://raw.githubusercontent.com/juno181/Ex4DGS/main/LICENSE.txt) ·
VGGT [LICENSE](https://raw.githubusercontent.com/facebookresearch/vggt/main/LICENSE.txt) · [VGGT-1B-Commercial](https://huggingface.co/facebook/VGGT-1B-Commercial) ·
SuperPoint [LICENSE](https://raw.githubusercontent.com/magicleap/SuperPointPretrainedNetwork/master/LICENSE) ·
[COLMAP license](https://colmap.github.io/license.html) · 以及上表中各仓库的主页
