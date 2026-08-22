#!/usr/bin/env python3
"""Build JSON-only showcase cuts for the wool-agent evolution experiments."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "runs/demos/wool_evolution_showcase"
MONO_FONT = Path("/System/Library/Fonts/Menlo.ttc")
WAIT_TARGET_SEC = 2.0
WAIT_MAX_SPEED = 20.0
ACTION_MIN_DISPLAY_SEC = 2.0
ACTION_RESULT_HOLD_SEC = 1.25
MEMORY_CREATED_MIN_DISPLAY_SEC = 5.0


@dataclass(frozen=True)
class RunSpec:
    slug: str
    run_dir: Path
    output_name: str
    source_crop: tuple[int, int, int, int] | None = None
    source_end_sec: float | None = None
    terminal_hold_sec: float = 0.0
    step_start: int | None = None
    step_end: int | None = None


RUN_SPECS = (
    RunSpec(
        slug="01_no_follow",
        run_dir=ROOT_DIR
        / "runs/demos/programmatic_wool_skill_video_retry2_20260725T064830Z/with_skill",
        output_name="01_no_follow_api_fastforward_json.mp4",
        # Show the failed interaction followed by repeated coordinate chasing.
        # Stop before the first successful use_item so this cut isolates the
        # limitation that motivated the follow action.
        step_start=3,
        step_end=10,
        terminal_hold_sec=3.0,
    ),
    RunSpec(
        slug="02_follow_without_memory",
        run_dir=ROOT_DIR
        / "runs/demos/programmatic_wool_follow_entity_id_20260725T120240Z/with_skill",
        output_name="02_follow_without_memory_api_fastforward_json.mp4",
        # This source captured a fixed desktop margin around the client.
        # Keep the whole Minecraft window, then letterbox it back to 1708x1024.
        source_crop=(0, 262, 1412, 762),
        # Keep only the first successful follow/use_item sequence and its scan,
        # whose observation exposes the resulting brown_wool drop.
        step_start=3,
        step_end=6,
        terminal_hold_sec=3.0,
    ),
    RunSpec(
        slug="02_follow_repeated_without_memory",
        run_dir=ROOT_DIR
        / "runs/demos/programmatic_wool_follow_entity_id_20260725T120240Z/with_skill",
        output_name="02_follow_repeated_without_memory_api_fastforward_json.mp4",
        source_crop=(0, 262, 1412, 762),
        # After brown wool has appeared, retain the repeated follow/use_item
        # loop against the same entity_id to motivate task memory.
        step_start=11,
        step_end=21,
        terminal_hold_sec=3.0,
    ),
    RunSpec(
        slug="03_memory_avoids_repeat",
        run_dir=ROOT_DIR
        / "runs/demos/programmatic_wool_memory_brown_sheep_20260726_run4/with_skill",
        output_name="03_memory_avoids_same_brown_sheep_api_fastforward_json.mp4",
        # Begin with the pre-memory follow of the brown sheep. Then show the
        # accepted memory write and the subsequent scan-only search behavior.
        step_start=2,
        step_end=7,
        terminal_hold_sec=3.0,
    ),
    RunSpec(
        slug="03_enhanced_scan_success",
        run_dir=ROOT_DIR
        / "runs/demos/programmatic_wool_memory_same_spawn_20260726T095341Z/with_skill",
        output_name="03_enhanced_scan_success_api_fastforward_json.mp4",
        terminal_hold_sec=3.0,
    ),
    RunSpec(
        slug="04_follow_handoff_success",
        run_dir=ROOT_DIR
        / "runs/demos/programmatic_wool_memory_same_spawn_20260726T113852Z/with_skill",
        output_name="04_follow_handoff_success_api_fastforward_json.mp4",
        terminal_hold_sec=3.0,
    ),
    RunSpec(
        slug="05a_before_recommended_action",
        run_dir=ROOT_DIR
        / "runs/demos/programmatic_wool_memory_same_spawn_20260726T095341Z/with_skill",
        output_name="05a_before_recommended_action_follow_move_to_failure.mp4",
        step_start=3,
        step_end=5,
        terminal_hold_sec=3.0,
    ),
    RunSpec(
        slug="05b_after_recommended_action",
        run_dir=ROOT_DIR
        / "runs/demos/programmatic_wool_memory_same_spawn_20260726T113852Z/with_skill",
        output_name="05b_after_recommended_action_follow_use_item_success.mp4",
        step_start=5,
        step_end=6,
        terminal_hold_sec=3.0,
    ),
)


@dataclass(frozen=True)
class StepTrace:
    step_index: int
    context_time: float
    model_time: float | None
    result_time: float | None
    observation: dict[str, Any]
    action: dict[str, Any] | None
    action_result: dict[str, Any] | None
    memory_update: dict[str, Any] | None


@dataclass(frozen=True)
class EditSegment:
    source_start: float
    source_end: float
    speed: float
    step_index: int
    phase: str
    overlay_payload: dict[str, Any]

    @property
    def source_duration(self) -> float:
        return self.source_end - self.source_start

    @property
    def output_duration(self) -> float:
        return self.source_duration / self.speed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--only", choices=[spec.slug for spec in RUN_SPECS])
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe")
    return parser.parse_args()


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = True,
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=capture_output,
        text=True,
    )
    return result.stdout if capture_output else ""


def probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
    return json.loads(
        run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                (
                    "format=duration,size:"
                    "stream=codec_name,profile,width,height,pix_fmt,level,avg_frame_rate"
                ),
                "-of",
                "json",
                str(path),
            ]
        )
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def relative_seconds(value: str, origin: datetime) -> float:
    return (parse_time(value) - origin).total_seconds()


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def rounded_number(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, int):
        return value
    return value


def compact_position(value: Any) -> list[Any] | None:
    if not isinstance(value, dict):
        return None
    if not all(axis in value for axis in ("x", "y", "z")):
        return None
    return [rounded_number(value["x"]), rounded_number(value["y"]), rounded_number(value["z"])]


def compact_inventory(value: Any) -> dict[str, int]:
    if not isinstance(value, list):
        return {}
    result: dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        count = item.get("count")
        if name and isinstance(count, int):
            result[name] = count
    return result


def compact_goal_progress(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if not isinstance(first, dict):
        return None
    result = {
        key: first[key]
        for key in ("item", "current_delta", "target_delta", "satisfied")
        if key in first
    }
    return result or None


def compact_active_follow(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("active") is not True:
        return None
    target = value.get("target")
    target = target if isinstance(target, dict) else {}
    result: dict[str, Any] = {
        "entity_id": target.get("entity_id") or target.get("id"),
        "name": target.get("name"),
        "follow_distance": rounded_number(value.get("follow_distance")),
    }
    return {key: item for key, item in result.items() if item is not None}


def compact_entity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {
        "entity_id": value.get("entity_id") or value.get("id"),
        "name": value.get("name"),
        "distance": rounded_number(value.get("distance")),
    }
    dropped_item = value.get("dropped_item")
    if isinstance(dropped_item, dict):
        result["dropped_item"] = {
            key: dropped_item[key]
            for key in ("name", "count")
            if key in dropped_item
        }
    details = value.get("details")
    details = details if isinstance(details, dict) else {}
    decoded = details.get("metadata_decoded")
    decoded = decoded if isinstance(decoded, dict) else {}
    wool = decoded.get("wool")
    if isinstance(wool, dict):
        result["wool"] = {
            key: wool[key]
            for key in ("color", "color_id", "is_sheared")
            if key in wool
        }
    return {key: item for key, item in result.items() if item is not None}


def select_relevant_entity(current_state: dict[str, Any]) -> dict[str, Any] | None:
    entities = current_state.get("nearby_entities")
    if not isinstance(entities, list):
        return None
    for entity in entities:
        if isinstance(entity, dict) and entity.get("name") == "sheep":
            return compact_entity(entity)
    for entity in entities:
        if isinstance(entity, dict) and isinstance(entity.get("dropped_item"), dict):
            return compact_entity(entity)
    return compact_entity(entities[0]) if entities else None


def compact_observed_dropped_items(
    current_state: dict[str, Any],
    *,
    limit: int = 2,
) -> list[dict[str, Any]]:
    entities = current_state.get("nearby_entities")
    if not isinstance(entities, list):
        return []
    result: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        dropped_item = entity.get("dropped_item")
        if not isinstance(dropped_item, dict):
            continue
        item_name = dropped_item.get("name")
        if not isinstance(item_name, str) or not item_name.endswith("_wool"):
            continue
        compact_drop = {
            key: item
            for key, item in {
                "entity_id": entity.get("entity_id") or entity.get("id"),
                "item": item_name,
                "count": dropped_item.get("count"),
                "distance": rounded_number(entity.get("distance")),
            }.items()
            if item is not None
        }
        if compact_drop:
            result.append(compact_drop)
        if len(result) >= limit:
            break
    return result


def compact_memory_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    selected_values = value.get("selected_values")
    compact_values: list[dict[str, Any]] = []
    if isinstance(selected_values, list):
        for selected in selected_values[:4]:
            if not isinstance(selected, dict) or "path" not in selected:
                continue
            compact_values.append(
                {
                    key: selected[key]
                    for key in ("path", "value")
                    if key in selected
                }
            )
    result: dict[str, Any] = {
        "memory_key": value.get("memory_key"),
        "source_ref": value.get("source_ref"),
    }
    if compact_values:
        result["selected_values"] = compact_values
    if value.get("note"):
        result["note"] = value["note"]
    return {key: item for key, item in result.items() if item is not None}


def compact_context_memory(user_payload: dict[str, Any]) -> list[dict[str, Any]]:
    run_context = json_object(user_payload.get("run_context"))
    memory = json_object(run_context.get("memory"))
    entries = memory.get("entries")
    if not isinstance(entries, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in entries[:2]:
        compact_entry = compact_memory_entry(entry)
        if compact_entry:
            result.append(compact_entry)
    return result


def compact_memory_update(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    outcomes = value.get("outcomes")
    if not isinstance(outcomes, list):
        return None
    accepted_outcome = next(
        (
            outcome
            for outcome in outcomes
            if isinstance(outcome, dict) and outcome.get("accepted") is True
        ),
        None,
    )
    if accepted_outcome is None:
        rejected_outcome = next(
            (outcome for outcome in outcomes if isinstance(outcome, dict)),
            None,
        )
        if rejected_outcome is None:
            return None
        return {
            key: item
            for key, item in {
                "accepted": False,
                "error_code": rejected_outcome.get("error_code"),
            }.items()
            if item is not None
        }

    result: dict[str, Any] = {
        "accepted": True,
        "operation": accepted_outcome.get("operation"),
    }
    entry = compact_memory_entry(accepted_outcome.get("entry"))
    if entry:
        result["entry"] = entry
    return {key: item for key, item in result.items() if item is not None}


def compact_previous_step(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {
        "step": value.get("step_index"),
        "type": value.get("action_type"),
        "ok": value.get("ok"),
    }
    status = value.get("status") or value.get("error_code")
    if status is not None:
        result["status"] = status
    target = value.get("target")
    if isinstance(target, dict):
        entity_id = target.get("entity_id") or target.get("id")
        if entity_id is not None:
            result["entity_id"] = entity_id
    if value.get("entity_id") is not None:
        result["entity_id"] = value["entity_id"]
    inventory_delta = value.get("inventory_delta")
    action_type = value.get("action_type")
    if isinstance(inventory_delta, dict) and (
        inventory_delta or action_type == "use_item"
    ):
        result["inventory_delta"] = inventory_delta
    spawned_drops = value.get("spawned_drops")
    if isinstance(spawned_drops, list) and (
        spawned_drops or action_type == "use_item"
    ):
        compact_drops = []
        for drop in spawned_drops[:2]:
            if not isinstance(drop, dict):
                continue
            compact_drop = {
                key: drop[key]
                for key in ("item", "count", "entity_id")
                if key in drop
            }
            if compact_drop:
                compact_drops.append(compact_drop)
        result["spawned_drops"] = compact_drops
    if "observed_effect" in value:
        result["observed_effect"] = bool(value["observed_effect"])
    metadata_delta = value.get("metadata_delta")
    if isinstance(metadata_delta, dict) and metadata_delta:
        result["metadata_delta"] = metadata_delta
    recommended_next_actions = value.get("recommended_next_actions")
    if isinstance(recommended_next_actions, list):
        retained_recommendations = [
            str(item)
            for item in recommended_next_actions[:3]
            if isinstance(item, str) and item
        ]
        if retained_recommendations:
            result["recommended_next_actions"] = retained_recommendations
    stopped = value.get("persistent_follow_stopped")
    if isinstance(stopped, dict):
        stopped_target = stopped.get("target")
        stopped_target = stopped_target if isinstance(stopped_target, dict) else {}
        result["follow_stopped"] = {
            key: item
            for key, item in {
                "entity_id": stopped_target.get("entity_id") or stopped_target.get("id"),
                "reason": stopped.get("stop_reason"),
            }.items()
            if item is not None
        }
    return {key: item for key, item in result.items() if item is not None}


def compact_observation(user_payload: dict[str, Any]) -> dict[str, Any]:
    compact_evidence = json_object(user_payload.get("compact_evidence"))
    current_state = json_object(compact_evidence.get("current_state"))
    observation: dict[str, Any] = {}

    goal_progress = compact_goal_progress(compact_evidence.get("goal_progress"))
    if goal_progress:
        observation["goal_progress"] = goal_progress
    position = compact_position(current_state.get("position"))
    if position:
        observation["position"] = position
    inventory = compact_inventory(current_state.get("inventory"))
    if inventory:
        observation["inventory"] = inventory
    active_follow = compact_active_follow(current_state.get("active_follow"))
    observation["active_follow"] = active_follow
    entity = select_relevant_entity(current_state)
    if entity:
        observation["entity"] = entity
    observed_dropped_items = compact_observed_dropped_items(current_state)
    if observed_dropped_items:
        observation["observed_dropped_items"] = observed_dropped_items
    memory_entries = compact_context_memory(user_payload)
    if memory_entries:
        observation["memory"] = memory_entries
    previous = compact_previous_step(compact_evidence.get("previous_step"))
    if previous:
        observation["previous"] = previous
    return observation


def load_step_traces(database_path: Path) -> tuple[datetime, list[StepTrace]]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        run_started_row = connection.execute(
            """
            SELECT created_at
            FROM trajectory_events
            WHERE event_type = 'run_started'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        if run_started_row is None:
            raise RuntimeError(f"run_started event missing: {database_path}")
        run_started = parse_time(run_started_row["created_at"])

        contexts: dict[int, tuple[float, dict[str, Any]]] = {}
        model_times: dict[int, float] = {}
        result_times: dict[int, float] = {}
        memory_updates: dict[int, dict[str, Any]] = {}
        for row in connection.execute(
            """
            SELECT event_type, payload, created_at
            FROM trajectory_events
            WHERE event_type IN (
                'context_built',
                'model_action',
                'action_result',
                'memory_update'
            )
            ORDER BY id
            """
        ):
            payload = json.loads(row["payload"])
            step_index = payload.get("step_index")
            if not isinstance(step_index, int):
                continue
            event_time = relative_seconds(row["created_at"], run_started)
            if row["event_type"] == "context_built":
                prompt_sections = json_object(payload.get("prompt_sections"))
                user_payload = json_object(prompt_sections.get("user_payload"))
                contexts[step_index] = (event_time, compact_observation(user_payload))
            elif row["event_type"] == "model_action":
                model_times[step_index] = event_time
            elif row["event_type"] == "action_result":
                result_times[step_index] = event_time
            elif row["event_type"] == "memory_update":
                compact_update = compact_memory_update(payload)
                if compact_update:
                    memory_updates[step_index] = compact_update

        actions: dict[int, dict[str, Any]] = {}
        action_results: dict[int, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT step_index, action, action_result FROM steps ORDER BY step_index"
        ):
            row_step_index = int(row["step_index"])
            actions[row_step_index] = json.loads(row["action"])
            action_results[row_step_index] = json.loads(row["action_result"])
    finally:
        connection.close()

    traces = [
        StepTrace(
            step_index=step_index,
            context_time=context_time,
            model_time=model_times.get(step_index),
            result_time=result_times.get(step_index),
            observation=observation,
            action=actions.get(step_index),
            action_result=action_results.get(step_index),
            memory_update=memory_updates.get(step_index),
        )
        for step_index, (context_time, observation) in sorted(contexts.items())
    ]
    return run_started, traces


