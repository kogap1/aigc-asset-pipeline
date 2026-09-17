"""ComfyUI 部署一致性基准：复用 gen_project 的 6 个 prompt × 16 张。

该实验只比较模型部署效果，不经过 Agent 改写、质量重试或后处理：
SD1.5 checkpoint + MTG LoRA -> ComfyUI -> CLIP-Score/FID/性能指标。
生成文件使用确定性命名，重复运行会跳过已有图片并继续未完成项。
"""
import argparse
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_pipeline import QualityGate, Scheduler, _wf_params, load_config
from metrics import compute_fid, make_grid, percentile, write_json, load_records


PROMPTS = [
    "a fantasy knight in ornate golden armor, mtg card art style",
    "a dark fantasy castle on a cliff at dusk, fantasy game card art",
    "a dragon breathing fire over a battlefield, fantasy game card art",
    "an elven wizard casting a lightning spell, mtg card art style",
    "a mystical forest with glowing spirits, fantasy game card art",
    "a necromancer raising undead warriors, dark fantasy card art",
]


def main():
    parser = argparse.ArgumentParser(description="6 prompts × N 张 ComfyUI LoRA 部署基准")
    parser.add_argument("--config", default=None)
    parser.add_argument("--out-dir", default="deliverables/comfy_lora_96")
    parser.add_argument("--real-dir", default="../gen_project/data/clean/eval")
    parser.add_argument("--num-per-prompt", type=int, default=16)
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--skip-fid", action="store_true")
    args = parser.parse_args()
    if args.num_per_prompt < 1:
        parser.error("--num-per-prompt 必须大于 0")

    cfg = load_config(args.config)
    scheduler = Scheduler(cfg)
    gate = QualityGate(cfg)
    out_dir = Path(args.out_dir).resolve()
    generated_dir = out_dir / "generated"
    download_dir = out_dir / "_downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    records = load_records(manifest_path)

    # 与 gen_project/evaluate.py 的实验口径对齐。
    generation = dict(cfg["generation"])
    generation.update({
        "width": 512,
        "height": 512,
        "steps": 30,
        "cfg": 7.5,
        "lora_strength": 1.0,
        "negative_prompt": "",
    })
    requested = len(PROMPTS) * args.num_per_prompt
    print(f"基准规模: {len(PROMPTS)} prompts × {args.num_per_prompt} = {requested} 张")

    for prompt_index, prompt in enumerate(PROMPTS):
        for image_index in range(args.num_per_prompt):
            item_id = f"p{prompt_index:02d}_i{image_index:02d}"
            target = generated_dir / f"{item_id}.png"
            seed = args.seed + prompt_index * 10000 + image_index
            record = records.get(item_id, {
                "item_id": item_id,
                "prompt_index": prompt_index,
                "image_index": image_index,
                "prompt": prompt,
                "seed": seed,
                "image": str(target.relative_to(out_dir)),
            })
            if target.is_file() and target.stat().st_size > 0:
                record["status"] = "generated"
                records[item_id] = record
                print(f"[SKIP] {item_id} 已存在")
                continue

            workflow, params = _wf_params("txt2img", generation, prompt, seed, None, cfg)
            params["PREFIX"] = f"benchmark_{item_id}"
            started = time.perf_counter()
            try:
                prompt_id = scheduler.submit(workflow, params)
                outputs = scheduler.poll(prompt_id)
                images = scheduler.download_images(outputs, download_dir, item_id)
                if not images:
                    raise RuntimeError("ComfyUI 未返回图片")
                shutil.move(str(images[0]), target)
                record.update({
                    "status": "generated",
                    "prompt_id": prompt_id,
                    "generation_sec": round(time.perf_counter() - started, 4),
                })
                print(f"[OK] {item_id} seed={seed} {record['generation_sec']:.2f}s")
            except Exception as error:
                record.update({
                    "status": "failed",
                    "error": str(error),
                    "generation_sec": round(time.perf_counter() - started, 4),
                })
                print(f"[FAIL] {item_id}: {error}")
            records[item_id] = record
            write_json(manifest_path, list(records.values()))

    generated_records = [
        records[f"p{p:02d}_i{i:02d}"]
        for p in range(len(PROMPTS))
        for i in range(args.num_per_prompt)
        if records.get(f"p{p:02d}_i{i:02d}", {}).get("status") == "generated"
        and (out_dir / records[f"p{p:02d}_i{i:02d}"]["image"]).is_file()
    ]

    print("开始计算 CLIP-Score...")
    for index, record in enumerate(generated_records, 1):
        if record.get("clip_score") is None:
            image_path = out_dir / record["image"]
            score_started = time.perf_counter()
            record["clip_score"] = round(gate.score(image_path, record["prompt"]), 6)
            record["score_sec"] = round(time.perf_counter() - score_started, 4)
            write_json(manifest_path, list(records.values()))
        print(f"[CLIP {index}/{len(generated_records)}] {record['item_id']}={record['clip_score']:.4f}")

    scores = [record["clip_score"] for record in generated_records if record.get("clip_score") is not None]
    latencies = [record["generation_sec"] for record in generated_records if record.get("generation_sec") is not None]
    summary = {
        "protocol": {
            "purpose": "gen_project LoRA -> ComfyUI deployment consistency",
            "prompts": PROMPTS,
            "num_per_prompt": args.num_per_prompt,
            "requested": requested,
            "resolution": [512, 512],
            "steps": 30,
            "cfg": 7.5,
            "lora_strength": 1.0,
            "negative_prompt": "",
            "seed_base": args.seed,
        },
        "result": {
            "generated": len(generated_records),
            "failed": requested - len(generated_records),
            "success_rate": round(len(generated_records) / requested, 6),
            "clip_score_mean": round(statistics.mean(scores), 6) if scores else None,
            "clip_score_std": round(statistics.pstdev(scores), 6) if len(scores) > 1 else 0.0 if scores else None,
            "clip_score_min": round(min(scores), 6) if scores else None,
            "clip_score_max": round(max(scores), 6) if scores else None,
            "generation_sec_mean": round(statistics.mean(latencies), 4) if latencies else None,
            "generation_sec_p50": round(percentile(latencies, 0.50), 4) if latencies else None,
            "generation_sec_p95": round(percentile(latencies, 0.95), 4) if latencies else None,
            "throughput_images_per_hour": round(3600 / statistics.mean(latencies), 2) if latencies else None,
        },
    }

    real_dir = Path(args.real_dir).resolve()
    if not args.skip_fid and len(generated_records) == requested and real_dir.is_dir():
        print(f"开始计算 FID: {generated_dir} vs {real_dir}")
        fid_started = time.perf_counter()
        summary["result"]["fid"] = round(compute_fid(generated_dir, real_dir), 6)
        summary["result"]["fid_sec"] = round(time.perf_counter() - fid_started, 4)
    elif not args.skip_fid:
        summary["result"]["fid"] = None
        summary["result"]["fid_note"] = "生成未满目标数量或 real_dir 不存在，未计算 FID"

    legacy_metrics = real_dir.parents[2] / "metrics" / "metrics.json" if len(real_dir.parents) >= 3 else None
    if legacy_metrics and legacy_metrics.is_file():
        summary["gen_project_reference"] = json.loads(legacy_metrics.read_text(encoding="utf-8"))
        summary["comparison_note"] = (
            "同 prompt/数量/分辨率/steps/CFG，但 Diffusers 与 ComfyUI sampler 实现不同；"
            "用于部署一致性趋势判断，不做逐像素一致性结论。"
        )

    ordered_files = [out_dir / record["image"] for record in generated_records]
    make_grid(ordered_files, out_dir / "samples_grid.png")
    write_json(manifest_path, list(records.values()))
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录: {out_dir}")
    return 0 if len(generated_records) == requested else 1


if __name__ == "__main__":
    sys.exit(main())
