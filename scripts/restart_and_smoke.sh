#!/usr/bin/env bash
# 在 checkpoint 软链接修复后安全重启 ComfyUI，并执行节点校验与冒烟。
# 用法: bash scripts/restart_and_smoke.sh
set -uo pipefail

AIGC="${AIGC:-$HOME/yl/aigc}"
PIPE="${PIPE:-$AIGC/asset_pipeline}"
COMFY="${COMFY:-$AIGC/comfyui}"
COMFY_PY="${COMFY_PY:-$AIGC/comfyenv/bin/python}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8188}"
LOG="${COMFY_LOG:-$AIGC/comfy.log}"
CHECKPOINT="$COMFY/models/checkpoints/v1-5-pruned-emaonly.safetensors"

cd "$PIPE" || exit 1

echo "==== [1/5] 校验 checkpoint ===="
if [ ! -s "$CHECKPOINT" ]; then
  echo "[ERROR] checkpoint 不存在或为空: $CHECKPOINT"
  exit 1
fi
ls -lhL "$CHECKPOINT"

echo "==== [2/5] 重启 ComfyUI ===="
mapfile -t PIDS < <(pgrep -f "$COMFY/main.py" || true)
if [ "${#PIDS[@]}" -gt 0 ]; then
  echo "停止旧进程: ${PIDS[*]}"
  kill "${PIDS[@]}"
  for _ in $(seq 1 20); do
    pgrep -f "$COMFY/main.py" >/dev/null || break
    sleep 1
  done
fi

nohup "$COMFY_PY" "$COMFY/main.py" --listen 0.0.0.0 --port 8188 > "$LOG" 2>&1 &
echo "新进程 PID=$!"

echo "==== [3/5] 等待 API 就绪 ===="
READY=0
for i in $(seq 1 60); do
  if curl -sf "$BASE_URL/system_stats" >/dev/null; then
    READY=1
    echo "ComfyUI 已就绪（${i} 次探测）"
    break
  fi
  sleep 2
done
if [ "$READY" != "1" ]; then
  echo "[ERROR] ComfyUI 120 秒未就绪"
  tail -120 "$LOG"
  exit 1
fi

echo "==== [4/5] 校验节点 ===="
python validate_wf.py --base_url "$BASE_URL" || exit 1

echo "==== [5/5] 冒烟出图 ===="
if python scripts/smoke.py; then
  echo "[OK] 重启后冒烟完成"
  exit 0
fi

echo "[ERROR] 冒烟仍失败，输出相关日志："
grep -iE "clip|checkpoint|text encoder|missing|exception|error" "$LOG" | tail -120 || true
exit 1
