from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from mc_agent_harness.harness.tool_registry import DEFAULT_HARNESS_ACTIONS


CREATIVE_MANIFEST_SCHEMA = "mc-agent-harness.minedojo-creative-manifest.v1"
DEFAULT_NEGATIVE_PROMPT_COUNT = 7


@dataclass(frozen=True, slots=True)
class MineDojoCreativeBuildSummary:
    """Summary for one authentic MineDojo creative-task manifest build."""

    schema_version: str
    task_count: int
    collections: dict[str, int]
    calibration_statuses: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        """Convert the summary into a JSON-safe dictionary."""

        return asdict(self)


def adapt_creative_catalog(
    tasks: dict[str, Any] | Iterable[tuple[str, Any]],
    *,
    negative_prompt_count: int = DEFAULT_NEGATIVE_PROMPT_COUNT,
) -> tuple[list[dict[str, Any]], MineDojoCreativeBuildSummary]:
    """Convert MineDojo creative task metadata into executable harness manifests."""

    if negative_prompt_count < 1:
        raise ValueError("negative_prompt_count must be positive.")
    items = list(tasks.items()) if isinstance(tasks, dict) else list(tasks)
    normalized = [_normalize_creative_row(task_id, payload) for task_id, payload in items]
    manifests = [
        _creative_manifest(
            task,
            negative_prompts=_select_negative_prompts(
                task,
                normalized,
                count=negative_prompt_count,
            ),
        )
        for task in normalized
    ]
    collections: dict[str, int] = {}
    calibration_statuses: dict[str, int] = {}
    for manifest in manifests:
        collection = str(manifest["minedojo"].get("collection") or "unknown")
        collections[collection] = collections.get(collection, 0) + 1
        status = str(manifest["verifier"]["calibration"]["status"])
        calibration_statuses[status] = calibration_statuses.get(status, 0) + 1
    summary = MineDojoCreativeBuildSummary(
        schema_version=CREATIVE_MANIFEST_SCHEMA,
        task_count=len(manifests),
        collections=dict(sorted(collections.items())),
        calibration_statuses=dict(sorted(calibration_statuses.items())),
    )
    return manifests, summary


def write_creative_manifest_jsonl(
    manifests: Iterable[dict[str, Any]],
    *,
    output_path: str | Path,
    summary: MineDojoCreativeBuildSummary | None = None,
    summary_path: str | Path | None = None,
) -> Path:
    """Write creative manifests and an optional build summary to local snapshots."""

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = list(manifests)
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
        + ("\n" if rows else ""),
        encoding="utf-8",
    )
    if summary is not None and summary_path is not None:
        summary_output = Path(summary_path)
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary_output.write_text(
            json.dumps(summary.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return output


def _normalize_creative_row(task_id: str, payload: Any) -> dict[str, Any]:
    """Normalize one official YAML row while rejecting malformed creative tasks."""

    if not isinstance(payload, dict):
        raise ValueError(f"Creative task {task_id!r} must be an object.")
    prompt = str(payload.get("prompt") or "").strip()
    if not task_id.startswith("creative:") or not prompt:
        raise ValueError(f"Invalid MineDojo creative task row: {task_id!r}.")
    return {
        "task_id": task_id,
        "prompt": prompt,
        "guidance": str(payload.get("guidance") or "").strip(),
        "collection": str(payload.get("collection") or "unknown"),
        "source": payload.get("source"),
    }


def _creative_manifest(
    task: dict[str, Any],
    *,
    negative_prompts: list[str],
) -> dict[str, Any]:
    """Build one executable manifest whose success remains external to the agent."""

    frame_sampling = {
        "source": "agent_first_person_recording",
        "sample_fps": 2.0,
        "clip_length": 16,
        "window_stride": 8,
        "max_windows": 64,
        "key_frame_count": 3,
    }
    calibration = {
        "status": "pending",
        "method": "kmeans_2_centroid_midpoint",
        "score_threshold": None,
        "examples": [],
        "minimum_trajectories": 20,
        "recommended_trajectories": 200,
    }
    verifier = {
        "type": "creative_mineclip",
        "prompt": task["prompt"],
        "negative_prompts": negative_prompts,
        "frame_sampling": frame_sampling,
        "aggregation": "trajectory_mean",
        "score_threshold": None,
        "calibration": calibration,
    }
    return {
        "task_id": task["task_id"],
        "source": "minedojo",
        "category": "creative",
        "family": "Creative",
        "goal": task["prompt"],
        "description": "Authentic MineDojo creative task evaluated externally with MineCLIP.",
        "schema_version": CREATIVE_MANIFEST_SCHEMA,
        "allowed_actions": list(DEFAULT_HARNESS_ACTIONS),
        "verifier": verifier,
        "success_criteria": verifier,
        "knowledge_tags": _knowledge_tags(task["prompt"]),
        "runtime_profile": "live-mineflayer-creative",
        "reset_plan": {
            "game_mode": "survival",
            "clear_inventory": True,
            "clear_dropped_items": True,
            "initial_inventory": [],
        },
        "minedojo": {
            "programmatic": False,
            "creative": True,
            "creative_task_id": task["task_id"],
            "collection": task["collection"],
            "source": task["source"],
            "guidance": task["guidance"],
            "guidance_policy": "metadata_only_not_auto_prompted",
            "game_mode": "survival",
            "game_mode_policy": "creative_is_task_category_not_minecraft_creative_mode",
            "executable": True,
            "catalog_only": False,
            "adapter": "minedojo_creative_v1",
        },
    }


def _select_negative_prompts(
    task: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    count: int,
) -> list[str]:
    """Select deterministic, lexically distinct contrast prompts for MineCLIP scoring."""

    target_tokens = _tokens(task["prompt"])
    ranked: list[tuple[str, dict[str, Any]]] = []
    for candidate in candidates:
        if candidate["task_id"] == task["task_id"]:
            continue
        overlap = _jaccard(target_tokens, _tokens(candidate["prompt"]))
        if overlap > 0.35:
            continue
        digest = hashlib.sha256(
            f"{task['task_id']}|{candidate['task_id']}".encode("utf-8")
        ).hexdigest()
        ranked.append((digest, candidate))
    ranked.sort(key=lambda item: item[0])
    selected = [candidate["prompt"] for _, candidate in ranked[:count]]
    if len(selected) < count:
        raise ValueError(f"Could not select {count} negative prompts for {task['task_id']}.")
    return selected


def _knowledge_tags(prompt: str) -> list[str]:
    """Build compact retrieval tags from meaningful prompt tokens."""

    stop_words = {
        "a",
        "an",
        "and",
        "in",
        "of",
        "the",
        "to",
        "with",
        "world",
        "minecraft",
    }
    tokens = [token for token in sorted(_tokens(prompt)) if token not in stop_words]
    return [f"minecraft:creative/{token}" for token in tokens[:8]]


def _tokens(text: str) -> set[str]:
    """Tokenize an English task prompt for deterministic contrast selection."""

    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _jaccard(left: set[str], right: set[str]) -> float:
    """Return lexical Jaccard similarity for two prompt token sets."""

    union = left | right
    return len(left & right) / len(union) if union else 0.0
