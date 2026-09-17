"""完整生产链路基准：默认 6 类中文需求 × 100 张，共 600 张。

链路：DeepSeek Agent 改写/拆解 -> ComfyUI + LoRA -> CLIP 质量门与重试
     -> Real-ESRGAN 超分 -> 最终图再次 CLIP 评分 -> FID/性能汇总。
"""
import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_pipeline import DeepSeekAgent, Deliverer, load_config, run_tasks
from metrics import compute_fid, make_grid, percentile, write_json


SCENARIOS = [
    "创作史诗奇幻风格的华丽金甲骑士卡牌插画",
    "创作暮色中悬崖古堡的黑暗奇幻卡牌场景",
    "创作巨龙在战场上喷火的史诗奇幻卡牌插画",
    "创作精灵法师施放闪电法术的奇幻卡牌插画",
    "创作具有发光精灵的神秘森林奇幻卡牌场景",
    "创作亡灵法师召唤亡灵军队的黑暗奇幻卡牌插画",
]


def bootstrap_mean_ci(values, samples=2000, seed=42):
    if not values:
        return [None, None]
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(samples)]
    return [round(percentile(means, 0.025), 6), round(percentile(means, 0.975), 6)]


def wilson_interval(successes, total, z=1.96):
    """二项比例的 95% Wilson 区间，适合成功率/通过率。"""
    if total <= 0:
        return [None, None]
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return [round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6)]


def metric_block(records, threshold):
    gate_scores = [r["gate_score"] for r in records if r.get("gate_score") is not None]
    final_scores = [r["final_score"] for r in records if r.get("final_score") is not None]
    total_secs = [r["total_sec"] for r in records if r.get("total_sec") is not None]
    retries = [max(0, len(r.get("attempts", [])) - 1) for r in records]
    actual_attempts = sum(len(r.get("attempts", [])) for r in records)
    initial_scores = []
    for record in records:
        first_scored = next((a.get("score") for a in record.get("attempts", []) if a.get("score") is not None), None)
        if first_scored is not None:
            initial_scores.append(first_scored)
    initial_passes = sum(1 for score in initial_scores if score >= threshold)
    gate_passes = sum(1 for score in gate_scores if score >= threshold)
    final_passes = sum(1 for score in final_scores if score >= threshold)
    first_pass_rate = initial_passes / len(initial_scores) if initial_scores else None
    gate_pass_rate = gate_passes / len(gate_scores) if gate_scores else None
    final_pass_rate = final_passes / len(final_scores) if final_scores else None
    return {
        "delivered": len(records),
        "actual_generation_attempts": actual_attempts,
        "extra_generation_attempts": max(0, actual_attempts - len(records)),
        "initial_score_mean_without_gate": round(statistics.mean(initial_scores), 6) if initial_scores else None,
        "initial_pass_rate_without_gate": round(first_pass_rate, 6) if first_pass_rate is not None else None,
        "initial_pass_rate_95ci": wilson_interval(initial_passes, len(initial_scores)),
        "gate_score_mean": round(statistics.mean(gate_scores), 6) if gate_scores else None,
        "gate_score_95ci": bootstrap_mean_ci(gate_scores),
        "gate_quality_pass_rate": round(gate_pass_rate, 6) if gate_pass_rate is not None else None,
        "gate_quality_pass_rate_95ci": wilson_interval(gate_passes, len(gate_scores)),
        "quality_gate_score_lift": round(statistics.mean(gate_scores) - statistics.mean(initial_scores), 6)
        if gate_scores and initial_scores else None,
        "quality_gate_pass_rate_lift": round(gate_pass_rate - first_pass_rate, 6)
        if gate_pass_rate is not None and first_pass_rate is not None else None,
        "final_score_mean": round(statistics.mean(final_scores), 6) if final_scores else None,
        "final_score_std": round(statistics.pstdev(final_scores), 6) if len(final_scores) > 1 else 0.0 if final_scores else None,
        "final_score_95ci": bootstrap_mean_ci(final_scores),
        "final_score_min": round(min(final_scores), 6) if final_scores else None,
        "final_score_max": round(max(final_scores), 6) if final_scores else None,
        "postprocess_score_delta": round(statistics.mean(final_scores) - statistics.mean(gate_scores), 6)
        if final_scores and gate_scores else None,
        "first_pass_rate": round(first_pass_rate, 6) if first_pass_rate is not None else None,
        "final_quality_pass_rate": round(final_pass_rate, 6) if final_pass_rate is not None else None,
        "final_quality_pass_rate_95ci": wilson_interval(final_passes, len(final_scores)),
        "mean_retries": round(statistics.mean(retries), 6) if retries else None,
        "total_sec_mean": round(statistics.mean(total_secs), 4) if total_secs else None,
        "total_sec_p50": round(percentile(total_secs, 0.50), 4) if total_secs else None,
        "total_sec_p95": round(percentile(total_secs, 0.95), 4) if total_secs else None,
        "throughput_deliveries_per_hour": round(3600 / statistics.mean(total_secs), 2) if total_secs else None,
    }


