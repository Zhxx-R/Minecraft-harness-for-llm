from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mc_agent_harness.harness.tool_registry import CANONICAL_PRIMITIVE_ACTIONS


PROGRAMMATIC_CATALOG_SCHEMA = "mc-agent-harness.minedojo-programmatic-catalog.v1"
STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "from",
    "given",
    "in",
    "of",
    "the",
    "to",
    "with",
    "without",
}


@dataclass(frozen=True, slots=True)
class MineDojoCatalogSource:
    """Source URLs used to build a local MineDojo task catalog snapshot."""

    programmatic_tasks_url: str
    tasks_specs_url: str | None = None
    tasks_suite_url: str | None = None


@dataclass(frozen=True, slots=True)
class MineDojoProgrammaticTask:
    """Catalog-only representation of one MineDojo programmatic task."""

    task_id: str
    category: str
    family: str
    goal: str
    guidance: str | None
    allowed_actions: list[str]
    knowledge_tags: list[str]
    success_criteria: dict[str, Any]
    benchmark_suite_tier: str | None = None
    minedojo: dict[str, Any] = field(default_factory=dict)
    source: str = "minedojo"
    task_type: str = "programmatic"
    schema_version: str = PROGRAMMATIC_CATALOG_SCHEMA

    def to_task_spec(self) -> dict[str, Any]:
        """Convert the catalog record into the harness task-spec shape."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class MineDojoCatalogSummary:
    """Summary statistics for one generated MineDojo programmatic catalog."""

    schema_version: str
    task_count: int
    categories: dict[str, int]
    suite_tiers: dict[str, int]
    source: MineDojoCatalogSource

    def to_dict(self) -> dict[str, Any]:
        """Convert summary metadata into a JSON-safe dictionary."""

        return {
            "schema_version": self.schema_version,
            "task_count": self.task_count,
            "categories": self.categories,
            "suite_tiers": self.suite_tiers,
            "source": asdict(self.source),
        }


class MineDojoProgrammaticCatalog:
    """Read-only provider for the local full MineDojo programmatic catalog."""

    def __init__(
        self,
        catalog_path: str | Path = "tasks/catalog/minedojo_programmatic_tasks.jsonl",
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self._records: dict[str, MineDojoProgrammaticTask] | None = None

    async def list_tasks(self) -> list[dict[str, Any]]:
        """Return compact summaries for every catalog task."""

        records = self._load_records()
        return [
            {
                "task_id": record.task_id,
                "source": record.source,
                "task_type": record.task_type,
                "category": record.category,
                "family": record.family,
                "goal": record.goal,
                "allowed_actions": record.allowed_actions,
                "knowledge_tags": record.knowledge_tags,
                "benchmark_suite_tier": record.benchmark_suite_tier,
                "catalog_only": record.minedojo.get("catalog_only", True),
            }
            for record in sorted(records.values(), key=lambda item: item.task_id)
        ]

    async def load_task(self, task_id: str) -> dict[str, Any]:
        """Load one catalog task as a harness task spec."""

        records = self._load_records()
        if task_id not in records:
            raise KeyError(f"MineDojo catalog task not found: {task_id}")
        return records[task_id].to_task_spec()

    def _load_records(self) -> dict[str, MineDojoProgrammaticTask]:
        """Load JSONL catalog records once and cache them by task id."""

        if self._records is not None:
            return self._records
        if not self.catalog_path.exists():
            raise FileNotFoundError(f"MineDojo programmatic catalog not found: {self.catalog_path}")

        records: dict[str, MineDojoProgrammaticTask] = {}
        lines = self.catalog_path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            record = MineDojoProgrammaticTask(
                task_id=str(payload["task_id"]),
                category=str(payload["category"]),
                family=str(payload["family"]),
                goal=str(payload["goal"]),
                guidance=payload.get("guidance"),
                allowed_actions=[str(item) for item in payload.get("allowed_actions", [])],
                knowledge_tags=[str(item) for item in payload.get("knowledge_tags", [])],
                success_criteria=dict(payload.get("success_criteria", {})),
                benchmark_suite_tier=payload.get("benchmark_suite_tier"),
                minedojo=dict(payload.get("minedojo", {})),
                source=str(payload.get("source", "minedojo")),
                task_type=str(payload.get("task_type", "programmatic")),
                schema_version=str(payload.get("schema_version", PROGRAMMATIC_CATALOG_SCHEMA)),
            )
            if record.task_id in records:
                raise ValueError(
                    f"Duplicate MineDojo catalog task id at line {line_number}: {record.task_id}"
                )
            records[record.task_id] = record
        self._records = records
        return records


def build_programmatic_catalog(
    programmatic_tasks: dict[str, Any],
    *,
    source: MineDojoCatalogSource,
    suite_tasks: dict[str, str] | None = None,
) -> tuple[list[MineDojoProgrammaticTask], MineDojoCatalogSummary]:
    """Build normalized catalog records from MineDojo's official task instruction YAML."""

    suite_tasks = suite_tasks or {}
    records = [
        _catalog_record_from_payload(
            task_id,
            payload,
            source=source,
            suite_tier=suite_tasks.get(task_id),
        )
        for task_id, payload in sorted(programmatic_tasks.items())
        if isinstance(payload, dict)
    ]
    categories = Counter(record.category for record in records)
    suite_tiers = Counter(
        record.benchmark_suite_tier or "not_in_official_suite"
        for record in records
    )
    summary = MineDojoCatalogSummary(
        schema_version=PROGRAMMATIC_CATALOG_SCHEMA,
        task_count=len(records),
        categories=dict(sorted(categories.items())),
        suite_tiers=dict(sorted(suite_tiers.items())),
        source=source,
    )
    return records, summary


