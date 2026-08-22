from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from mc_agent_harness.harness.evaluation import EvaluationRecorder
from mc_agent_harness.harness.tool_registry import ToolRegistry
from mc_agent_harness.models.router import (
    ModelActionResult,
    ModelRouter,
    ModelRouterError,
    ModelRouterTimeout,
    ModelUsage,
)
from mc_agent_harness.schemas.action import ActionDecision, HarnessAction, KnowledgeNeed


@dataclass(frozen=True, slots=True)
class ActionRepairConfig:
    """Controls how the harness repairs or safely degrades malformed model actions."""

    max_attempts: int = 1
    model_timeout_retries: int = 2
    model_timeout_backoff_sec: tuple[float, ...] = (2.0, 5.0)
    fallback_actions: tuple[HarnessAction, ...] = field(
        default_factory=lambda: (
            HarnessAction(type="query_inventory", args={}),
            HarnessAction(type="request_visual_snapshot", args={}),
        )
    )


class ActionRepairFailed(RuntimeError):
    """Raised when the harness cannot repair or safely replace a model action."""


class ActionGenerationTimeout(RuntimeError):
    """Raised when model timeout retries are exhausted before an action exists."""

    def __init__(self, message: str, *, step_index: int, attempts: int) -> None:
        super().__init__(message)
        self.step_index = step_index
        self.attempts = attempts


