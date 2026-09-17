#!/usr/bin/env bash
# 启动 4×A40 对应的 4 个 ComfyUI worker（端口 8188-8191）。
# 已就绪的端口不会重复启动。用法: bash scripts/run/start_comfy_workers.sh
set -euo pipefail

AIGC="${AIGC:-$HOME/yl/aigc}"
COMFY="${COMFY:-$AIGC/comfyui}"
COMFY_PY="${COMFY_PY:-$AIGC/comfyenv/bin/python}"
WORKER_COUNT="${WORKER_COUNT:-4}"
BASE_PORT="${BASE_PORT:-8188}"
LOG_DIR="${LOG_DIR:-$AIGC/comfy_workers_logs}"
mkdir -p "$LOG_DIR"

GPU_COUNT=$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
if [ "$GPU_COUNT" -lt "$WORKER_COUNT" ]; then
  echo "[ERROR] 检测到 ${GPU_COUNT} 张 GPU，少于要求的 ${WORKER_COUNT} 张"
  exit 1
fi

for worker in $(seq 0 $((WORKER_COUNT - 1))); do
  port=$((BASE_PORT + worker))
  url="http://127.0.0.1:$port"
  log="$LOG_DIR/worker_${worker}_${port}.log"
  if curl -sf "$url/system_stats" >/dev/null; then
    echo "[SKIP] worker=$worker gpu=$worker port=$port 已就绪"
    continue
  fi
  echo "[START] worker=$worker gpu=$worker port=$port"
  nohup env CUDA_VISIBLE_DEVICES="$worker" "$COMFY_PY" "$COMFY/main.py" \
    --listen 127.0.0.1 --port "$port" > "$log" 2>&1 &
done

echo "等待 worker 就绪..."
for worker in $(seq 0 $((WORKER_COUNT - 1))); do
  port=$((BASE_PORT + worker))
  url="http://127.0.0.1:$port"
  ready=0
  for _ in $(seq 1 90); do
    if curl -sf "$url/system_stats" >/dev/null; then
      ready=1
      break
    fi
    sleep 2
  done
  if [ "$ready" != "1" ]; then
    echo "[ERROR] worker=$worker port=$port 180秒未就绪"
    tail -80 "$LOG_DIR/worker_${worker}_${port}.log" 2>/dev/null || true
    exit 1
  fi
  device=$(curl -sf "$url/system_stats" | python -c 'import json,sys; d=json.load(sys.stdin)["devices"][0]; print(d["name"])')
  echo "[OK] worker=$worker port=$port device=$device"
done

echo "COMFYUI_WORKERS=$(for i in $(seq 0 $((WORKER_COUNT - 1))); do printf 'http://127.0.0.1:%s' "$((BASE_PORT+i))"; [ "$i" -lt $((WORKER_COUNT - 1)) ] && printf ','; done)"
