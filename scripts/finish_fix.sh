#!/usr/bin/env bash
# finish_fix.sh — 收尾：校验 checkpoint → 重转 LoRA → 重启 ComfyUI → 校验 → 冒烟
# 前置: checkpoint 已下载到 models/checkpoints/；本脚本与 convert_lora.py 已更新到服务器
# 用法: bash scripts/finish_fix.sh
set -uo pipefail

AIGC=~/yl/aigc
COMFY=$AIGC/comfyui
COMFY_BIN=$AIGC/comfyenv/bin
PIPE_PY=~/anaconda3/envs/aigc/bin/python
LOG=$AIGC/comfy.log

echo "==== [1/5] 校验 checkpoint 完整（含 CLIP） ===="
"$COMFY_BIN/python" - <<'PY'
import safetensors.torch as st
from pathlib import Path
p = Path.home() / "yl/aigc/comfyui/models/checkpoints/v1-5-pruned-emaonly.safetensors"
try:
    ks = list(st.load_file(str(p)))
except Exception as e:
    print(f"[ERROR] checkpoint 加载失败: {e}")
    raise SystemExit(1)
ok = any("cond_stage_model.text_model" in k for k in ks)
print(f"  tensors={len(ks)}  clip_ok={ok}")
if not ok:
    print("[ERROR] checkpoint 缺 CLIP（文件不完整/不是 SD1.5），先修好文件再继续")
    raise SystemExit(1)
PY

echo "==== [2/5] 重新转换 LoRA（convert_lora.py 已修正 key 映射） ===="
cd "$AIGC/asset_pipeline" || exit 1
"$COMFY_BIN/python" convert_lora.py \
  --src "$AIGC/gen_project/outputs/lora" \
  --dst "$COMFY/models/loras/mtg_lora.safetensors"

echo "==== [3/5] 重启 ComfyUI ===="
pkill -f "comfyui/main.py" 2>/dev/null; sleep 2
nohup "$COMFY_BIN/python" "$COMFY/main.py" --listen 0.0.0.0 --port 8188 > "$LOG" 2>&1 &

echo "==== [4/5] 等待就绪 ===="
READY=0
for i in $(seq 1 60); do
  curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && { echo "  就绪 (第 ${i} 次探测)"; READY=1; break; }
  sleep 2
done
[ "$READY" = "1" ] || { echo "[ERROR] ComfyUI 120 秒未就绪"; tail -20 "$LOG"; exit 1; }

echo "==== [5/5] 校验节点 + 冒烟出图 ===="
"$PIPE_PY" validate_wf.py --base_url http://127.0.0.1:8188 || exit 1
"$PIPE_PY" scripts/smoke.py

echo ""
echo "==== 检查 LoRA 是否真正挂上（应输出 0） ===="
grep -c "lora key not loaded" "$LOG" || true
echo "==== 冒烟输出 ===="
ls -la deliverables/smoke/ 2>/dev/null
