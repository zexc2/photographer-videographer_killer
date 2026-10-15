# 部署指南：从零跑起运镜工作台

照着下面 6 步做完，就能在浏览器里选机位、设运镜、让 GPU 渲染出视频。总共需要：

- 大约 **30 分钟**的配置时间；
- 一次性 **约 4 小时、约 $8** 的 GPU 时间，用来准备样例模型；
- 之后每次渲染 **约 1.5 分钟、约 $0.05**。

```
你的电脑（浏览器）──SSH 隧道──▶ 你的 Linux 服务器（前端 + 调度，不需要 GPU）──API──▶ RunPod GPU（A100 80GB）
                                                                                    └─ Global volume（存模型、环境、结果）
```

> **许可提醒**：样例用的 4C4D、MASt3R 和 N3V 数据集都**只允许非商业研究**，详见 [README 的许可表](README.md#第三方模型与数据许可)。

---

## 0. 需要准备什么

| 东西 | 要求 |
|---|---|
| 一台 Linux 机器 | 跑前端和调度用，不需要 GPU，2 核 4G 就够；云服务器或自己的 Linux 电脑都行。需要 Python ≥ 3.9、rsync、ssh、cron，**不需要 sudo**（缺软件时才需要装） |
| RunPod 账号 | 下一步注册，需要信用卡充值 |
| 你自己的电脑 | 有浏览器、能 SSH 到上面那台 Linux 机器 |

---

## 1. 注册 RunPod 并充值

1. 打开 **https://www.runpod.io/** → 点 **Sign Up**，注册账号（可以用 Google 或 GitHub 登录）。
2. 左侧菜单 **Billing** → 充值。**建议第一次充 $25**：样例模型约 $8，剩下的够渲染几百次。
3. 在 Billing 页面**打开低余额邮件提醒**（比如低于 $10 就提醒）。

> ⚠️ **余额一定不要归零。** RunPod 规定，余额归零一段时间后（目前是 15 天），存储卷会被**永久删除**，训练好的模型和环境就没了。
> 本项目的护栏也会在**余额低于 $3 时拒绝开 GPU**。

### 推荐机型：A100 80GB

本项目只用 **NVIDIA A100 80GB**，选 **Secure Cloud**：

| 型号 | 参考价格 | 说明 |
|---|---|---|
| **A100 SXM 80GB**（首选） | 约 $1.79/小时 | 我们所有实测数据都在这款上 |
| A100 PCIe 80GB | 约 $1.64/小时 | 同样可用 |
| H100（SXM / PCIe / NVL） | 约 $2.9–4.0/小时 | **只在所有 A100 都没货时**才用 |

为什么是 80GB：4C4D 训练的峰值显存是 24.6 GB，GEN3C 是 43.6 GB；24 GB、48 GB 的卡要么装不下，要么没有余量。

**你不需要在 RunPod 网页上手动选卡或开 pod。** 部署好以后，所有开 GPU 的操作都经过本项目的 `gpu-up`，它只允许上面这几种型号，会自动在所有区域找有货的 A100，找不到才用 H100。如果你在网页上手动开 pod，也请选 A100 80GB，并且名字**不要**以 `atlas-` 开头，免得被护栏当成自己的 pod 关掉。

价格以 RunPod 页面为准。

---

## 2. 在 RunPod 上建存储卷和 API key

### 2.1 Global volume（存储卷）

1. 左侧菜单 **Storage** → 新建一个 **Global volume**：
   - 名字随意，比如 `atlas_data`；
   - Location 选 **Any**，即不绑定机房，哪个机房有 A100 都能挂载；
   - 大小建议 **50 GB**。样例实际用量约 20 GB：4C4D 环境包、数据和模型。
2. 建好后**复制它的 ID**，第 4 步要用。

### 2.2 两个 API key

左侧菜单 **Settings → API Keys → Create API Key**，建两个：

| 用途 | 权限 | 保存位置 |
|---|---|---|
| 护栏开 / 关 pod | **Restricted**，只勾选 `pods:read`、`pods:write`、`volumes:read`、`catalog:read` | `~/.config/atlas-gpu/runpod_api_key`（第 4 步） |
| runpodctl 读账户余额 | **Read Only** | `runpodctl config` 里（第 3 步） |

如果后台显示的权限名称和这里不一样，就按含义选：Pods 读写，Volumes 只读，Catalog 只读，其余全部不勾。

> 🔒 key 只保存在你自己的服务器上，**不要**提交到 git、贴到聊天或 issue 里。仓库的 `.gitignore` 已经排除了常见的密钥文件名。

---

## 3. 准备服务器

用 SSH 登录你的 Linux 服务器：

```bash
git clone https://github.com/zexc2/photographer-videographer_killer.git ~/app
cd ~/app
```

安装 **runpodctl 2.x**：从 https://github.com/runpod/runpodctl/releases 下载 Linux 版本（一般是 `linux-amd64`），放到 `~/.local/bin/runpodctl`，然后：

```bash
chmod +x ~/.local/bin/runpodctl
runpodctl config --apiKey <第 2.2 步的 Read Only key>
runpodctl user          # 能看到账户信息就说明配置好了
```

---

## 4. 一键部署

```bash
cd ~/app
./deploy.sh
```

它会依次：

- 检查系统工具；
- 安装 Python 依赖（系统 Python 缺 Flask 时，会在仓库里建 `.venv`）；
- 安装 GPU 护栏命令（`~/bin/gpu-up` 等）和每 10 分钟一次的巡检；
- 启动运镜工作台，并设成开机自动启动；
- 最后列出还没完成的事项。

第一次运行时，下面两项一般还没就绪，按提示补上：

```bash
# ① 护栏用的 Restricted key（用编辑器粘贴，不要用 echo，免得留在命令历史里）
install -m 600 /dev/null ~/.config/atlas-gpu/runpod_api_key
nano ~/.config/atlas-gpu/runpod_api_key

# ② Global volume 的 ID：把 global_volume_id 填成第 2.1 步复制的 ID
nano ~/.config/atlas-gpu/config.json
```

如果提示 `~/bin` 不在 PATH 里，按提示加上。然后**再运行一次 `./deploy.sh`**，直到每项都是 ✅。

最后做一次只读检查，不会真开 pod：

```bash
gpu-up --dry-run        # 能看到选中的 A100、价格、余额、Global volume
gpu-status              # 列出 atlas- pod（现在应该是空的）和余额
```

---

## 5. 准备样例模型（一次性，约 4 小时、约 $8）

工作台需要一个训练好的 4C4D 模型。下面这一步会在 GPU 上依次完成：

1. 安装 4C4D 环境，并打包存到 Global volume，以后开机直接解压；
2. 下载 N3V `flame_steak` 数据；
3. 用 4 个机位训练模型；
4. 把模型放到 Global volume 的 `models/n3v_flame_steak/`。

```bash
gpu-up --hours 6                 # 开一台 A100（最多 6 小时，到点护栏自动关）
atlas-run --detach n3v_repro     # 推送任务并在后台运行
gpu-status                       # 随时查看运行时长和花费
```

任务跑完 5 分钟后，护栏会**自动关闭 pod**，不需要守着。想看进度，可以用 `atlas-run --list` 找到 run ID，再用 `atlas-run --attach <run_id>`。

完成后，服务器上的 `~/runs/<run_id>/out/` 里有训练指标和官方运镜视频，可以和[我们的复现结果](docs/reproduction-n3v.md)对比（PSNR 约 24）。

---

## 6. 打开工作台

在**你自己的电脑**上：

```bash
ssh -L 8765:127.0.0.1:8765 <用户名>@<服务器地址>
```

保持这个窗口开着，浏览器打开 **http://localhost:8765**。

如果工作台就跑在你自己的 Linux 电脑上，不需要隧道，直接打开这个地址就行。

1. 第一次打开：点 **"准备场景预览"**。会开一次 GPU，导出点云、机位和缩略图，约 1.5 分钟、$0.04。
2. 选一个固定机位，加关键帧设置运镜，用"镜头视角"预览。
3. 点 **"确认生成"**。弹窗会显示 GPU、预计时间和花费，确认后约 1.5 分钟出视频。

详细用法见 [frontend/README.md](frontend/README.md)。

---

## 花费一览（A100 SXM，2026-10 实测）

| 操作 | 时间 | 花费 |
|---|---|---|
| 准备样例模型（一次性） | 约 4 小时 | 约 $8 |
| 准备场景预览（每个场景一次） | 约 1.5 分钟 | 约 $0.04 |
| 渲染一段 4 秒运镜 | 约 1.5 分钟 | 约 $0.05 |
| Global volume 存储 | 持续 | 按 RunPod 存储价格，50 GB 每月几美元 |

## 防止烧钱的护栏（自动生效）

- 只允许 A100 80GB，没货才用 H100；名字必须以 `atlas-` 开头，同时最多 1 个。
- 每个 pod 有时长上限（默认、也是最多 6 小时，工作台的任务是 30 分钟），到点自动关。
- GPU 连续 30 分钟空闲就自动关；任务结束 5 分钟后自动关。
- 余额低于 $3 时拒绝开 pod。
- 只会操作 `atlas-` 开头的 pod，不会碰你账号下其他项目的 pod。

详见 [guard/README.md](guard/README.md)。

## 常见问题

| 现象 | 处理 |
|---|---|
| `gpu-up` 说没有货 | A100 偶尔全区缺货，护栏会自动尝试 H100；都没有就过几分钟再试 |
| `gpu-up` 说余额不足或读不到余额 | 去 Billing 充值；检查 `runpodctl user` 是否正常 |
| 页面任务失败，提示"还没有模型" | 先完成第 5 步 |
| 怀疑有 pod 没关 | `gpu-status` 查看，`gpu-down --all` 全部停掉 |
| 页面打不开 | 运行 `./deploy.sh --check`；看 `~/logs/frontend.log`；确认 SSH 隧道还开着 |
| 护栏日志 | `~/logs/gpu-guard.log` |
