from __future__ import annotations

from copy import deepcopy
from typing import Any

from mc_agent_harness.harness.tool_registry import (
    ACTION_PRIMITIVE_GUIDE,
    DEFAULT_HARNESS_ACTIONS,
    PROMPT_HIDDEN_ACTIONS,
)


SYSTEM_PROMPT_KIND = "system_prompt"
SYSTEM_PROMPT_KEY = "system"
ACTION_PROMPT_KIND = "action"
RUNTIME_SETTING_KIND = "runtime_setting"
HOT_RELOAD_KEY = "hot_reload"

DEFAULT_SYSTEM_PROMPT = """You are a Minecraft task-solving agent running inside a harness.

Role:
You solve Minecraft tasks by choosing one audited harness action at a time.

Behavior rules:
Use only the validated harness actions listed in the current action contract.
Do not write raw Mineflayer JavaScript, MineDojo Python, shell commands, or free-form code.
Do not invent blocks, items, entities, positions, recipes, inventory contents, or skill results.
Prefer broad but cheap exploration when the target is unclear: scan, inspect inventory, or request a visual snapshot if allowed.
Use the knowledge tools when Minecraft terms, recipes, task semantics, or Mineflayer behavior are unclear.
When a recoverable failure may depend on an unknown Minecraft mechanic, query relevant knowledge before repeating the unchanged action.
Prefer a retrieved promoted skill only when its triggers and preconditions match the current task.
When a visible memory source contains a durable entity-specific fact that rules out a target or prevents repeating a failed strategy, you MUST include a source-grounded memory_update in the same response; memory_update=[] is invalid for that turn. A read-only action selected in the same response may also be the source: use the current step index and the selected action type, and the harness will resolve it after the action result is retained.
When concrete evidence indicates the goal is satisfied, use submit_for_evaluation if it is available; the evaluator, not you, decides success.
Return exactly one JSON object matching this shape:
{"reasoning_summary":"short auditable reason for this action, not private chain-of-thought","evidence":["specific observation, previous action result, skill, or knowledge evidence used"],"knowledge_need":{"needed":false,"query":null,"reason":null},"memory_update":[],"action":{"type":"query_inventory","args":{}}}
"""

DEFAULT_RECOMMENDED_NEXT_ACTIONS: dict[str, list[str]] = {
    "follow": [
        "use_item: Use when the task requires using the held item on the followed entity.",
        ("move_to_and_engage_combat: Use when the task requires attacking the followed entity."),
    ],
}

IMPLEMENTED_ACTIONS: tuple[str, ...] = tuple(DEFAULT_HARNESS_ACTIONS)
HARD_HIDDEN_ACTIONS: frozenset[str] = frozenset(PROMPT_HIDDEN_ACTIONS)


def default_system_payload() -> dict[str, Any]:
    """Return a fresh default system-prompt payload."""

    return {"content": DEFAULT_SYSTEM_PROMPT}


def default_hot_reload_payload() -> dict[str, Any]:
    """Return the default prompt hot-reload policy."""

    return {"enabled": True}


def default_action_payload(action_type: str) -> dict[str, Any]:
    """Return one fresh prompt-facing action configuration."""

    if action_type not in IMPLEMENTED_ACTIONS:
        raise KeyError(action_type)
    guide = deepcopy(ACTION_PRIMITIVE_GUIDE.get(action_type, {}))
    return {
        "purpose": str(guide.get("purpose") or ""),
        "args": guide.get("args") if isinstance(guide.get("args"), dict) else {},
        "returns": str(guide.get("returns") or ""),
        "when_to_use": str(guide.get("when_to_use") or ""),
        "recommended_next_actions": list(DEFAULT_RECOMMENDED_NEXT_ACTIONS.get(action_type, [])),
        "prompt_visible": action_type not in PROMPT_HIDDEN_ACTIONS,
    }


def default_action_payloads() -> dict[str, dict[str, Any]]:
    """Return fresh defaults for every executable harness action."""

    return {action_type: default_action_payload(action_type) for action_type in IMPLEMENTED_ACTIONS}
