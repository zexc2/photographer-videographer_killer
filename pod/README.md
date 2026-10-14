# pod 端脚本（阶段 2）

这些脚本在 RunPod GPU pod 上运行，由服务器上的 `atlas-run` 推送过去执行。服务器本身不跑任何模型。

| 文件 | 作用 |
|---|---|
| `lib/common.sh` | 公共函数：路径约定、busy 心跳、Miniforge、环境打包和恢复、代码克隆、HF token、写 Global volume、安装报告 |
| `setup_gen3c.sh` | GEN3C 环境和权重 |
| `setup_vipe.sh` | ViPE 环境和模型缓存（独立环境，和 GEN3C 不兼容） |
| `setup_4c4d.sh` | 4C4D 环境、MASt3R 环境和 MASt3R 权重 |
| `run_task.sh` | 通用任务包装：计时、峰值显存、状态文件、结束时把 `out/` 同步回 Global volume |
| `pre_stop.sh` | 巡检 stop pod 前调用，把所有 run 的 `out/` 同步回 Global volume |
| `tasks/selftest.sh` | 第一次开 pod 用：检查挂载和 GPU，实测 Global volume 与容器盘的读写速度 |
| `tasks/setup.sh` | 安装入口：`atlas-run setup gen3c vipe 4c4d` |
| `tasks/hello.sh` | 最小任务，测试执行器 |

所有 setup 脚本都是**幂等**的，按"方案 C"工作（见 [docs/storage-and-env.md](../docs/storage-and-env.md)）：

1. Global volume 的 `envpacks/` 里有当前版本的环境包 → 解压到容器盘，几分钟完成
2. 没有 → 完整安装，安装完打包上传，下次开机直接复用
3. 权重只下载一次，存在 `$ATLAS_VOL/weights/<模型>/`，以 `.complete` 标记表示下载完整

版本键是 setup 脚本加 `common.sh` 的哈希。脚本有改动，下次运行时就会重新安装、重新打包。

## 各环境一览

