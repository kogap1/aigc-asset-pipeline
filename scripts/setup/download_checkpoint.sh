#!/usr/bin/env bash
# 下载 SD1.5 官方 checkpoint（stable-diffusion-v1-5 仓库，gated）
# 直链可下就用直链；401 则用 HF token；都没 token 则提示用合并脚本兜底。
# 用法: bash scripts/setup/download_checkpoint.sh
set -uo pipefail
COMFY=~/yl/aigc/comfyui
DST="$COMFY/models/checkpoints/v1-5-pruned-emaonly.safetensors"
URL="https://hf-mirror.com/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors"

rm -f "$DST"
code=$(wget -S -O /dev/null "$URL" 2>&1 | grep -oE "HTTP/[0-9.]+ [0-9]+" | tail -1 | grep -oE "[0-9]+$")
echo "直链状态码: $code"

if [ "$code" = "200" ]; then
  echo ">> 无需 token，直接下载"
  wget -c -O "$DST" "$URL"
elif [ -n "${HF_TOKEN:-}" ]; then
  echo ">> gated，用 HF_TOKEN 环境变量下载"
  wget -c -O "$DST" --header="Authorization: Bearer $HF_TOKEN" "$URL"
elif [ -f ~/.cache/huggingface/token ]; then
  echo ">> gated，用已登录 token 下载"
  TOKEN=$(cat ~/.cache/huggingface/token)
  wget -c -O "$DST" --header="Authorization: Bearer $TOKEN" "$URL"
else
  echo ""
  echo ">> 仓库 gated 且没有 token，二选一："
  echo "   A) 拿 token 下载（浏览器登录 https://huggingface.co → 打开"
  echo "      https://huggingface.co/settings/tokens 复制 token，然后："
  echo "      export HF_TOKEN=hf_xxx; bash scripts/setup/download_checkpoint.sh"
  echo "      注意：登录后还要先访问仓库页面点同意许可才行）"
  echo "   B) 用合并脚本从本地缓存转（推荐，零下载，30 秒）:"
  echo "      python tools/convert_diffusers_to_sd.py --dst $DST"
  exit 1
fi

echo ""
ls -lh "$DST"
