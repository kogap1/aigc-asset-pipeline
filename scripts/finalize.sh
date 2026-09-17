#!/usr/bin/env bash
# finalize.sh — 收尾一键：检查 checkpoint + 填配置 + 起 ComfyUI + 冒烟测试
# 前置: checkpoint 已就位（convert_diffusers_to_sd.py 合并完成）
# 用法: bash scripts/finalize.sh
set -uo pipefail

AIGC=~/yl/aigc
COMFY=$AIGC/comfyui
VENV=$AIGC/comfyenv
PIPE=$AIGC/asset_pipeline
BASE_URL="http://127.0.0.1:8188"
INPUT_DIR="$COMFY/input"
CHECKPOINT="$COMFY/models/checkpoints/v1-5-pruned-emaonly.safetensors"

echo "==== [1/4] 检查 checkpoint ===="
if [ -s "$CHECKPOINT" ]; then
  ls -lh "$CHECKPOINT"
else
  echo "[ERROR] checkpoint 不存在或为空: $CHECKPOINT"
  echo "  先跑: python $PIPE/scripts/convert_diffusers_to_sd.py --dst $CHECKPOINT"
  exit 1
fi

echo "==== [2/4] 填配置 (.env + config.yaml) ===="
ENV_FILE="$PIPE/.env"
if [ -s "$ENV_FILE" ]; then
  echo "  .env 已存在，跳过"
else
  echo "  未找到 .env，请输入 DeepSeek API Key（输入不回显）:"
  read -rs KEY
  echo
  if [ -z "$KEY" ]; then
    echo "  [ERROR] 空 key，手动补: echo 'DEEPSEEK_API_KEY=sk-xxx' > $ENV_FILE"
    exit 1
  fi
  printf 'DEEPSEEK_API_KEY=%s\n' "$KEY" > "$ENV_FILE"
  echo "  .env 已写入"
fi

CFG="$PIPE/config.yaml"
if grep -q 'input_dir: ""' "$CFG"; then
  sed -i "s#input_dir: \"\"#input_dir: \"$INPUT_DIR\"#" "$CFG"
  echo "  config.yaml: input_dir -> $INPUT_DIR"
else
  echo "  config.yaml: input_dir 已设置"
fi
grep -n input_dir "$CFG"

echo "==== [3/4] 启动 ComfyUI ===="
if curl -sf "$BASE_URL/system_stats" > /dev/null 2>&1; then
  echo "  已在运行，跳过"
else
  mkdir -p "$INPUT_DIR"
  nohup "$VENV/bin/python" "$COMFY/main.py" --listen 0.0.0.0 --port 8188 > "$AIGC/comfy.log" 2>&1 &
  echo "  已后台启动 (日志: $AIGC/comfy.log)"
  echo "  等待就绪..."
  for i in $(seq 1 60); do
    if curl -sf "$BASE_URL/system_stats" > /dev/null 2>&1; then
      echo "  就绪 (第 ${i} 次探测)"
      break
    fi
    [ "$i" -eq 60 ] && { echo "  [ERROR] 120 秒未就绪，看日志: tail -30 $AIGC/comfy.log"; exit 1; }
    sleep 2
  done
fi

echo "==== [4/4] 冒烟测试 ===="
cd "$PIPE" || exit 1
bash scripts/test_wf.sh
