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

from mc_agent_harness.tasks.minedojo_creative_adapter import (  # noqa: E402
    adapt_creative_catalog,
    write_creative_manifest_jsonl,
)


CREATIVE_TASKS_URL = (
    "https://raw.githubusercontent.com/MineDojo/MineDojo/"
    "2731bc27394269643b43828d9db8ab3a364601f0/"
    "minedojo/tasks/description_files/creative_tasks.yaml"
)


def parse_args() -> argparse.Namespace:
    """Parse CLI options for importing the official MineDojo creative task suite."""

    parser = argparse.ArgumentParser(
        description="Import MineDojo creative tasks into executable harness manifests."
    )
    parser.add_argument("--source-url", default=CREATIVE_TASKS_URL)
    parser.add_argument(
        "--source-file",
        type=Path,
        default=ROOT / "tasks" / "sources" / "minedojo" / "creative_tasks.yaml",
    )
    parser.add_argument(
        "--output-jsonl",
        type=Path,
        default=ROOT / "tasks" / "executable" / "minedojo_creative_tasks.jsonl",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=ROOT / "tasks" / "executable" / "minedojo_creative_tasks.summary.json",
    )
    parser.add_argument("--negative-prompt-count", type=int, default=7)
    parser.add_argument(
        "--download",
        action="store_true",
        help="Refresh --source-file from the official HTTPS source before importing.",
    )
    return parser.parse_args()


def main() -> None:
    """Import, validate, and persist all official MineDojo creative tasks."""

    args = parse_args()
    if args.download:
        args.source_file.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(args.source_url, timeout=60) as response:
            args.source_file.write_bytes(response.read())
    payload = yaml.safe_load(args.source_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a YAML object in {args.source_file}.")
    manifests, summary = adapt_creative_catalog(
        payload,
        negative_prompt_count=args.negative_prompt_count,
    )
    output = write_creative_manifest_jsonl(
        manifests,
        output_path=args.output_jsonl,
        summary=summary,
        summary_path=args.summary_path,
    )
    print(
        json.dumps(
            {
                **summary.to_json(),
                "source_file": str(args.source_file),
                "source_url": args.source_url,
                "output_jsonl": str(output),
                "summary_path": str(args.summary_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
