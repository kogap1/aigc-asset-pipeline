#!/usr/bin/env bash
# =====================================================================
# setup_conda.sh — asset_pipeline 服务器环境一键安装（conda 版）
#
# 目标服务器: Linux · 4×NVIDIA A40 · CUDA 12.2 · driver 535.183.01
#   （driver 535 最高兼容 cu124 的 torch wheel，本项目用已验证的 cu121）
#
# 作用：创建两个相互隔离的 conda 环境，装齐 asset_pipeline 全部依赖：
#   ├─ pipeline  编排器环境（agent_pipeline.py + CLIP-Score 质量门）
#   └─ comfy     ComfyUI 引擎环境（torch + 自定义节点 + 模型）
#   并完成：ComfyUI clone → 依赖/节点安装 → 模型下载 → LoRA 转换
#          → 启动 ComfyUI → 校验节点 →（可选）冒烟出图
#
# 幂等：重复执行安全，已存在/已下载的步骤自动跳过。
#
# 用法：
#   bash scripts/setup/setup_conda.sh            # 装环境+模型+启动
#   bash scripts/setup/setup_conda.sh --smoke    # 再跑节点校验 + 4 工作流冒烟出图
#   bash scripts/setup/setup_conda.sh --no-models   # 跳过模型下载（只装软件）
#
# 前置：asset_pipeline/ 已上传到 ~/yl/aigc/asset_pipeline，
#       服务器已装 anaconda3（默认 ~/anaconda3）。
# =====================================================================
set -uo pipefail

# ---------------- 可调参数 ----------------
AIGC="${AIGC:-$HOME/yl/aigc}"
CONDA_BASE="${CONDA_BASE:-$HOME/anaconda3}"
PY_VER="${PY_VER:-3.10}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu121}"
# 官方索引卡死会自动切上海交大镜像（mirror.sjtu.edu.cn/pytorch-wheels/cu121）。
PIP_MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"
GH_PFX="https://gh-proxy.com/https://github.com"
HF="https://hf-mirror.com"
CONDA_CH="https://mirrors.tuna.tsinghua.edu.cn/anaconda"

PIPE=$AIGC/asset_pipeline
COMFY=$AIGC/comfyui
PIPE_BIN=$CONDA_BASE/envs/pipeline/bin
COMFY_BIN=$CONDA_BASE/envs/comfy/bin
LOG=$AIGC/comfy.log
BASE_URL="http://127.0.0.1:8188"

SMOKE=0; NO_MODELS=0
for a in "$@"; do
  case "$a" in
    --smoke)     SMOKE=1 ;;
    --no-models) NO_MODELS=1 ;;
    *) echo "[WARN] 未知参数: $a（支持 --smoke / --no-models）" ;;
  esac
done

say() { echo -e "\n==== $* ===="; }
die() { echo "[ERROR] $*"; exit 1; }

# 官方 pytorch 索引国内常超时 → 失败自动换上海交大镜像（同 cu 版本）
CUDA_TAG="${TORCH_INDEX##*whl/}"
SJTU_INDEX="https://mirror.sjtu.edu.cn/pytorch-wheels/$CUDA_TAG"
torch_pip() { # torch_pip <env_bin> <torch/torchvision/... 版本串>
  local bin="$1"; shift
  "$bin/pip" install "$@" --index-url "$TORCH_INDEX" --timeout 90 --retries 3 \
    || { echo "[WARN] $TORCH_INDEX 不可用，换 SJTU 镜像重试..."; \
         "$bin/pip" install "$@" --index-url "$SJTU_INDEX" --timeout 90 --retries 3 \
           || die "torch 安装失败（官方与 SJTU 镜像都不可达）"; }
}

# ---------------- 0. 初始化 conda ----------------
say "[0/6] 初始化 conda ($CONDA_BASE)"
if [ ! -d "$CONDA_BASE" ]; then
  for c in "$HOME/anaconda3" "$HOME/miniconda3" /opt/conda /opt/miniconda3 /usr/local/anaconda3; do
    [ -d "$c" ] && { CONDA_BASE="$c"; PIPE_BIN="$c/envs/pipeline/bin"; COMFY_BIN="$c/envs/comfy/bin"; break; }
  done
  [ -d "$CONDA_BASE" ] || die "找不到 conda。请先安装 Anaconda/Miniconda，或用 CONDA_BASE=/path/to/conda 指定。"
fi
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh" || die "source conda.sh 失败：$CONDA_BASE 不是有效的 conda 安装"
conda --version >/dev/null || die "conda 不可用"

# 国内 conda 源。注意：tsinghua 已下线 pkgs/free（访问返回 404，会导致 create 失败），只保留 pkgs/main。
# 先 remove 再 add，保证幂等（重复运行不累积重复频道）。
conda config --remove channels "$CONDA_CH/pkgs/free" >/dev/null 2>&1 || true
conda config --remove channels "$CONDA_CH/pkgs/main" >/dev/null 2>&1 || true
conda config --add channels "$CONDA_CH/pkgs/main" >/dev/null 2>&1 || true
conda config --set show_channel_urls yes >/dev/null 2>&1 || true

