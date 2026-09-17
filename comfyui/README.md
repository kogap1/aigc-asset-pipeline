# ComfyUI 安装与启动（服务器）

> 本项目用 ComfyUI 作为生成引擎（headless API，端口 8188），挂 SD1.5 checkpoint + MTG LoRA。
> 编排器 `agent_pipeline.py` 只通过 HTTP API 与它通信，不碰图形界面。

## 1. 安装（一条命令）

前置：
- 项目已上传到服务器 `~/yl/aigc/asset_pipeline`
- 服务器 Python 已有 torch（`aigc` 环境，torch 2.5.1+cu121）

服务器已经有可用的 `aigc` conda 环境时，推荐复用其 torch，并为 ComfyUI 创建隔离 venv：

```bash
cd ~/yl/aigc/asset_pipeline
# 确认终端提示符已经是 (aigc)
bash scripts/setup_comfyui.sh
```

该脚本会把编排器依赖安装到当前 `aigc` 环境，并创建 `~/yl/aigc/comfyenv` 运行 ComfyUI。只有 conda CLI 正常且确实希望新建两个 conda 环境时，才使用 `scripts/setup_conda.sh --smoke`。

脚本做的事：
1. clone ComfyUI 到 `~/yl/aigc/comfyui`（GitHub 不通走 gh-proxy 镜像）
2. 建独立 venv `~/yl/aigc/comfyenv`（`--system-site-packages`，复用系统 torch，**不污染 `aigc` 训练环境**）
3. 装自定义节点：`ComfyUI_IPAdapter_plus`（IP-Adapter）、`ComfyUI-Impact-Pack`、`ComfyUI-Manager`
4. 下载模型（hf-mirror / gh-proxy）：SD1.5 checkpoint、IPAdapter、CLIP-ViT-H、RealESRGAN_x4plus；u2net.onnx 为后续去背景预留
5. 转换 LoRA：`convert_lora.py` 把 `~/yl/aigc/gen_project/outputs/lora`（diffusers 格式）转成 `comfyui/models/loras/mtg_lora.safetensors`（bfla 格式）

> 下载失败不中断（IPAdapter/CLIP-ViT-H 属加分项可跳过）；**SD1.5 checkpoint 必须下成功**，否则所有工作流都没法跑。

## 2. 启动

```bash
cd ~/yl/aigc/asset_pipeline
nohup ~/yl/aigc/comfyenv/bin/python ~/yl/aigc/comfyui/main.py \
  --listen 0.0.0.0 --port 8188 > comfy.log 2>&1 &
```

- 端口默认 8188，探测就绪：`curl http://127.0.0.1:8188/system_stats`
- 日志：`tail -f comfy.log`
- 停止：`pkill -f "comfyui/main.py"`（用前先 `pgrep -f "comfyui/main.py"` 确认）

## 3. 关键配置

编辑 `config.yaml`：

| 字段 | 说明 |
|------|------|
| `comfyui.base_url` | API 地址，默认 `http://127.0.0.1:8188` |
| `comfyui.input_dir` | **必填**（img2img/ipadapter/后处理需要）：`~/yl/aigc/comfyui/input` |
| `comfyui.checkpoint` | `v1-5-pruned-emaonly.safetensors` |
| `quality.threshold` | CLIP-Score 质量门，默认 0.30（低于则换 seed 重试） |

```bash
# 在 config.yaml 里改 input_dir 为本机真实路径
python - <<'EOF'
from agent_pipeline import load_config
cfg = load_config()
print("input_dir =", cfg["comfyui"]["input_dir"])
EOF
```

## 4. 校验与冒烟

```bash
bash scripts/test_wf.sh        # 等就绪 → 校验节点 → 4 工作流各出 1 张（deliverables/smoke/）
bash scripts/run_e2e.sh        # 端到端小批量 → deliverables/batch_*/ + manifest.json
```

> 当前 `wf_post.json` 只执行 Real-ESRGAN 超分。Impact-Pack 新版没有本项目原先假设的 `RemBG` 节点，去背景尚未接回主链路。

## 5. 踩坑记录

1. **LoRA 必须转格式**：`gen_project/outputs/lora` 是 diffusers/peft 格式，ComfyUI 只认 bfla 单文件。`convert_lora.py` 会把 key `base_model.model.unet.*` 改成 `lora_unet_*`、`.lora_A/.lora_B.weight` 改成 `.lora_down/.lora_up.weight`，并打印 down/up 数量校验。转换失败时看它输出的统计。
2. **GitHub 服务器不通**：clone/下载一律走 gh-proxy 或 hf-mirror；不行就本机下载后 scp。
3. **节点类名必须匹配**：手写工作流容易用错类名。`validate_wf.py` 会调 `/object_info` 校验每个 workflow 引用的节点，缺哪个报哪个（例如 `IPAdapterAdvanced` 来自 IPAdapter-Plus）。
4. **双环境隔离**：ComfyUI 用独立 venv，别在 `aigc` 环境里 `pip install -r requirements.txt`，避免把已训好的 gen_project 依赖冲掉。
5. **长命令断行**：任何粘贴到终端的长命令都先写成脚本再 `bash 脚本名`。
6. **IP-Adapter 效果差/下不了权重**：直接砍，删掉 wf_ipadapter 的调用即可，主链路（txt2img + Agent + 质量门 + 后处理）不受影响。