| | GEN3C | ViPE | 4C4D | MASt3R |
|---|---|---|---|---|
| 官方仓库（固定 commit） | [nv-tlabs/GEN3C](https://github.com/nv-tlabs/GEN3C) `db2ffe1` | [nv-tlabs/vipe](https://github.com/nv-tlabs/vipe) `8c9f361` | [yangzf-1023/4C4D](https://github.com/yangzf-1023/4C4D) `ed6a3cb`（已核实是官方仓库，[项目页](https://junshengzhou.github.io/4C4D/)、[arXiv 2604.04063](https://arxiv.org/abs/2604.04063)） | [naver/mast3r](https://github.com/naver/mast3r) `f5209af` |
| Python / torch / CUDA | 3.10 / 2.6 / 12.4（官方 yml） | 3.11 / 最新 cu128 | 3.10 / 2.4.1 / 12.4（**改过**，官方是 3.7 / 1.12 / 11.6，不支持 H100） | 3.11 / 2.4.1 / 12.4 |
| 需要编译 | transformer-engine 1.12、apex | vipe_ext | simple-knn、pointops2、fused-ssim、diff-gaussian-rasterization | 不需要（curope 可选，已跳过） |
| 权重 | 约 76 GB：GEN3C-Cosmos-7B、Cosmos-Tokenize1、T5-11B（跳过了 45 GB 用不到的 TF 权重） | 约 3–5 GB，首次运行自动下载，打成 tar 保存 | 不需要预训练权重 | 2.75 GB |
| HF token / 同意许可 | 不需要（三个仓库都没有 gating） | 不需要 | 不需要 | 不需要 |
| 首次安装（**实测**，A100 SXM） | 环境 19 分钟（含编译 apex 约 15 分钟）+ 权重下载并存入 Global volume 约 5 分钟 | 25 分钟（其中测试视频约 18 分钟，GeoCalib 就用了 11 分钟） | 4C4D 与 MASt3R 合计 13 分钟 | （含在左边） |
| 环境大小（**实测**） | 13 GB，压缩后 5.0 GB | 12 GB，压缩后 4.8 GB；模型缓存 4 GB | 13 GB，压缩后 5.2 GB | 压缩后 2.4 GB；权重 2.6 GB |
| 以后开机恢复（估算，按 Global volume 读取 75 MB/s 计） | 约 2–3 分钟 | 约 2–3 分钟 | 约 2–3 分钟 | 约 1 分钟 |

安装耗时和大小已在 2026-10-14 第一次开 pod 时实测；恢复耗时等下次开 pod 再实测。 每次运行都会把 `{耗时, 环境大小, 权重大小, GPU}` 追加到 `$ATLAS_VOL/setup-reports/<模型>.jsonl`。

## 对官方流程的改动（以及原因）

- **GEN3C**：官方用 `ln -sf` 把 CUDA 头文件软链进环境，这里改成复制，因为 conda-pack 不能处理绝对路径软链。权重自己用 `snapshot_download` 下载，只取需要的文件；官方脚本会下载 T5 的 TF 权重，每次重跑还要校验 MD5。
- **ViPE**：官方用 `uv sync` 装进仓库内的 `.venv`；这里改成 conda 环境加 `pip install --no-build-isolation .`（非 editable），这样可以打包。另外设置 `USE_SYSTEM_EIGEN=1`，避免编译时联网下载 Eigen。
- **4C4D**：
  - 环境升级到 py3.10 / torch 2.4 / CUDA 12.4，让 H100 也能跑。如果编译或训练出问题，退回官方的 py3.7 环境（只能在 A100 上跑）。
  - 修正官方 yml 里的路径错误：`./diff_gaussian_rasterization` 应为 `./diff-gaussian-rasterization`。
  - 补装代码里实际 import、但 yml 没列出的包：torch_scatter、scipy、scikit-image、opencv、open3d、mediapy 等。
  - 官方 README 里 `render.py --test` 的参数实际叫 `--validate`。
- **MASt3R**：官方 4C4D 流程用 MAtCha（`train.py --sfm_config posed --sfm_only`）生成稀疏视角点云。MAtCha 的安装用了 editable 方式，还写死了 `/usr/local/cuda-11.8`，没法打包。这里改为直接安装 naver/mast3r，阶段 3 自己写一个"已知位姿 → points3D"的小脚本。

## 已知限制

- 2026-10-14 第一次开 pod 实测，修了以下问题：
  - GEN3C：MoGe 把 huggingface-hub 升到 2.x，破坏了 transformers → 装完 MoGe 后按官方 requirements 重新固定版本。官方自检脚本出错时仍返回 0 → 改成检查输出里的 ERROR。apex master 和 torch 2.6 不兼容，24.10 又太旧 → 固定到 **25.09**
  - ViPE / 4C4D：conda-forge 会混进 CUDA 13.x → 固定 `cuda-version`；4C4D 编译 `.cpp` 时找不到 CUDA 头文件 → 把 `targets/x86_64-linux/include` 加进 CPATH
  - Global volume 不能设置文件时间 → `vol_sync` 不再用 `rsync -t`
- MoGe 3.x 要求 numpy≥2，和 GEN3C 固定的 numpy 1.26 冲突。它只用于单张图片模式，我们用的是视频 + ViPE，所以不影响
- ViPE 的 GeoCalib（估计内参）在测试视频上用了 11 分钟，而且主要在 CPU 上跑。阶段 3/4 要查清原因，也可以在已知内参时跳过这一步（Panoptic 有标定数据）
- **GEN3C 的命令行只支持预设运镜**：left、right、up、down、zoom_in、zoom_out、clockwise、counterclockwise，可以调距离和朝向模式。阶段 5 需要的自定义关键帧轨迹，要改 `generate_camera_trajectory` 或走官方 GUI 的 server 接口。4C4D 的 `render.py` 同样只有 ellipse、interpolate、arc 三种轨迹，改 `generate_path` 比较简单。

## 许可汇总（详见根目录 README）

| 组件 | 许可 | 能否商用 |
|---|---|---|
| GEN3C 代码 | Apache-2.0 | 可以 |
| GEN3C / Cosmos 权重 | NVIDIA Open Model License | 可以，但**模型卡写明：关闭安全护栏会自动终止许可**，而官方推理脚本强制关闭了护栏（`gen3c_dynamic.py:389`）。商用前需要处理 |
| T5-11B | Apache-2.0 | 可以 |
| ViPE 代码 | Apache-2.0（Unik3D 部分为 CC BY-NC-SA 4.0） | 代码可以；默认 pipeline 用的深度模型（UniDepth 等）为非商业许可（待逐个核实） |
| 4C4D | MIT，但内含 Inria 3DGS 光栅化器（仅限非商业 / 研究） | **不可以** |
| MASt3R | 代码 CC BY-NC-SA 4.0；checkpoint 还受训练数据集许可约束 | **不可以** |
| CMU Panoptic | 仅限非商业研究 | **不可以** |
