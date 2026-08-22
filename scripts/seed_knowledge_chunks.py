from __future__ import annotations

import argparse
import json

from mc_agent_harness.db.session import SessionLocal
from mc_agent_harness.knowledge.chunk_store import DatabaseKnowledgeStore
from mc_agent_harness.knowledge.static_provider import StaticKnowledgeProvider


def parse_args() -> argparse.Namespace:
    """Parse knowledge seeding options."""

    parser = argparse.ArgumentParser(description="Seed local Minecraft knowledge chunks into SQL.")
    parser.add_argument("--json", action="store_true", help="Print a JSON result.")
    return parser.parse_args()


def main() -> None:
    """Seed deterministic Week 1 knowledge into the Week 4 knowledge_chunks table."""

    args = parse_args()
    provider = StaticKnowledgeProvider()
    store = DatabaseKnowledgeStore(SessionLocal)
    count = store.upsert_static_provider(provider)
    result = {"ok": True, "knowledge_chunks": count}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Seeded {count} knowledge chunks.")


if __name__ == "__main__":
    main()
