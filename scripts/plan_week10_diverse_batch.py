from __future__ import annotations

import argparse
import json
from pathlib import Path

from mc_agent_harness.tasks.catalog import MineDojoProgrammaticCatalog
from mc_agent_harness.tasks.similarity import DiverseBatchPlanner


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    """Parse CLI options for planning a diversity-aware MineDojo training batch."""

    parser = argparse.ArgumentParser(
        description="Plan a low-similarity batch from the MineDojo catalog."
    )
    parser.add_argument(
        "--catalog-path",
        type=Path,
        default=ROOT / "tasks" / "catalog" / "minedojo_programmatic_tasks.jsonl",
    )
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-task-similarity", type=float, default=0.45)
    parser.add_argument("--category", action="append", default=None)
    parser.add_argument(
        "--output-path",
        type=Path,
        default=ROOT / "runs" / "week10" / "diverse_batch_plan.json",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Plan a diverse task batch and persist a JSON report."""

    catalog = MineDojoProgrammaticCatalog(args.catalog_path)
    summaries = await catalog.list_tasks()
    if args.category:
        allowed_categories = set(args.category)
        summaries = [
            summary
            for summary in summaries
            if summary.get("category") in allowed_categories
        ]
    selection = DiverseBatchPlanner().select_batch(
        summaries,
        batch_size=args.batch_size,
        max_pairwise_similarity=args.max_task_similarity,
    )
    selected_ids = set(selection.selected_task_ids)
    selected = [summary for summary in summaries if summary["task_id"] in selected_ids]
    report = {
        "catalog_path": str(args.catalog_path),
        "candidate_count": len(summaries),
        "batch_size": args.batch_size,
        "max_task_similarity_threshold": args.max_task_similarity,
        "threshold_satisfied": selection.max_pairwise_similarity <= args.max_task_similarity,
        "selected_task_ids": selection.selected_task_ids,
        "selected_tasks": selected,
        "max_pairwise_similarity": selection.max_pairwise_similarity,
        "pairwise_similarities": selection.pairwise_similarities,
        "deferred_count": len(selection.deferred_task_ids),
    }
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {**report, "output_path": str(args.output_path)}


def main() -> None:
    """Plan a Week 10 diverse batch and print the report summary."""

    import asyncio

    summary = asyncio.run(run(parse_args()))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
