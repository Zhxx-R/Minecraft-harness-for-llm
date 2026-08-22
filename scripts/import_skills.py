from __future__ import annotations

import argparse
import json
from pathlib import Path

from mc_agent_harness.db.session import create_database_engine, create_session_factory
from mc_agent_harness.skills.bundle import import_skill_bundle, result_json


def parse_args() -> argparse.Namespace:
    """Parse portable skill import options."""

    parser = argparse.ArgumentParser(
        description="Validate and import a portable skill bundle into SQL."
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--on-conflict",
        choices=("skip", "replace", "error"),
        default="skip",
        help="Safe default keeps an existing local skill with the same name/version.",
    )
    return parser.parse_args()


def main() -> None:
    """Import skills into the configured authoritative SQL database."""

    args = parse_args()
    engine = create_database_engine(args.database_url)
    result = import_skill_bundle(
        create_session_factory(engine),
        args.bundle,
        on_conflict=args.on_conflict,
    )
    print(json.dumps(result_json(result), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
