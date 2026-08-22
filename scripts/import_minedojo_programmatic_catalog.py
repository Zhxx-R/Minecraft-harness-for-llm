from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import urlopen

import yaml

from mc_agent_harness.tasks.catalog import (
    MineDojoCatalogSource,
    build_programmatic_catalog,
    flatten_suite_tasks,
    write_programmatic_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
PROGRAMMATIC_TASKS_URL = (
    "https://raw.githubusercontent.com/MineDojo/MineDojo/main/"
    "minedojo/tasks/description_files/programmatic_tasks.yaml"
)
TASKS_SPECS_URL = (
    "https://raw.githubusercontent.com/MineDojo/MineDojo/main/"
    "minedojo/tasks/description_files/tasks_specs.yaml"
)
TASKS_SUITE_URL = (
    "https://raw.githubusercontent.com/MineDojo/MineDojo/main/"
    "minedojo/tasks/description_files/tasks_suite.yaml"
)


def parse_args() -> argparse.Namespace:
    """Parse CLI options for importing MineDojo's full programmatic task catalog."""

    parser = argparse.ArgumentParser(
        description="Import MineDojo programmatic tasks into a local JSONL catalog."
    )
    parser.add_argument("--programmatic-url", default=PROGRAMMATIC_TASKS_URL)
    parser.add_argument("--tasks-specs-url", default=TASKS_SPECS_URL)
    parser.add_argument("--tasks-suite-url", default=TASKS_SUITE_URL)
    parser.add_argument("--programmatic-file", type=Path, default=None)
    parser.add_argument("--tasks-suite-file", type=Path, default=None)
    parser.add_argument(
        "--output-path",
        type=Path,
        default=ROOT / "tasks" / "catalog" / "minedojo_programmatic_tasks.jsonl",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=ROOT / "tasks" / "catalog" / "minedojo_programmatic_tasks.summary.json",
    )
    return parser.parse_args()


def main() -> None:
    """Import the official MineDojo programmatic catalog and print a summary."""

    args = parse_args()
    programmatic_payload = _load_yaml(args.programmatic_file, args.programmatic_url)
    suite_payload = _load_yaml(args.tasks_suite_file, args.tasks_suite_url)
    source = MineDojoCatalogSource(
        programmatic_tasks_url=args.programmatic_url,
        tasks_specs_url=args.tasks_specs_url,
        tasks_suite_url=args.tasks_suite_url,
    )
    records, summary = build_programmatic_catalog(
        programmatic_payload,
        source=source,
        suite_tasks=flatten_suite_tasks(suite_payload),
    )
    output_path, summary_path = write_programmatic_catalog(
        records,
        summary,
        output_path=args.output_path,
        summary_path=args.summary_path,
    )
    terminal_summary = {
        **summary.to_dict(),
        "output_path": str(output_path),
        "summary_path": str(summary_path),
    }
    print(json.dumps(terminal_summary, indent=2, sort_keys=True))


def _load_yaml(path: Path | None, url: str) -> dict[str, object]:
    """Load a YAML document from a local path or HTTPS URL."""

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
