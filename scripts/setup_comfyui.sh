#!/usr/bin/env bash
# 已有 aigc conda 环境时：ComfyUI 一键安装 + 模型下载 + LoRA 转换
# 前置: asset_pipeline/ 已上传到 ~/yl/aigc/asset_pipeline，并已 conda activate aigc
# 用法: bash scripts/setup_comfyui.sh
set -uo pipefail

AIGC=~/yl/aigc
COMFY=$AIGC/comfyui
VENV=$AIGC/comfyenv
PIPE=$AIGC/asset_pipeline
PYTHON_BIN="${PYTHON_BIN:-python}"
GH_PFX="https://gh-proxy.com/https://github.com"
HF="https://hf-mirror.com"

echo "==== [0/6] 检查当前 aigc 环境 ===="
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), sys.version; print("python", sys.version.split()[0], sys.executable)' \
  || { echo "[ERROR] 当前 Python 低于 3.10；请先进入 aigc 环境"; exit 1; }
"$PYTHON_BIN" -c 'import torch; print("torch", torch.__version__, "cuda", torch.cuda.is_available())' \
  || { echo "[ERROR] 当前环境缺少 torch；请先 conda activate aigc"; exit 1; }
"$PYTHON_BIN" -m pip install -r "$PIPE/requirements-pipeline.txt" \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  || { echo "[ERROR] 编排器依赖安装失败"; exit 1; }

echo "==== [1/6] clone ComfyUI ===="
mkdir -p "$AIGC"
if [ ! -d "$COMFY/.git" ]; then
  git clone "$GH_PFX/comfyanonymous/ComfyUI.git" "$COMFY" \
    || git clone https://gitee.com/mirrors/ComfyUI.git "$COMFY" \
    || { echo "[ERROR] ComfyUI clone 失败"; exit 1; }
else
  echo "已存在，跳过"
fi

echo "==== [2/6] 创建 ComfyUI venv（复用 aigc 的 torch） ===="
if [ ! -d "$VENV" ]; then
  "$PYTHON_BIN" -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"$VENV/bin/pip" install -r "$COMFY/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "==== [3/6] 自定义节点 ===="
mkdir -p "$COMFY/custom_nodes"
cd "$COMFY/custom_nodes" || exit 1
for repo in "cubiq/ComfyUI_IPAdapter_plus" "ltdrdata/ComfyUI-Impact-Pack" "ltdrdata/ComfyUI-Manager"; do
  name=$(basename "$repo")
  if [ ! -d "$name/.git" ]; then
    git clone "$GH_PFX/$repo.git" "$name" \
      || git clone https://gitee.com/mirrors/$name.git "$name" \
      || echo "[WARN] clone 失败(可跳过): $name"
  else
    echo "已存在: $name"
  fi
done
"$VENV/bin/pip" install -r "$COMFY/custom_nodes/ComfyUI-Impact-Pack/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple || echo "[WARN] Impact-Pack 依赖装失败"
"$VENV/bin/pip" install -r "$COMFY/custom_nodes/ComfyUI_IPAdapter_plus/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple || echo "[WARN] IPAdapter 依赖装失败"

echo "==== [4/6] 模型（hf-mirror / gh-proxy） ===="
mkdir -p "$COMFY/models/checkpoints" "$COMFY/models/loras" "$COMFY/models/ipadapter" \
         "$COMFY/models/clip_vision" "$COMFY/models/rembg" "$COMFY/models/upscale_models"
CHECKPOINT="$COMFY/models/checkpoints/v1-5-pruned-emaonly.safetensors"
LOCAL_CHECKPOINT="$PIPE/models/v1-5-pruned-emaonly.safetensors"
if [ ! -s "$CHECKPOINT" ] && [ -s "$LOCAL_CHECKPOINT" ]; then
  rm -f "$CHECKPOINT"
  ln -s "$LOCAL_CHECKPOINT" "$CHECKPOINT"
  echo "复用项目内 checkpoint: $LOCAL_CHECKPOINT"
fi
if [ ! -s "$CHECKPOINT" ]; then
  wget -c -O "$CHECKPOINT" \
    "$HF/Comfy-Org/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors" \
    || { rm -f "$CHECKPOINT"; echo "[WARN] checkpoint 下载失败"; }
fi
if [ ! -s "$COMFY/models/ipadapter/ip-adapter_sd15.safetensors" ]; then
  wget -c -O "$COMFY/models/ipadapter/ip-adapter_sd15.safetensors" \
    "$HF/h94/IP-Adapter/resolve/main/models/ip-adapter_sd15.safetensors" \
    || { rm -f "$COMFY/models/ipadapter/ip-adapter_sd15.safetensors"; echo "[WARN] ipadapter 下载失败(加分项，可跳过)"; }
fi
if [ ! -s "$COMFY/models/clip_vision/CLIP-ViT-H.safetensors" ]; then
  wget -c -O "$COMFY/models/clip_vision/CLIP-ViT-H.safetensors" \
    "$HF/h94/IP-Adapter/resolve/main/models/image_encoder/model.safetensors" \
    || { rm -f "$COMFY/models/clip_vision/CLIP-ViT-H.safetensors"; echo "[WARN] clip_vision 下载失败(加分项，可跳过)"; }
fi
if [ ! -s "$COMFY/models/rembg/u2net.onnx" ]; then
  wget -c -O "$COMFY/models/rembg/u2net.onnx" \
    "$GH_PFX/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx" \
    || { rm -f "$COMFY/models/rembg/u2net.onnx"; echo "[WARN] u2net.onnx 下载失败"; }
fi
if [ ! -s "$COMFY/models/upscale_models/RealESRGAN_x4plus.pth" ]; then
  wget -c -O "$COMFY/models/upscale_models/RealESRGAN_x4plus.pth" \
    "$GH_PFX/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth" \
    || { rm -f "$COMFY/models/upscale_models/RealESRGAN_x4plus.pth"; echo "[WARN] RealESRGAN 下载失败"; }
fi

echo "==== [5/6] 转换 LoRA（diffusers -> bfla） ===="
cd "$PIPE" || exit 1
"$VENV/bin/python" convert_lora.py \
  --src "$AIGC/gen_project/outputs/lora" \
  --dst "$COMFY/models/loras/mtg_lora.safetensors"

echo ""
echo "==== [6/6] 完成 ===="
echo "启动 ComfyUI:"
echo "  nohup $VENV/bin/python $COMFY/main.py --listen 0.0.0.0 --port 8188 > comfy.log 2>&1 &"
