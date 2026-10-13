# GPU 护栏（阶段 1）

所有 RunPod pod 的开、关、查看、自动巡检都走这里，目的是**不让 GPU 白烧钱**，也**不碰别的项目的 pod**。

| 命令 | 作用 |
|---|---|
| `gpu-up [--gpu auto] [--image IMG] [--hours 6] [--name xxx] [--disk 200] [--dry-run]` | 开 pod |
| `gpu-down <名字或ID> [--delete] [--dry-run]` / `gpu-down --all` | stop（或 delete）atlas- pod |
| `gpu-status [-v]` | 列出 atlas- pod：已运行时长、估算花费、距自动关机时间、GPU 利用率、空闲时长；`-v` 显示 SSH 命令 |
| `gpu-guard [--dry-run] [-v]` | 巡检，由 cron 每 10 分钟执行 |

四个命令都是 `atlas_gpu.py` 的软链接，用 `install.sh` 安装（幂等）。安装时还会生成服务器专用的 SSH key `~/.ssh/atlas_runpod`。

## 规则（写死在代码里）

- **GPU 白名单**：A100 80GB（SXM / PCIe）。只有两种 A100 在**所有区域**都没货，或者都开不出来时，才允许 H100（SXM / PCIe / NVL）。其他型号一律拒绝。
- **不锁区域**：创建 pod 时不指定数据中心，由 RunPod 在任何有货的区域调度。只用 Secure Cloud。
- **名字**：强制 `atlas-` 前缀；gpu-down / gpu-guard 只会操作 `atlas-` 前缀的 pod（有测试覆盖）。
- **数量**：同时最多 1 个处于计费状态的 atlas- pod（PROVISIONING / STARTING / RUNNING 都算）。
- **存储**：挂载 Global volume `atlas_data`。pod 内的路径由变量 `ATLAS_VOL_MOUNT` 决定（默认 `/workspace`），并作为环境变量 `$ATLAS_VOL` 传进 pod；容器盘根目录是 `$ATLAS_LOCAL=/opt/atlas`，默认 200 GB。详见 [docs/storage-and-env.md](../docs/storage-and-env.md)。
- **时长**：`--hours` 默认 6，最大 6；到点由巡检 stop。
- **余额**：开 pod 前用 `runpodctl user` 读取账户余额（v2 API 没有余额接口）；**余额 < $15 或读不到时拒绝开 pod**，因为余额归零 15 天后 Global volume 会被永久删除。跑满时长后余额会低于 $15 时，会给出提醒。`gpu-status` 也会显示余额。

## API key

Global volume 只能通过 **RunPod REST API v2** 挂载，runpodctl 2.15 不支持。所以护栏使用一个**单独的限权 key**：

1. RunPod 后台 → Settings → API Keys → Create API Key，名字建议 `atlas-gpu-guard`，权限选 **Restricted**，只勾选：
   - `pods:read`、`pods:write`（列出、开、停 pod）
   - `volumes:read`（按名字找 Global volume）
   - `catalog:read`（查 GPU 库存和价格）

   如果后台显示的权限名称和这里不一样，就按含义选 Pods 读写、Volumes 只读、Catalog 只读，其余全部不勾。
2. 在服务器上**自己**保存（不要贴到对话里）：
   ```bash
   install -m 600 /dev/null ~/.config/atlas-gpu/runpod_api_key
   nano ~/.config/atlas-gpu/runpod_api_key   # 粘贴 key，保存
   ```

脚本只在发请求时读取这个文件，从不打印、也不写进日志；文件权限宽于 600 时拒绝使用。`~/.runpod` 里 runpodctl 的 key 本脚本不碰。

没有这个 key 时：
- `gpu-up --dry-run` 仍可预览（库存来自 runpodctl）
- 真开 pod 会被拒绝
- 巡检退回 runpodctl，仍然能列出 pod 并按时长 stop

## 巡检逻辑（gpu-guard）

1. **超时**：本次运行时长 ≥ 设定上限 → stop。这一步只依赖 pod 列表，不需要 SSH；API 故障时自动改用 runpodctl。所以最坏情况下 pod 也只会跑满 6 小时。
2. **空闲**：
   - 先看 API v2 返回的 GPU 实时利用率 `runtime.gpus[].util`。大于 0 就算忙，不用 SSH。
   - 为 0 或拿不到时，再 SSH 到 pod 复核：`nvidia-smi` 在 5 秒内采样 5 次取最大值，同时检查 `$ATLAS_LOCAL/.atlas_busy`。这个标记 15 分钟内被 touch 过就不算空闲，因为下载数据、解压环境、CPU 预处理时 GPU 利用率本来就是 0。
   - **连续 ≥ 30 分钟**为 0 → stop。
   - 什么都测不到时：开机后 30 分钟内不计入空闲；之后按空闲计时（连不上的 pod 一样在花钱）。
3. **stop 前先同步**：容器盘在 stop 后会清空，所以 stop 前会尽力 SSH 执行一次 `$ATLAS_LOCAL/bin/pre_stop`（由阶段 2 的任务执行器提供，最多 10 分钟），把结果同步回 Global volume。失败了也照样 stop。
4. 没有通过 gpu-up 开的 atlas- pod 同样受管，按 6 小时上限处理。
5. 日志写到 `~/logs/gpu-guard.log`。

### 空闲检测方案

| 方案 | 说明 |
|---|---|
| A. 只用 RunPod API 的利用率 | 不需要 SSH；但这是单次快照，看不到 busy 标记 |
| B. 只用 SSH + nvidia-smi | 数据最直接；但依赖 SSH 可达 |
| **采用：A 为主，B 复核** | API 说"忙"时直接相信它（省一次 SSH）；API 说"空闲"或拿不到数据时，必须再经过 SSH 复核才会开始计空闲，避免一次快照误杀正在下载或预处理的任务 |

> 阶段 1 最初只用了 B，因为 runpodctl 拿不到利用率。改用 v2 API 以后就同时具备了两种手段。

## SSH

pod 的 SSH 公钥通过环境变量 `PUBLIC_KEY` 注入：包括本服务器专用的 `~/.ssh/atlas_runpod.pub`，加上 RunPod 账户里已登记的公钥（Joey 自己的电脑仍然能登录）。账户的 SSH key 设置不做任何改动。

## 测试

```bash
guard/tests/run_tests.sh   # 假 API v2 + 假 runpodctl / ssh，61 个用例，不访问真实账号
gpu-up --dry-run           # 真实账号上只读预览，不开 pod
```

**尚未在真实 pod 上验证的部分**（第一次真开 pod 时检查）：
- v2 API 实际返回的字段：`runtime.gpus[].util`、`ssh.direct.command`、`startedAt`、`cost`
- `PUBLIC_KEY` 注入是否生效
- 200 GB 容器盘在各区域是否都能开出来

## 运行时文件（都不在仓库里）

- `~/.config/atlas-gpu/runpod_api_key`（600）：限权 API key
- `~/.config/atlas-gpu/config.json`（**必需**）：`{"global_volume_id": "...", "global_volume_label": "..."}`。只按 ID 识别 Global volume，名字只用于显示
- `~/.ssh/atlas_runpod`：pod SSH 私钥
- `~/.local/state/atlas-gpu/<podID>.json`：时长上限、空闲计时
- `~/logs/gpu-guard.log`、`~/logs/gpu-guard.cron.log`