def overlay_payload(
    trace: StepTrace,
    *,
    action_visible: bool,
) -> dict[str, Any]:
    return {
        "step": trace.step_index,
        "observation": trace.observation,
        "memory_update": trace.memory_update if action_visible else None,
        "action": trace.action if action_visible else None,
    }


def terminal_overlay_payload(trace: StepTrace) -> dict[str, Any]:
    result = trace.action_result if isinstance(trace.action_result, dict) else {}
    runtime_observation = json_object(result.get("observation"))
    observation: dict[str, Any] = {}

    prior_progress = trace.observation.get("goal_progress")
    if isinstance(prior_progress, dict):
        goal_progress = dict(prior_progress)
        target_item = goal_progress.get("item")
        inventory_delta = result.get("inventory_delta")
        if isinstance(target_item, str) and isinstance(inventory_delta, dict):
            current_delta = inventory_delta.get(target_item)
            target_delta = goal_progress.get("target_delta")
            if isinstance(current_delta, int):
                goal_progress["current_delta"] = current_delta
                if isinstance(target_delta, int):
                    goal_progress["satisfied"] = current_delta >= target_delta
        observation["goal_progress"] = goal_progress

    position = compact_position(
        runtime_observation.get("position") or result.get("end_position")
    )
    if position:
        observation["position"] = position
    inventory = compact_inventory(runtime_observation.get("inventory"))
    if inventory:
        observation["inventory"] = inventory
    observation["active_follow"] = compact_active_follow(
        runtime_observation.get("active_follow")
    )
    entity = select_relevant_entity(runtime_observation)
    if entity:
        observation["entity"] = entity
    observed_dropped_items = compact_observed_dropped_items(runtime_observation)
    if observed_dropped_items:
        observation["observed_dropped_items"] = observed_dropped_items
    retained_memory = trace.observation.get("memory")
    if isinstance(retained_memory, list) and retained_memory:
        observation["memory"] = retained_memory
    previous = compact_previous_step(
        {
            **result,
            "step_index": trace.step_index,
            "action_type": (trace.action or {}).get("type"),
        }
    )
    if previous:
        observation["previous"] = previous
    return {
        "step": trace.step_index,
        "observation": observation,
        "memory_update": trace.memory_update,
        "action": trace.action,
    }


