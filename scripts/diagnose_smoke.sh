#!/usr/bin/env bash
# 校验工作流并运行冒烟；失败时自动输出最近一次 ComfyUI 执行异常和日志。
# 用法: bash scripts/diagnose_smoke.sh
set -uo pipefail

PIPE="${PIPE:-$HOME/yl/aigc/asset_pipeline}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8188}"
COMFY_LOG="${COMFY_LOG:-$HOME/yl/aigc/comfy.log}"
cd "$PIPE" || exit 1

echo "==== [1/3] ComfyUI 就绪检查 ===="
curl -sf "$BASE_URL/system_stats" >/dev/null \
  || { echo "[ERROR] ComfyUI 未就绪: $BASE_URL"; exit 1; }

echo "==== [2/3] 工作流节点校验 ===="
python validate_wf.py --base_url "$BASE_URL" || exit 1

echo "==== [3/3] 冒烟出图 ===="
if python scripts/smoke.py; then
  echo "[OK] 冒烟全部完成"
  exit 0
fi

echo
echo "==== 最近一次 ComfyUI execution_error ===="
python - "$BASE_URL" <<'PY'
import json
import sys
import requests

base_url = sys.argv[1]
history = requests.get(f"{base_url}/history", timeout=30).json()
errors = []
for prompt_id, entry in history.items():
    for kind, payload in entry.get("status", {}).get("messages", []):
        if kind == "execution_error":
            errors.append((prompt_id, payload))
if errors:
    prompt_id, payload = errors[-1]
    print("prompt_id:", prompt_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
else:
    print("history 中没有 execution_error")
PY

echo
echo "==== ComfyUI 日志末尾 ===="
tail -120 "$COMFY_LOG" 2>/dev/null || echo "日志不存在: $COMFY_LOG"
exit 1
