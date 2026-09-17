"""把本地 diffusers 模型合并成 ComfyUI 标准单文件 checkpoint（无需下载 4GB 单文件）。

复用 gen_project 已缓存的 botp/stable-diffusion-v1-5（diffusers 目录格式）：
  unet + text_encoder + vae 的 state dict 重命名后合并为一个 .safetensors。
key 前缀（ComfyUI 标准 SD1.5 checkpoint 格式）：
  model.diffusion_model.*  -> UNet
  cond_stage_model.transformer.* -> CLIP 文本编码器
  first_stage_model.*      -> VAE

用法（服务器 aigc 环境，能访问 huggingface 缓存）:
  python tools/convert_diffusers_to_sd.py \
    --model botp/stable-diffusion-v1-5 \
    --dst ~/yl/aigc/comfyui/models/checkpoints/v1-5-pruned-emaonly.safetensors
"""
import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="botp/stable-diffusion-v1-5",
                    help="diffusers 模型名（优先命中本地缓存）")
    ap.add_argument("--dst", required=True, help="输出单文件 checkpoint 路径")
    ap.add_argument("--fp32", action="store_true", help="用 fp32（默认 fp16，体积减半）")
    args = ap.parse_args()

    import torch
    from diffusers import StableDiffusionPipeline
    from safetensors.torch import save_file

    dtype = torch.float32 if args.fp32 else torch.float16
    print(f"加载 {args.model} ...")
    pipe = StableDiffusionPipeline.from_pretrained(args.model, torch_dtype=dtype)

    sd = {}
    n = 0
    for k, v in pipe.unet.state_dict().items():
        sd[f"model.diffusion_model.{k}"] = v
        n += 1
    for k, v in pipe.text_encoder.state_dict().items():
        sd[f"cond_stage_model.transformer.{k}"] = v
        n += 1
    for k, v in pipe.vae.state_dict().items():
        sd[f"first_stage_model.{k}"] = v
        n += 1

    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    save_file(sd, str(dst))
    size = os.path.getsize(dst) / 1e9
    print(f"合并完成: {n} 个 key -> {dst} ({size:.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