def wait_speed(duration: float) -> float:
    if duration <= WAIT_TARGET_SEC:
        return 1.0
    return min(WAIT_MAX_SPEED, duration / WAIT_TARGET_SEC)


def build_edit_segments(
    traces: list[StepTrace],
    video_duration: float,
    *,
    source_start: float = 0.0,
) -> list[EditSegment]:
    segments: list[EditSegment] = []
    cursor = max(0.0, min(video_duration, source_start))
    model_times = [
        trace.model_time
        for trace in traces
        if trace.model_time is not None
    ]

    for trace in traces:
        context_time = max(0.0, min(video_duration, trace.context_time))
        model_time = (
            max(0.0, min(video_duration, trace.model_time))
            if trace.model_time is not None
            else None
        )
        if model_time is None:
            start = max(cursor, context_time)
            if video_duration > start:
                duration = video_duration - start
                segments.append(
                    EditSegment(
                        source_start=start,
                        source_end=video_duration,
                        speed=wait_speed(duration),
                        step_index=trace.step_index,
                        phase="model_api",
                        overlay_payload=overlay_payload(trace, action_visible=False),
                    )
                )
                cursor = video_duration
            break

        wait_start = max(cursor, context_time)
        if model_time > wait_start + 0.01:
            duration = model_time - wait_start
            segments.append(
                EditSegment(
                    source_start=wait_start,
                    source_end=model_time,
                    speed=wait_speed(duration),
                    step_index=trace.step_index,
                    phase="model_api",
                    overlay_payload=overlay_payload(trace, action_visible=False),
                )
            )

        result_time = trace.result_time if trace.result_time is not None else model_time
        result_time = max(model_time, min(video_duration, result_time))
        next_model_time = next(
            (
                candidate
                for candidate in model_times
                if candidate is not None and candidate > model_time
            ),
            video_duration,
        )
        action_min_display_sec = ACTION_MIN_DISPLAY_SEC
        if (
            isinstance(trace.memory_update, dict)
            and trace.memory_update.get("accepted") is True
            and trace.memory_update.get("operation") == "created"
        ):
            action_min_display_sec = MEMORY_CREATED_MIN_DISPLAY_SEC
        action_end = max(
            model_time + action_min_display_sec,
            result_time + ACTION_RESULT_HOLD_SEC,
        )
        action_end = min(video_duration, next_model_time, action_end)
        if action_end > model_time + 0.01:
            segments.append(
                EditSegment(
                    source_start=model_time,
                    source_end=action_end,
                    speed=1.0,
                    step_index=trace.step_index,
                    phase="action",
                    overlay_payload=overlay_payload(trace, action_visible=True),
                )
            )
        cursor = max(cursor, action_end)

    if cursor < video_duration and traces:
        last_trace = traces[-1]
        duration = video_duration - cursor
        segments.append(
            EditSegment(
                source_start=cursor,
                source_end=video_duration,
                speed=wait_speed(duration),
                step_index=last_trace.step_index,
                phase="model_api",
                overlay_payload=overlay_payload(last_trace, action_visible=False),
            )
        )
    return [segment for segment in segments if segment.source_duration > 0.01]


