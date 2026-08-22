from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from mc_agent_harness.evaluation.calibration import calibrate_creative_threshold  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse MineCLIP calibration registry options."""

    parser = argparse.ArgumentParser(
        description="Calibrate one creative task from trajectory score examples using K=2."
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--examples-jsonl",
        type=Path,
        required=True,
        help="JSONL rows with task_id, score, and optional human_success.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "configs" / "creative_mineclip_calibration.json",
    )
    parser.add_argument("--minimum-examples", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    """Fit and merge one reviewed threshold into the calibration registry."""

    args = parse_args()
    examples = _load_examples(args.examples_jsonl, args.task_id)
    if len(examples) < args.minimum_examples:
        raise ValueError(
            f"Need at least {args.minimum_examples} examples for {args.task_id}; found {len(examples)}."
        )
    labels = [example.get("human_success") for example in examples]
    include_labels = all(isinstance(label, bool) for label in labels)
    result = calibrate_creative_threshold(
        [float(example["score"]) for example in examples],
        human_labels=[bool(label) for label in labels] if include_labels else None,
    )
    registry = _load_registry(args.output)
    registry[args.task_id] = {
        "status": "calibrated",
        "method": "kmeans_2_centroid_midpoint",
        "score_threshold": result.threshold,
        "sample_count": result.sample_count,
        "lower_centroid": result.lower_centroid,
        "upper_centroid": result.upper_centroid,
        "iterations": result.iterations,
        "f1": result.f1,
        "accuracy": result.accuracy,
        "examples_file": str(args.examples_jsonl),
        "human_labels_available": include_labels,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"task_id": args.task_id, **registry[args.task_id]}, indent=2, sort_keys=True))


def _load_examples(path: Path, task_id: str) -> list[dict[str, Any]]:
    """Load valid trajectory-level score examples for one creative task."""

    examples: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if (
            isinstance(payload, dict)
            and payload.get("task_id") == task_id
            and isinstance(payload.get("score"), (int, float))
        ):
            examples.append(payload)
    return examples


def _load_registry(path: Path) -> dict[str, Any]:
    """Load an existing calibration registry when one exists."""

    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Existing calibration registry must be a JSON object.")
    return payload


if __name__ == "__main__":
    main()
