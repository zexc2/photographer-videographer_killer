# 进度记录

每个阶段记录：做了什么 / 遇到的问题 / 耗时 / 花费。

| 阶段 | 日期 | 耗时 | GPU 花费 |
|---|---|---|---|
| 0 仓库初始化 | 2026-10-13 | ~10 分钟 | $0 |
| 1 GPU 护栏 | 2026-10-13 | ~30 分钟 | $0 |
| 1.1 改用 Global volume | 2026-10-13 | ~45 分钟 | $0 |
| 2 pod 环境脚本 | 2026-10-13 | ~1 小时 | $0（尚未开 pod） |

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

