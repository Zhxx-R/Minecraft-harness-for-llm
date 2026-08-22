from __future__ import annotations

import argparse
import json

from mc_agent_harness.db.session import SessionLocal
from mc_agent_harness.skills.initial import seed_initial_skills


def parse_args() -> argparse.Namespace:
    """Parse bootstrap skill seeding options."""

    parser = argparse.ArgumentParser(description="Seed promoted bootstrap skills into SQL.")
    parser.add_argument("--json", action="store_true", help="Print a JSON result.")
    return parser.parse_args()


def main() -> None:
    """Seed deterministic initial skills into the configured skills table."""

    args = parse_args()
    result = seed_initial_skills(SessionLocal)
    payload = {
        "ok": True,
        "created": result.created,
        "updated": result.updated,
        "unchanged": result.unchanged,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "Seeded initial skills: "
            f"{result.created} created, {result.updated} updated, {result.unchanged} unchanged."
        )


if __name__ == "__main__":
    main()
