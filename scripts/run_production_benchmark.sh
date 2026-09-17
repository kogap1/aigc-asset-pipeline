#!/usr/bin/env bash
# 正式生产验收：默认 6 类 × 100 张 = 600 张最终交付。
# 用法: bash scripts/run_production_benchmark.sh
# 断点续跑: BATCH_ID=production_600_YYYYMMDD_HHMMSS bash scripts/run_production_benchmark.sh
set -euo pipefail

PIPE="${PIPE:-$HOME/yl/aigc/asset_pipeline}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8188}"
REAL_DIR="${REAL_DIR:-$HOME/yl/aigc/gen_project/data/clean/eval}"
REFERENCE_METRICS="${REFERENCE_METRICS:-$HOME/yl/aigc/gen_project/metrics/metrics.json}"
PER_SCENARIO="${PER_SCENARIO:-100}"
TOTAL=$((6 * PER_SCENARIO))
BATCH_ID="${BATCH_ID:-production_${TOTAL}_$(date +%Y%m%d_%H%M%S)}"
MIN_FREE_GB="${MIN_FREE_GB:-20}"

cd "$PIPE"
echo "==== [1/5] 检查磁盘 ===="
FREE_KB=$(df -Pk "$PIPE" | awk 'NR==2 {print $4}')
NEEDED_KB=$((MIN_FREE_GB * 1024 * 1024))
if [ "$FREE_KB" -lt "$NEEDED_KB" ]; then
  echo "[ERROR] 可用磁盘不足 ${MIN_FREE_GB}GB"
  exit 1
fi
df -h "$PIPE"

echo "==== [2/5] 检查 ComfyUI ===="
curl -sf "$BASE_URL/system_stats" >/dev/null \
  || { echo "[ERROR] ComfyUI 未运行: $BASE_URL"; exit 1; }

echo "==== [3/5] 校验工作流 ===="
python validate_wf.py --base_url "$BASE_URL"

echo "==== [4/5] 检查 Agent 密钥 ===="
python -c 'from agent_pipeline import load_config, DeepSeekAgent; a=DeepSeekAgent(load_config()); assert a.api_key and "xxx" not in a.api_key, "请在 .env 填写 DEEPSEEK_API_KEY"; print("DeepSeek key OK")'

echo "==== [5/5] 正式生产验收：$BATCH_ID，共 $TOTAL 张 ===="
ARGS=(
  --batch-id "$BATCH_ID" \
  --per-scenario "$PER_SCENARIO" \
  --real-dir "$REAL_DIR" \
  --reference-metrics "$REFERENCE_METRICS"
)
if [ "${SKIP_FID:-0}" = "1" ]; then
  ARGS+=(--skip-fid)
fi
python production_benchmark.py "${ARGS[@]}"

echo "==== 完成 ===="
echo "结果: $PIPE/deliverables/$BATCH_ID/summary.json"
echo "图片: $PIPE/deliverables/$BATCH_ID/final_samples_grid.png"
