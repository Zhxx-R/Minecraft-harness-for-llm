from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider
from mc_agent_harness.tasks.similarity import DiverseBatchPlanner
from mc_agent_harness.training import (
    InMemoryTrainingQueue,
    RedisTrainingQueue,
    TrainingBudget,
    TrainingJobConfig,
    TrainingRunner,
    write_training_report,
)


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the Week 9 parallel training runner."""

    parser = argparse.ArgumentParser(description="Run Week 9 parallel MineDojo-style training.")
    parser.add_argument("--manifest-dir", type=Path, default=ROOT / "tasks" / "manifests")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs" / "week9")
    parser.add_argument(
        "--task-id",
        action="append",
        default=None,
        help="Task id to run; repeat to select multiple tasks.",
    )
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--model-profile", default="scripted-week9")
    parser.add_argument("--runtime-profile", default="benchmark-minimal")
    parser.add_argument("--seed", type=int, default=20260624)
    parser.add_argument("--worker-concurrency", type=int, default=5)
    parser.add_argument("--max-steps-per-task", type=int, default=None)
    parser.add_argument("--max-tokens-per-task", type=int, default=None)
    parser.add_argument("--max-runtime-sec-per-task", type=float, default=None)
    parser.add_argument("--queue-backend", choices=["memory", "redis"], default="memory")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--diverse-batch-size", type=int, default=None)
    parser.add_argument("--max-task-similarity", type=float, default=None)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Run the training job and return a compact terminal summary."""

    budget = TrainingBudget(
        max_steps_per_task=args.max_steps_per_task,
        max_tokens_per_task=args.max_tokens_per_task,
        max_runtime_sec_per_task=args.max_runtime_sec_per_task,
        worker_concurrency=args.worker_concurrency,
    )
    config = TrainingJobConfig(
        job_id=args.job_id or TrainingJobConfig().job_id,
        model_profile=args.model_profile,
        runtime_profile=args.runtime_profile,
        seed=args.seed,
        budget=budget,
        queue_backend=args.queue_backend,
        audit_backend="benchmark-recorder",
    )
    queue = (
        RedisTrainingQueue(redis_url=args.redis_url, job_id=config.job_id)
        if args.queue_backend == "redis"
        else InMemoryTrainingQueue()
    )
    provider = MineDojoTaskProvider(manifest_dir=args.manifest_dir)
    runner = TrainingRunner(provider, config, queue)
    task_ids = await _select_task_ids(provider, args)
    report = await runner.run(task_ids=task_ids)
    json_path, markdown_path = write_training_report(report, args.output_dir)
    return {
        "job_id": report.job_id,
        "status": report.status,
        "queue_backend": report.queue_backend,
        "task_count": report.task_count,
        "success_count": report.success_count,
        "success_rate": report.success_rate,
        "max_observed_concurrency": report.max_observed_concurrency,
        "total_steps": report.total_steps,
        "total_tokens": report.total_tokens,
        "selected_task_ids": task_ids,
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
    }


async def _select_task_ids(
    provider: MineDojoTaskProvider,
    args: argparse.Namespace,
) -> list[str] | None:
    """Optionally select a diversity-aware executable training batch."""

    if args.diverse_batch_size is None:
        return args.task_id

    candidate_ids = args.task_id
    if candidate_ids is None:
        summaries = await provider.list_tasks()
        candidate_ids = [str(summary["task_id"]) for summary in summaries]
    task_specs = [await provider.load_task(task_id) for task_id in candidate_ids]
    selection = DiverseBatchPlanner().select_batch(
        task_specs,
        batch_size=args.diverse_batch_size,
        max_pairwise_similarity=args.max_task_similarity,
    )
    return selection.selected_task_ids


def main() -> None:
    """Run Week 9 training and print summary JSON."""

    summary = asyncio.run(run(parse_args()))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