mk_env() {  # mk_env <env_name>
  local name="$1"
  if conda env list | awk '{print $1}' | grep -qx "$name"; then
    echo "  conda env '$name' 已存在，跳过创建"
  else
    echo "  创建 conda env '$name' (python=$PY_VER) ..."
    conda create -n "$name" "python=$PY_VER" -y || die "conda create $name 失败"
  fi
  "${CONDA_BASE}/envs/$name/bin/pip" install -U pip wheel setuptools $PIP_MIRROR -q
}

# ---------------- 1. pipeline 环境（编排器 + 质量门） ----------------
say "[1/6] pipeline 环境：torch + 编排器依赖"
mk_env pipeline
echo "  >> 安装 torch 2.5.1+cu121（与 aigc 训练环境同版本，已验证）..."
torch_pip "$PIPE_BIN" torch==2.5.1 torchvision==0.20.1
echo "  >> 安装 requirements.txt ..."
"$PIPE_BIN/pip" install -r "$PIPE/requirements.txt" $PIP_MIRROR --timeout 60 --retries 3 \
  || die "pipeline 依赖安装失败"
"$PIPE_BIN/python" -c "import openai, open_clip, torch, PIL, yaml; print('  pipeline 依赖 OK, torch', torch.__version__)"

# ---------------- 2. comfy 环境（ComfyUI 引擎） ----------------
say "[2/6] comfy 环境：torch + ComfyUI + 自定义节点"
mk_env comfy
echo "  >> 安装 torch/torchvision/torchaudio 2.5.1+cu121 ..."
torch_pip "$COMFY_BIN" torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1

# clone ComfyUI（GitHub 不通走 gh-proxy，再不行走 gitee）
mkdir -p "$AIGC"
if [ ! -d "$COMFY/.git" ]; then
  echo "  >> clone ComfyUI ..."
  git clone "$GH_PFX/comfyanonymous/ComfyUI.git" "$COMFY" \
    || git clone https://gitee.com/mirrors/ComfyUI.git "$COMFY" \
    || die "ComfyUI clone 失败"
else
  echo "  ComfyUI 已存在，跳过 clone"
fi

echo "  >> 安装 ComfyUI requirements ..."
"$COMFY_BIN/pip" install -r "$COMFY/requirements.txt" $PIP_MIRROR --timeout 60 --retries 3 \
  || echo "[WARN] ComfyUI requirements 安装不完整，继续"

# 已知坑：新版 ComfyUI requirements 带 comfy_kitchen，与 torch 2.5.1 不兼容（启动即崩）。
# 本项目工作流（txt2img/img2img/ipadapter/post）用不到 kitchen 节点 → 直接移除。
echo "  >> 移除 comfy_kitchen（torch 2.5.1 兼容性）..."
"$COMFY_BIN/pip" uninstall -y comfy_kitchen >/dev/null 2>&1 || true
rm -rf "$CONDA_BASE/envs/comfy/lib/python$PY_VER/site-packages/comfy_kitchen"* 2>/dev/null
"$COMFY_BIN/python" -c "import torch; print('  comfy torch OK', torch.__version__, torch.version.cuda)"

# 自定义节点
mkdir -p "$COMFY/custom_nodes"
cd "$COMFY/custom_nodes" || die "cd custom_nodes 失败"
for repo in "cubiq/ComfyUI_IPAdapter_plus" "ltdrdata/ComfyUI-Impact-Pack" "ltdrdata/ComfyUI-Manager"; do
  name=$(basename "$repo")
  if [ ! -d "$name/.git" ]; then
    echo "  >> clone $name ..."
    git clone "$GH_PFX/$repo.git" "$name" \
      || git clone "https://gitee.com/mirrors/$name.git" "$name" \
      || echo "[WARN] clone 失败(可跳过): $name"
  else
    echo "  $name 已存在，跳过"
  fi
done

echo "  >> 装自定义节点依赖 ..."
for req in "ComfyUI-Impact-Pack/requirements.txt" "ComfyUI_IPAdapter_plus/requirements.txt"; do
  [ -f "$COMFY/custom_nodes/$req" ] && \
    "$COMFY_BIN/pip" install -r "$COMFY/custom_nodes/$req" $PIP_MIRROR --timeout 60 --retries 3 \
      || echo "[WARN] $req 安装失败(可手工补)"
done
# Impact-Pack / RemBG 补齐依赖（sam2 服务器拉不到且本项目用不上 → 跳过）
"$COMFY_BIN/pip" install segment-anything transformers dill matplotlib scikit-image piexif \
  scipy opencv-python-headless numpy onnxruntime rembg $PIP_MIRROR --timeout 60 --retries 3 \
  || echo "[WARN] Impact-Pack/RemBG 依赖补装失败(可手工补)"

