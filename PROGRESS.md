# 进度记录

每个阶段记录：做了什么 / 遇到的问题 / 耗时 / 花费。

| 阶段 | 日期 | 耗时 | GPU 花费 |
|---|---|---|---|
| 0 仓库初始化 | 2026-10-13 | ~10 分钟 | $0 |
| 1 GPU 护栏 | 2026-10-13 | ~30 分钟 | $0 |
| 1.1 改用 Global volume | 2026-10-13 | ~45 分钟 | $0 |
| 2 pod 环境脚本 | 2026-10-13 | ~1 小时 | $0（尚未开 pod） |
| 2.2 第一次开 pod：安装三套环境 | 2026-10-14 | pod 2h03m | **$3.97** |
| 3 Panoptic 数据脚本 | 2026-10-14 | ~1 小时 | $0（尚未开 pod） |

---

## 阶段 0：仓库初始化（2026-10-13）

**做了什么**
- 保存项目简报为 `CLAUDE.md`
- `~/app` 初始化 git（main 分支），先写 `.gitignore`（屏蔽密钥、权重、数据集、视频、点云、日志等）再做第一次提交
- 建立 `README.md` 骨架（背景、阶段规划、架构、第三方许可表）与本文件

**遇到的问题**
- 无

**耗时**：约 10 分钟
**花费**：$0（未使用 GPU）

---

## 阶段 1：GPU 护栏（2026-10-13）

**做了什么**
- `guard/atlas_gpu.py`，以 `~/bin/gpu-up`、`gpu-down`、`gpu-status`、`gpu-guard` 的形式安装：
  - 白名单只有 A100 80GB，A100 全部无货时才允许 H100；名字强制 atlas- 前缀；最多同时 1 个；挂载 `atlas_data` 到 `/workspace`；`--hours` 默认 6、最大 6
  - 只能操作 atlas- 前缀的 pod，别的项目的 pod 一律拒绝
- 巡检：atlas 用户的 crontab 每 10 分钟执行一次（用 `flock` 防重入）。超时 → stop；GPU 连续 30 分钟利用率为 0 → stop；日志写到 `~/logs/gpu-guard.log`
- 空闲检测选了 SSH + nvidia-smi，没有用 RunPod API。理由见 [guard/README.md](guard/README.md)
- 离线测试 `guard/tests/run_tests.sh`：用假的 runpodctl / ssh 跑 39 个用例，全部通过。在真实账号上做了只读的 dry-run，没有开 pod

**Network Volume 建议**（已作废，见阶段 1.1）
- 区域选 **US-KS-2**：这是唯一同时列出 A100 SXM、A100 PCIe、H100（SXM / PCIe / NVL）的数据中心，A100 没货时回退到 H100 的空间最大。备选 US-MO-1
- 大小建议 **200 GB**，约 $14/月（按 $0.07/GB/月）。估算：GEN3C 权重和依赖模型 60–80 GB，三套 conda 环境约 35 GB，MASt3R 和 ViPE 权重约 5 GB，Panoptic 的 1–2 个序列（只下载选中的机位）10–20 GB，训练产出约 20 GB。以后可以扩容，但不能缩小

**遇到的问题**
- runpodctl 2.15 不提供 GPU 利用率字段，所以空闲检测改用 SSH
- `runpodctl ssh info` 的输出格式和 pod 的部分字段要等有了真实 pod 才能确认，第一次开 pod 时验证

**耗时**：约 30 分钟
**花费**：$0（只做了只读查询和 dry-run）

---

## 阶段 1.1：改用 Global volume（2026-10-13）

**决定（Joey）**：不用 US-KS-2 的 Network volume，改用 Global volume `atlas_data`。原因是 pod 停掉后同机房经常没货，而 Global volume 不绑机房。

**做了什么**
- 核实了官方文档（Global volume 还是 beta）：
  - 底层是对象存储，没有文件锁和原子 rename，最终一致
  - 默认挂在 `/workspace`，同时挂 network volume 时变成 `/workspace-global`
  - **只有 REST API v2 能挂载**，runpodctl 2.15 不支持
  - 账户余额归零 15 天后 volume 会被删除
- 护栏改成以 RunPod API v2 为主，runpodctl 作为兜底：
  - `gpu-up` 不再锁区域，在所有区域找 A100（都没货或都开不出来才用 H100），挂载 Global volume，路径统一用变量 `$ATLAS_VOL`
  - 容器盘默认 200 GB（`$ATLAS_LOCAL=/opt/atlas`），`.atlas_busy` 标记移到容器盘
  - 巡检的空闲检测改为 API 利用率为主、SSH 复核；stop 前先执行 `pre_stop`，把结果同步回 Global volume
  - API 故障时，巡检退回 runpodctl，照样按时长 stop
  - 新增服务器专用的 pod SSH key，通过 `PUBLIC_KEY` 注入 pod
