#!/usr/bin/env bash
# 项目阶段一→阶段二桥接实验：同 6 个 prompt × 16 张，共 96 张。
# 可断点续跑。用法: bash scripts/run_benchmark_96.sh
set -euo pipefail

PIPE="${PIPE:-$HOME/yl/aigc/asset_pipeline}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8188}"
REAL_DIR="${REAL_DIR:-$HOME/yl/aigc/gen_project/data/clean/eval}"
OUT_DIR="${OUT_DIR:-$PIPE/deliverables/comfy_lora_96}"
NUM_PER_PROMPT="${NUM_PER_PROMPT:-16}"

cd "$PIPE"
echo "==== [1/3] 检查 ComfyUI ===="
curl -sf "$BASE_URL/system_stats" >/dev/null \
  || { echo "[ERROR] ComfyUI 未运行: $BASE_URL"; exit 1; }

echo "==== [2/3] 校验工作流 ===="
python validate_wf.py --base_url "$BASE_URL"

echo "==== [3/3] 运行统一基准 ===="
python benchmark_96.py \
  --out-dir "$OUT_DIR" \
  --real-dir "$REAL_DIR" \
  --num-per-prompt "$NUM_PER_PROMPT"

echo "==== 基准完成 ===="
echo "指标: $OUT_DIR/summary.json"
echo "明细: $OUT_DIR/manifest.json"
echo "样例: $OUT_DIR/samples_grid.png"
