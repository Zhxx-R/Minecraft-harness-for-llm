from __future__ import annotations

import re
from dataclasses import dataclass

from mc_agent_harness.schemas.skill import SkillSpec


SKILL_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "of",
    "the",
    "to",
    "with",
}


@dataclass(frozen=True, slots=True)
class SkillSimilarityBreakdown:
    """Weighted similarity components for two skill specs."""

    action_types: float
    action_targets: float
    triggers: float
    task_scope: float
    dependencies: float
    name: float
    total: float


@dataclass(frozen=True, slots=True)
class SkillDuplicateMatch:
    """A candidate-to-existing skill match that may represent duplicate work."""

    skill: SkillSpec
    similarity: float
    breakdown: SkillSimilarityBreakdown


class SkillSimilarityScorer:
    """Deterministic scorer used to detect duplicate skill candidates."""

    def score(self, left: SkillSpec, right: SkillSpec) -> SkillSimilarityBreakdown:
        """Return a weighted similarity breakdown for two skill specs."""

        action_types = _jaccard(_action_types(left), _action_types(right))
        action_targets = _jaccard(_action_targets(left), _action_targets(right))
        triggers = _jaccard(_tokens_from_list(left.triggers), _tokens_from_list(right.triggers))
        task_scope = _jaccard(
            _tokens_from_list(left.task_scope),
            _tokens_from_list(right.task_scope),
        )
        dependencies = _jaccard(
            _tokens_from_list(left.dependencies),
            _tokens_from_list(right.dependencies),
        )
        name = _jaccard(_tokens(left.name), _tokens(right.name))
        total = (
            0.25 * action_types
            + 0.25 * action_targets
            + 0.15 * triggers
            + 0.15 * task_scope
            + 0.15 * dependencies
            + 0.05 * name
        )
        if left.name == right.name and action_types == 1.0 and action_targets == 1.0:
            total = max(total, 0.95)
        elif left.name == right.name:
            total = max(total, 0.9)
        return SkillSimilarityBreakdown(
            action_types=action_types,
            action_targets=action_targets,
            triggers=triggers,
            task_scope=task_scope,
            dependencies=dependencies,
            name=name,
            total=round(total, 6),
        )


class SkillCandidateDeduper:
    """Find near-duplicate skills before promotion or candidate insertion."""

    def __init__(self, scorer: SkillSimilarityScorer | None = None) -> None:
        self.scorer = scorer or SkillSimilarityScorer()

    def find_duplicates(
        self,
        candidate: SkillSpec,
        existing_skills: list[SkillSpec],
        *,
        threshold: float = 0.82,
    ) -> list[SkillDuplicateMatch]:
        """Return existing skills whose similarity is at or above the threshold."""

        matches = []
        for skill in existing_skills:
            if skill.name == candidate.name and skill.version == candidate.version:
                continue
            breakdown = self.scorer.score(candidate, skill)
            if breakdown.total >= threshold:
                matches.append(
                    SkillDuplicateMatch(
                        skill=skill,
                        similarity=breakdown.total,
                        breakdown=breakdown,
                    )
                )
        return sorted(
            matches,
            key=lambda match: (-match.similarity, match.skill.name, match.skill.version),
        )


def _action_types(spec: SkillSpec) -> frozenset[str]:
    """Return action type features from a skill action plan."""

    return frozenset(action.type for action in spec.action_plan)


def _action_targets(spec: SkillSpec) -> frozenset[str]:
    """Return target item/block/entity/station features from a skill action plan."""

    targets: set[str] = set()
    for action in spec.action_plan:
        for key in ("item", "item_id", "block", "block_id", "entity", "entity_id", "station"):
            value = action.args.get(key)
            if isinstance(value, str) and value:
                targets.update(_tokens(value))
    return frozenset(targets)


def _tokens_from_list(values: list[str]) -> frozenset[str]:
    """Tokenize a list of identifier or prose strings."""

    tokens: set[str] = set()
    for value in values:
        tokens.update(_tokens(value))
    return frozenset(tokens)


def _tokens(text: str) -> frozenset[str]:
    """Tokenize skill names and descriptions into lowercase features."""

    return frozenset(
        token
        for token in re.split(r"[^a-zA-Z0-9]+", text.lower())
        if token and token not in SKILL_STOPWORDS
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Return Jaccard similarity for two feature sets."""

    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)
