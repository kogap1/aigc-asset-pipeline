# asset_pipeline 实施计划

> **分工**：我（本机）一次性写好全部代码/脚本/工作流/文档；你（服务器）按脚本执行并回传结果；我据此采集实测指标、写总结文档。

**目标**：在已训好的 MTG 风格 LoRA 之上，搭建「ComfyUI + DeepSeek Agent 的端到端自动化素材生成管线」。
**架构**：一句话需求 → DeepSeek Function Calling 拆任务 → 调度器调 ComfyUI API（txt2img/img2img/IP-Adapter）→ CLIP-Score 质量门 → 去背景/超分后处理 → 结构化交付。
**技术栈**：ComfyUI（headless API）、deepseek-chat、open_clip、rembg、Real-ESRGAN。

---

## 关键前置知识点（决定成败，先看）

1. **LoRA 格式转换（最关键）**：服务器 `outputs/lora` 是 **diffusers/peft 格式**（目录 + `adapter_model.safetensors`）。ComfyUI 只认 **bfla 格式**（单个 `.safetensors`，key 形如 `lora_unet_*`）。必须转换，否则 ComfyUI 加载不了 LoRA。→ 写 `convert_lora.py` 在服务器上跑。
2. **SD1.5 checkpoint**：ComfyUI 要单个 `.safetensors` checkpoint（如 `v1-5-pruned-emaonly.safetensors`），不是 diffusers 目录。→ setup 脚本从 HF 镜像下载。
3. **IP-Adapter 需要额外组件**：IPAdapter 权重 + CLIP-ViT-H 图像编码器（约 2.5GB）。
4. **ComfyUI 工作流 JSON 手写有风险**：节点类名必须和已装的自定义节点匹配。→ 写 `validate_wf.py`，装完节点后校验类名是否存在，缺失即报错不静默。

---

## Task 1：项目骨架
**Files**：`config.yaml`、`.env.example`、`requirements-pipeline.txt`、`.gitignore`
- `config.yaml`：ComfyUI 地址/端口、工作流路径、质量阈值 `quality_threshold: 0.30`、重试次数 3、输出目录
- `.env.example`：`DEEPSEEK_API_KEY=sk-xxx`（`.env` 不入库）
- `requirements-pipeline.txt`：`openai`（DeepSeek 兼容 OpenAI SDK）、`PyYAML`、`rembg`、`onnxruntime-gpu`、`realesrgan`、`requests`、`Pillow`

## Task 2：ComfyUI 工作流 JSON
**Files**：`comfyui/workflows/wf_txt2img.json`、`wf_img2img.json`、`wf_ipadapter.json`、`wf_post.json` + `validate_wf.py`
- 每个 workflow 是 ComfyUI API 格式（`{"prompt": {...节点...}, "extra": {}}`），节点类名如下：
  - `wf_txt2img`：CheckpointLoaderSimple → LoraLoader → CLIPTextEncode（正/负 prompt）→ EmptyLatentImage → KSampler → VAEDecode → SaveImage
  - `wf_img2img`：多一个 LoadImage + denoise 参数（0.6）
  - `wf_ipadapter`：CheckpointLoaderSimple + LoadImage(参考图) → IPAdapterAdvanced → KSampler
  - `wf_post`：LoadImage → RemBG（去背景）→ UpscaleModelLoader + ImageUpscaleWithModel（Real-ESRGAN ×4）→ SaveImage
- `validate_wf.py`：用 ComfyUI `/object_info` 接口校验每个 workflow 引用的节点类名都存在，缺失则打印清单。

## Task 3：setup_comfyui.sh（服务器一键装）
**Files**：`scripts/setup_comfyui.sh` + `convert_lora.py`
- 安装 ComfyUI（git clone + 用现有 `aigc` 环境或新 venv）、ComfyUI-Manager
- 装自定义节点：IPAdapter-Plus、Impact-Pack（RemBG）、ComfyUI-Image-Upscale
- 下载模型（走 hf-mirror）：SD1.5 checkpoint ~4GB、IPAdapter_sd15 ~70MB、CLIP-ViT-H ~2.5GB、u2net.onnx ~170MB、RealESRGAN_x4plus ~64MB
- `convert_lora.py`：把 `outputs/lora`（diffusers）转成 `comfyui/models/loras/mtg_lora.safetensors`（bfla），key 重命名 `base_model.model.unet.` → `lora_unet_`、text_encoder → `lora_te1_`

## Task 4：agent_pipeline.py（编排器）
**Files**：`agent_pipeline.py`
- 类与方法（供 Task 5 测试）：
  - `DeepSeekAgent.parse_requirement(req_text) -> list[dict]`：调 deepseek-chat，Function Calling `generate_assets`，返回 `[{prompt, count, mode, ref_image?}]`
  - `Scheduler.submit(workflow, inputs) -> job_id` / `poll(job_id) -> image_path`：ComfyUI API `POST /prompt` + 轮询
  - `QualityGate.check(image_path, prompt) -> score`：open_clip 算 CLIP-Score；`score < threshold` 返回重试
  - `PostProcess.run(image_path)`：调 `wf_post.json` 去背景+超分
  - `Deliverer.write(batch_dir, manifest)`：写 `deliverables/{batch_id}/` + `manifest.json` + `state.json`
- 主流程：需求 → 解析 → 逐任务（选 workflow → 提交 → 质量门失败换 seed 重试×3 → 后处理）→ 交付
- 错误处理：DeepSeek 退避重试×3、非法 JSON 重解析、ComfyUI 超时跳过、断点续跑

## Task 5：agent_test.py（本机可跑）
**Files**：`agent_test.py`
- 测解析：`parse_requirement("做 3 张精灵卡牌")` → 校验字段全、count=3、prompt 非空
- 测 schema：function calling 定义合法
- 测质量门：喂假分数 → 通过/重试判定正确

## Task 6：服务器运行脚本 + 文档
**Files**：`scripts/test_wf.sh`、`scripts/run_e2e.sh`、`comfyui/README.md`
- `test_wf.sh`：启动 ComfyUI → `validate_wf.py` → 每个工作流各跑 1 张冒烟出图
- `run_e2e.sh`：给 3-6 张需求 → 全链路 → 检查 deliverables + manifest
- `comfyui/README.md`：安装/启动/端口/踩坑说明

## Task 7：本机自检
- 所有 .py 语法检查、`agent_pipeline.py` / `agent_test.py` 可导入
- `agent_test.py` 本机跑绿（无需服务器）

---

## Phase 1：服务器执行（你跑，我指导）
1. `bash scripts/setup_comfyui.sh` → 装好环境 + 模型 + LoRA 转换
2. `nohup python main.py --listen 0.0.0.0 > comfy.log 2>&1 &` 起 ComfyUI
3. `bash scripts/test_wf.sh` → 4 个工作流冒烟出图
4. `bash scripts/run_e2e.sh` → 端到端小批量
5. 回传 deliverables + 日志 → 我采集实测指标 → 写 `docs/PROJECT_SUMMARY.md`

## 风险（简）
- ComfyUI 节点装不上 → 降级纯 Python 方案（方案 2 兜底）
- LoRA 转换 key 命名错 → convert_lora.py 自带 key 校验，转换后打印数量核对
- IP-Adapter 效果差 → 砍掉，不影响主链路
