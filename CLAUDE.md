# 项目简报：360/多机位 → AI 运镜视频（photographer-videographer_killer）

第一件事：把这整份简报原样保存为 ~/app/CLAUDE.md，之后所有会话都以它为准。我（Joey）后续的新决定，你更新到 CLAUDE.md 里对应的位置。

## 1. 项目目标
我是 ERA MOMENT 的创始人，公司做活动摄影摄像。想验证：活动现场用 1 台 360 相机（Insta360 X3）+ 几台三脚架上固定机位的手机拍摄，事后用 AI 重建场景，生成"游机位"运镜视频，替代或补充现场摄像师。
- 成本基准：美国 8 小时活动摄像师报价很少低于 $1500，所以整套 AI 方案每场活动的 GPU 成本必须明显低于这个数，才有商业意义。
- 第一阶段（现在）：用公开多机位数据 CMU Panoptic，分别在 GEN3C 和 4C4D 上各跑一次，对比效果、耗时、成本。最终输出必须有运镜（相机移动）。
- 第二阶段：我在可控环境下自己当被拍对象，用 Insta360 + 手机实拍测试（我目前还没买 Insta360）。
- 这也是我 USF BUS 205 课程的项目，所以 README 和进度记录要能给老师看。

## 2. 环境
- 服务器：DigitalOcean 老服务器，2 核 4G 内存，数据盘只剩约 20G。你运行在 Linux 用户 atlas 下，没有 sudo。
- 这台服务器上还跑着公司官网（Caddy）和其他用户（erastandard、pipeline）的服务。绝对不要碰 /home/atlas 以外的东西、系统服务、Caddy 配置、端口 80/443。
- 服务器只负责：前端、调度、存小文件。所有重计算（3D 重建、训练、生成）都在 RunPod GPU pod 上完成。大文件（数据集、权重、conda 环境、训练产出）放在 RunPod Network Volume（/workspace），不要放服务器上。
- runpodctl 2.15 在 ~/.local/bin，key 在 ~/.runpod/（不要读取、打印、复制 key）。2.15 的命令格式是 `runpodctl pod list/create/stop`，参数先用 --help 确认。
- 同一个 RunPod 账号下还有别的项目的 pod（例如 eramoment_find_attendee_tool），只能操作 atlas- 前缀的 pod。
- GitHub 公开仓库：zexc2/photographer-videographer_killer，remote 用 git@github-pvk:zexc2/photographer-videographer_killer.git（~/.ssh/config 已配置）。

## 3. 工作阶段（按顺序做，每阶段结束 commit + push，并向我汇报）

### 阶段 0：仓库初始化
- ~/app 初始化 git，main 分支，先写 .gitignore 再做第一次提交
- 建好 README.md 骨架和 PROGRESS.md（每阶段记录：做了什么、遇到的问题、耗时、花费）

### 阶段 1：GPU 护栏（全部用 dry-run 验证，不真开 pod）
- ~/bin/gpu-up：所有开 pod 都必须走它。白名单：A100 80GB（PCIe/SXM），无货时允许 H100，其他型号拒绝；名字强制 atlas- 前缀；同时最多 1 个；挂载 Network Volume atlas_data 到 /workspace；参数 --gpu --image --hours（默认 6，最大 6）；成功后输出 pod ID 和 SSH 命令
- ~/bin/gpu-down：只能 stop/remove atlas- 前缀的 pod
- ~/bin/gpu-status：列出 atlas- pod、已运行时长、估算花费、距自动关机的时间
- 巡检脚本 + atlas 的 crontab（每 10 分钟）：超过设定时长 → stop；GPU 利用率连续 30 分钟为 0 → stop（优先读 RunPod API，读不到再 SSH 跑 nvidia-smi，选可靠方案并说明理由）；日志写 ~/logs/gpu-guard.log
- 告诉我 Network Volume 应该建在哪个区域（要有 A100 80GB 货源），建议大小