- 存储分层和 conda 环境方案对比（A Docker 镜像 / B 每次安装 / C 装一次后 conda-pack 打包）写在 [docs/storage-and-env.md](docs/storage-and-env.md)，**采用 C**
- 测试扩充到 50 个用例，全部通过；修了一个 bug：`--gpu h100` 时原来没有检查 A100 的库存

**遇到的问题**
- runpodctl 不支持 Global volume，需要 Joey 另建一个限权 API key（见 guard/README.md）

**耗时**：约 45 分钟
**花费**：$0

---

## 阶段 2：pod 环境脚本（2026-10-13）

**做了什么**
- 核实了三个官方仓库，固定到具体 commit：GEN3C `db2ffe1`、ViPE `8c9f361`、4C4D `ed6a3cb`（确认是 CVPR 2026 论文作者本人的仓库）、MASt3R `f5209af`
- `pod/setup_gen3c.sh`、`setup_vipe.sh`、`setup_4c4d.sh`：幂等，按方案 C 工作（装一次 → conda-pack 打包到 Global volume → 以后开机解压）。每次运行都记录耗时、环境大小、权重大小
- `runner/atlas_run.py`（`atlas-run`）：服务器把脚本和任务输入 rsync 到 pod → 后台执行 → 轮询进度 → 只把小文件（≤20 MB 的预览、指标）拉回服务器，并算出本次 GPU 花费。HF token 只通过 SSH stdin 传
- `pod/run_task.sh`：busy 心跳、峰值显存采样、结束时把 `out/` 同步回 Global volume；`pre_stop.sh`：被巡检 stop 前抢救未完成任务的结果
- `tasks/selftest.sh`：第一次开 pod 时实测 Global volume 的读写速度，方案 C 的时间估算要靠这个数据验证
- 本机测试 `runner/tests/run_tests.sh`：20 个用例全部通过（执行器全流程、同步、拉回大小限制、失败任务、pre_stop、环境打包和恢复）；护栏测试 50 个，仍然全部通过

**技术选择**
- 4C4D 环境：官方 py3.7 / torch 1.12 / CUDA 11.6 不支持 H100 → 升级到 py3.10 / torch 2.4 / CUDA 12.4，重新编译 4 个 CUDA 扩展（采用）；出问题时退回官方环境（只能用 A100）
- MASt3R 预处理：官方走 MAtCha（editable 安装加写死的 CUDA 11.8 路径，没法打包）→ 直接装 naver/mast3r，阶段 3 自己写"已知位姿 → 点云"脚本（采用）

**遇到的问题和发现**
- GEN3C 官方权重脚本会下载约 121 GB，其中 45 GB 是用不到的 T5 TF 权重 → 改为只下载需要的约 76 GB
- **GEN3C 和 4C4D 的命令行都只有预设运镜**，阶段 5 的自定义关键帧需要改代码（两边都可以改，4C4D 更简单）
- **许可**：4C4D 和 MASt3R 都是非商业许可；GEN3C 权重允许商用，但模型卡写明"关闭安全护栏会终止许可"，而官方推理脚本强制关闭了护栏。第一阶段是研究验证，不影响；商用前必须解决
- 所有 setup 脚本都还没有在真实 pod 上验证

**耗时**：约 1 小时
**花费**：$0

---

## 阶段 2.1：volume ID 与余额检查（2026-10-13）

- Global volume 改为只按配置里的 ID 识别。名字含斜杠，只用于显示；同名的其他 volume 不会被误用（有测试覆盖）
- gpu-up 开 pod 前检查余额：< $15 或读不到就拒绝；跑满时长后会低于 $15 时给出提醒；gpu-status 显示余额
- 护栏测试 61 个、执行器测试 20 个，全部通过
- 真实账号 dry-run：两种 A100 都有货（SXM $1.79/h，3 个区域；PCIe 2 个区域）。**但服务器上没有找到 API key 文件，余额也仍是 $7.07**，所以 v2 API 这条路还没验证，见汇报

**花费**：$0

---

## 阶段 2.2：第一次开 pod，安装并打包三套环境（2026-10-13 23:25 – 2026-10-14 01:27 UTC）

**结果**：全部成功，pod 已 stop。Global volume 上现在有：

