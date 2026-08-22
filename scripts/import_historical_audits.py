#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mc_agent_harness.core.config import settings
from mc_agent_harness.db.history_import import import_historical_audits


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface for historical audit consolidation."""

    parser = argparse.ArgumentParser(
        description=(
            "Import the most complete copy of every runs/**/*.sqlite3 audit run "
            "into the configured shared database."
        )
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=Path(settings.artifact_root),
        help="Directory recursively scanned for *.sqlite3 files (default: configured artifact root).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Target SQLAlchemy database URL (default: DATABASE_URL/configured database).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan and validate the import, then roll back the target transaction.",
    )
    parser.add_argument(
        "--keep-running-status",
        action="store_true",
        help=(
            "Preserve historical status=running rows. By default they become interrupted "
            "so archived runs do not appear active."
        ),
    )
    return parser


def main() -> int:
    """Run the historical audit import and print machine-readable statistics."""

    args = build_parser().parse_args()
    stats = import_historical_audits(
        runs_root=args.runs_root,
        database_url=args.database_url,
        dry_run=args.dry_run,
        normalize_running=not args.keep_running_status,
    )
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