def json_lines(payload: dict[str, Any]) -> list[str]:
    step = payload["step"]
    observation = payload["observation"]
    memory_update = payload.get("memory_update")
    action = payload["action"]
    lines = ["{", f'  "step": {json.dumps(step)},', '  "observation": {']
    observation_items = list(observation.items())
    for index, (key, value) in enumerate(observation_items):
        comma = "," if index < len(observation_items) - 1 else ""
        encoded = json.dumps(value, ensure_ascii=True, separators=(", ", ": "))
        lines.append(f'    {json.dumps(key)}: {encoded}{comma}')
    lines.append("  },")
    if memory_update is not None:
        encoded_memory_update = json.dumps(
            memory_update,
            ensure_ascii=True,
            separators=(", ", ": "),
        )
        lines.append(f'  "memory_update": {encoded_memory_update},')
    if action is None:
        lines.append('  "action": null')
    else:
        action_type = json.dumps(action.get("type"), ensure_ascii=True)
        action_args = json.dumps(
            action.get("args") or {},
            ensure_ascii=True,
            separators=(", ", ": "),
        )
        lines.extend(
            [
                '  "action": {',
                f'    "type": {action_type},',
                f'    "args": {action_args}',
                "  }",
            ]
        )
    lines.append("}")
    return lines


