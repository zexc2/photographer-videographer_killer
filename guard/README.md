# GPU 护栏（阶段 1）

所有 RunPod pod 的开、关、查看、自动巡检都走这里，目的是**不让 GPU 白烧钱**，也**不碰别的项目的 pod**。

| 命令 | 作用 |
|---|---|
| `gpu-up [--gpu auto] [--image IMG] [--hours 6] [--name xxx] [--dry-run]` | 开 pod |
| `gpu-down <名字或ID> [--delete] [--dry-run]` / `gpu-down --all` | stop（或 delete）atlas- pod |
| `gpu-status [-v]` | 列出 atlas- pod：已运行时长、估算花费、距自动关机时间、空闲时长 |
| `gpu-guard [--dry-run] [-v]` | 巡检，由 cron 每 10 分钟执行 |

四个命令都是 `atlas_gpu.py` 的软链接，用 `install.sh` 安装（幂等）。

## 规则（写死在代码里）

- **GPU 白名单**：A100 80GB（SXM / PCIe）。只有两种 A100 在 Network Volume 所在数据中心**都没货**时，才允许 H100（SXM / PCIe / NVL）。其他型号一律拒绝。
- **名字**：强制 `atlas-` 前缀；gpu-down / gpu-guard 只会操作 `atlas-` 前缀的 pod，同账号下其他项目的 pod 不受影响（有测试覆盖）。
- **数量**：同时最多 1 个运行中的 atlas- pod。
- **存储**：只用 Secure Cloud，固定挂载 Network Volume `atlas_data` 到 `/workspace`，pod 建在 Volume 所在的数据中心。
- **时长**：`--hours` 默认 6，最大 6；到点由巡检 stop。
- **Key**：脚本只调用 `runpodctl`，由它自己读取 API key，脚本本身从不读取 key。

## 巡检逻辑（gpu-guard）

1. **超时**：本次运行时长 ≥ 设定上限 → stop。这一步只依赖 RunPod API，不需要 SSH，所以最坏情况下 pod 也只会跑满 6 小时。
2. **空闲**：SSH 到 pod 执行 `nvidia-smi`，5 秒内采样 5 次，取最大值。
   - 利用率 > 0 → 空闲计时清零。
   - 利用率 = 0 → 开始或继续计时，**连续 ≥ 30 分钟** → stop。
   - pod 上的 `/workspace/.atlas_busy` 在 15 分钟内被 touch 过 → 不算空闲。下载数据、装环境、CPU 预处理时 GPU 利用率本来就是 0，由任务执行器（阶段 2）负责定期 touch 这个文件。
   - SSH 连不上：开机后 30 分钟内不计入空闲（镜像拉取、启动阶段）；超过 30 分钟仍连不上，按空闲计时。连不上的 pod 一样在花钱。
3. 没有通过 gpu-up 开的 atlas- pod 同样受管，按 6 小时上限处理。
4. 日志写到 `~/logs/gpu-guard.log`。

### 为什么用 SSH + nvidia-smi，而不是 RunPod API

| 方案 | 优点 | 问题 |
|---|---|---|
| A. RunPod API（GraphQL 的 `gpuUtilPercent`） | 不需要 SSH | runpodctl 2.15 不提供这个字段，要直接调 API 就得让脚本自己读 API key，违反"不读取 key"的规则；而且这个遥测是平台采样的，有时为空或滞后 |
| **B. SSH 执行 nvidia-smi（采用）** | 读的是 GPU 驱动的真实数据；SSH 命令由 `runpodctl ssh info` 提供，不碰 key | 依赖 SSH 可达，上面已处理连不上的情况 |

API 只用来查 pod 列表、运行时长和价格，这些 runpodctl 都能直接提供。

## 测试

```bash
guard/tests/run_tests.sh     # 用假的 runpodctl / ssh 离线测试，不会访问真实账号
gpu-up --dry-run             # 真实账号上只读预览（查库存、价格、Volume），不开 pod
```

**尚未在真实 pod 上验证的部分**（第一次真开 pod 时检查）：`runpodctl ssh info` 的实际输出格式，以及 pod JSON 里 `lastStartedAt`、`costPerHr` 这几个字段。代码对字段缺失做了兜底：取不到开机时间就用 gpu-up 记录的时间。

## 运行时文件（都不在仓库里）

- `~/.local/state/atlas-gpu/<podID>.json`：时长上限、空闲计时
- `~/.config/atlas-gpu/config.json`（可选）：`{"network_volume_id": "..."}`。不写时按名字 `atlas_data` 自动查找
- `~/logs/gpu-guard.log`、`~/logs/gpu-guard.cron.log`
