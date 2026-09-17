#!/usr/bin/env bash
# 4×A40 工业化求职项目正式验收：启动4 worker并行完成默认600张。
# 用法: bash scripts/run_industrial_benchmark.sh
# 断点续跑: BATCH_ID=production_600_... bash scripts/run_industrial_benchmark.sh
set -euo pipefail

PIPE="${PIPE:-$HOME/yl/aigc/asset_pipeline}"
BASE_PORT="${BASE_PORT:-8188}"
WORKER_COUNT="${WORKER_COUNT:-4}"

cd "$PIPE"
bash scripts/start_comfy_workers.sh

WORKERS=""
for worker in $(seq 0 $((WORKER_COUNT - 1))); do
  [ -n "$WORKERS" ] && WORKERS+=","
  WORKERS+="http://127.0.0.1:$((BASE_PORT + worker))"
done
export COMFYUI_WORKERS="$WORKERS"
echo "worker pool: $COMFYUI_WORKERS"

if [ "${RUN_SCALING:-1}" = "1" ]; then
  bash scripts/run_scaling_benchmark.sh
fi

bash scripts/run_production_benchmark.sh