def wrap_json_line(line: str, max_chars: int = 98) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    indent = len(line) - len(line.lstrip(" "))
    prefix = " " * (indent + 4)
    chunks: list[str] = []
    remaining = line
    while len(remaining) > max_chars:
        split_at = max(
            remaining.rfind(", ", 0, max_chars),
            remaining.rfind("}, ", 0, max_chars),
        )
        if split_at <= indent:
            split_at = max_chars
        else:
            split_at += 1
        chunks.append(remaining[:split_at].rstrip())
        remaining = prefix + remaining[split_at:].lstrip()
    chunks.append(remaining)
    return chunks


def render_overlay(
    payload: dict[str, Any],
    *,
    width: int,
    height: int,
    output_path: Path,
) -> None:
    if not MONO_FONT.is_file():
        raise FileNotFoundError(MONO_FONT)
    lines: list[str] = []
    for line in json_lines(payload):
        lines.extend(wrap_json_line(line))

    scale = width / 1708.0
    font_size = max(16, round((20 if len(lines) <= 18 else 18) * scale))
    font = ImageFont.truetype(str(MONO_FONT), size=font_size)
    line_height = round(font_size * 1.42)
    padding_x = round(24 * scale)
    padding_y = round(20 * scale)
    margin = round(28 * scale)
    panel_width = min(width - margin * 2, round(1180 * scale))
    panel_height = min(
        height - margin * 2,
        padding_y * 2 + line_height * len(lines),
    )
    panel_left = margin
    panel_top = height - margin - panel_height

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (
            panel_left,
            panel_top,
            panel_left + panel_width,
            panel_top + panel_height,
        ),
        radius=round(15 * scale),
        fill=(5, 10, 18, 220),
        outline=(105, 160, 255, 110),
        width=max(1, round(2 * scale)),
    )
    draw.multiline_text(
        (panel_left + padding_x, panel_top + padding_y),
        "\n".join(lines),
        font=font,
        fill=(235, 242, 252, 255),
        spacing=max(2, round(4 * scale)),
    )
    canvas.save(output_path)