def write_programmatic_catalog(
    records: Iterable[MineDojoProgrammaticTask],
    summary: MineDojoCatalogSummary,
    *,
    output_path: str | Path,
    summary_path: str | Path,
) -> tuple[Path, Path]:
    """Write catalog JSONL records and summary metadata to disk."""

    output = Path(output_path)
    summary_output = Path(summary_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record.to_task_spec(), ensure_ascii=False, sort_keys=True)
        for record in sorted(records, key=lambda item: item.task_id)
    ]
    output.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    summary_output.write_text(
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output, summary_output


def flatten_suite_tasks(tasks_suite: dict[str, Any]) -> dict[str, str]:
    """Return a map of task id to official suite tier from tasks_suite.yaml."""

    result: dict[str, str] = {}
    programmatic = tasks_suite.get("programmatic_tasks") if isinstance(tasks_suite, dict) else None
    if not isinstance(programmatic, dict):
        return result
    for tier, task_ids in programmatic.items():
        if not isinstance(task_ids, list):
            continue
        for task_id in task_ids:
            if isinstance(task_id, str):
                result[task_id] = str(tier)
    return result


def _catalog_record_from_payload(
    task_id: str,
    payload: dict[str, Any],
    *,
    source: MineDojoCatalogSource,
    suite_tier: str | None,
) -> MineDojoProgrammaticTask:
    """Build one normalized catalog record from a MineDojo YAML payload."""

    category = str(payload.get("category") or _infer_category(task_id))
    goal = str(payload.get("prompt") or task_id.replace("_", " "))
    guidance = payload.get("guidance")
    guidance_text = str(guidance) if guidance is not None else None
    return MineDojoProgrammaticTask(
        task_id=task_id,
        category=category,
        family=_family_for_category(category),
        goal=goal,
        guidance=guidance_text,
        allowed_actions=_allowed_actions_for_category(category),
        knowledge_tags=_knowledge_tags(task_id, category, goal),
        success_criteria={
            "type": "minedojo_programmatic",
            "task_id": task_id,
            "category": category,
            "catalog_only": True,
        },
        benchmark_suite_tier=suite_tier,
        minedojo={
            "programmatic": True,
            "creative": False,
            "catalog_only": True,
            "official_prompt": goal,
            "official_guidance_available": bool(guidance_text),
            "source_urls": asdict(source),
        },
    )


def _infer_category(task_id: str) -> str:
    """Infer a task category from the MineDojo task id prefix."""

    if task_id.startswith("combat_"):
        return "combat"
    if task_id.startswith("harvest_"):
        return "harvest"
    if task_id.startswith("techtree_"):
        return "techtree"
    if task_id.startswith("survival"):
        return "survival"
    return "programmatic"


def _family_for_category(category: str) -> str:
    """Map MineDojo category strings to display family names."""

    return {
        "combat": "Combat",
        "harvest": "Harvest",
        "survival": "Survival",
        "techtree": "TechTree",
    }.get(category, category.title())


def _allowed_actions_for_category(category: str) -> list[str]:
    """Return the unified primitive action surface for every live-trainable task."""

    _ = category
    return list(CANONICAL_PRIMITIVE_ACTIONS)


def _knowledge_tags(task_id: str, category: str, goal: str) -> list[str]:
    """Create deterministic tags for task selection and later knowledge retrieval."""

    tags = {f"minedojo:category/{category}", f"minedojo:task/{task_id}"}
    for token in _tokens(task_id) | _tokens(goal):
        if len(token) >= 3 and not token.isdigit():
            tags.add(f"minecraft:term/{token}")
    return sorted(tags)


def _tokens(text: str) -> set[str]:
    """Tokenize task ids and prompts into stable lowercase terms."""

    return {
        token
        for token in re.split(r"[^a-zA-Z0-9]+", text.lower())
        if token and token not in STOPWORDS
    }
