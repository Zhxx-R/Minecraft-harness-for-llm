from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mc_agent_harness.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkRunner,
    write_benchmark_report,
)
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the Week 6 benchmark runner."""

    parser = argparse.ArgumentParser(description="Run the Week 6 MineDojo-style task benchmark.")
    parser.add_argument("--manifest-dir", type=Path, default=ROOT / "tasks" / "manifests")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs" / "week6")
    parser.add_argument("--task-id", action="append", default=None, help="Task id to run; repeat to select multiple tasks.")
    parser.add_argument("--model-profile", default="scripted-week6")
    parser.add_argument("--runtime-profile", default="benchmark-minimal")
    parser.add_argument("--seed", type=int, default=20260624)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--input-cost-per-1k", type=float, default=0.0)
    parser.add_argument("--output-cost-per-1k", type=float, default=0.0)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Run the configured benchmark and return a compact terminal summary."""

    provider = MineDojoTaskProvider(manifest_dir=args.manifest_dir)
    runner = BenchmarkRunner(
        provider,
        BenchmarkConfig(
            model_profile=args.model_profile,
            runtime_profile=args.runtime_profile,
            seed=args.seed,
            max_steps=args.max_steps,
            input_cost_per_1k=args.input_cost_per_1k,
            output_cost_per_1k=args.output_cost_per_1k,
        ),
    )
    report = await runner.run(task_ids=args.task_id)
    json_path, markdown_path = write_benchmark_report(report, args.output_dir)
    return {
        "benchmark_id": report.benchmark_id,
        "task_count": report.task_count,
        "success_count": report.success_count,
        "success_rate": report.success_rate,
        "invalid_action_rate": report.invalid_action_rate,
        "runtime_crash_rate": report.runtime_crash_rate,
        "total_steps": report.total_steps,
        "total_tokens": report.total_tokens,
        "estimated_cost": report.estimated_cost,
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
    }


def main() -> None:
    """Run the Week 6 benchmark and print summary JSON."""

    summary = asyncio.run(run(parse_args()))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
