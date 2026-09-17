#!/usr/bin/env bash
# fix_rembg.sh — 查清 RemBG 节点：确保 onnxruntime → 重启 → 校验 → 通过则冒烟
# 用法: bash scripts/fix_rembg.sh
set -uo pipefail

VENV=~/yl/aigc/comfyenv
COMFY=~/yl/aigc/comfyui
PIPE=~/yl/aigc/asset_pipeline
LOG=~/yl/aigc/comfy.log

echo "==== [1/5] 确保 onnxruntime 装好 ===="
if "$VENV/bin/python" -c "import onnxruntime" 2>/dev/null; then
  echo "  onnxruntime 已装: $("$VENV/bin/python" -c 'import onnxruntime; print(onnxruntime.__version__)')"
else
  echo "  onnxruntime 缺失，安装中..."
  "$VENV/bin/pip" install onnxruntime -i https://pypi.tuna.tsinghua.edu.cn/simple
fi

echo "==== [2/5] 验证 rembg 可导入 ===="
"$VENV/bin/python" -c "import rembg; print('  rembg OK', rembg.__version__)" 2>&1 | tail -3

echo "==== [3/5] 重启 ComfyUI ===="
pkill -f "comfyui/main.py" 2>/dev/null; sleep 2
nohup "$VENV/bin/python" "$COMFY/main.py" --listen 0.0.0.0 --port 8188 > "$LOG" 2>&1 &
for i in $(seq 1 60); do
  curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && { echo "  就绪(第 $i 次)"; break; }
  sleep 2
done

echo "==== [4/5] 校验节点 ===="
cd "$PIPE" || exit 1
if python validate_wf.py; then
  echo ""
  echo "==== [5/5] 全部通过，冒烟出图 ===="
  bash scripts/test_wf.sh
else
  echo ""
  echo "==== [5/5] 还有缺失，列出实际注册的抠图/分割相关节点名 ===="
  curl -s http://127.0.0.1:8188/object_info > /tmp/oi.json
  python -c "import json; d=json.load(open('/tmp/oi.json')); print(sorted([k for k in d if any(s in k for s in ['em','bg','BG','Seg','seg','Rem'])]))"
fi