def build_agent_plan(agent, per_scenario):
    raw_plans = []
    tasks = []
    for scenario_id, requirement in enumerate(SCENARIOS):
        request = (
            f"{requirement}。需要生成 {per_scenario} 张，主体保持一致但允许构图、光影和细节变化；"
            "这是全新创作，不使用参考图。"
        )
        parsed = agent.parse_requirement(request)
        usable = [task for task in parsed if task.get("prompt")]
        if not usable:
            raise RuntimeError(f"Agent 未能解析场景 {scenario_id}: {requirement}")
        raw_plans.append({"scenario_id": scenario_id, "requirement": requirement, "agent_tasks": parsed})

        # 保留 Agent 的多任务拆解，同时强制每类最终总数完全等于 per_scenario。
        base_count, remainder = divmod(per_scenario, len(usable))
        for index, task in enumerate(usable):
            count = base_count + (1 if index < remainder else 0)
            if count == 0:
                continue
            tasks.append({
                "prompt": task["prompt"],
                "count": count,
                "mode": "txt2img",
                "ref_image": None,
                "scenario_id": scenario_id,
                "requirement": requirement,
            })
    return raw_plans, tasks


def main():
    parser = argparse.ArgumentParser(description="完整 Agentic 生产链路正式验收基准")
    parser.add_argument("--config", default=None)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--per-scenario", type=int, default=100)
    parser.add_argument("--real-dir", default="../gen_project/data/clean/eval")
    parser.add_argument("--reference-metrics", default="../gen_project/metrics/metrics.json")
    parser.add_argument("--skip-fid", action="store_true")
    args = parser.parse_args()
    if args.per_scenario < 1:
        parser.error("--per-scenario 必须大于 0")

    cfg = load_config(args.config)
    requested = len(SCENARIOS) * args.per_scenario
    batch_id = args.batch_id or time.strftime(f"production_{requested}_%Y%m%d_%H%M%S")
    batch_dir = Path(cfg["deliver"]["out_root"]) / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    plan_path = batch_dir / "agent_plan.json"

    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        tasks = plan["normalized_tasks"]
        print(f"复用 Agent 计划: {plan_path}")
    else:
        raw_plans, tasks = build_agent_plan(DeepSeekAgent(cfg), args.per_scenario)
        plan = {
            "scenarios": SCENARIOS,
            "per_scenario": args.per_scenario,
            "requested": requested,
            "raw_agent_plans": raw_plans,
            "normalized_tasks": tasks,
        }
        write_json(plan_path, plan)
        print(f"Agent 计划已保存: {plan_path}")

    if sum(int(task["count"]) for task in tasks) != requested:
        raise RuntimeError("Agent 归一化任务总数不等于目标数量")

    print(f"开始完整链路: {len(SCENARIOS)} 类 × {args.per_scenario} = {requested} 张")
    benchmark_started = time.perf_counter()
    batch_dir, records = run_tasks(tasks, cfg, batch_id=batch_id)
    benchmark_wall_sec = time.perf_counter() - benchmark_started
    task_scenarios = {str(index): task["scenario_id"] for index, task in enumerate(tasks)}
    delivered = []
    for record in records:
        task_index = record.get("task_id", "_").split("_", 1)[0]
        record["scenario_id"] = task_scenarios.get(task_index)
        image = batch_dir / (record.get("image") or "")
        if record.get("status") != "failed" and image.is_file():
            delivered.append(record)
    Deliverer(cfg).write_manifest(batch_dir, records)

    threshold = cfg["quality"]["threshold"]
    result = metric_block(delivered, threshold)
    result.update({
        "requested": requested,
        "failed": requested - len(delivered),
        "delivery_success_rate": round(len(delivered) / requested, 6),
        "delivery_success_rate_95ci": wilson_interval(len(delivered), requested),
        "benchmark_wall_sec": round(benchmark_wall_sec, 4),
        "observed_throughput_per_hour": round(len(delivered) / benchmark_wall_sec * 3600, 2)
        if benchmark_wall_sec > 0 else None,
    })

    by_scenario = {}
    for scenario_id, requirement in enumerate(SCENARIOS):
        subset = [r for r in delivered if r.get("scenario_id") == scenario_id]
        by_scenario[str(scenario_id)] = {
            "requirement": requirement,
            **metric_block(subset, threshold),
        }

    by_worker = {}
    for worker_url in sorted({r.get("worker_url") for r in delivered if r.get("worker_url")}):
        subset = [r for r in delivered if r.get("worker_url") == worker_url]
        by_worker[worker_url] = metric_block(subset, threshold)

    evaluation_dir = batch_dir / "_evaluation_final"
    raw_evaluation_dir = batch_dir / "_evaluation_raw"
    evaluation_dir.mkdir(exist_ok=True)
    raw_evaluation_dir.mkdir(exist_ok=True)
    final_files = []
    for record in delivered:
        source = batch_dir / record["image"]
        target = evaluation_dir / f"{record['task_id'].replace('_', '-')}{source.suffix}"
        if not target.exists():
            try:
                os.link(source, target)
            except OSError:
                target.symlink_to(source)
        final_files.append(source)
        raw_source = batch_dir / (record.get("raw_image") or "")
        raw_target = raw_evaluation_dir / f"{record['task_id'].replace('_', '-')}{raw_source.suffix or '.png'}"
        if raw_source.is_file() and not raw_target.exists():
            try:
                os.link(raw_source, raw_target)
            except OSError:
                raw_target.symlink_to(raw_source)

    real_dir = Path(args.real_dir).resolve()
    if not args.skip_fid and len(delivered) == requested and real_dir.is_dir():
        print("开始对质量门原图计算 FID...")
        fid_started = time.perf_counter()
        result["gate_raw_fid"] = round(compute_fid(raw_evaluation_dir, real_dir), 6)
        result["gate_raw_fid_sec"] = round(time.perf_counter() - fid_started, 4)
        print("开始对最终超分交付图计算 FID...")
        fid_started = time.perf_counter()
        result["final_fid"] = round(compute_fid(evaluation_dir, real_dir), 6)
        result["final_fid_sec"] = round(time.perf_counter() - fid_started, 4)
    elif not args.skip_fid:
        result["final_fid"] = None
        result["fid_note"] = f"交付未满{requested}张或真实评估目录不存在"

    summary = {
        "experiment": f"phase2_full_agentic_delivery_{requested}",
        "interpretation": (
            "阶段二包含 Agent prompt、质量筛选/重试和超分，衡量最终系统交付效果。"
            "本实验是内部生产验收，不冒充论文级 FID-50K。"
        ),
        "evaluation_scope": f"FID-{requested} internal acceptance benchmark",
        "protocol": {
            "scenarios": SCENARIOS,
            "per_scenario": args.per_scenario,
            "quality_threshold": threshold,
            "max_retries": cfg["quality"]["max_retries"],
            "generation": cfg["generation"],
            "postprocess": "Real-ESRGAN via wf_post",
            "final_images_rescored": True,
        },
        "result": result,
        "by_scenario": by_scenario,
        "by_worker": by_worker,
    }

    reference_path = Path(args.reference_metrics).resolve()
    if reference_path.is_file():
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        summary["upstream_phase1_model_validation"] = reference
        summary["project_progression_note"] = (
            "阶段一指标用于证明 LoRA 训练有效，是上游过渡证据；阶段二指标用于生产验收，"
            "不是把两个阶段当成竞争模型直接排名。"
        )

    make_grid(final_files, batch_dir / "final_samples_grid.png")
    write_json(batch_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"批次目录: {batch_dir}")
    return 0 if len(delivered) == requested else 1


if __name__ == "__main__":
    sys.exit(main())
