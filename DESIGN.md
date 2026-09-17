# 游戏素材自动化生成管线（asset_pipeline）设计文档

> 日期：2026-08-17 · 状态：设计已确认，待实施（3-5 天标准版）
> 定位：在已完成的「MTG 卡图 LoRA 微调」项目（`../gen_project`）之上，搭建一条**端到端自动化素材生成管线**。复用已训好的 MTG 风格 LoRA。
> **已确认范围（2026-08-17）**：砍 ControlNet，保留 **txt2img / img2img / IP-Adapter** 三个生成工作流 + 后处理；DeepSeek key 已有；服务器磁盘足够可装 ComfyUI。

## 1. 目标与定位

把「会画 MTG 风格模型的工具」升级为「需求进来、成品出去的自动化素材工厂」，将需求分析、Prompt 构建、生成调度、质量筛选、后处理等环节串联为端到端的自动化链路。

**不做**：Flux 前沿模型、视频生成（超出当前能力范围）。
**复用**：`../gen_project/outputs/lora`（MTG LoRA）、`evaluate.py` 的 CLIP-Score 代码（open_clip）。

## 2. 总体架构（方案 1：ComfyUI 引擎 + DeepSeek Agent 编排）

```
一句话需求（如"做 6 张中国风武将卡图，参考这张风格图"）
   ↓
DeepSeek Agent（Function Calling）
   ├─ 需求分析 → 结构化任务 [{prompt, count, control?, ref_image?}]
   └─ 质量决策 → 分数不达标自动换 seed 重试
   ↓
调度器 → ComfyUI API（headless，挂 MTG LoRA）
   ├─ wf_txt2img      文生图
   ├─ wf_img2img      旧图翻新 / 风格重绘
   └─ wf_ipadapter    风格迁移（参考图 → 目标内容）
   ↓
CLIP-Score 质量门（复用 open_clip，阈值不达标自动重试×3）
   ↓
后处理（去背景 rembg / 超分 Real-ESRGAN）
   ↓
deliverables/{批次}/ 成品图 + manifest.json（含每条评分）
```

## 3. 组件清单

### 3.1 ComfyUI（生成引擎，服务器 headless）
- 安装到服务器，API 模式（端口 8188），进程用 systemd/nohup 常驻
- 工作流 JSON（`comfyui/workflows/`）：
  | 文件 | 功能 | 关键节点 |
  |------|------|---------|
  | `wf_txt2img.json` | 文生图 | Checkpoint 加载 SD1.5 + MTG LoRA、CLIP Text Encode、KSampler |
  | `wf_img2img.json` | 图生图 | Load Image + denoise 强度 |
  | `wf_ipadapter.json` | 风格迁移 | IP-Adapter（参考图） |
  | `wf_post.json` | 后处理 | RemBG（去背景）+ Real-ESRGAN（超分） |
- 自定义节点：ComfyUI-Manager、IPAdapter-Plus、Impact-Pack（RemBG）、Image-Upscale（Real-ESRGAN）
- 模型：SD1.5 + LoRA + IPAdapter 权重 + CLIP-ViT-H 图像编码器（走 hf-mirror 下载）

### 3.2 agent_pipeline.py（编排器）
- `DeepSeekAgent`：调 `deepseek-chat`，系统提示词定义任务 schema，注册 function tool `generate_assets(requests: [{prompt, count, control, ref_image}])`
- `Scheduler`：把结构化任务转成 ComfyUI API `POST /prompt` 请求，按 workflow 类型选 JSON 模板填参，并发提交、轮询结果
- `QualityGate`：复用 CLIP-Score，`score < threshold` → 换 seed 重试（最多 3 次）
- `PostProcess`：调 `wf_post.json` 去背景 + 超分
- `Deliverer`：写 `deliverables/{batch_id}/`（图 + manifest.json + state.json）

### 3.3 配置
- `config.yaml`：ComfyUI URL、工作流路径、质量阈值、重试次数、输出目录
- `.env`：`DEEPSEEK_API_KEY`（不提交）

## 4. 端到端数据流

```
需求(自然语言) → DeepSeek 解析为结构化任务
  → 对每个任务选 workflow 模板、填 prompt/seed/size
  → ComfyUI API 提交 → 轮询出图
  → CLIP-Score 质量门（fail → 换 seed 重试×3）
  → 后处理（去背景/超分）
  → 写 deliverables/{batch_id}/ + manifest.json（含每条 score 与状态）
```

## 5. 错误处理

| 环节 | 策略 |
|------|------|
| DeepSeek API | 限流/超时 → 指数退避重试×3；非法 JSON → 要求模型修正后重解析 |
| ComfyUI 提交 | 失败重试×2；任务卡死 → 超时跳过并记录 |
| 质量门 | 分数 < 阈值 → 换 seed 重试×3；仍不达标 → 标记 `low_quality` 留档不删 |
| 断点恢复 | `state.json` 记录每任务状态（待提交/完成/失败），中断后可续跑 |

## 6. 测试

**本机可跑（纯逻辑，无服务器）**：
1. Agent 解析测试：给一句需求 → 校验结构化 JSON（字段全、count 合理、prompt 非空）
2. Function Calling schema 校验
3. 质量门函数：喂假分数 → 判定通过/重试

**服务器集成**：
4. ComfyUI 单工作流冒烟：POST 一个 txt2img → 确认出图
5. IP-Adapter / img2img 各验一次出图
6. 小批量端到端：给"3-6 张"需求 → 全链路 → 检查 deliverables + manifest.json

## 7. 交付物与量化指标（实测后回填，不编数字）

- 交付物：ComfyUI + DeepSeek Function Calling 端到端自动化素材生成管线
- 可量化：质量门通过率、交付成品平均 CLIP-Score、IP-Adapter 风格一致性（参考图 vs 生成图相似度）、端到端耗时

## 8. 目录规划（新项目文件夹 asset_pipeline/）

```
asset_pipeline/
├── DESIGN.md                 本设计文档
├── config.yaml               配置
├── .env                      DEEPSEEK_API_KEY（不入库）
├── agent_pipeline.py         编排器（Agent + 调度 + 质量门 + 后处理 + 交付）
├── agent_test.py             本机逻辑测试
├── comfyui/
│   ├── workflows/*.json      5 个工作流
│   └── README.md             ComfyUI 安装与启动说明
├── scripts/
│   ├── setup_comfyui.sh      安装 ComfyUI + 节点 + 模型（服务器）
│   ├── test_wf.sh            各工作流冒烟（heredoc 执行，避免粘贴断行）
│   └── run_e2e.sh            端到端小批量测试
├── deliverables/             输出（gitignore）
└── docs/                     后续：PROJECT_SUMMARY.md（实施完成后）
```

## 9. 范围（3-5 天）

- Day 1：ComfyUI 安装 + 模型/节点准备 + txt2img 工作流通
- Day 2：img2img / IP-Adapter 工作流验证
- Day 3：agent_pipeline.py（DeepSeek Function Calling + 调度 + 质量门）
- Day 4：后处理 + 交付 + 错误处理 + 本机测试
- Day 5：端到端小批量跑通 + 采集实测指标 + 文档

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| ComfyUI 自定义节点安装复杂 | 用 ComfyUI-Manager 一键装；按官方 README 逐步来；卡住就降到纯 Python 方案（方案 2 兜底） |
| hf-mirror 下载模型慢/失败 | 分批下载、断点重试；必要时走本地中转 |
| IP-Adapter 效果不达预期 | 属加分项，不行就砍，不影响主链路（txt2img + Agent + 质量门 + 后处理） |
| DeepSeek 限流 | 退避重试 + 降低并发 |
