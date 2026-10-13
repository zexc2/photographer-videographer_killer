# photographer-videographer_killer

> 360 / 多机位素材 → AI 场景重建 → "游机位"运镜视频
> ERA MOMENT 内部验证项目 · USF BUS 205 课程项目

## 项目背景

ERA MOMENT 做活动摄影摄像。美国 8 小时活动摄像师报价很少低于 **$1500**。
本项目验证：活动现场只放 1 台 360 相机（Insta360 X3）+ 几台三脚架固定手机，
事后用 AI 重建场景并生成带相机运动的视频，能否以**明显低于 $1500 的 GPU 成本**替代或补充现场摄像师。

## 阶段规划

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 仓库初始化 | ✅ |
| 1 | GPU 成本护栏（开/关/巡检 RunPod pod），见 [guard/](guard/README.md) | ✅ |
| 2 | pod 环境脚本（GEN3C / ViPE / 4C4D） | ⏳ |
| 3 | CMU Panoptic 数据准备 | ⏳ |
| 4 | GEN3C vs 4C4D 基线对比（效果 / 耗时 / 成本） | ⏳ |
| 5 | Flask 前端：上传 → 3D 预览 → 设定运镜 → 生成 | ⏳ |
| 6 | 接入 360 转透视工具 | ⏳ |

详细进度、问题、耗时与花费见 [PROGRESS.md](PROGRESS.md)。

## 架构

- **调度服务器**（2 核 4G）：只跑前端、调度脚本、存小文件，不跑任何模型。
- **RunPod GPU pod**（A100 80GB，必要时 H100）：所有 3D 重建、训练、视频生成。
- **RunPod Network Volume**（挂载在 `/workspace`）：数据集、模型权重、conda 环境、训练产出。

## 目录结构（随阶段补充）

```
.
├── README.md       项目说明
├── PROGRESS.md     每阶段进度记录
├── CLAUDE.md       项目简报 / 工作约定
└── guard/          GPU 护栏：gpu-up / gpu-down / gpu-status / gpu-guard + 离线测试
```

## 第三方模型与数据许可

本仓库**不包含**任何第三方模型权重、数据集或其代码副本，只提供链接和调用脚本。使用前请自行阅读并遵守各自许可：

| 名称 | 链接 | 许可（以官方页面为准） |
|---|---|---|
| CMU Panoptic Studio Dataset | http://domedb.perception.cs.cmu.edu/ | 仅限非商业研究用途，见官网使用条款 |
| GEN3C（代码） | https://github.com/nv-tlabs/GEN3C | 见仓库 LICENSE（待阶段 2 核实） |
| GEN3C-Cosmos-7B（权重） | https://huggingface.co/nvidia/GEN3C-Cosmos-7B | NVIDIA Open Model License |
| ViPE | https://github.com/nv-tlabs/vipe | 见仓库 LICENSE（待阶段 2 核实） |
| 4C4D（CVPR 2026） | 官方仓库待核实（候选 https://github.com/yangzf-1023/4C4D） | 待阶段 2 核实 |
| MASt3R | https://github.com/naver/mast3r | CC BY-NC-SA 4.0（非商业，待核实） |

> 注：CMU Panoptic 与 MASt3R 等为非商业许可，本项目第一阶段仅做学术/技术验证；若进入商业使用需另行评估许可。

## 安全约定

公开仓库，不提交任何密钥、token、权重、数据集、视频、点云、训练产出、日志、pod ID 或 IP；单个文件不超过 10MB。
