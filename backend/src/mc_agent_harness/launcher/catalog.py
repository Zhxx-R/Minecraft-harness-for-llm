from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


TaskKind = Literal["programmatic", "creative"]


@dataclass(frozen=True, slots=True)
class ExecutableTask:
    """One trusted executable manifest and the fixed snapshot that owns it."""

    task_id: str
    kind: TaskKind
    manifest_path: Path
    manifest: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        """Return compact task metadata used by the paginated launch catalog."""

        reset_plan = _record(self.manifest.get("reset_plan"))
        verifier = _record(self.manifest.get("verifier"))
        minedojo = _record(self.manifest.get("minedojo"))
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "category": str(self.manifest.get("category") or self.kind),
            "family": str(self.manifest.get("family") or self.kind.title()),
            "goal": str(self.manifest.get("goal") or self.task_id),
            "description": str(self.manifest.get("description") or ""),
            "runtime_profile": str(self.manifest.get("runtime_profile") or ""),
            "verifier_type": str(verifier.get("type") or "unknown"),
            "biome_hint": _optional_text(reset_plan.get("biome_hint")),
            "initial_inventory_count": len(_list(reset_plan.get("initial_inventory"))),
            "spawn_mob_count": sum(
                int(entry.get("count") or 0)
                for entry in _record_list(reset_plan.get("spawn_mobs"))
            ),
            "collection": _optional_text(minedojo.get("collection")),
            "allowed_action_count": len(_list(self.manifest.get("allowed_actions"))),
        }

    def detail(self) -> dict[str, Any]:
        """Return the complete safe launch configuration shown before execution."""

        minedojo = _record(self.manifest.get("minedojo"))
        source_spec = _record(minedojo.get("source_spec"))
        return {
            **self.summary(),
            "reset_plan": _record(self.manifest.get("reset_plan")),
            "verifier": _record(self.manifest.get("verifier")),
            "success_criteria": _record(self.manifest.get("success_criteria")),
            "allowed_actions": [
                str(value) for value in _list(self.manifest.get("allowed_actions"))
            ],
            "knowledge_tags": [
                str(value) for value in _list(self.manifest.get("knowledge_tags"))
            ],
            "source_metadata": {
                "adapter": minedojo.get("adapter"),
                "collection": minedojo.get("collection"),
                "template_id": minedojo.get("template_id"),
                "official_prompt": minedojo.get("official_prompt"),
                "game_mode": minedojo.get("game_mode"),
                "source_confidence": source_spec.get("confidence"),
            },
        }


class ExecutableTaskCatalog:
    """Thread-safe cached reader for fixed programmatic and creative JSONL snapshots."""

    def __init__(self, programmatic_path: Path, creative_path: Path) -> None:
        """Configure the only two manifest files the dashboard may launch."""

        self.programmatic_path = programmatic_path.expanduser().resolve()
        self.creative_path = creative_path.expanduser().resolve()
        self._lock = threading.RLock()
        self._signature: tuple[tuple[str, int, int], ...] = ()
        self._tasks: dict[str, ExecutableTask] = {}

    def list_tasks(
        self,
        *,
        query: str = "",
        kind: str = "all",
        category: str | None = None,
        offset: int = 0,
        limit: int = 40,
    ) -> tuple[list[ExecutableTask], int, dict[str, int], dict[str, int]]:
        """Filter, sort, and paginate executable tasks without loading them in the client."""

        tasks = self._filtered_tasks(query=query, kind=kind, category=category)
        categories: dict[str, int] = {}
        kinds: dict[str, int] = {}
        for task in self._all_tasks():
            task_category = str(task.manifest.get("category") or task.kind)
            categories[task_category] = categories.get(task_category, 0) + 1
            kinds[task.kind] = kinds.get(task.kind, 0) + 1
        return tasks[offset : offset + limit], len(tasks), categories, kinds

    def get(self, task_id: str) -> ExecutableTask:
        """Return one exact trusted task id or raise a lookup error."""

        self._refresh_if_needed()
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def random_task(
        self,
        *,
        query: str = "",
        kind: str = "all",
        category: str | None = None,
        random_source: random.Random | random.SystemRandom | None = None,
    ) -> ExecutableTask:
        """Select one task from the same visible filter domain used by the task browser."""

        tasks = self._filtered_tasks(query=query, kind=kind, category=category)
        if not tasks:
            raise LookupError("No executable tasks match the requested filters.")
        chooser = random_source or random.SystemRandom()
        return chooser.choice(tasks)

    def _filtered_tasks(
        self,
        *,
        query: str,
        kind: str,
        category: str | None,
    ) -> list[ExecutableTask]:
        """Apply deterministic catalog filters before pagination or random selection."""

        normalized_query = query.strip().lower()
        normalized_category = category.strip().lower() if category else None
        tasks: list[ExecutableTask] = []
        for task in self._all_tasks():
            task_category = str(task.manifest.get("category") or task.kind).lower()
            if kind != "all" and task.kind != kind:
                continue
            if normalized_category and task_category != normalized_category:
                continue
            if normalized_query and normalized_query not in _search_text(task):
                continue
            tasks.append(task)
        kind_order = {"programmatic": 0, "creative": 1}
        return sorted(
            tasks,
            key=lambda task: (kind_order.get(task.kind, 9), task.task_id),
        )

    def _all_tasks(self) -> list[ExecutableTask]:
        """Return a stable copy of the cached task list."""

        self._refresh_if_needed()
        with self._lock:
            return list(self._tasks.values())

    def _refresh_if_needed(self) -> None:
        """Reload snapshots only when a file's size or modification time changes."""

        signature = tuple(
            (
                str(path),
                path.stat().st_mtime_ns,
                path.stat().st_size,
            )
            for path in (self.programmatic_path, self.creative_path)
        )
        with self._lock:
            if signature == self._signature:
                return
            tasks: dict[str, ExecutableTask] = {}
            for path, kind in (
                (self.programmatic_path, "programmatic"),
                (self.creative_path, "creative"),
            ):
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(),
                    start=1,
                ):
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    task_id = payload.get("task_id")
                    if not isinstance(task_id, str) or not task_id:
                        raise ValueError(f"{path}:{line_number} has no valid task_id.")
                    if task_id in tasks:
                        raise ValueError(f"Duplicate executable task id: {task_id}")
                    tasks[task_id] = ExecutableTask(
                        task_id=task_id,
                        kind=kind,
                        manifest_path=path,
                        manifest=payload,
                    )
            self._tasks = tasks
            self._signature = signature


def _search_text(task: ExecutableTask) -> str:
    """Build one normalized search document without exposing full manifest JSON."""

    values = (
        task.task_id,
        task.manifest.get("goal"),
        task.manifest.get("description"),
        task.manifest.get("family"),
        task.manifest.get("category"),
    )
    return " ".join(str(value or "").lower() for value in values)


def _record(value: Any) -> dict[str, Any]:
    """Return a shallow JSON object or an empty object for malformed optional fields."""

    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    """Return a JSON list or an empty list for malformed optional fields."""

    return list(value) if isinstance(value, list) else []


def _record_list(value: Any) -> list[dict[str, Any]]:
    """Return only object entries from an optional JSON list."""

    return [dict(entry) for entry in _list(value) if isinstance(entry, dict)]


def _optional_text(value: Any) -> str | None:
    """Normalize optional scalar metadata into display text."""

    return str(value) if value is not None and str(value).strip() else None
