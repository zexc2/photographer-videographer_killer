# 存储与环境方案（2026-10-13 决定）

## 1. 为什么改用 Global volume

Network volume 绑定一个机房。pod 停掉以后，同一个机房经常已经没有 A100，就开不出来。
[Global volume](https://docs.runpod.io/storage/globalvolume) 不绑机房，任何区域的 GPU pod 都能挂载，所以 `gpu-up` 可以在**所有区域**里找 A100。

从官方文档核实的要点（2026-10，Global volume 仍是 **beta**）：

| 项目 | 说明 |
|---|---|
| 底层 | 对象存储，**不是完整的 POSIX**：没有文件锁、rename 不是原子的、没有硬链接、没有权限位；最终一致；两个 pod 写同一个文件时后写的覆盖 |
| 适合 | 读多写少，比如模型权重、原始数据集 |
| 不适合 | 频繁写入：训练、checkpoint、conda 环境、大量小文件 |
| 挂载路径 | 只挂 Global 时默认 `/workspace`；同时挂 network volume 时变成 `/workspace-global`。我们在创建 pod 时**显式指定**路径（默认 `/workspace`，可用 `ATLAS_VOL_MOUNT` 改），并作为环境变量 **`$ATLAS_VOL`** 传进 pod，pod 上的脚本只认这个变量 |
| 计费 | 按用量计费（没有预设容量）；另有请求费：写/列目录 $0.005 / 千次，读 $0.0005 / 千次。存储单价文档里没写，以账单为准 |
| ⚠️ 余额 | **账户余额归零时 volume 会被标记，15 天后永久删除**。务必开启低余额提醒 |
| API | 只有 REST API v2 支持（`mounts.global[].volumeId`）；runpodctl 2.15 不支持 → 护栏改用 v2 API |

## 2. 数据放在哪里

| 位置 | 环境变量 | 放什么 | 生命周期 |
|---|---|---|---|
| Global volume | `$ATLAS_VOL`（默认 `/workspace`） | **只放冷数据**：`weights/` 模型权重、`datasets/panoptic/` 原始数据、`envpacks/` 环境压缩包（见下文方案 C）、`results/<run>/` 最终结果和报告 | 永久 |
| 容器盘 | `$ATLAS_LOCAL`（`/opt/atlas`） | `envs/` conda 环境、`src/` 代码仓库、`work/<run>/` 工作目录、中间文件、checkpoint、HF 下载缓存、`.atlas_busy` 空闲标记、`bin/pre_stop` | **pod stop 后清空** |

规则：
- 任务结束前，由任务执行器把要保留的结果同步回 `$ATLAS_VOL/results/<run>/`。
- 巡检 stop pod 之前，会先尽力执行 `$ATLAS_LOCAL/bin/pre_stop`（最多 10 分钟），把结果同步回去。
- 写 Global volume 时：先打包，尽量写大文件、少写小文件；用 `rsync -rt --inplace --no-perms --no-owner --no-group`（避免依赖原子 rename 和权限位）。
- Hugging Face 下载会用到文件锁和权限位，所以先下载到容器盘，再整体拷贝到 `$ATLAS_VOL/weights/`。

**容器盘建议 200 GB**（`gpu-up --disk` 默认值）。估算一次会话的用量：

| 项目 | 大小 |
|---|---|
| 1–2 套 conda 环境 | 15–35 GB |
| 必要时把权重拷到本地 NVMe 加速 | 0–80 GB |
| 数据 | 10–20 GB |
| 工作目录、中间文件、checkpoint | 30–60 GB |

容器盘按 $0.10/GB/月计费，200 GB 约合 $0.03/小时，可以忽略。如果某些机器开不出 200 GB，可以降到 120 GB。

## 3. conda 环境：方案对比

下表的安装耗时是根据各项目依赖（PyTorch CUDA 扩展、transformer-engine、flash-attn、3DGS 光栅化器、MASt3R 的 curope 等）做的**估算**，阶段 2 第一次安装时实测后更新。GPU 按 A100 SXM $1.79/h 计算。

| | A) 自定义 Docker 镜像（GHCR / Docker Hub） | B) 每次开机装到容器盘 | **C) 推荐：装一次，conda-pack 打包存到 Global volume** |
|---|---|---|---|
| 做法 | 每个模型一个镜像（只含依赖，不含第三方代码和权重），用 GitHub Actions 构建后推到 GHCR | 每次开 pod 都运行 setup 脚本 | 第一次 = B；装完把每个环境用 `conda-pack` 打成**一个大文件** `envpacks/<env>.tar.zst` 放到 Global volume；以后开机直接解压到容器盘 |
| 首次耗时 | 写 Dockerfile 并调试 0.5–1 天；GitHub 免费 runner 没有 GPU，磁盘只剩约 14 GB 要手动清理，flash-attn 编译可能接近 6 小时的任务上限 | 0 | 和 B 一样：GEN3C 40–70 分钟，ViPE 15–25 分钟，4C4D+MASt3R 15–30 分钟；另加打包约 5 分钟 |
| 首次花费 | GPU $0（CI 免费），人工成本高 | — | 一次性约 $1.2–3.7（三套全装） |
| 每次开机耗时 | 拉 15–30 GB 镜像，约 5–15 分钟 | **40–125 分钟** | 读取并解压 5–10 GB，约 3–8 分钟（Global volume 的读取速度第一次实测） |
| 每次开机花费 | 约 $0.15–0.45（拉镜像期间是否计费需实测） | **约 $1.2–3.7**，还占用 6 小时上限 | 约 $0.1–0.25 |
| 稳定性 | 高，环境完全固定 | 低，上游 pip 包会变，每次装都可能失败 | 高，打包后环境就固定了 |
| 改环境 | 改 Dockerfile，重新构建、推送 | 改脚本 | 改脚本，重装一次，重新打包 |
| 其他 | 公开镜像要注意不能包含第三方代码；私有镜像需要在 RunPod 配置 registry 凭据 | — | 不能用 `pip install -e`（conda-pack 不支持），代码放在 `$ATLAS_LOCAL/src` 用 `PYTHONPATH`；CUDA 扩展编译成 `sm_80;sm_90`，A100 和 H100 通用；基础镜像版本固定 |

**推荐 C**，并且让 B 作为它的自动兜底：setup 脚本先找 `envpacks/` 里的压缩包，有就解压，没有就安装并打包。理由：
- 每次开机只多花几分钟。
- 不需要搭 CI。
- 不会公开分发第三方内容。
- 存在 Global volume 上的是一个大文件，正好符合对象存储"读多写少、文件大"的特点。

以后如果需要更快启动或可复现的发布，同一套 setup 脚本可以直接放进 Dockerfile，升级到方案 A。
