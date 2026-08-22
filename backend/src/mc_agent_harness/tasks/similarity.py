from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


TASK_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "from",
    "in",
    "of",
    "the",
    "to",
    "with",
    "without",
}


@dataclass(frozen=True, slots=True)
class TaskDescriptor:
    """Normalized task features used for diversity-aware training selection."""

    task_id: str
    category: str | None = None
    family: str | None = None
    goal_tokens: frozenset[str] = frozenset()
    knowledge_tags: frozenset[str] = frozenset()
    allowed_actions: frozenset[str] = frozenset()
    verifier_targets: frozenset[str] = frozenset()

    @classmethod
    def from_task_spec(cls, task_spec: dict[str, Any]) -> TaskDescriptor:
        """Extract deterministic similarity features from a harness task spec."""

        task_id = str(task_spec.get("task_id") or "")
        goal = str(task_spec.get("goal") or task_spec.get("description") or "")
        success_criteria = task_spec.get("success_criteria") or task_spec.get("verifier")
        return cls(
            task_id=task_id,
            category=_string_or_none(task_spec.get("category")),
            family=_string_or_none(task_spec.get("family")),
            goal_tokens=frozenset(_tokens(f"{task_id} {goal}")),
            knowledge_tags=frozenset(
                str(tag)
                for tag in task_spec.get("knowledge_tags", [])
                if isinstance(tag, str)
            ),
            allowed_actions=frozenset(
                str(action)
                for action in task_spec.get("allowed_actions", [])
                if isinstance(action, str)
            ),
            verifier_targets=frozenset(
                _verifier_targets(success_criteria) | _target_tokens_from_task_id(task_id)
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskSimilarityBreakdown:
    """Weighted similarity components for a pair of tasks."""

    category: float
    family: float
    goal: float
    knowledge_tags: float
    allowed_actions: float
    verifier_targets: float
    total: float


@dataclass(frozen=True, slots=True)
class DiverseBatchSelection:
    """Result returned by the diversity-aware task batch planner."""

    selected_task_ids: list[str]
    max_pairwise_similarity: float
    pairwise_similarities: dict[str, float] = field(default_factory=dict)
    deferred_task_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DiverseWavePlan:
    """Low-similarity task waves that may run concurrently within each wave."""

    waves: list[list[str]]
    max_wave_similarity: float
    wave_similarities: list[float]
    threshold_violations: list[int] = field(default_factory=list)


class TaskSimilarityScorer:
    """Deterministic task similarity scorer for parallel skill-training batches."""

    def score(self, left: TaskDescriptor, right: TaskDescriptor) -> TaskSimilarityBreakdown:
        """Return weighted similarity for a pair of task descriptors."""

        category = 1.0 if left.category and left.category == right.category else 0.0
        family = 1.0 if left.family and left.family == right.family else 0.0
        goal = _jaccard(left.goal_tokens, right.goal_tokens)
        knowledge_tags = _jaccard(left.knowledge_tags, right.knowledge_tags)
        allowed_actions = _jaccard(left.allowed_actions, right.allowed_actions)
        verifier_targets = _jaccard(left.verifier_targets, right.verifier_targets)
        total = (
            0.15 * category
            + 0.05 * family
            + 0.30 * goal
            + 0.20 * knowledge_tags
            + 0.15 * allowed_actions
            + 0.15 * verifier_targets
        )
        return TaskSimilarityBreakdown(
            category=category,
            family=family,
            goal=goal,
            knowledge_tags=knowledge_tags,
            allowed_actions=allowed_actions,
            verifier_targets=verifier_targets,
            total=round(total, 6),
        )


class DiverseBatchPlanner:
    """Greedy max-diversity planner for parallel single-agent training."""

    def __init__(self, scorer: TaskSimilarityScorer | None = None) -> None:
        self.scorer = scorer or TaskSimilarityScorer()

    def select_batch(
        self,
        tasks: Iterable[dict[str, Any] | TaskDescriptor],
        *,
        batch_size: int,
        max_pairwise_similarity: float | None = None,
    ) -> DiverseBatchSelection:
        """Select a deterministic low-similarity task batch."""

        if batch_size <= 0:
            raise ValueError("batch_size must be positive.")
        candidates = [_coerce_descriptor(task) for task in tasks]
        if not candidates:
            return DiverseBatchSelection(selected_task_ids=[], max_pairwise_similarity=0.0)

        remaining = sorted(candidates, key=lambda task: task.task_id)
        selected: list[TaskDescriptor] = []
        while remaining and len(selected) < batch_size:
            if not selected:
                next_task = _first_task(remaining)
            else:
                next_task = self._least_similar_candidate(
                    remaining,
                    selected,
                    max_pairwise_similarity=max_pairwise_similarity,
                )
            selected.append(next_task)
            remaining.remove(next_task)

        pairwise = self._pairwise(selected)
        return DiverseBatchSelection(
            selected_task_ids=[task.task_id for task in selected],
            max_pairwise_similarity=max(pairwise.values(), default=0.0),
            pairwise_similarities=pairwise,
            deferred_task_ids=[task.task_id for task in remaining],
        )

    def _least_similar_candidate(
        self,
        remaining: list[TaskDescriptor],
        selected: list[TaskDescriptor],
        *,
        max_pairwise_similarity: float | None,
    ) -> TaskDescriptor:
        """Return the remaining task with the lowest similarity to selected tasks."""

        scored = []
        for candidate in remaining:
            max_similarity = max(self.scorer.score(candidate, chosen).total for chosen in selected)
            if max_pairwise_similarity is not None and max_similarity > max_pairwise_similarity:
                threshold_penalty = 1
            else:
                threshold_penalty = 0
            same_category_count = sum(
                1
                for chosen in selected
                if chosen.category == candidate.category
            )
            scored.append(
                (
                    threshold_penalty,
                    max_similarity,
                    same_category_count,
                    candidate.task_id,
                    candidate,
                )
            )
        return sorted(scored, key=lambda item: (item[0], item[1], item[2], item[3]))[0][4]

    def _pairwise(self, selected: list[TaskDescriptor]) -> dict[str, float]:
        """Compute pairwise similarity values for selected tasks."""

        pairwise: dict[str, float] = {}
        for left_index, left in enumerate(selected):
            for right in selected[left_index + 1 :]:
                key = f"{left.task_id}::{right.task_id}"
                pairwise[key] = self.scorer.score(left, right).total
        return pairwise


class DiverseWavePlanner:
    """Arrange selected tasks into bounded concurrent waves with low internal similarity."""

    def __init__(self, scorer: TaskSimilarityScorer | None = None) -> None:
        self.scorer = scorer or TaskSimilarityScorer()

    def arrange(
        self,
        tasks: Iterable[dict[str, Any] | TaskDescriptor],
        *,
        wave_size: int,
        max_pairwise_similarity: float | None = None,
    ) -> DiverseWavePlan:
        """Greedily pair each anchor with the least-similar remaining tasks."""

        if wave_size <= 0:
            raise ValueError("wave_size must be positive.")
        remaining = [_coerce_descriptor(task) for task in tasks]
        task_ids = [task.task_id for task in remaining]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Diverse wave planning requires unique task ids.")
        waves: list[list[str]] = []
        similarities: list[float] = []
        violations: list[int] = []
        while remaining:
            wave = [remaining.pop(0)]
            while remaining and len(wave) < wave_size:
                scored = []
                for candidate in remaining:
                    maximum = max(self.scorer.score(candidate, chosen).total for chosen in wave)
                    threshold_penalty = int(
                        max_pairwise_similarity is not None
                        and maximum > max_pairwise_similarity
                    )
                    scored.append((threshold_penalty, maximum, candidate.task_id, candidate))
                selected_entry = min(scored, key=lambda item: (item[0], item[1], item[2]))
                if max_pairwise_similarity is not None and selected_entry[0] > 0:
                    break
                selected = selected_entry[3]
                wave.append(selected)
                remaining.remove(selected)
            wave_similarity = max(
                (
                    self.scorer.score(left, right).total
                    for index, left in enumerate(wave)
                    for right in wave[index + 1 :]
                ),
                default=0.0,
            )
            wave_index = len(waves)
            if (
                max_pairwise_similarity is not None
                and wave_similarity > max_pairwise_similarity
            ):
                violations.append(wave_index)
            waves.append([task.task_id for task in wave])
            similarities.append(wave_similarity)
        return DiverseWavePlan(
            waves=waves,
            max_wave_similarity=max(similarities, default=0.0),
            wave_similarities=similarities,
            threshold_violations=violations,
        )


def _coerce_descriptor(task: dict[str, Any] | TaskDescriptor) -> TaskDescriptor:
    """Convert task dictionaries into descriptors while preserving descriptors."""

    if isinstance(task, TaskDescriptor):
        return task
    return TaskDescriptor.from_task_spec(task)


def _first_task(tasks: list[TaskDescriptor]) -> TaskDescriptor:
    """Choose a deterministic first task while preferring official suite members."""

    return sorted(
        tasks,
        key=lambda task: (_suite_priority(task), task.category or "", task.task_id),
    )[0]


def _suite_priority(task: TaskDescriptor) -> int:
    """Return a deterministic priority for first-task selection."""

    if "standard" in task.knowledge_tags:
        return 0
    return 1


def _verifier_targets(verifier: Any) -> set[str]:
    """Extract item, block, entity, or MineDojo task targets from verifier specs."""

    targets: set[str] = set()
    if isinstance(verifier, list):
        for item in verifier:
            targets.update(_verifier_targets(item))
        return targets
    if not isinstance(verifier, dict):
        return targets
    for composite_key in ("all", "any"):
        if composite_key in verifier:
            targets.update(_verifier_targets(verifier[composite_key]))
    for key in (
        "item",
        "item_id",
        "block",
        "block_id",
        "entity",
        "entity_id",
        "name",
        "task_id",
        "category",
    ):
        value = verifier.get(key)
        if isinstance(value, str):
            targets.update(_tokens(value))
    return targets


def _target_tokens_from_task_id(task_id: str) -> set[str]:
    """Extract likely task target tokens from a MineDojo-style task id."""

    tokens = _tokens(task_id)
    return {
        token
        for token in tokens
        if token
        not in {
            "combat",
            "harvest",
            "techtree",
            "from",
            "barehand",
            "armors",
            "sword",
            "shield",
            "with",
        }
    }


def _tokens(text: str) -> set[str]:
    """Tokenize task ids and prompts into lowercase lexical features."""

    return {
        token
        for token in re.split(r"[^a-zA-Z0-9]+", text.lower())
        if token and token not in TASK_STOPWORDS
    }


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Return Jaccard similarity for two feature sets."""

    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def _string_or_none(value: Any) -> str | None:
    """Return a string value or None when the input is empty."""

    return value if isinstance(value, str) and value else None