| 内容 | 大小 |
|---|---|
| 环境包 `envpacks/`：gen3c 5.0 GB、vipe 4.8 GB、4c4d 5.2 GB、mast3r 2.4 GB | 17.4 GB |
| 权重 `weights/`：gen3c 71 GB、vipe 4.0 GB、mast3r 2.6 GB | 77.6 GB |

**实测数据**（A100 SXM 80GB，$1.79/h）

| 项目 | 实测 |
|---|---|
| Global volume 顺序写 / 读 | 164 MB/s / **75 MB/s**（读取偏慢：GEN3C 71 GB 权重全部读一遍约 16 分钟） |
| Global volume 小文件 | 350 个/秒 |
| 容器盘读 | 10 GB/s |
| GEN3C 安装 | 环境 19 分钟 + 权重 5 分钟 |
| ViPE 安装 | 25 分钟（其中测试视频 18 分钟） |
| 4C4D + MASt3R 安装 | 13 分钟 |
| 打包一个环境 | 44–80 秒 |

**花费**：pod 运行 2h03m，GPU 按 $1.79/h 计约 $3.66；账户余额从 $17.07 降到 $13.10，**实际花费 $3.97**（含容器盘和存储）。
如果第一次就全部顺利，大约只需要 1 小时、约 $1.8。多花的时间来自下面这些问题，每次都是查清原因、修改脚本后重装。

**遇到的问题（都已修复，写进了脚本注释和 pod/README.md）**
1. GEN3C：MoGe 把 huggingface-hub 升到 2.x，transformers 无法 import；官方自检脚本出错时仍返回 0，差点把坏环境打包 → 重新固定版本，自检改为严格模式
2. GEN3C：apex master 用了 torch 2.6 不支持的 `list[int]` 注解，24.10 又太旧编译不过 → 固定到 apex **25.09**
3. ViPE、4C4D：conda-forge 混进 CUDA 13.x → 固定 `cuda-version`，并在创建环境后检查 nvcc 版本
4. 4C4D：g++ 找不到 CUDA 头文件 → 把头文件目录加进 CPATH
5. Global volume 不能设置文件时间 → 同步时不用 `rsync -t`
6. 护栏：API 偶尔不返回 SSH 信息，巡检差点把正在装环境的 pod 误判为空闲 → 缓存上一次拿到的 SSH 命令（新增测试，共 62 个）
7. atlas-run：Python 输出被缓冲，跟踪不到进度 → 改为逐行输出；启动任务的 SSH 有时不返回（任务本身已正常启动）→ 加了 setsid 和超时，根本原因待查

**待办**
- 下次开 pod 时实测"从环境包恢复"的耗时（预计每个环境 2–3 分钟）
- ViPE 的 GeoCalib 太慢：Panoptic 有相机标定，可以直接用已知内参跳过这一步

---

## 阶段 3：CMU Panoptic 数据脚本（2026-10-14）

**做了什么**
- 确认 CMU 服务器仍可访问，并且支持 HTTP 范围请求。从服务器实测只有约 3.5 MB/s，所以每个机位只截取需要的 10 秒，不下载整段视频（单个机位最大 3 GB）
- 选了两段：`171204_pose1`（单人大动作）和 `160906_band1`（三人演奏，像活动现场）。用标定算出 31 台相机的位置，再看缩略图判断人的朝向，选出 4 个输入机位和 1 个评估机位，选择依据见 [docs/stage3-data.md](docs/stage3-data.md)
- `pod/tools/panoptic_prepare.py`：截取 → 去畸变 → COLMAP（厘米换成米）→ GEN3C 输入视频 → meta.json
- `pod/tools/mast3r_posed_points.py`：MASt3R 稠密匹配 + 已知位姿三角化，生成 4C4D 初始点云（代替官方用的 MAtCha）
- `pod/tasks/panoptic.sh`：从环境包恢复环境（顺便实测恢复耗时）→ 逐个序列处理 → ViPE → 打包存回 Global volume

**在服务器上的验证**（用真实下载的 5 帧，服务器不跑模型）
- 整个准备流程跑通
- 世界原点的竖线投影到 5 个机位都落在人身上，证明坐标和单位换算正确
- 三角化合成测试：误差中位数小于 1 mm

**遇到的问题**
- bash 的 `( set -e; … ) || …` 写法里 set -e 不生效，任务出错也不会停 → 改成放到后台再 `wait`
- ViPE 命令行不能直接使用已知内参（每次都先跑 GeoCalib）→ 保持官方默认流程，记录 ViPE 估计的焦距和真实标定的误差，作为"单台手机、没有标定"情况下的参考

**耗时**：约 1 小时　**花费**：$0

