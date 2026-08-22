from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast, get_args

from mc_agent_harness.harness.action_repair import ActionRepairPolicy
from mc_agent_harness.harness.context_manager import ContextManager
from mc_agent_harness.harness.context_memory import RunContextMemory
from mc_agent_harness.harness.evaluation import EvaluationRecorder
from mc_agent_harness.harness.lifecycle import LifecycleHooks
from mc_agent_harness.harness.planner import AgentPlanner, AgentPlanResult, TaskPlan
from mc_agent_harness.harness.state_store import StateStore
from mc_agent_harness.harness.tool_registry import DEFAULT_HARNESS_ACTIONS, ToolRegistry
from mc_agent_harness.knowledge import KNOWLEDGE_ACTION_TYPES, KnowledgeToolDispatcher
from mc_agent_harness.models.router import ModelRouter, ModelUsage
from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.schemas.action import ActionDecision, ActionType, HarnessAction


VALID_ACTION_TYPES = set(get_args(ActionType))
READ_ONLY_OR_LOW_RISK_ACTIONS = {
    "resolve_terms",
    "get_recipe",
    "retrieve_docs",
    "query_inventory",
    "request_visual_snapshot",
    "scan_blocks",
    "scan_dropped_items",
    "wait_ticks",
}


@dataclass(slots=True)
class ExecutionBudget:
    """Resource limits for one agent run."""

    max_steps: int = 200
    checkpoint_interval_steps: int = 5
    follow_handoff_max_wait_sec: float = 2.0
    follow_handoff_poll_interval_sec: float = 0.1
    follow_handoff_ready_margin: float = 0.5


@dataclass(frozen=True, slots=True)
class ExecutionStepResult:
    """Result of one observe-context-model-action cycle."""

    step_index: int
    observation: dict[str, Any]
    action: HarnessAction
    action_result: dict[str, Any]


