from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from mc_agent_harness.db.session import create_database_engine, create_session_factory
from mc_agent_harness.schemas.skill import SkillStatus
from mc_agent_harness.skills.bundle import export_skill_bundle, result_json


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    """Parse portable skill export options."""

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(
        description="Export validated SQL skills as a portable checksummed JSON bundle."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "runs" / "exports" / f"skills-{timestamp}.json",
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--status",
        action="append",
        choices=[status.value for status in SkillStatus],
        default=None,
        help="Repeat to export multiple lifecycle states; default: promoted.",
    )
    parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="Export every lifecycle state, including drafts and deprecated skills.",
    )
    parser.add_argument(
        "--learned-only",
        action="store_true",
        help="Exclude bootstrap skills whose SQL source_run_id is null.",
    )
    return parser.parse_args()


def main() -> None:
    """Export skills from the configured authoritative SQL database."""

    args = parse_args()
    if args.all_statuses and args.status:
        raise SystemExit("--all-statuses cannot be combined with --status.")
    statuses = (
        tuple(SkillStatus)
        if args.all_statuses
        else tuple(SkillStatus(status) for status in (args.status or ["promoted"]))
    )
    engine = create_database_engine(args.database_url)
    result = export_skill_bundle(
        create_session_factory(engine),
        args.output,
        statuses=statuses,
        learned_only=args.learned_only,
    )
    print(json.dumps(result_json(result), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
