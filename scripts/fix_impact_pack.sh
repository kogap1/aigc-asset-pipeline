#!/usr/bin/env bash
# fix_impact_pack.sh — 一次性装齐 Impact-Pack 依赖 + 重启 ComfyUI + 校验 + 冒烟
# 跳过 sam2（GitHub 拉不到，且 RemBG 用不上它）
# 用法: bash scripts/fix_impact_pack.sh
set -uo pipefail

COMFY=~/yl/aigc/comfyui
VENV=~/yl/aigc/comfyenv
PIPE=~/yl/aigc/asset_pipeline
LOG=~/yl/aigc/comfy.log
MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"

restart_comfy() {
  pkill -f "comfyui/main.py" 2>/dev/null
  sleep 2
  nohup "$VENV/bin/python" "$COMFY/main.py" --listen 0.0.0.0 --port 8188 > "$LOG" 2>&1 &
}
wait_ready() {
  for i in $(seq 1 60); do
    curl -sf http://127.0.0.1:8188/system_stats > /dev/null 2>&1 && { echo "  就绪(第 $i 次)"; return 0; }
    sleep 2
  done
  echo "  ✘ 120 秒未就绪"
  return 1
}

echo "==== [1/3] 装齐 Impact-Pack 的 PyPI 依赖 ===="
"$VENV/bin/pip" install segment-anything transformers dill matplotlib scikit-image piexif scipy opencv-python-headless numpy onnxruntime $MIRROR

echo ""
echo "==== [2/3] 重启 ComfyUI ===="
restart_comfy
wait_ready || { echo "看日志:"; tail -30 "$LOG"; exit 1; }

echo ""
echo "==== [3/3] 校验节点 ===="
cd "$PIPE" || exit 1
if python validate_wf.py; then
  echo ""
  echo "==== 全部节点就绪，开始冒烟出图 ===="
  bash scripts/test_wf.sh
else
  echo ""
  echo "==== 仍有节点缺失，看缺什么 ===="
  grep -inE "ModuleNotFoundError|ImportError" "$LOG" | tail -10
  exit 2
fi
