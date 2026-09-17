#!/usr/bin/env bash
# 1 worker vs 4 worker 完整链路伸缩性预实验，各30张。
# 用法: bash scripts/run_scaling_benchmark.sh
set -euo pipefail

PIPE="${PIPE:-$HOME/yl/aigc/asset_pipeline}"
BASE_PORT="${BASE_PORT:-8188}"
WORKER_COUNT="${WORKER_COUNT:-4}"
STAMP="${STAMP:-$(date +%Y%m%d_%H%M%S)}"
SINGLE_BATCH="scaling_1w_30_$STAMP"
MULTI_BATCH="scaling_${WORKER_COUNT}w_30_$STAMP"
OUT_DIR="$PIPE/deliverables/scaling_$STAMP"

cd "$PIPE"
bash scripts/start_comfy_workers.sh

echo "==== 单 worker：30张 ===="
COMFYUI_WORKERS="http://127.0.0.1:$BASE_PORT" \
PER_SCENARIO=5 BATCH_ID="$SINGLE_BATCH" SKIP_FID=1 \
  bash scripts/run_production_benchmark.sh

WORKERS=""
for worker in $(seq 0 $((WORKER_COUNT - 1))); do
  [ -n "$WORKERS" ] && WORKERS+=","
  WORKERS+="http://127.0.0.1:$((BASE_PORT + worker))"
done

echo "==== ${WORKER_COUNT} worker：30张 ===="
COMFYUI_WORKERS="$WORKERS" \
PER_SCENARIO=5 BATCH_ID="$MULTI_BATCH" SKIP_FID=1 \
  bash scripts/run_production_benchmark.sh

python scaling_summary.py \
  --single "$PIPE/deliverables/$SINGLE_BATCH/summary.json" \
  --multi "$PIPE/deliverables/$MULTI_BATCH/summary.json" \
  --workers "$WORKER_COUNT" \
  --out "$OUT_DIR/summary.json"

echo "伸缩性结果: $OUT_DIR/summary.json"
