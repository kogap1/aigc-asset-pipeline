"""校验 ComfyUI 工作流模板引用的节点类名是否已安装。

用法: python validate_wf.py [--base_url http://127.0.0.1:8188] [--workflows comfyui/workflows]
有缺失节点时退出码为 1，方便脚本判断。
"""
import argparse
import json
import sys
from pathlib import Path

import requests


def get_available_classes(base_url: str) -> set:
    r = requests.get(f"{base_url}/object_info", timeout=30)
    r.raise_for_status()
    return set(r.json().keys())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base_url", default="http://127.0.0.1:8188")
    ap.add_argument("--workflows", default="comfyui/workflows")
    args = ap.parse_args()

    wf_dir = Path(args.workflows)
    if not wf_dir.is_dir():
        print(f"[ERROR] 工作流目录不存在: {wf_dir}")
        return 1

    available = get_available_classes(args.base_url)
    missing_all = {}
    ok = 0
    for wf in sorted(wf_dir.glob("wf_*.json")):
        data = json.loads(wf.read_text(encoding="utf-8"))
        classes = {n.get("class_type") for n in data["prompt"].values()}
        missing = sorted(classes - available)
        if missing:
            missing_all[wf.name] = missing
            print(f"[FAIL] {wf.name}: 缺失节点 {missing}")
        else:
            ok += 1
            print(f"[OK]   {wf.name}: {len(classes)} 个节点全部存在")

    if missing_all:
        print("\n缺失清单：")
        for wf, miss in missing_all.items():
            print(f"  {wf}: {miss}")
        print("→ 请用 ComfyUI-Manager 安装对应自定义节点后重试。")
        return 1
    print(f"\n全部 {ok} 个工作流校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