# ---------------- 3. 下载模型 ----------------
say "[3/6] 下载模型（幂等，失败不中断）"
if [ "$NO_MODELS" = "1" ]; then
  echo "  --no-models，跳过"
else
  mkdir -p "$COMFY/models/checkpoints" "$COMFY/models/loras" "$COMFY/models/ipadapter" \
           "$COMFY/models/clip_vision" "$COMFY/models/rembg" "$COMFY/models/upscale_models"
  dl() { # dl <本地路径> <url> [说明]
    local f="$1" u="$2" d="${3:-}"
    if [ -s "$f" ]; then echo "  已存在: $(basename "$f")"; return 0; fi
    rm -f "$f"  # 清掉上次失败留下的 0 字节占位文件
    echo "  >> 下载 $(basename "$f") ${d:-}..."
    wget -q --show-progress -c -O "$f" "$u" && echo "  OK $(basename "$f")" \
      || { rm -f "$f"; echo "[WARN] 下载失败(可重跑本脚本): $(basename "$f")"; }
  }
  dl "$COMFY/models/checkpoints/v1-5-pruned-emaonly.safetensors" \
     "$HF/Comfy-Org/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors" "[必须] SD1.5"
  dl "$COMFY/models/ipadapter/ip-adapter_sd15.safetensors" \
     "$HF/h94/IP-Adapter/resolve/main/models/ip-adapter_sd15.safetensors" "[可选] IPAdapter"
  dl "$COMFY/models/clip_vision/CLIP-ViT-H.safetensors" \
     "$HF/h94/IP-Adapter/resolve/main/models/image_encoder/model.safetensors" "[可选] CLIP-ViT-H"
  dl "$COMFY/models/rembg/u2net.onnx" \
     "$GH_PFX/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx" "[RemBG 去背景]"
  dl "$COMFY/models/upscale_models/RealESRGAN_x4plus.pth" \
     "$GH_PFX/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth" "[超分]"
fi

# ---------------- 4. 转换 LoRA（diffusers -> bfla） ----------------
say "[4/6] 转换 MTG LoRA（diffusers -> bfla）"
SRC_LORA="$AIGC/gen_project/outputs/lora"
if [ -d "$SRC_LORA" ]; then
  cd "$PIPE" || die "cd $PIPE 失败"
  "$COMFY_BIN/python" tools/convert_lora.py --src "$SRC_LORA" --dst "$COMFY/models/loras/mtg_lora.safetensors" \
    || echo "[WARN] LoRA 转换失败"
else
  echo "  [SKIP] 未找到 $SRC_LORA，跳过（后续可手工跑 tools/convert_lora.py）"
fi

# ---------------- 5. 启动 ComfyUI ----------------
say "[5/6] 启动 ComfyUI（headless, port 8188）"
pkill -f "comfyui/main.py" 2>/dev/null
sleep 1
nohup "$COMFY_BIN/python" "$COMFY/main.py" --listen 0.0.0.0 --port 8188 > "$LOG" 2>&1 &
READY=0
for i in $(seq 1 60); do
  curl -sf "$BASE_URL/system_stats" > /dev/null 2>&1 && { echo "  ComfyUI 就绪（第 ${i} 次探测）"; READY=1; break; }
  sleep 2
done
[ "$READY" = "1" ] || { echo "[ERROR] ComfyUI 120 秒未就绪，看日志尾部："; tail -30 "$LOG"; exit 1; }

# ---------------- 6. 校验 + 冒烟 ----------------
say "[6/6] 校验工作流节点"
cd "$PIPE" || die "cd $PIPE 失败"
if "$PIPE_BIN/python" tools/validate_wf.py --base_url "$BASE_URL"; then
  if [ "$SMOKE" = "1" ]; then
    echo -e "\n==> 冒烟出图（4 工作流各 1 张）..."
    "$PIPE_BIN/python" tools/smoke.py
    ls -la deliverables/smoke/ 2>/dev/null
  else
    echo -e "\n（加 --smoke 可冒烟出图验证）"
  fi
else
  echo "[WARN] 有节点缺失，用 ComfyUI-Manager 补装后重跑本脚本"
fi

# ---------------- 完成 ----------------
echo ""
echo "=================================================================="
echo "  安装完成。日常使用："
echo "    # 编排器（agent_pipeline.py / 冒烟 / e2e）"
echo "    source $CONDA_BASE/etc/profile.d/conda.sh"
echo "    conda activate pipeline"
echo "    cd ~/yl/aigc/asset_pipeline"
echo "    bash scripts/run/run_e2e.sh"
echo ""
echo "    # ComfyUI 已在跑（端口 8188），日志 tail -f $LOG"
echo "    # 重启: pkill -f 'comfyui/main.py'; $COMFY_BIN/python $COMFY/main.py --listen 0.0.0.0 --port 8188 &"
echo "=================================================================="
