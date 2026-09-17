"""4 个工作流冒烟出图（需 ComfyUI 已启动）。

流程: 先用 txt2img 生成一张种子图 → 放入 ComfyUI input → 依次跑
      img2img / ipadapter / wf_post，各自确认出图。
用法: python scripts/smoke.py
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_pipeline import Scheduler, load_config

SMOKE_PROMPT = ("a majestic knight in epic mtg card art style, fantasy illustration, "
                "dramatic rim lighting, detailed, high quality, 4k")


def submit_and_download(sched, out_dir, workflow, params, label):
    pid = sched.submit(workflow, params)
    outputs = sched.poll(pid)
    imgs = sched.download_images(outputs, out_dir, label)
    print(f"[OK] {workflow} -> {imgs[0].name}")
    return imgs[0]


def main() -> int:
    cfg = load_config()
    sched = Scheduler(cfg)
    gen = cfg["generation"]
    out_dir = Path("deliverables/smoke")
    out_dir.mkdir(parents=True, exist_ok=True)

    base = {
        "CHECKPOINT": cfg["comfyui"].get("checkpoint", "v1-5-pruned-emaonly.safetensors"),
        "LORA_NAME": gen.get("lora_name", "mtg_lora.safetensors"),
        "LORA_STRENGTH": gen.get("lora_strength", 0.8),
        "POSITIVE": SMOKE_PROMPT,
        "NEGATIVE": gen.get("negative_prompt", ""),
        "WIDTH": gen.get("width", 512),
        "HEIGHT": gen.get("height", 768),
        "BATCH": 1,
        "SEED": 12345,
        "STEPS": gen.get("steps", 28),
        "CFG": gen.get("cfg", 7.0),
        "SAMPLER": gen.get("sampler", "euler"),
        "SCHEDULER": gen.get("scheduler", "normal"),
    }

    seed = submit_and_download(sched, out_dir, "wf_txt2img", {**base, "PREFIX": "smoke_txt2img"}, "smoke_txt2img")
    print("[OK] wf_txt2img 种子图生成完成")

    if not (sched.input_dir and sched.input_dir.is_dir()):
        print("[SKIP] input_dir 未配置，跳过 img2img / ipadapter / post（配好 config 再跑）")
        return 0

    sched.input_dir.mkdir(parents=True, exist_ok=True)
    ref = sched.input_dir / "smoke_seed.png"
    shutil.copy(seed, ref)

    submit_and_download(sched, out_dir, "wf_img2img",
                        {**base, "PREFIX": "smoke_img2img", "IMAGE": ref.name,
                         "DENOISE": gen.get("img2img_denoise", 0.6)}, "smoke_img2img")

    ipa_params = {
        **base, "PREFIX": "smoke_ipadapter", "IMAGE": ref.name,
        "CLIP_VISION": cfg["comfyui"].get("clip_vision", "CLIP-ViT-H.safetensors"),
        "IPADAPTER": cfg["comfyui"].get("ipadapter", "ip-adapter_sd15.safetensors"),
        "IPADAPTER_WEIGHT": cfg["comfyui"].get("ipadapter_weight", 0.8),
    }
    try:
        submit_and_download(sched, out_dir, "wf_ipadapter", ipa_params, "smoke_ipadapter")
    except Exception as e:
        print(f"[WARN] wf_ipadapter 失败(可跳过): {e}")

    pp = cfg.get("postprocess", {})
    try:
        submit_and_download(sched, out_dir, "wf_post",
                            {"IMAGE": ref.name,
                             "REMBG_MODEL": pp.get("rembg_model", "u2net"),
                             "UPSCALE_MODEL": pp.get("upscale_model", "RealESRGAN_x4plus.pth"),
                             "PREFIX": "smoke_post"}, "smoke_post")
    except Exception as e:
        print(f"[WARN] wf_post 失败: {e}")

    print("\n冒烟完成，输出目录: deliverables/smoke")
    return 0


if __name__ == "__main__":
    sys.exit(main())