class ActionRepairPolicy:
    """Repairs malformed model output before it can reach the runtime boundary."""

    def __init__(self, config: ActionRepairConfig | None = None) -> None:
        self.config = config or ActionRepairConfig()

    async def generate_valid_action(
        self,
        model_router: ModelRouter,
        messages: list[dict[str, Any]],
        registry: ToolRegistry,
        recorder: EvaluationRecorder,
        run_id: str,
        step_index: int,
        prompt_visible_actions: list[str] | None = None,
    ) -> ModelActionResult:
        """Generate, repair, or safely replace one action within the active tool scope."""

        current_messages = messages
        repair_allowed_actions: list[str] = (
            [str(action_type) for action_type in prompt_visible_actions]
            if prompt_visible_actions is not None
            else [str(action_type) for action_type in registry.prompt_visible_actions]
        )
        last_error: str | None = None
        last_raw_content: str | None = None

        for attempt_index in range(self.config.max_attempts + 1):
            if attempt_index > 0:
                await recorder.record(
                    run_id,
                    "model_repair_attempt",
                    {
                        "step_index": step_index,
                        "attempt_index": attempt_index,
                        "previous_error": last_error,
                        "allowed_actions": repair_allowed_actions,
                    },
                )

            try:
                model_result = await self._generate_with_timeout_recovery(
                    model_router=model_router,
                    messages=current_messages,
                    recorder=recorder,
                    run_id=run_id,
                    step_index=step_index,
                    repair_attempt_index=attempt_index,
                )
            except ModelRouterError as exc:
                last_error = str(exc)
                last_raw_content = exc.raw_content
                await recorder.record(
                    run_id,
                    "model_error" if attempt_index == 0 else "model_repair_failed",
                    {
                        "step_index": step_index,
                        "attempt_index": attempt_index,
                        "error": str(exc),
                        "raw_content": exc.raw_content,
                        "usage": asdict(exc.usage),
                        "raw_response": exc.raw_response,
                    },
                )
                current_messages = self._repair_messages(
                    messages,
                    raw_content=exc.raw_content,
                    error=str(exc),
                    allowed_actions=repair_allowed_actions,
                )
                continue

            try:
                registry.validate(model_result.action)
                if model_result.action.type not in repair_allowed_actions:
                    raise ValueError(
                        "Action is not exposed by this turn's prompt configuration: "
                        f"{model_result.action.type}"
                    )
            except ValueError as exc:
                last_error = str(exc)
                last_raw_content = model_result.raw_content
                await recorder.record(
                    run_id,
                    "invalid_action" if attempt_index == 0 else "model_repair_failed",
                    {
                        "step_index": step_index,
                        "attempt_index": attempt_index,
                        "error": str(exc),
                        "raw_content": model_result.raw_content,
                        "action": model_result.action.model_dump(),
                        "decision": _decision_payload(model_result),
                        "usage": asdict(model_result.usage),
                        "raw_response": model_result.raw_response,
                    },
                )
                current_messages = self._repair_messages(
                    messages,
                    raw_content=model_result.raw_content,
                    error=str(exc),
                    allowed_actions=repair_allowed_actions,
                )
                continue

            await self._record_valid_model_action(
                recorder,
                run_id,
                step_index,
                model_result,
                attempt_index,
            )
            return model_result

        fallback = await self._fallback_action(
            recorder,
            run_id,
            step_index,
            registry,
            allowed_actions=repair_allowed_actions,
            last_error=last_error,
            last_raw_content=last_raw_content,
        )
        if fallback is not None:
            return fallback

        raise ActionRepairFailed(
            f"Model action repair failed after {self.config.max_attempts} attempts: {last_error}"
        )

    async def _generate_with_timeout_recovery(
        self,
        *,
        model_router: ModelRouter,
        messages: list[dict[str, Any]],
        recorder: EvaluationRecorder,
        run_id: str,
        step_index: int,
        repair_attempt_index: int,
    ) -> ModelActionResult:
        """Retry provider timeouts without advancing the agent step."""

        max_timeouts = max(0, self.config.model_timeout_retries)
        for timeout_attempt_index in range(max_timeouts + 1):
            try:
                return await model_router.generate_action(messages)
            except ModelRouterTimeout as exc:
                will_retry = timeout_attempt_index < max_timeouts
                await recorder.record(
                    run_id,
                    "model_timeout",
                    {
                        "step_index": step_index,
                        "repair_attempt_index": repair_attempt_index,
                        "timeout_attempt_index": timeout_attempt_index,
                        "will_retry": will_retry,
                        "error": str(exc),
                        "raw_response": exc.raw_response,
                    },
                )
                if not will_retry:
                    await recorder.record(
                        run_id,
                        "model_timeout_exhausted",
                        {
                            "step_index": step_index,
                            "repair_attempt_index": repair_attempt_index,
                            "attempts": timeout_attempt_index + 1,
                            "last_error": str(exc),
                            "raw_response": exc.raw_response,
                        },
                    )
                    raise ActionGenerationTimeout(
                        f"Model action generation timed out after {timeout_attempt_index + 1} attempts.",
                        step_index=step_index,
                        attempts=timeout_attempt_index + 1,
                    ) from exc
                backoff_sec = self._timeout_backoff(timeout_attempt_index)
                await recorder.record(
                    run_id,
                    "model_timeout_retry",
                    {
                        "step_index": step_index,
                        "repair_attempt_index": repair_attempt_index,
                        "timeout_attempt_index": timeout_attempt_index,
                        "next_attempt_index": timeout_attempt_index + 1,
                        "backoff_sec": backoff_sec,
                    },
                )
                if backoff_sec > 0:
                    await asyncio.sleep(backoff_sec)

        raise AssertionError("unreachable timeout retry state")

    def _timeout_backoff(self, timeout_attempt_index: int) -> float:
        """Return the configured backoff before the next model timeout retry."""

        if not self.config.model_timeout_backoff_sec:
            return 0.0
        index = min(timeout_attempt_index, len(self.config.model_timeout_backoff_sec) - 1)
        return max(0.0, float(self.config.model_timeout_backoff_sec[index]))

    def _repair_messages(
        self,
        messages: list[dict[str, Any]],
        raw_content: str | None,
        error: str,
        allowed_actions: list[str],
    ) -> list[dict[str, Any]]:
        """Build a constrained repair prompt from the original context and validation error."""

        repair_payload = {
            "repair_request": {
                "error": error,
                "bad_output": raw_content,
                "allowed_actions": allowed_actions,
                "required_shape": {
                    "reasoning_summary": "short auditable reason",
                    "evidence": ["prompt evidence used"],
                    "knowledge_need": {"needed": False, "query": None, "reason": None},
                    "memory_update": [],
                    "action": {"type": "one_allowed_action", "args": {}},
                },
                "rules": [
                    "Return exactly one JSON object.",
                    "Do not include markdown fences or explanatory text.",
                    "Do not include private chain-of-thought; use a concise reasoning_summary only.",
                    "Choose only one action from allowed_actions.",
                    "If no progress action is safe, choose query_inventory when it is allowed.",
                ],
            }
        }
        return [
            *messages,
            {
                "role": "user",
                "content": json.dumps(repair_payload, ensure_ascii=True, sort_keys=True),
            },
        ]

    async def _record_valid_model_action(
        self,
        recorder: EvaluationRecorder,
        run_id: str,
        step_index: int,
        model_result: ModelActionResult,
        attempt_index: int,
    ) -> None:
        """Record a valid action and any successful repair metadata."""

        if attempt_index > 0:
            await recorder.record(
                run_id,
                "model_repair_success",
                {
                    "step_index": step_index,
                    "attempt_index": attempt_index,
                    "raw_content": model_result.raw_content,
                    "action": model_result.action.model_dump(),
                    "decision": _decision_payload(model_result),
                    "usage": asdict(model_result.usage),
                    "raw_response": model_result.raw_response,
                },
            )

        await recorder.record(
            run_id,
            "model_action",
            {
                "step_index": step_index,
                "raw_content": model_result.raw_content,
                "action": model_result.action.model_dump(),
                "decision": _decision_payload(model_result),
                "usage": asdict(model_result.usage),
                "raw_response": model_result.raw_response,
                "repair_attempts": attempt_index,
                "source": "model",
            },
        )

    async def _fallback_action(
        self,
        recorder: EvaluationRecorder,
        run_id: str,
        step_index: int,
        registry: ToolRegistry,
        allowed_actions: list[str],
        last_error: str | None,
        last_raw_content: str | None,
    ) -> ModelActionResult | None:
        """Return the first configured safe fallback action allowed by the current scope."""

        for action in self.config.fallback_actions:
            if action.type not in allowed_actions:
                continue
            try:
                registry.validate(action)
            except ValueError:
                continue

            result = ModelActionResult(
                action=action,
                raw_content=json.dumps(
                    {
                        "reasoning_summary": "Harness selected a safe fallback action after model repair failed.",
                        "evidence": ["model output could not be validated within the repair budget"],
                        "knowledge_need": {"needed": False, "query": None, "reason": None},
                        "memory_update": [],
                        "action": action.model_dump(),
                    },
                    sort_keys=True,
                ),
                usage=ModelUsage(),
                raw_response={"source": "harness_fallback"},
                decision=ActionDecision(
                    reasoning_summary="Harness selected a safe fallback action after model repair failed.",
                    evidence=["model output could not be validated within the repair budget"],
                    knowledge_need=KnowledgeNeed(needed=False),
                    action=action,
                ),
            )
            await recorder.record(
                run_id,
                "model_fallback_action",
                {
                    "step_index": step_index,
                    "action": action.model_dump(),
                    "last_error": last_error,
                    "last_raw_content": last_raw_content,
                },
            )
            await recorder.record(
                run_id,
                "model_action",
                {
                    "step_index": step_index,
                    "raw_content": result.raw_content,
                    "action": result.action.model_dump(),
                    "decision": _decision_payload(result),
                    "usage": asdict(result.usage),
                    "raw_response": result.raw_response,
                    "repair_attempts": self.config.max_attempts,
                    "source": "harness_fallback",
                },
            )
            return result

        await recorder.record(
            run_id,
            "model_repair_exhausted",
            {
                "step_index": step_index,
                "last_error": last_error,
                "last_raw_content": last_raw_content,
                "allowed_actions": allowed_actions,
            },
        )
        return None


def _decision_payload(model_result: ModelActionResult) -> dict[str, Any]:
    """Return a JSON-safe decision envelope for audit events."""

    if model_result.decision is None:
        return {
            "reasoning_summary": "",
            "evidence": [],
            "knowledge_need": {"needed": False, "query": None, "reason": None},
            "memory_update": [],
            "action": model_result.action.model_dump(mode="json"),
        }
    return model_result.decision.model_dump(mode="json")
