#!/usr/bin/env bash
# 端到端小批量：一句话需求 → 全链路 → 检查 deliverables + manifest
# 前置: ComfyUI 已启动，config.yaml 的 input_dir 已填，.env 里 DEEPSEEK_API_KEY 已填
# 用法: bash scripts/run/run_e2e.sh
set -euo pipefail

PIPE=~/yl/aigc/asset_pipeline
cd "$PIPE" || exit 1

REQ="${1:-做 3 张 MTG 风格的中国风武将卡牌图，史诗构图与戏剧性光影}"

echo "==> 需求: $REQ"
python agent_pipeline.py run --req "$REQ"

echo "==> 最新批次:"
LATEST=$(ls -dt deliverables/batch_* 2>/dev/null | head -1)
[ -n "$LATEST" ] || { echo "[ERROR] 未生成批次目录"; exit 1; }
echo "  $LATEST"
echo "==> manifest.json:"
cat "$LATEST/manifest.json" 2>/dev/null | head -60
echo "==> 交付图片:"
find "$LATEST" -maxdepth 1 -type f ! -name 'manifest.json' ! -name 'state.json' -print