### 阶段 2：pod 环境脚本（幂等，装在 /workspace，下次开 pod 直接复用）
- setup_gen3c.sh：GEN3C（github nv-tlabs/GEN3C，权重 nvidia/GEN3C-Cosmos-7B）
- setup_vipe.sh：ViPE 单独一套环境（官方说明它和 GEN3C 环境不兼容）
- setup_4c4d.sh：4C4D（CVPR 2026，先核实官方仓库，候选 yangzf-1023/4C4D），包括 MASt3R 预处理
- 每个脚本记录安装耗时、占用磁盘空间、需要的 token 或许可
- 写一个通用任务执行器：服务器把任务配置 rsync 到 pod → 在 pod 上执行 → 把小预览和指标拉回服务器

### 阶段 3：CMU Panoptic 数据
- 挑 1–2 个合适的序列（有人物动作、场景不太大），在 pod 上直接下载到 /workspace，不经过服务器
- 准备两种输入：GEN3C（单目视频 + ViPE 位姿，或多视角输入，看官方支持哪种）；4C4D（只选 4 个机位，模拟我们的稀疏机位设置，生成 COLMAP 格式）
- 留出 1 个没参与训练的机位做评估

### 阶段 4：基线测试
- GEN3C 和 4C4D 各跑一次，各自输出一段带运镜的视频
- 记录：环境准备时间、运行时间、峰值显存、GPU 花费、留出机位的 PSNR/SSIM/LPIPS（能算的就算），以及主观问题（人物是否变形、闪烁、穿帮）
- 写一份对比报告放进仓库，并换算成"一场 8 小时活动大约要多少钱"，和 $1500 对比

### 阶段 5：前端（Flask，跑在服务器上）
- 只监听 127.0.0.1，我通过 SSH 隧道访问，不开公网端口、不改 Caddy
- 流程：上传素材（单机位或多机位，有大小上限，上传完转存到 Network Volume 后删除服务器上的副本），或者选内置测试素材（Panoptic）→ 先生成 3D 预览（浏览器内的点云/高斯查看器）→ 我自己设置运镜：起点、路径、终点（关键帧控制相机的位置、朝向、视角），以及用源素材的第几秒到第几秒 → 我点确认后才开始生成 → 显示进度、花费 → 预览和下载结果
- 选择模型：GEN3C / 4C4D

### 阶段 6：接入我的 360 工具
- 我有一个把 360 素材转成普通透视视频、并给每个视角标注空间坐标（相机位置/朝向）的工具，我会提供代码。把它接到前端的上传流程里，让 360 素材可以作为输入

## 4. 规则
- 每一次真正开 pod 之前都先问我，告诉我：用什么 GPU、预计跑多久、预计花多少钱。我回复"开"你才开。用完立刻关掉。
- 仓库是公开的，绝对不能提交：API key、SSH key、token、.env、~/.runpod、模型权重（*.pt *.pth *.safetensors *.ckpt）、数据集、视频、点云、训练产出、日志、pod ID、IP。单个文件不超过 10MB。每次 commit 前用 git diff --cached 自查。
- 第三方模型和数据只写链接和许可说明，不要搬运它们的内容。在 README 里注明 CMU Panoptic、GEN3C（NVIDIA Open Model License）、4C4D 各自的许可。
- commit message 用中文。
- 服务器内存只有 4G：不在服务器上跑任何模型，不在服务器上解压大文件。
- 遇到需要我做的事（见第 5 节），停下来明确告诉我要做什么、去哪里做，不要绕过去。
- 不确定的技术选择，给我 2 个方案和你的推荐，然后按推荐继续往下做，不用每次都等我。

## 5. 需要我（Joey）提供或去做的事
1. 给 RunPod 账户充值（每次开 pod 前你提醒我检查余额）
2. 按你建议的区域和大小，在 RunPod 后台建 Network Volume atlas_data，然后把 volume ID 给你
3. 如果需要 Hugging Face token、或者需要在 Hugging Face 上同意模型许可，告诉我具体是哪个模型、哪个页面，由我去操作。token 由我放到 RunPod Secrets 或服务器上的文件里，你不要让我把 token 贴在对话中
4. 批准每一次开 pod
5. 提供我的 360 转透视工具代码（阶段 6）
6. 第二阶段的实拍素材（等我买了 Insta360 以后）
7. 审阅对比报告，决定下一步是 GEN3C、4C4D 还是等 Atlas

## 6. 汇报格式
每个阶段结束时给我一段简短的汇报：完成了什么 / 花了多少钱 / 遇到什么问题 / 下一步需要我做什么（如果有）。

现在从阶段 0 开始。
