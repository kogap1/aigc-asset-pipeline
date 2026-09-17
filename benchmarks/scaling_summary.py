"""汇总单 worker 与 4 worker 的完整链路伸缩性实验。"""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--single", required=True)
    parser.add_argument("--multi", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    single = json.loads(Path(args.single).read_text(encoding="utf-8"))
    multi = json.loads(Path(args.multi).read_text(encoding="utf-8"))
    one = single["result"]
    many = multi["result"]
    one_tp = one["observed_throughput_per_hour"]
    many_tp = many["observed_throughput_per_hour"]
    speedup = many_tp / one_tp
    report = {
        "experiment": f"1_worker_vs_{args.workers}_workers_full_pipeline",
        "single_worker": {
            "throughput_per_hour": one_tp,
            "p95_sec": one.get("total_sec_p95"),
            "success_rate": one.get("delivery_success_rate"),
            "final_score_mean": one.get("final_score_mean"),
        },
        "multi_worker": {
            "workers": args.workers,
            "throughput_per_hour": many_tp,
            "p95_sec": many.get("total_sec_p95"),
            "success_rate": many.get("delivery_success_rate"),
            "final_score_mean": many.get("final_score_mean"),
        },
        "speedup": round(speedup, 4),
        "parallel_efficiency": round(speedup / args.workers, 4),
        "note": "两组均走 Agent、质量门、重试、超分和最终评分；30张/组用于工程伸缩性预实验。",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