SuccessChecker = Callable[[dict[str, Any], list[ExecutionStepResult]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ExecutionRunResult:
    """Summary returned after one agent run completes or stops early."""

    run_id: str
    task_id: str
    steps: list[ExecutionStepResult]
    terminated: bool
    stop_reason: str


@dataclass(frozen=True, slots=True)
class SubmissionEvaluation:
    """Harness decision for one model-requested transition from acting to evaluation."""

    action_result: dict[str, Any]
    verifier: dict[str, Any]
    accepted: bool
    stop_reason: str | None


class ExecutionLoop:
    """Owns observe-think-act-verify-reflect orchestration."""

    def __init__(
        self,
        runtime: GameRuntime,
        model_router: ModelRouter,
        context_manager: ContextManager | None = None,
        tool_registry: ToolRegistry | None = None,
        recorder: EvaluationRecorder | None = None,
        lifecycle_hooks: LifecycleHooks | None = None,
        action_repair_policy: ActionRepairPolicy | None = None,
        state_store: StateStore | None = None,
        knowledge_tool_dispatcher: KnowledgeToolDispatcher | None = None,
        planner: AgentPlanner | None = None,
        budget: ExecutionBudget | None = None,
        success_checker: SuccessChecker | None = None,
    ) -> None:
        self.runtime = runtime
        self.model_router = model_router
        self.context_manager = context_manager or ContextManager()
        self.tool_registry = tool_registry or ToolRegistry(DEFAULT_HARNESS_ACTIONS)
        self.recorder = recorder or EvaluationRecorder()
        self.lifecycle_hooks = lifecycle_hooks or LifecycleHooks()
        self.action_repair_policy = action_repair_policy or ActionRepairPolicy()
        self.state_store = state_store
        self.knowledge_tool_dispatcher = knowledge_tool_dispatcher or KnowledgeToolDispatcher(
            self.context_manager.knowledge_provider
        )
        self.planner = planner
        self.budget = budget or ExecutionBudget()
        self.success_checker = success_checker

    async def run(
        self,
        task_id: str,
        task_spec: dict[str, Any] | None = None,
        task_memory: list[str] | None = None,
    ) -> ExecutionRunResult:
        """Run observe-context-model-action cycles until termination or budget exhaustion."""

        spec = {"task_id": task_id, **(task_spec or {})}
        run_id = str(spec.get("run_id") or uuid.uuid4())
        memory = list(task_memory or [])
        registry = self._registry_for_task(spec)
        manifest_allowed_actions = spec.get("allowed_actions")
        canonical_allowed_actions = list(registry.enabled_actions)
        if (
            manifest_allowed_actions is not None
            and manifest_allowed_actions != canonical_allowed_actions
        ):
            spec["manifest_allowed_actions"] = manifest_allowed_actions
        spec["allowed_actions"] = canonical_allowed_actions
        steps: list[ExecutionStepResult] = []
        terminated = False
        start_step_index = 0
        previous_step: dict[str, Any] | None = None
        run_context = RunContextMemory()
        task_plan: TaskPlan | None = None
        plan_revision_count = 0
        stop_reason: str | None = None
        seen_progress_feedback_jobs: set[str] = set()

        if spec.get("resume") and self.state_store is not None:
            checkpoint = await self.state_store.load_checkpoint(run_id)
            if checkpoint is not None:
                start_step_index = int(checkpoint.get("next_step_index", 0))
                memory = list(checkpoint.get("task_memory", memory))
                previous_step = _checkpoint_previous_step(checkpoint)
                run_context = RunContextMemory.from_json(checkpoint.get("run_context"))

        try:
            reset_result = await self.runtime.reset(spec)
        except Exception as exc:
            await self._record_runtime_error(run_id, phase="reset", error=exc)
            raise
        await self.recorder.record(
            run_id,
            "run_started",
            {
                "task_id": task_id,
                "task_spec": spec,
                "allowed_actions": canonical_allowed_actions,
                "resume": bool(spec.get("resume")),
                "start_step_index": start_step_index,
                "reset_result": reset_result,
            },
        )
        if isinstance(reset_result, dict) and reset_result.get("reset_policy"):
            await self.recorder.record(
                run_id,
                "environment_reset",
                {
                    "task_id": task_id,
                    "reset_policy": reset_result.get("reset_policy"),
                    "runtime": reset_result,
                },
            )
        if start_step_index:
            await self.recorder.record(
                run_id,
                "checkpoint_loaded",
                {"step_index": start_step_index, "next_step_index": start_step_index},
            )
        start_delay_sec = float(spec.get("start_delay_sec") or 0)
        if start_delay_sec > 0:
            await self.recorder.record(
                run_id,
                "start_delay",
                {"duration_sec": start_delay_sec, "reason": "manual_live_training_setup"},
            )
            await asyncio.sleep(start_delay_sec)

        if bool(spec.get("initial_visual_snapshot")) and start_step_index == 0:
            previous_step = await self._capture_initial_visual_snapshot(
                run_id=run_id,
                registry=registry,
            )

        for step_index in range(start_step_index, self.budget.max_steps):
            try:
                observation = await self.runtime.observe()
            except Exception as exc:
                await self._record_runtime_error(
                    run_id,
                    phase="observe",
                    error=exc,
                    step_index=step_index,
                )
                raise
            await self.recorder.record(
                run_id,
                "observation",
                {"step_index": step_index, "observation": observation},
            )
            progress_feedback = _new_progress_feedback(
                observation,
                seen_progress_feedback_jobs,
            )
            if progress_feedback is not None:
                await self.recorder.record(
                    run_id,
                    "mineclip_progress_feedback",
                    {"step_index": step_index, **progress_feedback},
                )
            if observation.get("terminated"):
                terminated = True
                stop_reason = "runtime_terminated"
                break
            if "_initial_inventory" not in spec and isinstance(observation.get("inventory"), list):
                spec["_initial_inventory"] = [
                    dict(item) for item in observation["inventory"] if isinstance(item, dict)
                ]
            if self.planner is not None and task_plan is None:
                plan_result = await self.planner.create_plan(
                    model_router=self.model_router,
                    task_spec=spec,
                    observation=observation,
                    task_memory=memory,
                    allowed_actions=list(registry.prompt_visible_actions),
                )
                task_plan = plan_result.plan
                await self._record_plan_result(
                    run_id=run_id,
                    task_id=task_id,
                    step_index=step_index,
                    event_type="agent_plan_created",
                    plan_result=plan_result,
                )

            context = await self.context_manager.build(
                observation=observation,
                task_memory=memory,
                task_spec=spec,
                allowed_actions=list(registry.enabled_actions),
                previous_step=previous_step,
                task_plan=task_plan.to_json() if task_plan is not None else None,
                run_context=run_context,
                step_index=step_index,
            )
            await self.recorder.record(
                run_id,
                "context_built",
                {
                    "step_index": step_index,
                    "resolved_terms": [term.canonical_id for term in context.resolved_terms],
                    "retrieved_docs": [document.id for document in context.retrieved_docs],
                    "retrieved_skills": [
                        {"name": skill.name, "version": skill.version}
                        for skill in context.retrieved_skills
                    ],
                    "skill_injection": context.prompt_sections.get("skill_injection", {}),
                    "retrieved_learning_candidates": [
                        {
                            "signature": candidate.signature,
                            "status": candidate.status.value,
                            "confidence": candidate.confidence,
                        }
                        for candidate in context.retrieved_learning_candidates
                    ],
                    "prompt_sections": context.prompt_sections,
                    "messages": context.audit_messages,
                },
            )

            model_result = await self.action_repair_policy.generate_valid_action(
                model_router=self.model_router,
                messages=context.messages,
                registry=registry,
                recorder=self.recorder,
                run_id=run_id,
                step_index=step_index,
                prompt_visible_actions=context.prompt_visible_actions,
            )

            memory_updates = (
                model_result.decision.memory_update
                if model_result.decision is not None
                else []
            )

            action = await self.lifecycle_hooks.before_action(model_result.action)
            submission: SubmissionEvaluation | None = None
            if action.type in KNOWLEDGE_ACTION_TYPES:
                knowledge_revision = self.knowledge_tool_dispatcher.knowledge_revision()
                action_result = run_context.knowledge.cached_result(
                    action,
                    step_index=step_index,
                    knowledge_revision=knowledge_revision,
                )
                if action_result is None:
                    action_result = await self.knowledge_tool_dispatcher.dispatch(
                        action,
                        knowledge_revision=knowledge_revision,
                    )
                run_context.knowledge.record(
                    step_index=step_index,
                    action=action,
                    result=action_result,
                    observation=observation,
                    task_spec=spec,
                    knowledge_revision=knowledge_revision,
                )
                await self.recorder.record(
                    run_id,
                    "knowledge_tool_call",
                    {
                        "step_index": step_index,
                        "action": action.model_dump(mode="json"),
                        "result": _knowledge_audit_payload(action_result),
                    },
                )
            elif action.type == "submit_for_evaluation":
                await self.recorder.record(
                    run_id,
                    "agent_finish_requested",
                    {
                        "step_index": step_index,
                        "task_id": task_id,
                        "action": action.model_dump(mode="json"),
                        "decision": _finish_decision_payload(model_result.decision),
                    },
                )
                submission = await self._evaluate_finish_submission(
                    run_id=run_id,
                    step_index=step_index,
                    task_spec=spec,
                    observation=observation,
                    action=action,
                    completed_steps=steps,
                )
                action_result = submission.action_result
            else:
                follow_handoff = await self._wait_for_follow_handoff(
                    action=action,
                    decision_observation=observation,
                )
                if follow_handoff is not None:
                    await self.recorder.record(
                        run_id,
                        "follow_handoff_wait",
                        {
                            "step_index": step_index,
                            "action": action.model_dump(mode="json"),
                            **follow_handoff,
                        },
                    )
                try:
                    action_result = await self.runtime.act(action)
                except TimeoutError as exc:
                    action_result = await self._build_action_timeout_result(
                        run_id=run_id,
                        step_index=step_index,
                        action=action,
                        error=exc,
                    )
                except Exception as exc:
                    await self._record_runtime_error(
                        run_id,
                        phase="act",
                        error=exc,
                        step_index=step_index,
                        payload={"action": action.model_dump()},
                    )
                    raise
            action_result = _apply_configured_action_recommendations(
                action=action,
                action_result=action_result,
                recommendations=context.action_recommendations,
                prompt_configuration=context.prompt_sections.get(
                    "prompt_configuration",
                    {},
                ),
            )
            await self.lifecycle_hooks.after_action(action, action_result)
            await self.recorder.record(
                run_id,
                "action_result",
                {
                    "step_index": step_index,
                    "action": action.model_dump(),
                    "result": action_result,
                },
            )
            progress_job = action_result.get("creative_progress_job")
            if isinstance(progress_job, dict):
                await self.recorder.record(
                    run_id,
                    "mineclip_progress_requested",
                    {
                        "step_index": step_index,
                        "action": action.model_dump(mode="json"),
                        "job": progress_job,
                    },
                )
            action_rpc_timeout = action_result.get("error_code") == "rpc_timeout"
            if action_rpc_timeout:
                await self.recorder.record(
                    run_id,
                    "runtime_action_timeout",
                    {
                        "phase": "act",
                        "step_index": step_index,
                        "action": action.model_dump(mode="json"),
                        "error_type": "ActionRpcTimeout",
                        "message": str(
                            action_result.get("message")
                            or "Mineflayer worker action RPC timed out."
                        ),
                        "action_status": "unknown",
                        "recoverable": bool(action_result.get("recoverable")),
                        "requires_worker_restart": bool(
                            action_result.get("requires_worker_restart")
                        ),
                        "worker_health": action_result.get("worker_health"),
                        "observation": action_result.get("observation"),
                    },
                )
            trace_observation = action_result.get("observation")
            if not isinstance(trace_observation, dict):
                trace_observation = observation
            run_context.memory.record_source(
                step_index=step_index,
                action=action,
                result=action_result,
            )
            if memory_updates:
                # Memory is consumed only by the next model turn, so resolve every
                # update after retaining this step's immutable action result. This
                # also permits a same-response scan action to supply the audited
                # source selected by its accompanying memory_update without an
                # additional model call.
                memory_outcomes = run_context.memory.apply_updates(
                    memory_updates,
                    decision_step_index=step_index,
                )
                await self.recorder.record(
                    run_id,
                    "memory_update",
                    {
                        "step_index": step_index,
                        "requests": [
                            update.model_dump(mode="json")
                            for update in memory_updates
                        ],
                        "outcomes": memory_outcomes,
                    },
                )
            run_context.trajectory.record(
                step_index=step_index,
                action=action,
                result=action_result,
                observation=trace_observation,
                task_spec=spec,
            )
            reachability = _reachability_analysis_payload(spec, step_index, action, action_result)
            if reachability is not None:
                await self.recorder.record(run_id, "reachability_analysis", reachability)

            steps.append(
                ExecutionStepResult(
                    step_index=step_index,
                    observation=observation,
                    action=action,
                    action_result=action_result,
                )
            )
            previous_step = {
                "step_index": step_index,
                "action": action.model_dump(mode="json"),
                "action_result": action_result,
            }
            if (
                self.planner is not None
                and task_plan is not None
                and self.planner.should_revise(
                    action=action,
                    action_result=action_result,
                    revision_count=plan_revision_count,
                )
            ):
                revision_observation = action_result.get("observation")
                if not isinstance(revision_observation, dict):
                    revision_observation = observation
                plan_result = await self.planner.revise_plan(
                    model_router=self.model_router,
                    task_spec=spec,
                    observation=revision_observation,
                    task_memory=memory,
                    allowed_actions=list(registry.prompt_visible_actions),
                    current_plan=task_plan,
                    previous_step=previous_step,
                )
                plan_revision_count += 1
                task_plan = plan_result.plan
                await self._record_plan_result(
                    run_id=run_id,
                    task_id=task_id,
                    step_index=step_index,
                    event_type="agent_plan_revised",
                    plan_result=plan_result,
                )
            await self._maybe_save_checkpoint(
                run_id=run_id,
                task_id=task_id,
                task_spec=spec,
                task_memory=memory,
                step_index=step_index,
                observation=observation,
                action=action,
                action_result=action_result,
                run_context=run_context,
            )
            if action_rpc_timeout:
                terminated = True
                stop_reason = "action_rpc_timeout"
                break
            if action_result.get("terminated"):
                terminated = True
                stop_reason = "action_terminated"
                break
            if submission is not None:
                await self.recorder.record(
                    run_id,
                    "step_verifier_result",
                    {
                        "step_index": step_index,
                        "task_id": task_id,
                        "success": bool(submission.verifier.get("success")),
                        "verifier": submission.verifier,
                        "trigger": "agent_finish_request",
                    },
                )
                await self.recorder.record(
                    run_id,
                    "agent_finish_accepted" if submission.accepted else "agent_finish_rejected",
                    {
                        "step_index": step_index,
                        "task_id": task_id,
                        "accepted": submission.accepted,
                        "stop_reason": submission.stop_reason,
                        "decision": _finish_decision_payload(model_result.decision),
                        "verifier": submission.verifier,
                        "action_result": submission.action_result,
                    },
                )
                if submission.accepted:
                    terminated = True
                    stop_reason = submission.stop_reason
                    break
            elif self.success_checker is not None:
                step_verifier = await self._check_step_success_with_recovery(
                    run_id=run_id,
                    step_index=step_index,
                    task_spec=spec,
                    steps=steps,
                )
                await self.recorder.record(
                    run_id,
                    "step_verifier_result",
                    {
                        "step_index": step_index,
                        "task_id": task_id,
                        "success": bool(step_verifier.get("success")),
                        "verifier": step_verifier,
                    },
                )
                if step_verifier.get("success"):
                    terminated = True
                    stop_reason = "success_checker"
                    break

        if stop_reason is None:
            stop_reason = "max_steps_exhausted"

        await self.recorder.record(
            run_id,
            "run_finished",
            {
                "task_id": task_id,
                "steps": len(steps),
                "terminated": terminated,
                "stop_reason": stop_reason,
            },
        )
        return ExecutionRunResult(
            run_id=run_id,
            task_id=task_id,
            steps=steps,
            terminated=terminated,
            stop_reason=stop_reason,
        )

    async def _wait_for_follow_handoff(
        self,
        *,
        action: HarnessAction,
        decision_observation: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Briefly preserve active follow before dispatching its entity-targeted handoff."""

        active_follow = decision_observation.get("active_follow")
        if not isinstance(active_follow, dict) or active_follow.get("active") is not True:
            return None
        target = active_follow.get("target")
        if not isinstance(target, dict) or not _action_targets_follow(action, target):
            return None
        max_wait_sec = max(0.0, self.budget.follow_handoff_max_wait_sec)
        if max_wait_sec <= 0:
            return None

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        initial_distance = _follow_target_distance(decision_observation, target)
        last_distance = initial_distance
        follow_distance = _positive_float(active_follow.get("follow_distance"), 1.25)
        ready_distance = follow_distance + max(
            0.0,
            self.budget.follow_handoff_ready_margin,
        )
        polls = 0
        status = "timeout"

        while True:
            try:
                latest_observation = await self.runtime.observe()
            except Exception as exc:
                return {
                    "status": "observation_error",
                    "target": _follow_target_payload(target),
                    "initial_distance": initial_distance,
                    "final_distance": last_distance,
                    "ready_distance": ready_distance,
                    "waited_ms": round((loop.time() - started_at) * 1000),
                    "poll_count": polls,
                    "error_type": type(exc).__name__,
                }
            polls += 1
            latest_active = latest_observation.get("active_follow")
            if not isinstance(latest_active, dict) or latest_active.get("active") is not True:
                status = "follow_inactive"
                break
            last_distance = _follow_target_distance(latest_observation, target)
            if last_distance is not None and last_distance <= ready_distance:
                status = "ready"
                break
            elapsed = loop.time() - started_at
            if elapsed >= max_wait_sec:
                break
            await asyncio.sleep(
                min(
                    max(0.01, self.budget.follow_handoff_poll_interval_sec),
                    max_wait_sec - elapsed,
                )
            )

        return {
            "status": status,
            "target": _follow_target_payload(target),
            "initial_distance": initial_distance,
            "final_distance": last_distance,
            "ready_distance": ready_distance,
            "waited_ms": round((loop.time() - started_at) * 1000),
            "poll_count": polls,
        }

    def _registry_for_task(self, task_spec: dict[str, Any]) -> ToolRegistry:
        """Return the run action registry without coupling termination to evaluation."""

        _ = task_spec
        return self.tool_registry

    async def _evaluate_finish_submission(
        self,
        *,
        run_id: str,
        step_index: int,
        task_spec: dict[str, Any],
        observation: dict[str, Any],
        action: HarnessAction,
        completed_steps: list[ExecutionStepResult],
    ) -> SubmissionEvaluation:
        """Resolve a finish request as verified, externally evaluated, or unverified."""

        action_result: dict[str, Any] = {
            "ok": False,
            "action_type": action.type,
            "submission_accepted": False,
            "evaluation_status": "pending",
            "task_success": None,
            "observation": observation,
        }
        candidate_step = ExecutionStepResult(
            step_index=step_index,
            observation=observation,
            action=action,
            action_result=action_result,
        )
        if self.success_checker is not None:
            verifier = await self._check_step_success_with_recovery(
                run_id=run_id,
                step_index=step_index,
                task_spec=task_spec,
                steps=[*completed_steps, candidate_step],
            )
        elif _task_requires_external_evaluation(task_spec):
            verifier = {
                "success": False,
                "inconclusive": True,
                "reason": "Task requires an external evaluator after agent execution.",
                "checks": [],
                "external_evaluator": "configured_by_task",
            }
        else:
            verifier = {
                "success": None,
                "inconclusive": True,
                "not_evaluated": True,
                "reason": (
                    "No authoritative evaluator is configured; the run may stop but task success "
                    "remains unverified."
                ),
                "checks": [],
            }

        if verifier.get("success") is True:
            action_result.update(
                {
                    "ok": True,
                    "submission_accepted": True,
                    "evaluation_status": "verified_success",
                    "task_success": True,
                    "verifier": verifier,
                    "summary": (
                        "Finish request accepted because the online verifier confirmed the task goal."
                    ),
                }
            )
            return SubmissionEvaluation(
                action_result=action_result,
                verifier=verifier,
                accepted=True,
                stop_reason="agent_submitted_verified",
            )

        if verifier.get("inconclusive") is True and _task_requires_external_evaluation(task_spec):
            action_result.update(
                {
                    "ok": True,
                    "submission_accepted": True,
                    "evaluation_status": "external_evaluation_required",
                    "task_success": None,
                    "verifier": verifier,
                    "summary": (
                        "Finish request accepted; the recorded trajectory now requires external "
                        "evaluation."
                    ),
                }
            )
            return SubmissionEvaluation(
                action_result=action_result,
                verifier=verifier,
                accepted=True,
                stop_reason="agent_submitted_for_external_evaluation",
            )

        if verifier.get("not_evaluated") is True:
            action_result.update(
                {
                    "ok": True,
                    "submission_accepted": True,
                    "evaluation_status": "not_evaluated",
                    "task_success": None,
                    "verifier": verifier,
                    "summary": (
                        "Finish request accepted without an evaluator. The run is complete, but "
                        "task success remains unverified."
                    ),
                }
            )
            return SubmissionEvaluation(
                action_result=action_result,
                verifier=verifier,
                accepted=True,
                stop_reason="agent_finished_unverified",
            )

        reason = str(verifier.get("reason") or "The evaluator did not confirm task completion.")
        action_result.update(
            {
                "ok": False,
                "submission_accepted": False,
                "evaluation_status": (
                    "evaluation_inconclusive" if verifier.get("inconclusive") else "rejected"
                ),
                "task_success": False if verifier.get("inconclusive") is not True else None,
                "error_code": (
                    "evaluation_not_ready"
                    if verifier.get("inconclusive") is True
                    else "submission_rejected"
                ),
                "recoverable": True,
                "verifier": verifier,
                "summary": f"Finish request rejected. Continue from verifier evidence: {reason}",
            }
        )
        return SubmissionEvaluation(
            action_result=action_result,
            verifier=verifier,
            accepted=False,
            stop_reason=None,
        )

    async def _capture_initial_visual_snapshot(
        self,
        *,
        run_id: str,
        registry: ToolRegistry,
    ) -> dict[str, Any] | None:
        """Capture an optional creative-task baseline frame before the first model turn."""

        action = HarnessAction(type="request_visual_snapshot", args={})
        try:
            registry.validate(action)
        except ValueError as exc:
            await self.recorder.record(
                run_id,
                "initial_visual_snapshot",
                {
                    "ok": False,
                    "skipped": True,
                    "reason": str(exc),
                    "action": action.model_dump(mode="json"),
                },
            )
            return None
        try:
            action_result = await self.runtime.act(action)
        except Exception as exc:  # noqa: BLE001 - baseline vision is optional and auditable.
            await self.recorder.record(
                run_id,
                "initial_visual_snapshot",
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "action": action.model_dump(mode="json"),
                },
            )
            return None
        await self.recorder.record(
            run_id,
            "initial_visual_snapshot",
            {
                "ok": bool(action_result.get("ok")),
                "action": action.model_dump(mode="json"),
                "result": action_result,
            },
        )
        return {
            "step_index": -1,
            "action": action.model_dump(mode="json"),
            "action_result": action_result,
        }

    async def _maybe_save_checkpoint(
        self,
        run_id: str,
        task_id: str,
        task_spec: dict[str, Any],
        task_memory: list[str],
        step_index: int,
        observation: dict[str, Any],
        action: HarnessAction,
        action_result: dict[str, Any],
        run_context: RunContextMemory,
    ) -> None:
        """Persist a checkpoint when checkpointing is enabled for this step."""

        if self.state_store is None:
            return
        if self.budget.checkpoint_interval_steps <= 0:
            return
        if (step_index + 1) % self.budget.checkpoint_interval_steps != 0:
            return

        state = {
            "run_id": run_id,
            "task_id": task_id,
            "task_spec": task_spec,
            "task_memory": task_memory,
            "step_index": step_index,
            "next_step_index": step_index + 1,
            "last_observation": observation,
            "last_action": action.model_dump(),
            "last_action_result": action_result,
            "run_context": run_context.to_json(),
        }
        await self.state_store.save_checkpoint(run_id, state)
        await self.recorder.record(
            run_id,
            "checkpoint_saved",
            {"step_index": step_index, "next_step_index": step_index + 1},
        )

    async def _record_plan_result(
        self,
        *,
        run_id: str,
        task_id: str,
        step_index: int,
        event_type: str,
        plan_result: AgentPlanResult,
    ) -> None:
        """Persist a planner model call and the resulting contextual TaskPlan."""

        await self.recorder.record(
            run_id,
            "agent_plan_model_call",
            {
                "step_index": step_index,
                "task_id": task_id,
                "raw_content": plan_result.raw_content,
                "action": {"type": event_type, "args": plan_result.plan.to_json()},
                "usage": _usage_payload(plan_result.usage),
                "raw_response": plan_result.raw_response,
                "source": "planner",
            },
        )
        await self.recorder.record(
            run_id,
            event_type,
            {
                "step_index": step_index,
                "task_id": task_id,
                "plan": plan_result.plan.to_json(),
                "fallback_reason": plan_result.fallback_reason,
            },
        )

    async def _build_action_timeout_result(
        self,
        *,
        run_id: str,
        step_index: int,
        action: HarnessAction,
        error: TimeoutError,
    ) -> dict[str, Any]:
        """Convert an unhandled action timeout into an auditable unknown result."""

        post_timeout_observation: dict[str, Any] | None = None
        observe_error: dict[str, Any] | None = None
        try:
            post_timeout_observation = await self.runtime.observe()
        except Exception as exc:  # noqa: BLE001 - preserve original timeout and audit observe failure.
            observe_error = {"error_type": type(exc).__name__, "message": str(exc)}

        recoverable = action.type in READ_ONLY_OR_LOW_RISK_ACTIONS
        payload = {
            "phase": "act",
            "step_index": step_index,
            "action": action.model_dump(mode="json"),
            "error_type": type(error).__name__,
            "message": str(error),
            "action_status": "unknown",
            "recoverable": recoverable,
            "observation": post_timeout_observation,
            "observe_error": observe_error,
        }
        await self.recorder.record(run_id, "runtime_action_timeout", payload)
        return {
            "ok": False,
            "action_type": action.type,
            "error_code": "action_timeout",
            "message": str(error) or "Runtime action timed out before returning a result.",
            "recoverable": recoverable,
            "action_status": "unknown",
            "observation": post_timeout_observation or {},
            "observe_error": observe_error,
        }

    async def _check_step_success_with_recovery(
        self,
        *,
        run_id: str,
        step_index: int,
        task_spec: dict[str, Any],
        steps: list[ExecutionStepResult],
    ) -> dict[str, Any]:
        """Run the step verifier with one timeout retry and auditable fallback."""

        assert self.success_checker is not None
        attempts = 2
        last_error: TimeoutError | None = None
        for attempt_index in range(attempts):
            try:
                return await self.success_checker(task_spec, steps)
            except TimeoutError as exc:
                last_error = exc
                will_retry = attempt_index < attempts - 1
                await self.recorder.record(
                    run_id,
                    "verifier_timeout",
                    {
                        "step_index": step_index,
                        "attempt_index": attempt_index,
                        "will_retry": will_retry,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                if will_retry:
                    await asyncio.sleep(0)

        message = str(last_error) if last_error is not None else "Verifier timed out."
        await self.recorder.record(
            run_id,
            "verifier_timeout_exhausted",
            {
                "step_index": step_index,
                "attempts": attempts,
                "message": message,
            },
        )
        return {
            "success": False,
            "inconclusive": True,
            "reason": f"Verifier timeout after {attempts} attempts: {message}",
            "checks": [],
        }

    async def _record_runtime_error(
        self,
        run_id: str,
        phase: str,
        error: Exception,
        step_index: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record a runtime exception before propagating it to the caller."""

        await self.recorder.record(
            run_id,
            "runtime_error",
            {
                "phase": phase,
                "step_index": step_index,
                "error_type": type(error).__name__,
                "message": str(error),
                **(payload or {}),
            },
        )


_FOLLOW_HANDOFF_ACTION_TYPES = frozenset(
    {
        "use_item",
        "move_to_and_engage_combat",
        "engage_combat",
        "fight_entity",
    }
)


def _action_targets_follow(
    action: HarnessAction,
    target: dict[str, Any],
) -> bool:
    """Return whether an entity-targeted action addresses the active follow target."""

    if action.type not in _FOLLOW_HANDOFF_ACTION_TYPES:
        return False
    target_id = target.get("entity_id")
    if target_id is None:
        target_id = target.get("id")
    action_entity_id = action.args.get("entity_id")
    if action_entity_id is not None:
        return str(action_entity_id) == str(target_id)
    action_target = action.args.get("entity") or action.args.get("name")
    if action_target is None:
        return False
    return str(action_target) in {
        str(value)
        for value in (target_id, target.get("name"), target.get("type"))
        if value is not None
    }


def _follow_target_distance(
    observation: dict[str, Any],
    target: dict[str, Any],
) -> float | None:
    """Read the current distance to one followed entity from a fresh observation."""

    target_id = target.get("entity_id")
    if target_id is None:
        target_id = target.get("id")
    target_name = target.get("name")
    entities = observation.get("nearby_entities")
    if not isinstance(entities, list):
        return None
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_id = entity.get("entity_id")
        if entity_id is None:
            entity_id = entity.get("id")
        if target_id is not None:
            matches = str(entity_id) == str(target_id)
        else:
            matches = target_name is not None and entity.get("name") == target_name
        if not matches:
            continue
        distance = entity.get("distance")
        if isinstance(distance, (int, float)) and not isinstance(distance, bool):
            return float(distance)
        return None
    return None


def _follow_target_payload(target: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded target identity for follow-handoff audit events."""

    target_id = target.get("entity_id")
    if target_id is None:
        target_id = target.get("id")
    return {
        key: value
        for key, value in {
            "entity_id": target_id,
            "name": target.get("name"),
            "type": target.get("type"),
        }.items()
        if value is not None
    }


def _positive_float(value: Any, default: float) -> float:
    """Coerce one positive numeric runtime value without accepting booleans."""

    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return default


def _coerce_action_types(values: Any) -> list[ActionType]:
    """Convert task manifest action names into typed action literals."""

    if not isinstance(values, list):
        raise ValueError("task_spec.allowed_actions must be a list.")
    action_types: list[ActionType] = []
    for value in values:
        action_type = str(value)
        if action_type not in VALID_ACTION_TYPES:
            raise ValueError(f"Unknown action in task_spec.allowed_actions: {action_type}")
        action_types.append(cast(ActionType, action_type))
    return action_types


def _finish_decision_payload(decision: ActionDecision | None) -> dict[str, Any]:
    """Return the bounded model claim that accompanied a finish request."""

    if decision is None:
        return {"reasoning_summary": "", "evidence": []}
    return {
        "reasoning_summary": decision.reasoning_summary,
        "evidence": list(decision.evidence),
        "knowledge_need": decision.knowledge_need.model_dump(mode="json"),
    }


def _apply_configured_action_recommendations(
    *,
    action: HarnessAction,
    action_result: dict[str, Any],
    recommendations: dict[str, list[str]],
    prompt_configuration: Any,
) -> dict[str, Any]:
    """Overlay prompt-configured follow-ups while preserving raw worker guidance."""

    if action_result.get("ok") is not True or action.type not in recommendations:
        return action_result
    configured = list(recommendations[action.type])
    has_runtime_recommendations = "recommended_next_actions" in action_result
    if not configured and not has_runtime_recommendations:
        return action_result

    updated = dict(action_result)
    if has_runtime_recommendations:
        runtime_value = action_result.get("recommended_next_actions")
        updated["runtime_recommended_next_actions"] = (
            list(runtime_value) if isinstance(runtime_value, list) else runtime_value
        )
    updated["recommended_next_actions"] = configured
    updated["recommended_next_actions_source"] = "prompt_configuration"

    audit = prompt_configuration if isinstance(prompt_configuration, dict) else {}
    revision = audit.get("revision")
    if revision is not None:
        updated["prompt_config_revision"] = revision
    versions = audit.get("configuration_versions")
    action_versions = versions.get("actions") if isinstance(versions, dict) else None
    action_version = (
        action_versions.get(action.type)
        if isinstance(action_versions, dict)
        else None
    )
    if isinstance(action_version, dict):
        updated["recommendation_config_version"] = dict(action_version)
    return updated


def _new_progress_feedback(
    observation: dict[str, Any],
    seen_job_ids: set[str],
) -> dict[str, Any] | None:
    """Return one newly completed online MineCLIP result for dedicated audit logging."""

    progress = observation.get("creative_progress")
    if not isinstance(progress, dict):
        return None
    latest = progress.get("latest")
    if not isinstance(latest, dict):
        return None
    job_id = latest.get("job_id")
    if not isinstance(job_id, str) or not job_id or job_id in seen_job_ids:
        return None
    seen_job_ids.add(job_id)
    return {
        "feedback": latest,
        "pending_jobs": progress.get("pending_jobs"),
        "buffer_ready": progress.get("buffer_ready"),
        "advisory_only": True,
        "success_authority": "human_review",
    }


def _task_requires_external_evaluation(task_spec: dict[str, Any]) -> bool:
    """Return whether a task declares an evaluator that runs after action execution."""

    verifier = task_spec.get("verifier") or task_spec.get("success_criteria")
    return _verifier_requires_external_evaluation(verifier)


def _verifier_requires_external_evaluation(verifier: Any) -> bool:
    """Find an external evaluator marker inside a composite verifier specification."""

    if isinstance(verifier, list):
        return any(_verifier_requires_external_evaluation(item) for item in verifier)
    if not isinstance(verifier, dict):
        return False
    if verifier.get("type") == "creative_mineclip" or verifier.get("external_evaluator"):
        return True
    return any(
        _verifier_requires_external_evaluation(verifier.get(key))
        for key in ("all", "any")
        if key in verifier
    )


def _knowledge_audit_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Reduce a knowledge tool result to stable audit metadata and bounded snippets."""

    payload = {
        "ok": result.get("ok"),
        "tool": result.get("tool") or result.get("action_type"),
        "query": result.get("query"),
        "scope": result.get("scope"),
        "item": result.get("item"),
        "error_code": result.get("error_code"),
        "message": result.get("message"),
        "source_policy": result.get("source_policy"),
        "state_summary": result.get("state_summary"),
        "cache_hit": result.get("cache_hit"),
        "cache_signature": result.get("cache_signature"),
        "cached_from_step_index": result.get("cached_from_step_index"),
        "knowledge_revision": result.get("knowledge_revision"),
    }
    if isinstance(result.get("terms"), list):
        payload["terms"] = [
            {
                "canonical_id": item.get("canonical_id"),
                "kind": item.get("kind"),
                "name": item.get("name"),
            }
            for item in result["terms"]
            if isinstance(item, dict)
        ]
    if isinstance(result.get("recipe"), dict):
        recipe = result["recipe"]
        payload["recipe"] = {
            "output": recipe.get("output"),
            "output_count": recipe.get("output_count"),
            "station": recipe.get("station"),
            "ingredients": recipe.get("ingredients"),
        }
    if isinstance(result.get("docs"), list):
        payload["docs"] = [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "tags": item.get("tags"),
                "truncated": item.get("truncated"),
            }
            for item in result["docs"]
            if isinstance(item, dict)
        ]
    return {key: value for key, value in payload.items() if value is not None}


def _usage_payload(usage: ModelUsage) -> dict[str, int | None]:
    """Convert model usage into the persistent audit shape."""

    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def _checkpoint_previous_step(checkpoint: dict[str, Any]) -> dict[str, Any] | None:
    """Recover the latest ReAct action/result pair from a persisted checkpoint."""

    action = checkpoint.get("last_action")
    action_result = checkpoint.get("last_action_result")
    if not isinstance(action, dict) and not isinstance(action_result, dict):
        return None
    return {
        "step_index": checkpoint.get("step_index"),
        "action": action if isinstance(action, dict) else None,
        "action_result": action_result if isinstance(action_result, dict) else None,
    }


def _reachability_analysis_payload(
    task_spec: dict[str, Any],
    step_index: int,
    action: HarnessAction,
    action_result: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract a dedicated audit payload for move_to pathfinding diagnostics."""

    if action.type != "move_to":
        return None
    if not any(
        key in action_result
        for key in (
            "path_summary",
            "movement_policy",
            "scaffolding_item_names",
            "available_scaffolding_count",
            "planning_timeout_ms",
            "navigation_failure_reason",
            "inventory_delta",
            "consumed_items",
            "scaffolding_delta",
            "scaffolding_consumed",
            "nearest_reachable_position",
            "target_height_delta",
            "path_resets",
        )
    ):
        return None
    runtime = task_spec.get("runtime") if isinstance(task_spec.get("runtime"), dict) else {}
    training = task_spec.get("training") if isinstance(task_spec.get("training"), dict) else {}
    observation = (
        action_result.get("observation")
        if isinstance(action_result.get("observation"), dict)
        else {}
    )
    return {
        "step_index": step_index,
        "task_id": task_spec.get("task_id"),
        "agent_id": task_spec.get("agent_id")
        or runtime.get("username")
        or training.get("worker_id"),
        "username": runtime.get("username"),
        "worker_id": training.get("worker_id"),
        "action": action.model_dump(mode="json"),
        "ok": action_result.get("ok"),
        "error_code": action_result.get("error_code"),
        "target": action_result.get("target") or action.args.get("position"),
        "tolerance": action_result.get("tolerance") or action.args.get("tolerance"),
        "timeout_ms": action_result.get("timeout_ms") or action.args.get("timeout_ms"),
        "planning_timeout_ms": action_result.get("planning_timeout_ms"),
        "distance": action_result.get("distance"),
        "start_position": action_result.get("start_position"),
        "target_position": action_result.get("target_position"),
        "end_position": action_result.get("end_position"),
        "initial_distance": action_result.get("initial_distance"),
        "final_distance": action_result.get("final_distance"),
        "distance_delta": action_result.get("distance_delta"),
        "reached_tolerance": action_result.get("reached_tolerance"),
        "progress_status": action_result.get("progress_status"),
        "position_after": observation.get("position"),
        "diagnosis": action_result.get("diagnosis"),
        "navigation_failure_reason": action_result.get("navigation_failure_reason"),
        "state_summary": action_result.get("state_summary"),
        "movement_policy": action_result.get("movement_policy"),
        "scaffolding_item_names": action_result.get("scaffolding_item_names"),
        "available_scaffolding_count": action_result.get("available_scaffolding_count"),
        "inventory_delta": action_result.get("inventory_delta"),
        "consumed_items": action_result.get("consumed_items"),
        "scaffolding_delta": action_result.get("scaffolding_delta"),
        "scaffolding_consumed": action_result.get("scaffolding_consumed"),
        "suggested_affordances": action_result.get("suggested_affordances"),
        "nearest_reachable_position": action_result.get("nearest_reachable_position"),
        "target_height_delta": action_result.get("target_height_delta"),
        "requires_break_count": action_result.get("requires_break_count"),
        "requires_place_count": action_result.get("requires_place_count"),
        "has_parkour": action_result.get("has_parkour"),
        "path_summary": action_result.get("path_summary"),
        "path_resets": action_result.get("path_resets"),
    }
