#!/usr/bin/env bash
# 工作流冒烟：等 ComfyUI 就绪 → 校验节点 → 各工作流出图一张
# 前置: ComfyUI 已启动（nohup ... & ），asset_pipeline 在 ~/yl/aigc/asset_pipeline
# 用法: bash scripts/test_wf.sh
set -uo pipefail

PIPE=~/yl/aigc/asset_pipeline
BASE_URL="${BASE_URL:-http://127.0.0.1:8188}"
cd "$PIPE" || exit 1

echo "==> 等待 ComfyUI 就绪 ($BASE_URL)"
for i in $(seq 1 60); do
  if curl -sf "$BASE_URL/system_stats" > /dev/null 2>&1; then
    echo "   就绪 (第 ${i} 次探测)"
    break
  fi
  [ "$i" -eq 60 ] && { echo "[ERROR] ComfyUI 60 秒未就绪"; exit 1; }
  sleep 2
done

echo "==> 校验工作流节点类名"
if ! python validate_wf.py --base_url "$BASE_URL"; then
  echo "[ERROR] 有节点未安装，请先装齐自定义节点"
  exit 1
fi

echo "==> 各工作流冒烟出图"
python scripts/smoke.py

echo "==> 冒烟输出:"
ls -la deliverables/smoke/ 2>/dev/null
