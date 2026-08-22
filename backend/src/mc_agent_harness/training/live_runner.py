from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from mc_agent_harness.configuration.service import DatabasePromptConfigProvider
from mc_agent_harness.db.models import ModelCallRecord, StepRecord, TaskMemoryRecord
from mc_agent_harness.db.session import SessionFactory, SessionLocal
from mc_agent_harness.harness.context_manager import ContextManager, ContextPolicy
from mc_agent_harness.harness.action_repair import (
    ActionGenerationTimeout,
    ActionRepairConfig,
    ActionRepairPolicy,
)
from mc_agent_harness.harness.execution_loop import (
    ExecutionBudget,
    ExecutionLoop,
    ExecutionRunResult,
    ExecutionStepResult,
)
from mc_agent_harness.harness.persistent_recorder import (
    PersistedEventCallback,
    PersistentEvaluationRecorder,
)
from mc_agent_harness.harness.tool_registry import DEFAULT_HARNESS_ACTIONS, ToolRegistry
from mc_agent_harness.knowledge.chunk_store import DatabaseKnowledgeStore
from mc_agent_harness.knowledge.database_provider import DatabaseKnowledgeProvider
from mc_agent_harness.models.router import ModelRouter
from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.runtime.mineflayer_client import MineflayerClient
from mc_agent_harness.runtime.server_commands import (
    ServerCommandExecutor,
    ServerCommandResetConfig,
    ServerCommandResetRuntime,
)
from mc_agent_harness.runtime.threat_pause import ThreatPauseConfig, ThreatPauseRuntime
from mc_agent_harness.schemas.learning import LearningCandidateSpec
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus
from mc_agent_harness.skills.initial import seed_initial_skills
from mc_agent_harness.skills.learning import LearningCandidateSnapshot, LearningCandidateStore
from mc_agent_harness.skills.library import SkillLibrary, SkillLibraryError, SkillLibrarySnapshot
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider
from mc_agent_harness.training.runner import TrainingBudget, TrainingTaskRequest


RuntimeFactory = Callable[[str, float], GameRuntime]
ModelRouterFactory = Callable[[dict[str, Any]], ModelRouter]


@dataclass(frozen=True, slots=True)
class LiveMinecraftConfig:
    """Connection settings shared by live Mineflayer training workers."""

    host: str
    port: int
    username_prefix: str = "HarnessTrainer"
    spawn_timeout_ms: int = 20000


@dataclass(frozen=True, slots=True)
class LiveWorkerSpec:
    """One live worker endpoint and its isolated Minecraft server placement."""

    worker_id: str
    worker_url: str
    username: str
    server_id: str | None = None
    minecraft_host: str | None = None
    minecraft_port: int | None = None
    rcon_host: str | None = None
    rcon_port: int | None = None
    world_dir: str | None = None


@dataclass(frozen=True, slots=True)
class RandomTeleportResetConfig:
    """Optional live-reset policy that spreads the bot to a random server-side location."""

    enabled: bool = False
    center_x: int = 0
    center_z: int = 0
    spread_distance: int = 0
    max_range: int = 200
    clear_start_position: bool = True
    only_when_biome_missing: bool = False


@dataclass(frozen=True, slots=True)
class LiveTrainingConfig:
    """Configuration for one parallel single-agent live training job."""

    job_id: str = field(default_factory=lambda: f"week10_live_{uuid.uuid4().hex[:12]}")
    runtime_profile: str = "live-mineflayer-training"
    model_profile: str = "default"
    budget: TrainingBudget = field(default_factory=lambda: TrainingBudget(max_steps_per_task=20))
    duplicate_threshold: float = 0.82
    auto_promote: bool = False
    max_task_memory_items: int = 5
    max_retrieved_skills: int = 3
    min_skill_relevance: float = 0.5
    start_delay_sec: float = 0.0
    clear_inventory_on_reset: bool = False
    clear_inventory_items: tuple[str, ...] = ()
    clear_all_inventory_on_reset: bool = False
    clear_inventory_wait_ms: int = 750
    reset_drop_fallback: bool = True
    model_timeout_retries: int = 2
    model_timeout_backoff_sec: tuple[float, ...] = (2.0, 5.0)
    model_timeout_requeues: int = 1
    model_timeout_requeue_delay_sec: float = 10.0
    worker_failure_requeues: int = 1
    max_task_retries: int = 0
    task_retry_delay_sec: float = 2.0
    task_waves: tuple[tuple[str, ...], ...] = ()
    seed_initial_skills: bool = True
    initial_visual_snapshot: bool = False
    server_command_reset: ServerCommandResetConfig = field(default_factory=ServerCommandResetConfig)
    threat_pause: ThreatPauseConfig = field(default_factory=ThreatPauseConfig)
    random_teleport_reset: RandomTeleportResetConfig = field(
        default_factory=RandomTeleportResetConfig
    )


@dataclass(frozen=True, slots=True)
class LiveSkillUpdate:
    """Skill candidate, duplicate, and promotion result for one successful task."""

    candidate: dict[str, Any] | None
    duplicate_matches: list[dict[str, Any]]
    promoted: dict[str, Any] | None
    skipped_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LiveLearningUpdate:
    """Failure-hypothesis recording and recovery-validation result for one run."""

    recorded: list[dict[str, Any]]
    validated: list[dict[str, Any]]
    skipped_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LiveModelUsage:
    """Persisted model-call and token totals for one live task attempt."""

    model_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LiveTrainingOutcome:
    """Final result for one live training task attempt."""

    task_id: str
    attempt: int
    run_id: str | None
    worker_id: str
    username: str
    server_id: str | None
    memory_namespace: str
    success: bool
    status: str
    verifier: dict[str, Any]
    steps: int
    duration_sec: float
    model_usage: LiveModelUsage = field(default_factory=LiveModelUsage)
    stop_reason: str | None = None
    runtime_error: str | None = None
    failure_class: str | None = None
    skill_update: LiveSkillUpdate | None = None
    learning_update: LiveLearningUpdate | None = None


WorkerRecoveryCallback = Callable[
    [LiveWorkerSpec, LiveTrainingOutcome],
    Awaitable[dict[str, Any]],
]


@dataclass(frozen=True, slots=True)
class LiveTrainingResumeState:
    """Completed-wave state restored from an external durable checkpoint."""

    completed_wave_count: int
    attempt_outcomes: tuple[LiveTrainingOutcome, ...]
    skill_snapshot_revision: str | None
    learning_snapshot_revision: str | None


