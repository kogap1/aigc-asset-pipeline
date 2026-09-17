#!/usr/bin/env bash
# asset_pipeline 收尾修复：补丁 convert_lora / 装 rembg / 下 checkpoint / 转 LoRA
# 用法: bash scripts/fix.sh
set -uo pipefail
AIGC=~/yl/aigc
COMFY=$AIGC/comfyui
VENV=$AIGC/comfyenv
PIPE=$AIGC/asset_pipeline
HF="https://hf-mirror.com"

echo "==== [1/4] 补丁 convert_lora.py（支持旧命名 pytorch_lora_weights） ===="
python - <<'PYEOF'
import pathlib
p = pathlib.Path.home() / "yl/aigc/asset_pipeline/convert_lora.py"
s = p.read_text()
old = '''    if src.is_dir():
        src_file = src / "adapter_model.safetensors"
        if not src_file.exists():
            print(f"[ERROR] 目录里没有 adapter_model.safetensors: {src}")
            return 1'''
new = '''    if src.is_dir():
        src_file = next((src / c for c in ("adapter_model.safetensors", "pytorch_lora_weights.safetensors") if (src / c).exists()), None)
        if src_file is None:
            print(f"[ERROR] 目录里没有 adapter_model.safetensors / pytorch_lora_weights.safetensors: {src}")
            return 1'''
if old in s:
    p.write_text(s.replace(old, new))
    print("  patched OK")
else:
    print("  already patched (skip)")
PYEOF

echo "==== [2/4] 装 rembg（Impact-Pack RemBG 依赖） ===="
"$VENV/bin/pip" install rembg -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "==== [3/4] 下载 SD1.5 checkpoint（自动定位路径） ===="
CHECKPOINT="$COMFY/models/checkpoints/v1-5-pruned-emaonly.safetensors"
if [ -s "$CHECKPOINT" ]; then  # -s: 非空才算已存在（避免 0 字节占位坑）
  echo "  已存在，跳过"
else
  REL=$(python - <<'PYEOF'
from huggingface_hub import HfApi
api = HfApi(endpoint="https://hf-mirror.com")
files = api.list_repo_files("Comfy-Org/stable-diffusion-v1-5")
cands = [f for f in files if "v1-5-pruned-emaonly" in f and f.endswith(".safetensors")]
print(cands[0] if cands else "")
PYEOF
)
  if [ -z "$REL" ]; then
    echo "  API 没查到，尝试常见路径..."
    for u in "architectures/diffusers/SD1.5/v1-5-pruned-emaonly.safetensors" "v1-5-pruned-emaonly.safetensors"; do
      code=$(wget -S -O /dev/null "$HF/Comfy-Org/stable-diffusion-v1-5/resolve/main/$u" 2>&1 | grep -oE "HTTP/[0-9.]+ [0-9]+" | tail -1 | grep -oE "[0-9]+$")
      if [ "$code" = "200" ]; then REL="$u"; echo "  命中: $u"; break; fi
    done
  fi
  if [ -z "$REL" ]; then
    echo "  [ERROR] 找不到 checkpoint，把 huggingface_hub 列表输出贴给我"
    python - <<'PYEOF'
from huggingface_hub import HfApi
api = HfApi(endpoint="https://hf-mirror.com")
for f in api.list_repo_files("Comfy-Org/stable-diffusion-v1-5"):
    if f.endswith((".safetensors", ".ckpt")):
        print(f)
PYEOF
    exit 1
  fi
  echo "  下载: $HF/Comfy-Org/stable-diffusion-v1-5/resolve/main/$REL"
  wget -c -O "$CHECKPOINT" "$HF/Comfy-Org/stable-diffusion-v1-5/resolve/main/$REL"
  ls -lh "$CHECKPOINT"
fi

echo "==== [4/4] 转换 LoRA（diffusers -> bfla） ===="
cd "$PIPE" || exit 1
"$VENV/bin/python" convert_lora.py \
  --src "$AIGC/gen_project/outputs/lora" \
  --dst "$COMFY/models/loras/mtg_lora.safetensors"

echo ""
echo "==== 完成 ===="
ls -lh "$COMFY/models/checkpoints/" "$COMFY/models/loras/" 2>/dev/null
