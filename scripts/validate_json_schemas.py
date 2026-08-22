from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "packages" / "shared-schemas"
TASK_MANIFEST_DIR = ROOT / "tasks" / "manifests"
EXECUTABLE_MANIFEST_DIR = ROOT / "tasks" / "executable"


def main() -> None:
    """Validate shared schemas and representative payloads without network access."""

    schemas = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)

    registry = Registry().with_resources(
        [
            (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
            for schema in schemas.values()
        ]
    )

    canonical_actions = schemas["action.schema.json"]["properties"]["type"]["enum"]

    Draft202012Validator(schemas["action.schema.json"]).validate(
        {"type": "submit_for_evaluation", "args": {}}
    )
    Draft202012Validator(schemas["action.schema.json"]).validate(
        {"type": "follow", "args": {"entity_id": 143, "follow_distance": 1.25}}
    )
    Draft202012Validator(schemas["skill.schema.json"], registry=registry).validate(
        {
            "name": "collect_wood",
            "version": "0.1.0",
            "description": "Collect starter wood.",
            "triggers": ["log", "wood"],
            "preconditions": ["near tree"],
            "action_plan": [
                {"type": "scan_blocks", "args": {"block": "oak_log", "count": 1}},
                {
                    "type": "dig_block_at",
                    "args": {"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}},
                },
                {"type": "wait_ticks", "args": {"ticks": 20}},
            ],
            "validation": {"source": "unit"},
            "source_run_id": "run_test",
            "source_step_range": {"start": 0, "end": 2},
            "task_scope": ["minecraft:harvest"],
            "dependencies": ["oak_log", "action:dig_block_at"],
            "metrics": {"usage_count": 0},
            "status": "draft",
        }
    )
    Draft202012Validator(schemas["trajectory-event.schema.json"]).validate(
        {
            "run_id": "run_test",
            "event_type": "knowledge_retrieval",
            "timestamp": "2026-06-21T00:00:00Z",
            "payload": {"query": "wooden pickaxe"},
        }
    )
    task_validator = Draft202012Validator(schemas["task-manifest.schema.json"], registry=registry)
    representative_task = {
        "task_id": "minedojo_harvest_oak_log",
        "source": "minedojo",
        "category": "harvest",
        "family": "Harvest",
        "goal": "Harvest one oak log.",
        "allowed_actions": canonical_actions,
        "verifier": {"type": "inventory_contains", "item": "oak_log", "count": 1},
        "success_criteria": {"type": "inventory_contains", "item": "oak_log", "count": 1},
        "knowledge_tags": ["minecraft:block/oak_log"],
        "benchmark": {
            "seed": 6101,
            "max_steps": 1,
            "scripted_actions": [
                {"type": "scan_blocks", "args": {"block": "oak_log", "count": 1}},
                {
                    "type": "dig_block_at",
                    "args": {"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}},
                },
                {"type": "wait_ticks", "args": {"ticks": 20}},
            ],
        },
    }
    task_validator.validate(representative_task)

    manifest_count = 0
    for manifest_path in sorted(TASK_MANIFEST_DIR.rglob("*.json")):
        task_validator.validate(json.loads(manifest_path.read_text(encoding="utf-8")))
        manifest_count += 1

    executable_manifest_count = 0
    for manifest_path in sorted(EXECUTABLE_MANIFEST_DIR.glob("*.jsonl")):
        for line_number, line in enumerate(
            manifest_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                task_validator.validate(json.loads(line))
            except Exception as exc:
                raise ValueError(
                    f"Invalid executable manifest {manifest_path}:{line_number}: {exc}"
                ) from exc
            executable_manifest_count += 1

    print(
        f"validated {len(schemas)} shared JSON schemas, {manifest_count} curated manifests, "
        f"and {executable_manifest_count} executable manifests"
    )


if __name__ == "__main__":
    main()
