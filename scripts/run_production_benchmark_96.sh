#!/usr/bin/env bash
# 完整生产链路：Agent -> 质量门/重试 -> 超分 -> 最终评分，共交付96张。
# 用法: bash scripts/run_production_benchmark_96.sh
# 断点续跑: BATCH_ID=production_96_YYYYMMDD_HHMMSS bash scripts/run_production_benchmark_96.sh
set -euo pipefail

PIPE="${PIPE:-$HOME/yl/aigc/asset_pipeline}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8188}"
REAL_DIR="${REAL_DIR:-$HOME/yl/aigc/gen_project/data/clean/eval}"
REFERENCE_METRICS="${REFERENCE_METRICS:-$HOME/yl/aigc/gen_project/metrics/metrics.json}"
BATCH_ID="${BATCH_ID:-production_96_$(date +%Y%m%d_%H%M%S)}"

cd "$PIPE"
echo "==== [1/4] 检查 ComfyUI ===="
curl -sf "$BASE_URL/system_stats" >/dev/null \
  || { echo "[ERROR] ComfyUI 未运行: $BASE_URL"; exit 1; }

echo "==== [2/4] 校验工作流 ===="
python validate_wf.py --base_url "$BASE_URL"

echo "==== [3/4] 检查 Agent 密钥 ===="
python -c 'from agent_pipeline import load_config, DeepSeekAgent; a=DeepSeekAgent(load_config()); assert a.api_key and "xxx" not in a.api_key, "请在 .env 填写 DEEPSEEK_API_KEY"; print("DeepSeek key OK")'

echo "==== [4/4] 完整生产实验：$BATCH_ID ===="
python production_benchmark_96.py \
  --batch-id "$BATCH_ID" \
  --per-scenario 16 \
  --real-dir "$REAL_DIR" \
  --reference-metrics "$REFERENCE_METRICS"

echo "==== 完成 ===="
echo "结果: $PIPE/deliverables/$BATCH_ID/summary.json"
echo "图片: $PIPE/deliverables/$BATCH_ID/final_samples_grid.png"
