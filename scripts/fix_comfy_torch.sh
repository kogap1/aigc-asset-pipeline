#!/usr/bin/env bash
# fix_comfy_torch.sh — ComfyUI 与 torch 2.5.1 不兼容自动修复（自愈版）
# 方案A: 卸载 comfy_kitchen 走 fallback；失败则 方案B: git 回退 ComfyUI 到 torch2.5 兼容版本
# 用法: bash scripts/fix_comfy_torch.sh
set -uo pipefail

COMFY=~/yl/aigc/comfyui
VENV=~/yl/aigc/comfyenv
LOG=~/yl/aigc/comfy.log
BASE="http://127.0.0.1:8188"

stop_comfy() { pkill -f "comfyui/main.py" 2>/dev/null; sleep 1; }
start_comfy() {
  nohup "$VENV/bin/python" "$COMFY/main.py" --listen 0.0.0.0 --port 8188 > "$LOG" 2>&1 &
}
wait_ready() {
  for i in $(seq 1 60); do
    curl -sf "$BASE/system_stats" > /dev/null 2>&1 && { echo "  ✔ 就绪 (第 ${i} 次探测)"; return 0; }
    sleep 2
  done
  echo "  ✘ 120 秒未就绪"
  return 1
}

echo "==== [诊断] quant_ops.py 里 comfy_kitchen 的 import 结构 ===="
grep -n -B3 -A6 "comfy_kitchen" "$COMFY/comfy/quant_ops.py" | head -40

echo ""
echo "==== [方案A] 移除 comfy_kitchen ===="
"$VENV/bin/pip" uninstall -y comfy_kitchen 2>&1 | tail -1
# pip 常认不出它（元数据对不上），直接删目录兜底（venv + anaconda aigc 两处都清）
rm -rf "$VENV/lib/python3.10/site-packages/comfy_kitchen"*
rm -rf ~/anaconda3/envs/aigc/lib/python3.10/site-packages/comfy_kitchen"*" 2>/dev/null
ls -d "$VENV/lib/python3.10/site-packages/comfy_kitchen"* 2>/dev/null || echo "  已移除干净"

stop_comfy
start_comfy
echo "  等待就绪..."
if wait_ready; then
  echo "  → 方案A 成功"
  exit 0
fi
echo "  方案A 失败，日志尾部:"
tail -15 "$LOG"

echo ""
if grep -q "comfy_kitchen" "$LOG"; then
  echo "==== [方案B] 回退 ComfyUI 到 torch2.5 兼容版本 ===="
  cd "$COMFY" || exit 1
  INTRO=$(git log -S "comfy_kitchen" --format="%H" -- comfy/quant_ops.py | tail -1)
  if [ -z "$INTRO" ]; then
    echo "  [ERROR] 没找到引入 comfy_kitchen 的 commit，把下面 git log 输出发回:"
    git log --oneline -5
    exit 2
  fi
  PARENT=$(git rev-parse "$INTRO^")
  echo "  引入 commit: $INTRO"
  echo "  切到父提交: $PARENT"
  git checkout "$PARENT"
  stop_comfy
  start_comfy
  echo "  等待就绪..."
  if wait_ready; then
    echo "  → 方案B 成功"
    exit 0
  fi
  echo "  [ERROR] 方案B 仍失败，日志尾部:"
  tail -30 "$LOG"
  exit 1
else
  echo "  [ERROR] 日志不是 comfy_kitchen 报错，是别的问题。日志尾部:"
  tail -30 "$LOG"
  exit 3
fi