WaveCheckpointCallback = Callable[
    [int, list[LiveTrainingOutcome], str | None, str | None],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class LiveTrainingReport:
    """Aggregate report for one live parallel training job."""

    job_id: str
    status: str
    started_at: str
    finished_at: str
    duration_sec: float
    task_count: int
    attempt_count: int
    retried_task_count: int
    wave_count: int
    task_waves: list[list[str]]
    success_count: int
    auto_promote: bool
    duplicate_threshold: float
    skill_snapshot: dict[str, Any] | None
    learning_snapshot: dict[str, Any] | None
    model_usage: LiveModelUsage
    outcomes: list[LiveTrainingOutcome]
    attempt_outcomes: list[LiveTrainingOutcome]

    def to_json(self) -> dict[str, Any]:
        """Convert the report into a JSON-safe dictionary."""

        return asdict(self)


class LiveTrainingRunner:
    """Parallel single-agent trainer for live Mineflayer programmatic tasks."""

    def __init__(
        self,
        *,
        task_provider: MineDojoTaskProvider,
        minecraft: LiveMinecraftConfig,
        workers: list[LiveWorkerSpec],
        session_factory: SessionFactory = SessionLocal,
        config: LiveTrainingConfig | None = None,
        skill_library: SkillLibrary | None = None,
        learning_store: LearningCandidateStore | None = None,
        runtime_factory: RuntimeFactory | None = None,
        model_router_factory: ModelRouterFactory | None = None,
        server_command_executor: ServerCommandExecutor | None = None,
        server_command_executors: dict[str, ServerCommandExecutor] | None = None,
        resume_state: LiveTrainingResumeState | None = None,
        wave_checkpoint_callback: WaveCheckpointCallback | None = None,
        event_callback: PersistedEventCallback | None = None,
        worker_recovery_callback: WorkerRecoveryCallback | None = None,
    ) -> None:
        if not workers:
            raise ValueError("LiveTrainingRunner requires at least one worker.")
        self.task_provider = task_provider
        self.minecraft = minecraft
        self.workers = workers
        self.session_factory = session_factory
        self.config = config or LiveTrainingConfig()
        self.skill_library = skill_library or SkillLibrary(session_factory=session_factory)
        self.learning_store = learning_store or LearningCandidateStore(
            session_factory=session_factory
        )
        self.runtime_factory = runtime_factory or _default_runtime_factory
        self.model_router_factory = model_router_factory or (lambda _task_spec: ModelRouter())
        self.server_command_executor = server_command_executor
        self.server_command_executors = dict(server_command_executors or {})
        self.resume_state = resume_state
        self.wave_checkpoint_callback = wave_checkpoint_callback
        self.event_callback = event_callback
        self.worker_recovery_callback = worker_recovery_callback
        self._biome_location_caches: dict[str, dict[str, tuple[int, int]]] = {
            worker.worker_id: {} for worker in self.workers
        }
        self._skill_snapshot: SkillLibrarySnapshot | None = None
        self._learning_snapshot: LearningCandidateSnapshot | None = None
        if self.config.max_task_retries < 0:
            raise ValueError("max_task_retries must be non-negative.")
        if self.config.task_retry_delay_sec < 0:
            raise ValueError("task_retry_delay_sec must be non-negative.")
        if self.config.worker_failure_requeues < 0:
            raise ValueError("worker_failure_requeues must be non-negative.")
        if self.config.server_command_reset.enabled or self.config.threat_pause.enabled:
            missing = [
                worker.worker_id
                for worker in self.workers
                if self._server_command_executor_for(worker) is None
            ]
            if missing:
                raise ValueError(
                    "A server command executor is required for every worker when reset or "
                    f"threat pause is enabled; missing workers: {', '.join(missing)}."
                )

    async def run(self, task_ids: list[str]) -> LiveTrainingReport:
        """Run programmatic tasks across isolated live Mineflayer workers."""

        if self.config.seed_initial_skills:
            seed_initial_skills(self.session_factory)
        self._skill_snapshot = await self.skill_library.capture_snapshot()
        self._learning_snapshot = await self.learning_store.capture_snapshot()
        started_perf = time.perf_counter()
        started_at = _utc_now()
        task_waves = _normalized_task_waves(task_ids, self.config.task_waves)
        self._validate_resume_state(task_waves)
        completed_wave_count = (
            self.resume_state.completed_wave_count if self.resume_state is not None else 0
        )
        attempt_outcomes = list(
            self.resume_state.attempt_outcomes if self.resume_state is not None else ()
        )
        for wave_index, task_wave in enumerate(task_waves):
            if wave_index < completed_wave_count:
                continue
            worker_results = await self._run_wave(task_wave)
            attempt_outcomes.extend(outcome for results in worker_results for outcome in results)
            if self.wave_checkpoint_callback is not None:
                await self.wave_checkpoint_callback(
                    wave_index + 1,
                    list(attempt_outcomes),
                    self._skill_snapshot.revision,
                    self._learning_snapshot.revision,
                )
        task_order = {task_id: index for index, task_id in enumerate(task_ids)}
        attempt_outcomes.sort(
            key=lambda outcome: (task_order.get(outcome.task_id, len(task_order)), outcome.attempt)
        )
        attempt_outcomes = await self._finalize_skill_updates(attempt_outcomes)
        outcomes = _final_task_outcomes(task_ids, attempt_outcomes)
        success_count = sum(1 for outcome in outcomes if outcome.success)
        finished_at = _utc_now()
        status = _report_status(outcomes, success_count)
        return LiveTrainingReport(
            job_id=self.config.job_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=time.perf_counter() - started_perf,
            task_count=len(task_ids),
            attempt_count=len(attempt_outcomes),
            retried_task_count=sum(
                1
                for task_id in task_ids
                if sum(1 for outcome in attempt_outcomes if outcome.task_id == task_id) > 1
            ),
            wave_count=len(task_waves),
            task_waves=task_waves,
            success_count=success_count,
            auto_promote=self.config.auto_promote,
            duplicate_threshold=self.config.duplicate_threshold,
            skill_snapshot=self._skill_snapshot.to_json()
            if self._skill_snapshot is not None
            else None,
            learning_snapshot=self._learning_snapshot.to_json()
            if self._learning_snapshot is not None
            else None,
            model_usage=_sum_model_usage(attempt_outcomes),
            outcomes=outcomes,
            attempt_outcomes=attempt_outcomes,
        )

    def _validate_resume_state(self, task_waves: list[list[str]]) -> None:
        """Validate that checkpoint state is compatible with this immutable batch view."""

        state = self.resume_state
        if state is None:
            return
        if state.completed_wave_count < 0 or state.completed_wave_count > len(task_waves):
            raise ValueError("Checkpoint completed_wave_count is outside the task-wave range.")
        completed_task_ids = {
            task_id for wave in task_waves[: state.completed_wave_count] for task_id in wave
        }
        outcome_task_ids = {outcome.task_id for outcome in state.attempt_outcomes}
        if not outcome_task_ids.issubset(completed_task_ids):
            raise ValueError("Checkpoint outcomes include tasks outside completed waves.")
        execution_complete = state.completed_wave_count == len(task_waves)
        if not execution_complete:
            if (
                state.skill_snapshot_revision is not None
                and self._skill_snapshot is not None
                and state.skill_snapshot_revision != self._skill_snapshot.revision
            ):
                raise ValueError("Skill snapshot changed after the checkpoint was created.")
            if (
                state.learning_snapshot_revision is not None
                and self._learning_snapshot is not None
                and state.learning_snapshot_revision != self._learning_snapshot.revision
            ):
                raise ValueError("Learning snapshot changed after the checkpoint was created.")

    async def _run_wave(self, task_ids: list[str]) -> list[list[LiveTrainingOutcome]]:
        """Run one bounded concurrent task wave including all configured retries."""

        queue: asyncio.Queue[TrainingTaskRequest | None] = asyncio.Queue()
        for task_id in task_ids:
            await queue.put(
                TrainingTaskRequest.build(
                    job_id=self.config.job_id,
                    task_id=task_id,
                    attempt=1,
                    runtime_profile=self.config.runtime_profile,
                )
            )
        active_workers = self.workers[: min(len(self.workers), len(task_ids))]
        worker_tasks = [
            asyncio.create_task(self._worker_loop(worker, queue)) for worker in active_workers
        ]
        try:
            await queue.join()
            for _ in active_workers:
                await queue.put(None)
            return await asyncio.gather(*worker_tasks)
        except asyncio.CancelledError:
            for task in worker_tasks:
                task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            raise

    async def _worker_loop(
        self,
        worker: LiveWorkerSpec,
        queue: asyncio.Queue[TrainingTaskRequest | None],
    ) -> list[LiveTrainingOutcome]:
        """Drain training requests for one live worker."""

        outcomes: list[LiveTrainingOutcome] = []
        while True:
            request = await queue.get()
            try:
                if request is None:
                    return outcomes
                outcome = await self._run_one(request, worker)
                outcomes.append(outcome)
                if self._should_retry(request, outcome):
                    if _worker_recovery_required(outcome):
                        recovery = await self._recover_worker(worker, outcome)
                        if recovery.get("success") is not True:
                            continue
                    retry_request = _retry_task_request(request)
                    await self._record_task_requeued(
                        outcome=outcome,
                        request=request,
                        retry_request=retry_request,
                    )
                    delay_sec = self._retry_delay(outcome)
                    if delay_sec > 0:
                        await asyncio.sleep(delay_sec)
                    await queue.put(retry_request)
                    continue
            finally:
                queue.task_done()

    async def _run_one(
        self,
        request: TrainingTaskRequest,
        worker: LiveWorkerSpec,
    ) -> LiveTrainingOutcome:
        """Execute and verify one task while deferring skill writes to the batch barrier."""

        started_perf = time.perf_counter()
        run_id = _run_id(
            self.config.job_id,
            request.task_id,
            worker.worker_id,
            request.attempt,
        )
        recorder = PersistentEvaluationRecorder(
            self.session_factory,
            task_id=request.task_id,
            agent_id=worker.username,
            worker_id=worker.worker_id,
            event_callback=self.event_callback,
        )
        runtime = self.runtime_factory(
            worker.worker_url,
            (self.minecraft.spawn_timeout_ms / 1000) + 30,
        )
        command_executor = self._server_command_executor_for(worker)
        if command_executor is not None and self.config.server_command_reset.enabled:
            runtime = ServerCommandResetRuntime(
                runtime,
                executor=command_executor,
                config=self.config.server_command_reset,
                biome_location_cache=self._biome_location_caches[worker.worker_id],
            )
        if command_executor is not None and self.config.threat_pause.enabled:
            runtime = ThreatPauseRuntime(
                runtime,
                executor=command_executor,
                config=self.config.threat_pause,
            )
        task_spec: dict[str, Any] | None = None
        result: ExecutionRunResult | None = None
        verifier: dict[str, Any] = {"success": False, "reason": "not_run", "checks": []}
        try:
            task_spec = await self.task_provider.load_task(request.task_id)
            _reject_catalog_only(task_spec)
            task_spec = self._live_task_spec(task_spec, request, worker, run_id)
            task_memory = _load_task_memory(
                self.session_factory,
                request.task_id,
                request.memory_namespace,
                self.config.max_task_memory_items,
            )
            result = await asyncio.wait_for(
                self._execute_loop(runtime, recorder, task_spec, task_memory),
                timeout=self.config.budget.max_runtime_sec_per_task,
            )
            runtime_failure = _execution_runtime_failure(result)
            if runtime_failure is not None:
                verifier = {
                    "success": False,
                    "inconclusive": True,
                    "reason": runtime_failure["message"],
                    "checks": [],
                    "operational_failure": runtime_failure,
                }
                await recorder.record(
                    run_id,
                    "run_runtime_error",
                    {
                        "task_id": request.task_id,
                        "memory_namespace": request.memory_namespace,
                        **runtime_failure,
                    },
                )
                return _outcome(
                    request=request,
                    worker=worker,
                    run_id=run_id,
                    success=False,
                    status="runtime_error",
                    verifier=verifier,
                    result=result,
                    started_perf=started_perf,
                    runtime_error=str(runtime_failure["message"]),
                    session_factory=self.session_factory,
                )
            verifier = await self._verify_with_recovery(
                request=request,
                run_id=run_id,
                task_spec=task_spec,
                result=result,
                runtime=runtime,
                recorder=recorder,
            )
            await recorder.record(
                run_id,
                "verifier_result",
                {
                    "task_id": request.task_id,
                    "success": bool(verifier.get("success")),
                    "verifier": verifier,
                    "memory_namespace": request.memory_namespace,
                },
            )
            if verifier.get("success"):
                return _outcome(
                    request=request,
                    worker=worker,
                    run_id=run_id,
                    success=True,
                    status="succeeded",
                    verifier=verifier,
                    result=result,
                    started_perf=started_perf,
                    session_factory=self.session_factory,
                )
            if verifier.get("inconclusive"):
                await recorder.record(
                    run_id,
                    "run_verification_inconclusive",
                    {
                        "task_id": request.task_id,
                        "verifier": verifier,
                        "memory_namespace": request.memory_namespace,
                    },
                )
                return _outcome(
                    request=request,
                    worker=worker,
                    run_id=run_id,
                    success=False,
                    status="verification_inconclusive",
                    verifier=verifier,
                    result=result,
                    started_perf=started_perf,
                    runtime_error=str(verifier.get("reason") or "verification inconclusive"),
                    session_factory=self.session_factory,
                )
            _append_failure_memory(
                self.session_factory,
                request.task_id,
                request.memory_namespace,
                verifier,
                run_id,
            )
            return _outcome(
                request=request,
                worker=worker,
                run_id=run_id,
                success=False,
                status="failed",
                verifier=verifier,
                result=result,
                started_perf=started_perf,
                session_factory=self.session_factory,
            )
        except ActionGenerationTimeout as exc:
            message = f"{type(exc).__name__}: {exc}"
            if task_spec is not None:
                await recorder.record(
                    run_id,
                    "run_model_timeout",
                    {
                        "task_id": request.task_id,
                        "message": message,
                        "step_index": exc.step_index,
                        "attempts": exc.attempts,
                        "memory_namespace": request.memory_namespace,
                    },
                )
            return _outcome(
                request=request,
                worker=worker,
                run_id=run_id,
                success=False,
                status="model_timeout",
                verifier={
                    "success": False,
                    "inconclusive": True,
                    "reason": message,
                    "checks": [],
                },
                result=result,
                started_perf=started_perf,
                runtime_error=message,
                steps=_persisted_step_count(self.session_factory, run_id),
                session_factory=self.session_factory,
            )
        except TimeoutError:
            timeout_sec = self.config.budget.max_runtime_sec_per_task
            message = (
                f"Task exceeded max_runtime_sec_per_task={timeout_sec:.3f}."
                if timeout_sec is not None
                else "Task execution timed out."
            )
            if task_spec is not None:
                await recorder.record(
                    run_id,
                    "run_task_timeout",
                    {
                        "task_id": request.task_id,
                        "message": message,
                        "memory_namespace": request.memory_namespace,
                        "max_runtime_sec_per_task": timeout_sec,
                    },
                )
            return _outcome(
                request=request,
                worker=worker,
                run_id=run_id,
                success=False,
                status="task_timeout",
                verifier={
                    "success": False,
                    "inconclusive": True,
                    "reason": message,
                    "checks": [],
                },
                result=result,
                started_perf=started_perf,
                runtime_error=message,
                steps=_persisted_step_count(self.session_factory, run_id),
                session_factory=self.session_factory,
            )
        except asyncio.CancelledError:
            if task_spec is not None:
                await recorder.record(
                    run_id,
                    "run_interrupted",
                    {
                        "task_id": request.task_id,
                        "reason": "asyncio_task_cancelled",
                        "memory_namespace": request.memory_namespace,
                        "persisted_steps": _persisted_step_count(self.session_factory, run_id),
                    },
                )
            raise
        except Exception as exc:  # noqa: BLE001 - training reports must capture task failures.
            message = f"{type(exc).__name__}: {exc}"
            if task_spec is not None:
                await recorder.record(
                    run_id,
                    "runtime_error",
                    {
                        "phase": "live_training",
                        "step_index": None,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "task_id": request.task_id,
                    },
                )
                await recorder.record(
                    run_id,
                    "run_failed",
                    {"task_id": request.task_id, "message": message},
                )
            _append_failure_memory(
                self.session_factory,
                request.task_id,
                request.memory_namespace,
                {"success": False, "reason": message, "checks": []},
                run_id,
            )
            return _outcome(
                request=request,
                worker=worker,
                run_id=run_id,
                success=False,
                status="runtime_error",
                verifier={"success": False, "reason": message, "checks": []},
                result=result,
                started_perf=started_perf,
                runtime_error=message,
                steps=_persisted_step_count(self.session_factory, run_id),
                session_factory=self.session_factory,
            )
        finally:
            try:
                await runtime.close()
            except Exception as exc:  # noqa: BLE001 - close failures must not hide task outcomes.
                if task_spec is not None:
                    await recorder.record(
                        run_id,
                        "runtime_error",
                        {
                            "phase": "close",
                            "step_index": None,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                            "task_id": request.task_id,
                        },
                    )

    async def _execute_loop(
        self,
        runtime: GameRuntime,
        recorder: PersistentEvaluationRecorder,
        task_spec: dict[str, Any],
        task_memory: list[str],
    ) -> ExecutionRunResult:
        """Run one live observe-context-model-action loop."""

        loop = ExecutionLoop(
            runtime=runtime,
            model_router=self.model_router_factory(task_spec),
            context_manager=ContextManager(
                policy=ContextPolicy(
                    max_retrieved_skills=self.config.max_retrieved_skills,
                    min_skill_relevance=self.config.min_skill_relevance,
                ),
                knowledge_provider=DatabaseKnowledgeProvider(
                    DatabaseKnowledgeStore(SessionLocal)
                ),
                skill_library=self._skill_snapshot or self.skill_library,
                learning_candidates=self._learning_snapshot,
                prompt_config_provider=DatabasePromptConfigProvider(SessionLocal),
            ),
            tool_registry=ToolRegistry(DEFAULT_HARNESS_ACTIONS),
            recorder=recorder,
            action_repair_policy=ActionRepairPolicy(
                ActionRepairConfig(
                    model_timeout_retries=self.config.model_timeout_retries,
                    model_timeout_backoff_sec=self.config.model_timeout_backoff_sec,
                )
            ),
            budget=ExecutionBudget(
                max_steps=self.config.budget.max_steps_per_task or 20,
                checkpoint_interval_steps=0,
            ),
            success_checker=self._step_success_checker,
        )
        return await loop.run(
            str(task_spec["task_id"]),
            task_spec=task_spec,
            task_memory=task_memory,
        )

    def _should_retry(
        self,
        request: TrainingTaskRequest,
        outcome: LiveTrainingOutcome,
    ) -> bool:
        """Return true when a failed task should receive another isolated attempt."""

        if outcome.success or not _retryable_outcome(outcome):
            return False
        retry_limit = self.config.max_task_retries
        if outcome.status == "model_timeout":
            retry_limit = max(retry_limit, self.config.model_timeout_requeues)
        if _worker_recovery_required(outcome):
            retry_limit = max(retry_limit, self.config.worker_failure_requeues)
        return request.attempt <= retry_limit

    def _retry_delay(self, outcome: LiveTrainingOutcome) -> float:
        """Return the configured delay before requeueing one failed attempt."""

        if outcome.status == "model_timeout":
            return self.config.model_timeout_requeue_delay_sec
        return self.config.task_retry_delay_sec

    async def _record_task_requeued(
        self,
        *,
        outcome: LiveTrainingOutcome,
        request: TrainingTaskRequest,
        retry_request: TrainingTaskRequest,
    ) -> None:
        """Audit that a failed attempt was retained and requeued for another attempt."""

        if outcome.run_id is None:
            return
        recorder = PersistentEvaluationRecorder(
            self.session_factory,
            task_id=outcome.task_id,
            agent_id=outcome.username,
            worker_id=outcome.worker_id,
        )
        await recorder.record(
            outcome.run_id,
            "task_requeued",
            {
                "task_id": request.task_id,
                "reason": outcome.status,
                "failure_class": outcome.failure_class,
                "from_attempt": request.attempt,
                "to_attempt": retry_request.attempt,
                "memory_namespace": request.memory_namespace,
                "delay_sec": self._retry_delay(outcome),
            },
        )

    async def _recover_worker(
        self,
        worker: LiveWorkerSpec,
        outcome: LiveTrainingOutcome,
    ) -> dict[str, Any]:
        """Restart or externally recover a worker before requeueing an unknown action state."""

        if self.worker_recovery_callback is None:
            recovery = {
                "success": False,
                "reason": "worker_recovery_callback_not_configured",
            }
        else:
            try:
                recovery = await self.worker_recovery_callback(worker, outcome)
            except Exception as exc:  # noqa: BLE001 - recovery failure must remain auditable.
                recovery = {
                    "success": False,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
        if outcome.run_id is not None:
            recorder = PersistentEvaluationRecorder(
                self.session_factory,
                task_id=outcome.task_id,
                agent_id=outcome.username,
                worker_id=outcome.worker_id,
            )
            await recorder.record(
                outcome.run_id,
                "worker_recovery",
                {
                    "task_id": outcome.task_id,
                    "failure_class": outcome.failure_class,
                    "worker_id": outcome.worker_id,
                    "recovery": recovery,
                },
            )
        return recovery

    def _server_command_executor_for(
        self,
        worker: LiveWorkerSpec,
    ) -> ServerCommandExecutor | None:
        """Resolve the RCON executor assigned to one worker's server instance."""

        return self.server_command_executors.get(worker.worker_id, self.server_command_executor)

    async def _verify_with_recovery(
        self,
        *,
        request: TrainingTaskRequest,
        run_id: str,
        task_spec: dict[str, Any],
        result: ExecutionRunResult,
        runtime: GameRuntime,
        recorder: PersistentEvaluationRecorder,
    ) -> dict[str, Any]:
        """Run final verification with one timeout retry and observation refresh."""

        attempts = 2
        last_error: TimeoutError | None = None
        for attempt_index in range(attempts):
            try:
                return await self.task_provider.verify(_run_state(task_spec, result))
            except TimeoutError as exc:
                last_error = exc
                will_retry = attempt_index < attempts - 1
                await recorder.record(
                    run_id,
                    "verifier_timeout",
                    {
                        "task_id": request.task_id,
                        "step_index": None,
                        "attempt_index": attempt_index,
                        "will_retry": will_retry,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                if will_retry:
                    try:
                        observation = await runtime.observe()
                    except Exception as observe_exc:  # noqa: BLE001 - keep verifier timeout primary.
                        observation = {
                            "observe_error": {
                                "error_type": type(observe_exc).__name__,
                                "message": str(observe_exc),
                            }
                        }
                    await recorder.record(
                        run_id,
                        "verifier_timeout_observation",
                        {
                            "task_id": request.task_id,
                            "attempt_index": attempt_index,
                            "observation": observation,
                        },
                    )
                    await asyncio.sleep(0)

        message = str(last_error) if last_error is not None else "Verifier timed out."
        await recorder.record(
            run_id,
            "verifier_timeout_exhausted",
            {
                "task_id": request.task_id,
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

    async def _step_success_checker(
        self,
        task_spec: dict[str, Any],
        steps: list[ExecutionStepResult],
    ) -> dict[str, Any]:
        """Verify live task success after each completed action result."""

        return await self.task_provider.verify(_run_state_from_steps(task_spec, steps))

    def _live_task_spec(
        self,
        task_spec: dict[str, Any],
        request: TrainingTaskRequest,
        worker: LiveWorkerSpec,
        run_id: str,
    ) -> dict[str, Any]:
        """Attach live runtime and training metadata to one task spec."""

        manifest_allowed_actions = task_spec.get("allowed_actions")
        reset_plan = self._reset_plan(task_spec)
        return {
            **task_spec,
            "agent_id": worker.username,
            "reset_plan": reset_plan,
            "run_id": run_id,
            "allowed_actions": list(DEFAULT_HARNESS_ACTIONS),
            "manifest_allowed_actions": manifest_allowed_actions,
            "require_inventory_delta": True,
            "runtime_profile": self.config.runtime_profile,
            "runtime": {
                "host": worker.minecraft_host or self.minecraft.host,
                "port": worker.minecraft_port or self.minecraft.port,
                "username": worker.username,
                "spawn_timeout_ms": self.minecraft.spawn_timeout_ms,
                "reset_policy": self._reset_policy(task_spec),
            },
            "training": {
                "job_id": request.job_id,
                "attempt": request.attempt,
                "memory_namespace": request.memory_namespace,
                "worker_id": worker.worker_id,
                "agent_id": worker.username,
                "server_id": worker.server_id,
                "server_endpoint": {
                    "host": worker.minecraft_host or self.minecraft.host,
                    "port": worker.minecraft_port or self.minecraft.port,
                    "rcon_host": worker.rcon_host,
                    "rcon_port": worker.rcon_port,
                    "world_dir": worker.world_dir,
                },
                "mode": "parallel_single_agent_live",
                "skill_snapshot": self._skill_snapshot.to_json()
                if self._skill_snapshot is not None
                else None,
                "learning_snapshot": self._learning_snapshot.to_json()
                if self._learning_snapshot is not None
                else None,
            },
            "start_delay_sec": self.config.start_delay_sec,
            "initial_visual_snapshot": self.config.initial_visual_snapshot,
        }

    def _reset_plan(self, task_spec: dict[str, Any]) -> dict[str, Any]:
        """Build the task reset plan after applying live-run environment overrides."""

        base = task_spec.get("reset_plan") if isinstance(task_spec.get("reset_plan"), dict) else {}
        reset_plan = {**base}
        if not self.config.random_teleport_reset.enabled:
            return reset_plan
        if (
            self.config.random_teleport_reset.only_when_biome_missing
            and isinstance(reset_plan.get("biome_hint"), str)
            and reset_plan.get("biome_hint")
        ):
            return reset_plan
        if self.config.random_teleport_reset.clear_start_position:
            reset_plan.pop("start_position", None)
        notes = list(reset_plan.get("notes") if isinstance(reset_plan.get("notes"), list) else [])
        notes.append(
            "Live runner override: random teleport on reset was enabled for environment isolation."
        )
        reset_plan["notes"] = notes
        reset_plan["random_teleport"] = {
            "enabled": True,
            "center": {
                "x": self.config.random_teleport_reset.center_x,
                "z": self.config.random_teleport_reset.center_z,
            },
            "spread_distance": self.config.random_teleport_reset.spread_distance,
            "max_range": self.config.random_teleport_reset.max_range,
        }
        return reset_plan

    def _reset_policy(self, task_spec: dict[str, Any]) -> dict[str, Any]:
        """Build worker-side environment reset policy for one live task."""

        if (
            not self.config.clear_inventory_on_reset
            and not self.config.clear_all_inventory_on_reset
        ):
            return {"clear_inventory": {"enabled": False}}
        items = list(self.config.clear_inventory_items) or _verifier_target_items(task_spec)
        mode = "all" if self.config.clear_all_inventory_on_reset else "items"
        return {
            "clear_inventory": {
                "enabled": True,
                "mode": mode,
                "items": [] if mode == "all" else items,
                "wait_ms": self.config.clear_inventory_wait_ms,
                "drop_fallback": self.config.reset_drop_fallback,
            }
        }

    async def _finalize_skill_updates(
        self,
        outcomes: list[LiveTrainingOutcome],
    ) -> list[LiveTrainingOutcome]:
        """Classify failures, validate recoveries, then write skills after the batch barrier."""

        finalized = list(outcomes)
        learning_by_run: dict[str, list[LearningCandidateSpec]] = {}
        snapshot = self._skill_snapshot.to_json() if self._skill_snapshot is not None else None
        learning_snapshot = (
            self._learning_snapshot.to_json() if self._learning_snapshot is not None else None
        )
        for index, outcome in enumerate(finalized):
            if outcome.success or outcome.run_id is None:
                continue
            decision = await self.learning_store.record_failure(outcome.run_id)
            if decision.should_record and decision.candidate is not None:
                learning_update = LiveLearningUpdate(
                    recorded=[_learning_payload(decision.candidate)],
                    validated=[],
                )
            else:
                learning_update = LiveLearningUpdate(
                    recorded=[],
                    validated=[],
                    skipped_reason=decision.reason,
                )
            finalized[index] = replace(outcome, learning_update=learning_update)

        for index, outcome in enumerate(finalized):
            if not outcome.success or outcome.run_id is None:
                continue
            validated = await self.learning_store.record_success(outcome.run_id)
            learning_by_run[outcome.run_id] = validated
            finalized[index] = replace(
                outcome,
                learning_update=LiveLearningUpdate(
                    recorded=[],
                    validated=[_learning_payload(candidate) for candidate in validated],
                    skipped_reason=None if validated else "no_matching_recovery_candidate",
                ),
            )

        skill_finalized: list[LiveTrainingOutcome] = []
        for outcome in finalized:
            if not outcome.success or outcome.run_id is None:
                skill_finalized.append(outcome)
                continue
            recorder = PersistentEvaluationRecorder(
                self.session_factory,
                task_id=outcome.task_id,
                agent_id=outcome.username,
                worker_id=outcome.worker_id,
            )
            await recorder.record(
                outcome.run_id,
                "skill_batch_finalize_started",
                {
                    "source_run_id": outcome.run_id,
                    "batch_job_id": self.config.job_id,
                    "skill_snapshot": snapshot,
                    "learning_snapshot": learning_snapshot,
                },
            )
            try:
                skill_update = await self._update_skill(
                    outcome.run_id,
                    recorder,
                    learning_candidates=learning_by_run.get(outcome.run_id, []),
                )
            except Exception as exc:  # noqa: BLE001 - gameplay success must survive skill write failure.
                await recorder.record(
                    outcome.run_id,
                    "skill_batch_finalize_failed",
                    {
                        "source_run_id": outcome.run_id,
                        "batch_job_id": self.config.job_id,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                skill_update = LiveSkillUpdate(
                    candidate=None,
                    duplicate_matches=[],
                    promoted=None,
                    skipped_reason="skill_update_error",
                )
            else:
                await recorder.record(
                    outcome.run_id,
                    "skill_batch_finalize_finished",
                    {
                        "source_run_id": outcome.run_id,
                        "batch_job_id": self.config.job_id,
                        "skipped_reason": skill_update.skipped_reason,
                        "candidate": skill_update.candidate,
                        "promoted": skill_update.promoted,
                    },
                )
            skill_finalized.append(replace(outcome, skill_update=skill_update))
        return skill_finalized

    async def _update_skill(
        self,
        run_id: str,
        recorder: PersistentEvaluationRecorder,
        *,
        learning_candidates: list[LearningCandidateSpec] | None = None,
    ) -> LiveSkillUpdate:
        """Create a skill candidate, deduplicate it, and optionally promote it."""

        try:
            candidate = await self.skill_library.create_candidate(
                run_id,
                learning_candidates=learning_candidates,
            )
        except SkillLibraryError as exc:
            skip_reason = _skill_skip_reason(str(exc))
            await recorder.record(
                run_id,
                "skill_candidate_skipped",
                {
                    "reason": skip_reason,
                    "message": str(exc),
                },
            )
            return LiveSkillUpdate(
                candidate=None,
                duplicate_matches=[],
                promoted=None,
                skipped_reason=skip_reason,
            )
        if candidate.status == SkillStatus.promoted:
            return LiveSkillUpdate(
                candidate=_skill_payload(candidate),
                duplicate_matches=[],
                promoted=_skill_payload(candidate),
                skipped_reason="already_promoted_from_source_run",
            )
        matches = await self.skill_library.find_duplicates(
            candidate,
            threshold=self.config.duplicate_threshold,
            audit_run_id=run_id,
        )
        if matches:
            return LiveSkillUpdate(
                candidate=_skill_payload(candidate),
                duplicate_matches=[
                    {
                        "name": match.skill.name,
                        "version": match.skill.version,
                        "similarity": match.similarity,
                    }
                    for match in matches
                ],
                promoted=None,
                skipped_reason="duplicate_candidate",
            )
        if not self.config.auto_promote:
            return LiveSkillUpdate(
                candidate=_skill_payload(candidate),
                duplicate_matches=[],
                promoted=None,
                skipped_reason="auto_promote_disabled",
            )
        promoted = await self.skill_library.promote(candidate, audit_run_id=run_id)
        await self.learning_store.mark_promoted(
            learning_candidates or [],
            skill_name=promoted.name,
            skill_version=promoted.version,
            audit_run_id=run_id,
        )
        return LiveSkillUpdate(
            candidate=_skill_payload(candidate),
            duplicate_matches=[],
            promoted=_skill_payload(promoted),
        )


def _default_runtime_factory(worker_url: str, request_timeout: float) -> GameRuntime:
    """Create the default live Mineflayer runtime client."""

    return MineflayerClient(worker_url, request_timeout=request_timeout)


def _retry_task_request(request: TrainingTaskRequest) -> TrainingTaskRequest:
    """Build a retry request that preserves task-local memory across attempts."""

    return TrainingTaskRequest(
        job_id=request.job_id,
        task_id=request.task_id,
        attempt=request.attempt + 1,
        memory_namespace=request.memory_namespace,
        runtime_profile=request.runtime_profile,
    )


def _skill_skip_reason(message: str) -> str:
    """Extract a stable skill skip reason from SkillLibraryError text."""

    marker = ": "
    if "policy skipped run" in message and marker in message:
        return message.rsplit(marker, 1)[-1].strip() or "skill_creation_policy_skipped"
    if "no successful progress actions" in message:
        return "no_promotable_progress_action"
    return "skill_candidate_skipped"


def _report_status(outcomes: list[LiveTrainingOutcome], success_count: int) -> str:
    """Classify aggregate training status without mixing failures and inconclusive runs."""

    if not outcomes:
        return "empty"
    if success_count == len(outcomes):
        return "succeeded"
    inconclusive_statuses = {"model_timeout", "task_timeout", "verification_inconclusive"}
    inconclusive_count = sum(1 for outcome in outcomes if outcome.status in inconclusive_statuses)
    if success_count + inconclusive_count == len(outcomes):
        return "completed_with_inconclusive"
    return "completed_with_failures"


def _reject_catalog_only(task_spec: dict[str, Any]) -> None:
    """Reject catalog-only tasks that do not yet have executable verifier/runtime metadata."""

    minedojo = task_spec.get("minedojo") if isinstance(task_spec.get("minedojo"), dict) else {}
    success_criteria = task_spec.get("success_criteria")
    if minedojo.get("catalog_only") or (
        isinstance(success_criteria, dict) and success_criteria.get("catalog_only")
    ):
        raise ValueError(
            "Catalog-only MineDojo tasks need executable harness manifests before live training."
        )


def _run_state(task_spec: dict[str, Any], result: ExecutionRunResult) -> dict[str, Any]:
    """Build verifier input from an execution result."""

    return _run_state_from_steps(task_spec, result.steps)


def _execution_runtime_failure(result: ExecutionRunResult) -> dict[str, Any] | None:
    """Extract an operational failure that must take priority over task verification."""

    if not result.steps:
        return None
    last_step = result.steps[-1]
    action_result = last_step.action_result
    error_code = str(action_result.get("error_code") or "")
    if result.stop_reason != "action_rpc_timeout" and error_code != "rpc_timeout":
        return None
    worker_health = (
        action_result.get("worker_health")
        if isinstance(action_result.get("worker_health"), dict)
        else {}
    )
    responsive = worker_health.get("responsive") is True
    return {
        "phase": "act",
        "step_index": last_step.step_index,
        "error_type": "ActionRpcTimeout",
        "error_code": "rpc_timeout",
        "message": str(
            action_result.get("message")
            or "Mineflayer worker did not return a definitive action result."
        ),
        "action": last_step.action.model_dump(mode="json"),
        "worker_health": worker_health,
        "requires_worker_restart": bool(action_result.get("requires_worker_restart", True)),
        "worker_responsive": responsive,
    }


def _run_state_from_steps(
    task_spec: dict[str, Any],
    steps: list[ExecutionStepResult],
) -> dict[str, Any]:
    """Build verifier input from completed execution steps."""

    initial_observation = steps[0].observation if steps else {}
    return {
        "task_id": task_spec["task_id"],
        "task_spec": task_spec,
        "initial_observation": initial_observation,
        "initial_inventory": initial_observation.get("inventory", [])
        if isinstance(initial_observation, dict)
        else [],
        "require_inventory_delta": True,
        "steps": [
            {
                "step_index": step.step_index,
                "observation": step.observation,
                "action": step.action.model_dump(mode="json"),
                "action_result": step.action_result,
            }
            for step in steps
        ],
    }


def _verifier_target_items(task_spec: dict[str, Any]) -> list[str]:
    """Extract inventory target item ids from task verifier metadata."""

    verifier = task_spec.get("verifier") or task_spec.get("success_criteria") or {}
    items: set[str] = set()
    _collect_verifier_items(verifier, items)
    return sorted(items)


def _collect_verifier_items(verifier: Any, items: set[str]) -> None:
    """Collect inventory item targets from nested verifier dictionaries."""

    if not isinstance(verifier, dict):
        return
    if verifier.get("type") in {"inventory_contains", "inventory_delta_contains"}:
        item = verifier.get("item") or verifier.get("item_id") or verifier.get("name")
        if isinstance(item, str) and item:
            items.add(item)
    for key in ("all", "any"):
        nested = verifier.get(key)
        if isinstance(nested, list):
            for child in nested:
                _collect_verifier_items(child, items)


def _load_task_memory(
    session_factory: SessionFactory,
    task_id: str,
    namespace: str,
    limit: int,
) -> list[str]:
    """Load recent task-local memory notes for one namespace."""

    with session_factory() as session:
        records = session.scalars(
            select(TaskMemoryRecord)
            .where(
                TaskMemoryRecord.task_id == task_id,
                TaskMemoryRecord.namespace == namespace,
            )
            .order_by(TaskMemoryRecord.id.desc())
            .limit(limit)
        ).all()
    return [record.content for record in reversed(records)]


def _append_failure_memory(
    session_factory: SessionFactory,
    task_id: str,
    namespace: str,
    verifier: dict[str, Any],
    run_id: str,
) -> None:
    """Persist a failed-attempt reflection in the task-local namespace."""

    reason = str(verifier.get("reason") or "unknown failure")
    with session_factory() as session:
        session.add(
            TaskMemoryRecord(
                task_id=task_id,
                namespace=namespace,
                content=f"Run {run_id} failed verifier: {reason}",
                memory_metadata={"run_id": run_id, "verifier": verifier},
            )
        )
        session.commit()


def _outcome(
    *,
    request: TrainingTaskRequest,
    worker: LiveWorkerSpec,
    run_id: str,
    success: bool,
    status: str,
    verifier: dict[str, Any],
    result: ExecutionRunResult | None,
    started_perf: float,
    session_factory: SessionFactory,
    runtime_error: str | None = None,
    skill_update: LiveSkillUpdate | None = None,
    steps: int | None = None,
) -> LiveTrainingOutcome:
    """Build a final live training outcome."""

    completed_steps = (
        steps if steps is not None else (len(result.steps) if result is not None else 0)
    )
    return LiveTrainingOutcome(
        task_id=request.task_id,
        attempt=request.attempt,
        run_id=run_id,
        worker_id=worker.worker_id,
        username=worker.username,
        server_id=worker.server_id,
        memory_namespace=request.memory_namespace,
        success=success,
        status=status,
        verifier=verifier,
        steps=completed_steps,
        duration_sec=time.perf_counter() - started_perf,
        model_usage=_persisted_model_usage(session_factory, run_id),
        stop_reason=result.stop_reason if result is not None else None,
        runtime_error=runtime_error,
        failure_class=_failure_class(
            success=success,
            status=status,
            verifier=verifier,
            result=result,
            runtime_error=runtime_error,
        ),
        skill_update=skill_update,
    )


def _persisted_model_usage(
    session_factory: SessionFactory,
    run_id: str,
) -> LiveModelUsage:
    """Aggregate persisted model-call usage for one attempt without estimating tokens."""

    with session_factory() as session:
        records = session.scalars(
            select(ModelCallRecord).where(ModelCallRecord.run_id == run_id)
        ).all()
    return LiveModelUsage(
        model_call_count=len(records),
        input_tokens=sum(_usage_int(record.usage, "input_tokens") for record in records),
        output_tokens=sum(_usage_int(record.usage, "output_tokens") for record in records),
        total_tokens=sum(_usage_int(record.usage, "total_tokens") for record in records),
    )


def _sum_model_usage(outcomes: list[LiveTrainingOutcome]) -> LiveModelUsage:
    """Sum attempt-level model usage into one batch report total."""

    return LiveModelUsage(
        model_call_count=sum(outcome.model_usage.model_call_count for outcome in outcomes),
        input_tokens=sum(outcome.model_usage.input_tokens for outcome in outcomes),
        output_tokens=sum(outcome.model_usage.output_tokens for outcome in outcomes),
        total_tokens=sum(outcome.model_usage.total_tokens for outcome in outcomes),
    )


def _usage_int(usage: dict[str, Any], key: str) -> int:
    """Return one integer usage field while treating missing provider values as zero."""

    value = usage.get(key)
    return int(value) if isinstance(value, int) else 0


def _failure_class(
    *,
    success: bool,
    status: str,
    verifier: dict[str, Any],
    result: ExecutionRunResult | None,
    runtime_error: str | None,
) -> str | None:
    """Classify final task failures for training reports without hiding raw evidence."""

    if success:
        return None
    last_step = result.steps[-1] if result is not None and result.steps else None
    if last_step is not None:
        action_type = last_step.action.type
        action_result = last_step.action_result
        error_code = str(action_result.get("error_code") or action_result.get("status") or "")
        if error_code == "rpc_timeout":
            worker_health = action_result.get("worker_health")
            if isinstance(worker_health, dict) and worker_health.get("responsive") is False:
                return "worker_unresponsive"
            return "action_rpc_timeout"
        if action_type == "scan_entities" and _empty_result_list(action_result, "entities"):
            return "target_not_found"
        if action_type == "scan_blocks" and _empty_result_list(action_result, "blocks"):
            return "target_not_found"
        if action_type == "move_to" and error_code in {
            "timeout",
            "path_timeout",
            "no_path",
            "path_stopped",
        }:
            return "navigation_timeout" if error_code != "no_path" else "target_unreachable"
        if (
            action_type in {"move_to_and_engage_combat", "engage_combat"}
            and error_code == "target_unreachable"
        ):
            return "target_unreachable"
    if status in {"task_timeout", "model_timeout", "runtime_error", "verification_inconclusive"}:
        return status
    if verifier.get("success") is False:
        return "verifier_failed"
    if runtime_error:
        return "runtime_error"
    return "failed"


def _empty_result_list(action_result: dict[str, Any], field: str) -> bool:
    """Return true when a scan action returned an empty result list."""

    value = action_result.get(field)
    return isinstance(value, list) and not value


def _persisted_step_count(session_factory: SessionFactory, run_id: str) -> int:
    """Count completed action-result steps already persisted for a failed live run."""

    with session_factory() as session:
        count = session.scalar(
            select(func.count()).select_from(StepRecord).where(StepRecord.run_id == run_id)
        )
    return int(count or 0)


def _skill_payload(skill: SkillSpec) -> dict[str, Any]:
    """Return compact skill identity and lifecycle metadata."""

    return {
        "name": skill.name,
        "version": skill.version,
        "status": skill.status.value
        if isinstance(skill.status, SkillStatus)
        else str(skill.status),
        "source_run_id": skill.source_run_id,
    }


def _learning_payload(candidate: LearningCandidateSpec) -> dict[str, Any]:
    """Return compact failure-learning state for the live training report."""

    return {
        "id": candidate.id,
        "signature": candidate.signature,
        "scope_key": candidate.scope_key,
        "kind": candidate.kind.value,
        "status": candidate.status.value,
        "hypothesis": candidate.hypothesis,
        "failure_status": candidate.failure_status,
        "action_type": candidate.action_type,
        "target": candidate.target,
        "support_count": candidate.support_count,
        "recovery_count": candidate.recovery_count,
        "confidence": candidate.confidence,
    }


def _run_id(job_id: str, task_id: str, worker_id: str, attempt: int) -> str:
    """Return a stable-ish run id for one live training attempt."""

    suffix = uuid.uuid4().hex[:8]
    safe_task_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in task_id)
    base = f"{job_id}_{worker_id}_{safe_task_id}_attempt-{attempt}"
    run_id = f"{base}_{suffix}"
    if len(run_id) <= 64:
        return run_id
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:10]
    unique_suffix = f"_{digest}_{suffix}"
    return f"{base[: 64 - len(unique_suffix)]}{unique_suffix}"


def _retryable_outcome(outcome: LiveTrainingOutcome) -> bool:
    """Classify retryable task outcomes while rejecting deterministic setup failures."""

    if outcome.status not in {
        "failed",
        "task_timeout",
        "model_timeout",
        "runtime_error",
        "verification_inconclusive",
    }:
        return False
    message = (outcome.runtime_error or str(outcome.verifier.get("reason") or "")).lower()
    non_retryable_markers = (
        "catalog-only",
        "task manifest not found",
        "unknown model profile",
        "missing base_url",
        "missing api_key",
        "authentication",
        "unauthorized",
        "forbidden",
    )
    return not any(marker in message for marker in non_retryable_markers)


def _worker_recovery_required(outcome: LiveTrainingOutcome) -> bool:
    """Return whether an attempt ended with unknown worker-side action state."""

    return outcome.failure_class in {"action_rpc_timeout", "worker_unresponsive"}


def _final_task_outcomes(
    task_ids: list[str],
    attempt_outcomes: list[LiveTrainingOutcome],
) -> list[LiveTrainingOutcome]:
    """Select one final outcome per task while preserving the requested task order."""

    by_task: dict[str, list[LiveTrainingOutcome]] = {}
    for outcome in attempt_outcomes:
        by_task.setdefault(outcome.task_id, []).append(outcome)
    final: list[LiveTrainingOutcome] = []
    for task_id in task_ids:
        attempts = by_task.get(task_id, [])
        if not attempts:
            continue
        successful = [outcome for outcome in attempts if outcome.success]
        final.append(successful[-1] if successful else attempts[-1])
    return final


def _normalized_task_waves(
    task_ids: list[str],
    configured_waves: tuple[tuple[str, ...], ...],
) -> list[list[str]]:
    """Validate configured task waves or return one unconstrained scheduler wave."""

    if not configured_waves:
        return [list(task_ids)]
    waves = [list(wave) for wave in configured_waves]
    if any(not wave for wave in waves):
        raise ValueError("Configured task waves cannot be empty.")
    flattened = [task_id for wave in waves for task_id in wave]
    if len(flattened) != len(set(flattened)):
        raise ValueError("Configured task waves cannot contain duplicate task ids.")
    if len(flattened) != len(task_ids) or set(flattened) != set(task_ids):
        raise ValueError("Configured task waves must contain every requested task exactly once.")
    return waves


def _utc_now() -> str:
    """Return an ISO UTC timestamp for reports."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
