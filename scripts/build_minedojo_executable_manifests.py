from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.request import urlopen

import yaml


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from mc_agent_harness.tasks.minedojo_adapter import (  # noqa: E402
    adapt_programmatic_catalog,
    write_executable_manifest_jsonl,
)


TASKS_SPECS_URL = (
    "https://raw.githubusercontent.com/MineDojo/MineDojo/main/"
    "minedojo/tasks/description_files/tasks_specs.yaml"
)


def parse_args() -> argparse.Namespace:
    """Parse CLI options for building executable MineDojo manifest snapshots."""

    parser = argparse.ArgumentParser(
        description="Adapt the local MineDojo programmatic catalog into executable harness manifests."
    )
    parser.add_argument(
        "--catalog-path",
        type=Path,
        default=ROOT / "tasks" / "catalog" / "minedojo_programmatic_tasks.jsonl",
    )
    parser.add_argument("--tasks-specs-url", default=TASKS_SPECS_URL)
    parser.add_argument("--tasks-specs-file", type=Path, default=None)
    parser.add_argument("--no-official-specs", action="store_true")
    parser.add_argument(
        "--output-jsonl",
        type=Path,
        default=None,
        help="Optional JSONL output path. If omitted, the script runs as dry-run only.",
    )
    parser.add_argument("--summary-path", type=Path, default=None)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Build executable manifest metadata and optionally persist supported manifests."""

    args = parse_args()
    tasks = _load_catalog(args.catalog_path)
    task_specs = {} if args.no_official_specs else _load_task_specs(args.tasks_specs_file, args.tasks_specs_url)
    adaptations, summary = adapt_programmatic_catalog(tasks, task_specs=task_specs)
    if args.output_jsonl is not None:
        summary_path = args.summary_path or args.output_jsonl.with_suffix(".summary.json")
        write_executable_manifest_jsonl(
            adaptations,
            output_path=args.output_jsonl,
            summary=summary,
            summary_path=summary_path,
        )
    payload = {
        **summary.to_json(),
        "catalog_path": str(args.catalog_path),
        "official_specs_loaded": bool(task_specs),
        "output_jsonl": str(args.output_jsonl) if args.output_jsonl else None,
        "sample_supported_task_ids": [
            item.task_id for item in adaptations if item.supported
        ][:10],
        "sample_unsupported": [
            {
                "task_id": item.task_id,
                "reason": item.unsupported_reason,
            }
            for item in adaptations
            if not item.supported
        ][:10],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))


def _load_catalog(path: Path) -> list[dict[str, object]]:
    """Load local MineDojo catalog JSONL rows."""

    tasks: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            tasks.append(payload)
    return tasks


def _load_task_specs(path: Path | None, url: str) -> dict[str, object]:
    """Load official MineDojo task template specs from local YAML or HTTPS."""

    if path is not None:
        content = path.read_text(encoding="utf-8")
    else:
        with urlopen(url, timeout=60) as response:
            content = response.read().decode("utf-8")
    payload = yaml.safe_load(content)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected YAML mapping from {path or url}.")
    return payload


if __name__ == "__main__":
    main()
