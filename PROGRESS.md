# 进度记录

每个阶段记录：做了什么 / 遇到的问题 / 耗时 / 花费。

| 阶段 | 日期 | 耗时 | GPU 花费 |
|---|---|---|---|
| 0 仓库初始化 | 2026-10-13 | ~10 分钟 | $0 |
| 1 GPU 护栏 | 2026-10-13 | ~30 分钟 | $0 |

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

**Network Volume 建议**
- 区域选 **US-KS-2**：这是唯一同时列出 A100 SXM、A100 PCIe、H100（SXM / PCIe / NVL）的数据中心，A100 没货时回退到 H100 的空间最大。备选 US-MO-1
- 大小建议 **200 GB**，约 $14/月（按 $0.07/GB/月）。估算：GEN3C 权重和依赖模型 60–80 GB，三套 conda 环境约 35 GB，MASt3R 和 ViPE 权重约 5 GB，Panoptic 的 1–2 个序列（只下载选中的机位）10–20 GB，训练产出约 20 GB。以后可以扩容，但不能缩小

**遇到的问题**
- runpodctl 2.15 不提供 GPU 利用率字段，所以空闲检测改用 SSH
- `runpodctl ssh info` 的输出格式和 pod 的部分字段要等有了真实 pod 才能确认，第一次开 pod 时验证

**耗时**：约 30 分钟
**花费**：$0（只做了只读查询和 dry-run）
