#!/usr/bin/env bash
# 正式生产验收（唯一入口）：默认 6 类 × 100 张 = 600 张最终交付。
#
# 用法: bash scripts/run/run_production_benchmark.sh
# 断点续跑: BATCH_ID=production_600_YYYYMMDD_HHMMSS bash scripts/run/run_production_benchmark.sh
#
# 环境变量:
#   PER_SCENARIO     每场景张数，默认 100（=16 即 96 张桥接规模）
#   WORKERS          ComfyUI worker 数，默认 1；>1 时自动拉起 worker 池（端口从 BASE_PORT 递增）
#   RUN_SCALING      默认 0；=1 时额外跑「1 worker 30 张 vs N worker 30 张」并出伸缩对比
#   MIN_FREE_GB      磁盘下限，默认 20
#   SKIP_FID         默认 0；=1 跳过 FID 计算
#   BATCH_ID         指定批次号以断点续跑
#   COMFYUI_WORKERS  显式指定 worker 地址列表（逗号分隔）；指定后不再按 WORKERS 自动生成
set -euo pipefail

PIPE="${PIPE:-$HOME/yl/aigc/asset_pipeline}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8188}"
BASE_PORT="${BASE_PORT:-8188}"
REAL_DIR="${REAL_DIR:-$HOME/yl/aigc/gen_project/data/clean/eval}"
REFERENCE_METRICS="${REFERENCE_METRICS:-$HOME/yl/aigc/gen_project/metrics/metrics.json}"
PER_SCENARIO="${PER_SCENARIO:-100}"
WORKERS="${WORKERS:-1}"
RUN_SCALING="${RUN_SCALING:-0}"
TOTAL=$((6 * PER_SCENARIO))
BATCH_ID="${BATCH_ID:-production_${TOTAL}_$(date +%Y%m%d_%H%M%S)}"
MIN_FREE_GB="${MIN_FREE_GB:-20}"

build_worker_list() {  # build_worker_list <worker 数>
  local count="$1" out="" i
  for i in $(seq 0 $((count - 1))); do
    [ -n "$out" ] && out+=","
    out+="http://127.0.0.1:$((BASE_PORT + i))"
  done
  printf '%s' "$out"
}

cd "$PIPE"

echo "==== [1/5] 检查磁盘 ===="
FREE_KB=$(df -Pk "$PIPE" | awk 'NR==2 {print $4}')
NEEDED_KB=$((MIN_FREE_GB * 1024 * 1024))
if [ "$FREE_KB" -lt "$NEEDED_KB" ]; then
  echo "[ERROR] 可用磁盘不足 ${MIN_FREE_GB}GB"
  exit 1
fi
df -h "$PIPE"

echo "==== [2/5] 准备 worker ===="
if [ -z "${COMFYUI_WORKERS:-}" ]; then
  if [ "$WORKERS" -gt 1 ]; then
    WORKER_COUNT="$WORKERS" BASE_PORT="$BASE_PORT" bash scripts/run/start_comfy_workers.sh
  fi
  COMFYUI_WORKERS="$(build_worker_list "$WORKERS")"
fi
export COMFYUI_WORKERS
echo "worker pool: $COMFYUI_WORKERS"

echo "==== [3/5] 检查 ComfyUI、工作流与 Agent 密钥 ===="
curl -sf "$BASE_URL/system_stats" >/dev/null \
  || { echo "[ERROR] ComfyUI 未运行: $BASE_URL"; exit 1; }
python tools/validate_wf.py --base_url "$BASE_URL"
python -c 'from agent_pipeline import load_config, DeepSeekAgent; a=DeepSeekAgent(load_config()); assert a.api_key and "xxx" not in a.api_key, "请在 .env 填写 DEEPSEEK_API_KEY"; print("DeepSeek key OK")'

echo "==== [4/5] 正式生产验收：$BATCH_ID，共 $TOTAL 张 ===="
ARGS=(
  --batch-id "$BATCH_ID"
  --per-scenario "$PER_SCENARIO"
  --real-dir "$REAL_DIR"
  --reference-metrics "$REFERENCE_METRICS"
)
if [ "${SKIP_FID:-0}" = "1" ]; then
  ARGS+=(--skip-fid)
fi
python benchmarks/production.py "${ARGS[@]}"
echo "结果: $PIPE/deliverables/$BATCH_ID/summary.json"
echo "网格: $PIPE/deliverables/$BATCH_ID/final_samples_grid.png"

if [ "$RUN_SCALING" = "1" ]; then
  echo "==== [5/5] 伸缩性对比：1 worker vs $WORKERS worker，各 30 张 ===="
  STAMP="$(date +%Y%m%d_%H%M%S)"
  SINGLE_BATCH="scaling_1w_30_$STAMP"
  MULTI_BATCH="scaling_${WORKERS}w_30_$STAMP"
  SCALING_DIR="$PIPE/deliverables/scaling_$STAMP"

  echo "-- 单 worker：30 张 --"
  # 自调用必须带 RUN_SCALING=0，否则外层 RUN_SCALING=1 被继承会导致无限递归
  COMFYUI_WORKERS="http://127.0.0.1:$BASE_PORT" \
  PER_SCENARIO=5 BATCH_ID="$SINGLE_BATCH" SKIP_FID=1 RUN_SCALING=0 \
    bash scripts/run/run_production_benchmark.sh

  echo "-- $WORKERS worker：30 张 --"
  COMFYUI_WORKERS="$COMFYUI_WORKERS" \
  PER_SCENARIO=5 BATCH_ID="$MULTI_BATCH" SKIP_FID=1 RUN_SCALING=0 \
    bash scripts/run/run_production_benchmark.sh

  mkdir -p "$SCALING_DIR"
  python benchmarks/scaling_summary.py \
    --single "$PIPE/deliverables/$SINGLE_BATCH/summary.json" \
    --multi "$PIPE/deliverables/$MULTI_BATCH/summary.json" \
    --workers "$WORKERS" \
    --out "$SCALING_DIR/summary.json"
  echo "伸缩性结果: $SCALING_DIR/summary.json"
else
  echo "==== [5/5] 伸缩性对比已跳过（RUN_SCALING=1 可开启） ===="
fi

echo "==== 完成 ===="