def render_segment(
    *,
    ffmpeg: str,
    source_video: Path,
    card_path: Path,
    segment: EditSegment,
    source_crop: tuple[int, int, int, int] | None,
    output_width: int,
    output_height: int,
    output_path: Path,
) -> None:
    source_filter = ""
    if source_crop is not None:
        crop_x, crop_y, crop_width, crop_height = source_crop
        source_filter = (
            f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
            f"scale={output_width}:{output_height}:"
            "force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={output_width}:{output_height}:"
            "(ow-iw)/2:(oh-ih)/2:color=black,"
        )
    run_command(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{segment.source_start:.3f}",
            "-t",
            f"{segment.source_duration:.3f}",
            "-i",
            str(source_video),
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            str(card_path),
            "-filter_complex",
            (
                f"[0:v]{source_filter}setpts=(PTS-STARTPTS)/{segment.speed:.8f},"
                "fps=30,format=rgba[base];"
                "[1:v]format=rgba[card];"
                "[base][card]overlay=0:0:shortest=1,"
                "scale=in_range=pc:out_range=tv,format=yuv420p[outv]"
            ),
            "-map",
            "[outv]",
            "-an",
            "-t",
            f"{segment.output_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(output_path),
        ],
        capture_output=True,
    )


def render_run(
    *,
    spec: RunSpec,
    output_dir: Path,
    ffmpeg: str,
    ffprobe: str,
) -> dict[str, Any]:
    source_video = spec.run_dir / "agent_pov.mp4"
    database_path = spec.run_dir / "audit.sqlite3"
    for required in (source_video, database_path):
        if not required.is_file():
            raise FileNotFoundError(required)

    source_probe = probe_video(ffprobe, source_video)
    stream = source_probe["streams"][0]
    width = int(stream["width"])
    height = int(stream["height"])
    source_duration = float(source_probe["format"]["duration"])
    _, all_traces = load_step_traces(database_path)
    traces = [
        trace
        for trace in all_traces
        if (spec.step_start is None or trace.step_index >= spec.step_start)
        and (spec.step_end is None or trace.step_index <= spec.step_end)
    ]
    if not traces:
        raise RuntimeError(f"{spec.slug}: selected step range contains no traces")

    edit_source_start = traces[0].context_time if spec.step_start is not None else 0.0
    derived_source_end = source_duration
    if spec.step_end is not None:
        next_trace = next(
            (
                trace
                for trace in all_traces
                if trace.step_index > spec.step_end
            ),
            None,
        )
        if next_trace is not None:
            derived_source_end = next_trace.context_time
    edit_source_end = min(
        source_duration,
        derived_source_end,
        spec.source_end_sec if spec.source_end_sec is not None else source_duration,
    )
    if edit_source_end <= edit_source_start:
        raise RuntimeError(
            f"{spec.slug}: invalid source range "
            f"{edit_source_start:.2f}-{edit_source_end:.2f}"
        )

    segments = build_edit_segments(
        traces,
        edit_source_end,
        source_start=edit_source_start,
    )
    if spec.terminal_hold_sec > 0:
        freeze_source_duration = min(0.1, edit_source_end - edit_source_start)
        segments.append(
            EditSegment(
                source_start=edit_source_end - freeze_source_duration,
                source_end=edit_source_end,
                speed=freeze_source_duration / spec.terminal_hold_sec,
                step_index=traces[-1].step_index,
                phase="terminal_observation",
                overlay_payload=terminal_overlay_payload(traces[-1]),
            )
        )

    run_output_dir = output_dir / spec.slug
    run_output_dir.mkdir(parents=True, exist_ok=True)
    final_video = output_dir / spec.output_name

    overlay_payloads: dict[str, Any] = {}
    edit_map: list[dict[str, Any]] = []
    output_cursor = 0.0
    for index, segment in enumerate(segments):
        overlay_payloads[str(index)] = segment.overlay_payload
        edit_map.append(
            {
                "index": index,
                "step": segment.step_index,
                "phase": segment.phase,
                "source_start_sec": round(segment.source_start, 3),
                "source_end_sec": round(segment.source_end, 3),
                "speed": round(segment.speed, 3),
                "output_start_sec": round(output_cursor, 3),
                "output_end_sec": round(output_cursor + segment.output_duration, 3),
            }
        )
        output_cursor += segment.output_duration

    (run_output_dir / "overlay_payloads.json").write_text(
        json.dumps(overlay_payloads, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_output_dir / "edit_map.json").write_text(
        json.dumps(edit_map, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with tempfile.TemporaryDirectory(prefix=f"{spec.slug}_showcase_") as temp_name:
        temp_dir = Path(temp_name)
        concat_lines: list[str] = []
        for index, segment in enumerate(segments):
            card_path = temp_dir / f"card_{index:03d}.png"
            segment_path = temp_dir / f"segment_{index:03d}.mp4"
            render_overlay(
                segment.overlay_payload,
                width=width,
                height=height,
                output_path=card_path,
            )
            render_segment(
                ffmpeg=ffmpeg,
                source_video=source_video,
                card_path=card_path,
                segment=segment,
                source_crop=spec.source_crop,
                output_width=width,
                output_height=height,
                output_path=segment_path,
            )
            concat_lines.append(f"file '{segment_path.as_posix()}'")

        concat_path = temp_dir / "concat.txt"
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        run_command(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                str(final_video),
            ]
        )

    output_probe = probe_video(ffprobe, final_video)
    actual_duration = float(output_probe["format"]["duration"])
    if abs(actual_duration - output_cursor) > 1.5:
        raise RuntimeError(
            f"{spec.slug}: expected {output_cursor:.2f}s, got {actual_duration:.2f}s"
        )

    preview_path = run_output_dir / "preview.jpg"
    run_command(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{min(actual_duration * 0.55, max(0.0, actual_duration - 1)):.3f}",
            "-i",
            str(final_video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(preview_path),
        ]
    )

    summary = {
        "slug": spec.slug,
        "source_video": str(source_video),
        "output_video": str(final_video),
        "source_duration_sec": source_duration,
        "edit_source_start_sec": edit_source_start,
        "edit_source_end_sec": edit_source_end,
        "output_duration_sec": actual_duration,
        "segment_count": len(segments),
        "step_count": len([trace for trace in traces if trace.action is not None]),
        "step_start": spec.step_start,
        "step_end": spec.step_end,
        "source_crop": spec.source_crop,
        "output_probe": output_probe,
        "preview": str(preview_path),
    }
    (run_output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_specs = [
        spec for spec in RUN_SPECS if args.only is None or spec.slug == args.only
    ]
    summaries = []
    for spec in selected_specs:
        print(json.dumps({"status": "started", "run": spec.slug}), flush=True)
        summary = render_run(
            spec=spec,
            output_dir=output_dir,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
        )
        summaries.append(summary)
        print(json.dumps({"status": "completed", **summary}), flush=True)
    summary_path = output_dir / "summary.json"
    if args.only and summary_path.is_file():
        existing = json.loads(summary_path.read_text(encoding="utf-8"))
        existing = existing if isinstance(existing, list) else []
        refreshed_slugs = {summary["slug"] for summary in summaries}
        summaries = [
            summary
            for summary in existing
            if isinstance(summary, dict) and summary.get("slug") not in refreshed_slugs
        ] + summaries
        summaries.sort(key=lambda item: item.get("slug", ""))
    summary_path.write_text(
        json.dumps(summaries, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
