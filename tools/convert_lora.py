"""把 diffusers/peft 格式的 LoRA 转换为 ComfyUI bfla 格式。

diffusers 格式 key:
  base_model.model.unet.down_blocks.0.attentions.0.transformer_blocks.0.attn1.to_q.lora_A.weight
  base_model.model.text_encoder.text_model.encoder.layers.0.self_attn.q_proj.lora_A.weight

bfla 格式 key（ComfyUI 期望）:
  lora_unet_down_blocks.0.attentions.0.transformer_blocks.0.attn1.to_q.lora_down.weight
  lora_te1_text_model.encoder.layers.0.self_attn.q_proj.lora_down.weight

用法:
  python tools/convert_lora.py --src <diffusers目录或safetensors> --dst <out.safetensors>
"""
import argparse
import json
import re
import sys
from pathlib import Path

from safetensors.torch import load_file, save_file

PREFIX_MAP = [
    ("base_model.model.unet.", "lora_unet_"),
    ("base_model.model.text_encoder.", "lora_te1_"),
    ("unet.", "lora_unet_"),
    ("text_encoder.", "lora_te1_"),
]


def _build_unet_map():
    """diffusers UNet 模块路径 → ComfyUI 路径（SD1.5 固定结构）。

    diffusers 用 down_blocks.N / up_blocks.N / mid_block，
    ComfyUI 用 input_blocks.K / output_blocks.K / middle_block。
    只换前缀不换结构会让 ComfyUI 一个 LoRA key 都对不上（日志里全是
    "lora key not loaded"），MTG LoRA 等于白挂。
    """
    m = {
        "conv_in": "input_blocks.0.0",
        "conv_out": "output_blocks.15.0",
    }
    for i in range(4):  # down_blocks
        base = 1 + 3 * i
        if i < 3:
            m[f"down_blocks.{i}.downsamplers.0.conv"] = f"input_blocks.{base + 2}.0.op"
        for j in range(2):
            m[f"down_blocks.{i}.resnets.{j}"] = f"input_blocks.{base + j}.0"
            if i < 3:
                m[f"down_blocks.{i}.attentions.{j}"] = f"input_blocks.{base + j}.1"
    m["mid_block.resnets.0"] = "middle_block.0"
    m["mid_block.attentions.0"] = "middle_block.1"
    m["mid_block.resnets.1"] = "middle_block.2"
    for i in range(4):  # up_blocks
        base = 4 * i
        for j in range(3):
            m[f"up_blocks.{i}.resnets.{j}"] = f"output_blocks.{base + j}.0"
            if i < 3:
                m[f"up_blocks.{i}.attentions.{j}"] = f"output_blocks.{base + j}.1"
        if i < 3:
            m[f"up_blocks.{i}.upsamplers.0.conv"] = f"output_blocks.{base + 3}.0.op"
    return sorted(m.items(), key=lambda x: len(x[0]), reverse=True)  # 长 key 优先，避免前缀误匹配


UNET_MAP = _build_unet_map()


def remap_unet(path: str):
    for old, new in UNET_MAP:
        if path == old or path.startswith(old + "."):
            return new + path[len(old):]
    return None


def convert_key(key: str):
    for old_pfx, new_pfx in PREFIX_MAP:
        if not key.startswith(old_pfx):
            continue
        k = new_pfx + key[len(old_pfx):]
        m = re.match(r"^(.*?)\.(lora_[AB])\.(weight|alpha)$", k)
        if not m:
            return None
        path, lora_part, tail = m.groups()
        if new_pfx == "lora_unet_":
            suffix = remap_unet(path[len(new_pfx):])  # 映射表里是不带 lora_unet_ 前缀的路径
            if suffix is None:
                return None
            path = new_pfx + suffix
        k = f"{path}.{lora_part}.{tail}"
        # 统一点号分隔 + ComfyUI 的 lora_down/lora_up 命名
        for a, b in ((".lora_A.weight", ".lora_down.weight"),
                     (".lora_B.weight", ".lora_up.weight"),
                     (".lora_A.alpha", ".alpha"),
                     (".lora_B.alpha", ".alpha")):
            k = k.replace(a, b)
        return k
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="diffusers LoRA 目录或单个 .safetensors")
    ap.add_argument("--dst", required=True, help="输出 bfla .safetensors")
    args = ap.parse_args()

    src = Path(args.src)
    if src.is_dir():
        # diffusers 不同版本命名：新版 adapter_model.safetensors，旧版 pytorch_lora_weights.safetensors
        src_file = next((src / c for c in ("adapter_model.safetensors", "pytorch_lora_weights.safetensors")
                         if (src / c).exists()), None)
        if src_file is None:
            print(f"[ERROR] 目录里没有 adapter_model.safetensors / pytorch_lora_weights.safetensors: {src}")
            return 1
    else:
        src_file = src

    tensors = load_file(str(src_file))
    converted = {}
    n_skip = 0
    n_unet = n_te = 0
    n_down = n_up = 0
    for k, v in tensors.items():
        nk = convert_key(k)
        if nk is None:
            n_skip += 1
            continue
        converted[nk] = v
        if nk.startswith("lora_unet_"):
            n_unet += 1
        elif nk.startswith("lora_te1_"):
            n_te += 1
        if nk.endswith("lora_down.weight"):
            n_down += 1
        if nk.endswith("lora_up.weight"):
            n_up += 1

    if not converted:
        print("[ERROR] 没有可转换的 key，检查 LoRA 是否为 diffusers/peft 格式。")
        return 1

    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"modelspec.architecture": "sd1", "converted_by": "convert_lora.py"}
    save_file(converted, str(dst), metadata=metadata)

    print(f"原始 key 总数: {len(tensors)}")
    print(f"转换成功: {len(converted)} (unet={n_unet}, te1={n_te})  跳过: {n_skip}")
    print(f"lora_down={n_down}  lora_up={n_up}")
    if n_down != n_up:
        print("[WARN] down/up 数量不一致，ComfyUI 可能加载失败")
    else:
        print("[OK] down/up 成对，数量一致")
    print(f"已写出: {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
