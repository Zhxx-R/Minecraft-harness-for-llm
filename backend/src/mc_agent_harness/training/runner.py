from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from mc_agent_harness.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkRunner,
    BenchmarkTaskResult,
)
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider


TrainingTaskStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "runtime_crashed",
    "timeout",
    "token_budget_exceeded",
]

TrainingJobStatus = Literal["empty", "succeeded", "completed_with_failures"]


@dataclass(frozen=True, slots=True)
class TrainingBudget:
    """Resource limits shared by every task attempt in one training job."""

    max_steps_per_task: int | None = None
    max_tokens_per_task: int | None = None
    max_runtime_sec_per_task: float | None = None
    worker_concurrency: int = 5

    def __post_init__(self) -> None:
        """Reject invalid budget values before workers are launched."""

        if self.max_steps_per_task is not None and self.max_steps_per_task <= 0:
            raise ValueError("max_steps_per_task must be positive when set.")
        if self.max_tokens_per_task is not None and self.max_tokens_per_task < 0:
            raise ValueError("max_tokens_per_task must be non-negative when set.")
        if self.max_runtime_sec_per_task is not None and self.max_runtime_sec_per_task <= 0:
            raise ValueError("max_runtime_sec_per_task must be positive when set.")
        if self.worker_concurrency <= 0:
            raise ValueError("worker_concurrency must be positive.")


@dataclass(frozen=True, slots=True)
class TrainingJobConfig:
    """Static configuration for one parallel training job."""

    job_id: str = field(default_factory=lambda: f"week9_{uuid.uuid4().hex[:12]}")
    model_profile: str = "scripted-week9"
    runtime_profile: str = "benchmark-minimal"
    seed: int = 20260624
    budget: TrainingBudget = field(default_factory=TrainingBudget)
    queue_backend: str = "memory"
    audit_backend: str = "benchmark-recorder"


@dataclass(frozen=True, slots=True)
class TrainingTaskRequest:
    """Queue item describing one task attempt and its isolated memory namespace."""

    job_id: str
    task_id: str
    attempt: int
    memory_namespace: str
    runtime_profile: str

    @classmethod
    def build(
        cls,
        *,
        job_id: str,
        task_id: str,
        attempt: int,
        runtime_profile: str,
    ) -> TrainingTaskRequest:
        """Create a request with a deterministic task-local memory namespace."""

        namespace = f"{job_id}:{task_id}:attempt-{attempt}"
        return cls(
            job_id=job_id,
            task_id=task_id,
            attempt=attempt,
            memory_namespace=namespace,
            runtime_profile=runtime_profile,
        )

    def queue_key(self) -> str:
        """Return a stable key for queue state maps and Redis hashes."""

        return f"{self.task_id}#{self.attempt}"


@dataclass(frozen=True, slots=True)
class TrainingTaskOutcome:
    """Final result and audit metrics for one training task attempt."""

    task_id: str
    run_id: str | None
    memory_namespace: str
    attempt: int
    status: TrainingTaskStatus
    success: bool
    verifier: dict[str, Any]
    steps: int
    duration_sec: float
    invalid_action_count: int
    runtime_error_count: int
    runtime_crashed: bool
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    error: str | None = None

    @classmethod
    def from_benchmark(
        cls,
        *,
        request: TrainingTaskRequest,
        result: BenchmarkTaskResult,
        token_budget_exceeded: bool,
    ) -> TrainingTaskOutcome:
        """Convert a deterministic benchmark task result into a training outcome."""

        if token_budget_exceeded:
            status: TrainingTaskStatus = "token_budget_exceeded"
            success = False
        elif result.success:
            status = "succeeded"
            success = True
        elif result.runtime_crashed:
            status = "runtime_crashed"
            success = False
        else:
            status = "failed"
            success = False

        return cls(
            task_id=request.task_id,
            run_id=result.run_id,
            memory_namespace=request.memory_namespace,
            attempt=request.attempt,
            status=status,
            success=success,
            verifier=result.verifier,
            steps=result.steps,
            duration_sec=result.duration_sec,
            invalid_action_count=result.invalid_action_count,
            runtime_error_count=result.runtime_error_count,
            runtime_crashed=result.runtime_crashed,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            estimated_cost=result.estimated_cost,
            error=result.error,
        )

    @classmethod
    def timed_out(
        cls,
        *,
        request: TrainingTaskRequest,
        duration_sec: float,
        timeout_sec: float,
    ) -> TrainingTaskOutcome:
        """Build an outcome for a task attempt stopped by runtime budget."""

        return cls(
            task_id=request.task_id,
            run_id=None,
            memory_namespace=request.memory_namespace,
            attempt=request.attempt,
            status="timeout",
            success=False,
            verifier={
                "success": False,
                "reason": f"Exceeded {timeout_sec:.3f}s runtime budget.",
                "checks": [],
            },
            steps=0,
            duration_sec=duration_sec,
            invalid_action_count=0,
            runtime_error_count=0,
            runtime_crashed=False,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            estimated_cost=0.0,
            error=f"Task exceeded max_runtime_sec_per_task={timeout_sec:.3f}.",
        )

    @classmethod
    def failed_before_run(
        cls,
        *,
        request: TrainingTaskRequest,
        error: Exception,
        duration_sec: float,
    ) -> TrainingTaskOutcome:
        """Build an outcome for task loading or scheduler-level failures."""

        message = f"{type(error).__name__}: {error}"
        return cls(
            task_id=request.task_id,
            run_id=None,
            memory_namespace=request.memory_namespace,
            attempt=request.attempt,
            status="failed",
            success=False,
            verifier={"success": False, "reason": message, "checks": []},
            steps=0,
            duration_sec=duration_sec,
            invalid_action_count=0,
            runtime_error_count=0,
            runtime_crashed=False,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            estimated_cost=0.0,
            error=message,
        )


@dataclass(frozen=True, slots=True)
class TrainingQueueState:
    """Auditable queue state for one task attempt."""

    task_id: str
    attempt: int
    memory_namespace: str
    status: TrainingTaskStatus
    worker_id: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TrainingJobReport:
    """Aggregate training report exported as JSON and Markdown."""

    job_id: str
    status: TrainingJobStatus
    model_profile: str
    runtime_profile: str
    queue_backend: str
    audit_backend: str
    seed: int
    started_at: str
    finished_at: str
    duration_sec: float
    task_count: int
    success_count: int
    success_rate: float
    total_steps: int
    total_tokens: int
    estimated_cost: float
    max_observed_concurrency: int
    outcomes: list[TrainingTaskOutcome]
    queue_states: list[TrainingQueueState]


class TrainingQueue(Protocol):
    """Queue contract used by the training runner and future distributed workers."""

    backend_name: str

    async def enqueue(self, request: TrainingTaskRequest) -> None:
        """Store one task request for a worker."""

    async def close_for_workers(self, worker_count: int) -> None:
        """Push worker sentinels after all tasks are enqueued."""

    async def get(self) -> TrainingTaskRequest | None:
        """Return the next task request, or None when a worker should stop."""

    async def task_done(self) -> None:
        """Acknowledge that a worker has finished the current queue item."""

    async def mark_started(self, request: TrainingTaskRequest, worker_id: str) -> None:
        """Mark a task request as running."""

    async def mark_finished(
        self,
        request: TrainingTaskRequest,
        outcome: TrainingTaskOutcome,
    ) -> None:
        """Mark a task request as finished with its terminal status."""

    def snapshot_states(self) -> list[TrainingQueueState]:
        """Return queue states suitable for report export."""

    @property
    def max_observed_concurrency(self) -> int:
        """Return the highest number of simultaneously running workers."""


class InMemoryTrainingQueue:
    """Asyncio queue implementation for local CI and single-process training."""

    backend_name = "memory"

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TrainingTaskRequest | None] = asyncio.Queue()
        self._states: dict[str, TrainingQueueState] = {}
        self._active_count = 0
        self._max_observed_concurrency = 0
        self._lock = asyncio.Lock()

    async def enqueue(self, request: TrainingTaskRequest) -> None:
        """Put a task request onto the local queue and record its queued state."""

        now = _utc_now()
        async with self._lock:
            self._states[request.queue_key()] = TrainingQueueState(
                task_id=request.task_id,
                attempt=request.attempt,
                memory_namespace=request.memory_namespace,
                status="queued",
                queued_at=now,
            )
        await self._queue.put(request)

    async def close_for_workers(self, worker_count: int) -> None:
        """Add one stop sentinel per worker."""

        for _ in range(worker_count):
            await self._queue.put(None)

    async def get(self) -> TrainingTaskRequest | None:
        """Return the next local queue item."""

        return await self._queue.get()

    async def task_done(self) -> None:
        """Acknowledge one local queue item."""

        self._queue.task_done()

    async def mark_started(self, request: TrainingTaskRequest, worker_id: str) -> None:
        """Move a queued item into running state."""

        now = _utc_now()
        async with self._lock:
            current = self._states.get(request.queue_key())
            self._states[request.queue_key()] = replace(
                current or _queued_state(request),
                status="running",
                worker_id=worker_id,
                started_at=now,
            )
            self._active_count += 1
            self._max_observed_concurrency = max(
                self._max_observed_concurrency,
                self._active_count,
            )

    async def mark_finished(
        self,
        request: TrainingTaskRequest,
        outcome: TrainingTaskOutcome,
    ) -> None:
        """Move a running item into its terminal state."""

        now = _utc_now()
        async with self._lock:
            current = self._states.get(request.queue_key())
            self._states[request.queue_key()] = replace(
                current or _queued_state(request),
                status=outcome.status,
                finished_at=now,
                error=outcome.error,
            )
            self._active_count = max(0, self._active_count - 1)

    def snapshot_states(self) -> list[TrainingQueueState]:
        """Return queue states sorted by task id and attempt."""

        return sorted(self._states.values(), key=lambda state: (state.task_id, state.attempt))

    @property
    def max_observed_concurrency(self) -> int:
        """Return the highest local worker concurrency observed."""

        return self._max_observed_concurrency


class RedisTrainingQueue(InMemoryTrainingQueue):
    """Redis-backed queue adapter with an in-memory mirror for local report export."""

    backend_name = "redis"

    def __init__(self, *, redis_url: str, job_id: str) -> None:
        super().__init__()
        self.redis_url = redis_url
        self.job_id = job_id
        self.queue_key = f"mc-agent-harness:training:{job_id}:queue"
        self.state_key = f"mc-agent-harness:training:{job_id}:states"
        self._redis: Any | None = None

    async def _client(self) -> Any:
        """Create the Redis asyncio client lazily."""

        if self._redis is None:
            from redis.asyncio import from_url

            self._redis = from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def enqueue(self, request: TrainingTaskRequest) -> None:
        """Push a task request into Redis and mirror state locally."""

        await super().enqueue(request)
        redis = await self._client()
        await redis.rpush(
            self.queue_key,
            json.dumps({"type": "task", "request": asdict(request)}, sort_keys=True),
        )
        await redis.hset(
            self.state_key,
            request.queue_key(),
            json.dumps(asdict(_queued_state(request)), sort_keys=True),
        )

    async def close_for_workers(self, worker_count: int) -> None:
        """Push one Redis stop sentinel per worker."""

        redis = await self._client()
        for _ in range(worker_count):
            await redis.rpush(self.queue_key, json.dumps({"type": "stop"}, sort_keys=True))

    async def get(self) -> TrainingTaskRequest | None:
        """Pop the next Redis queue item."""

        redis = await self._client()
        _, payload = await redis.blpop(self.queue_key, timeout=0)
        item = json.loads(payload)
        if item.get("type") == "stop":
            return None
        request = item.get("request")
        if not isinstance(request, dict):
            raise ValueError("Redis training queue item is missing request payload.")
        return TrainingTaskRequest(**request)

    async def task_done(self) -> None:
        """Acknowledge a Redis queue item.

        Redis lists do not need local task_done bookkeeping, so this is intentionally empty.
        """

    async def mark_started(self, request: TrainingTaskRequest, worker_id: str) -> None:
        """Mirror running state locally and in Redis."""

        await super().mark_started(request, worker_id)
        redis = await self._client()
        state = _find_state(self.snapshot_states(), request)
        await redis.hset(
            self.state_key,
            request.queue_key(),
            json.dumps(asdict(state), sort_keys=True),
        )

    async def mark_finished(
        self,
        request: TrainingTaskRequest,
        outcome: TrainingTaskOutcome,
    ) -> None:
        """Mirror terminal state locally and in Redis."""

        await super().mark_finished(request, outcome)
        redis = await self._client()
        state = _find_state(self.snapshot_states(), request)
        await redis.hset(
            self.state_key,
            request.queue_key(),
            json.dumps(asdict(state), sort_keys=True),
        )


class TrainingRunner:
    """Runs MineDojo-derived tasks in parallel with isolated task memory namespaces."""

    def __init__(
        self,
        task_provider: MineDojoTaskProvider,
        config: TrainingJobConfig | None = None,
        queue: TrainingQueue | None = None,
    ) -> None:
        self.task_provider = task_provider
        self.config = config or TrainingJobConfig()
        self.queue = queue or InMemoryTrainingQueue()

    async def run(self, task_ids: list[str] | None = None) -> TrainingJobReport:
        """Run selected tasks concurrently and return an auditable report."""

        selected_task_ids = await self._select_task_ids(task_ids)
        started_at = _utc_now()
        started_perf = time.perf_counter()
        requests = [
            TrainingTaskRequest.build(
                job_id=self.config.job_id,
                task_id=task_id,
                attempt=1,
                runtime_profile=self.config.runtime_profile,
            )
            for task_id in selected_task_ids
        ]
        worker_count = min(self.config.budget.worker_concurrency, len(requests)) if requests else 0

        for request in requests:
            await self.queue.enqueue(request)
        if worker_count:
            await self.queue.close_for_workers(worker_count)

        worker_results = await asyncio.gather(
            *[self._worker(f"worker-{index + 1}") for index in range(worker_count)]
        )
        outcomes = [outcome for results in worker_results for outcome in results]
        outcomes.sort(key=lambda outcome: selected_task_ids.index(outcome.task_id))

        finished_at = _utc_now()
        duration = time.perf_counter() - started_perf
        success_count = sum(1 for outcome in outcomes if outcome.success)
        task_count = len(outcomes)
        status: TrainingJobStatus
        if not outcomes:
            status = "empty"
        elif success_count == task_count:
            status = "succeeded"
        else:
            status = "completed_with_failures"

        return TrainingJobReport(
            job_id=self.config.job_id,
            status=status,
            model_profile=self.config.model_profile,
            runtime_profile=self.config.runtime_profile,
            queue_backend=self.queue.backend_name,
            audit_backend=self.config.audit_backend,
            seed=self.config.seed,
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=duration,
            task_count=task_count,
            success_count=success_count,
            success_rate=(success_count / task_count) if task_count else 0.0,
            total_steps=sum(outcome.steps for outcome in outcomes),
            total_tokens=sum(outcome.total_tokens for outcome in outcomes),
            estimated_cost=sum(outcome.estimated_cost for outcome in outcomes),
            max_observed_concurrency=self.queue.max_observed_concurrency,
            outcomes=outcomes,
            queue_states=self.queue.snapshot_states(),
        )

    async def _select_task_ids(self, task_ids: list[str] | None) -> list[str]:
        """Select explicit task ids or every manifest task from the provider."""

        if task_ids is not None:
            return list(task_ids)
        summaries = await self.task_provider.list_tasks()
        return [str(summary["task_id"]) for summary in summaries]

    async def _worker(self, worker_id: str) -> list[TrainingTaskOutcome]:
        """Drain queue items until a stop sentinel is received."""

        outcomes: list[TrainingTaskOutcome] = []
        while True:
            request = await self.queue.get()
            try:
                if request is None:
                    return outcomes
                await self.queue.mark_started(request, worker_id)
                await asyncio.sleep(0)
                outcome = await self._run_one(request)
                await self.queue.mark_finished(request, outcome)
                outcomes.append(outcome)
            finally:
                await self.queue.task_done()

    async def _run_one(self, request: TrainingTaskRequest) -> TrainingTaskOutcome:
        """Run one task through the benchmark harness under Week 9 budgets."""

        started = time.perf_counter()
        try:
            task_spec = await self.task_provider.load_task(request.task_id)
            task_spec = {
                **task_spec,
                "training": {
                    "job_id": request.job_id,
                    "attempt": request.attempt,
                    "memory_namespace": request.memory_namespace,
                    "queue_backend": self.queue.backend_name,
                },
            }
            runner = BenchmarkRunner(
                self.task_provider,
                BenchmarkConfig(
                    model_profile=self.config.model_profile,
                    runtime_profile=request.runtime_profile,
                    seed=self.config.seed,
                    max_steps=self.config.budget.max_steps_per_task,
                ),
            )
            task_coro = runner.run_task(task_spec)
            if self.config.budget.max_runtime_sec_per_task is None:
                result = await task_coro
            else:
                result = await asyncio.wait_for(
                    task_coro,
                    timeout=self.config.budget.max_runtime_sec_per_task,
                )
        except asyncio.TimeoutError:
            return TrainingTaskOutcome.timed_out(
                request=request,
                duration_sec=time.perf_counter() - started,
                timeout_sec=float(self.config.budget.max_runtime_sec_per_task or 0.0),
            )
        except Exception as exc:  # noqa: BLE001 - scheduler reports must capture failed attempts.
            return TrainingTaskOutcome.failed_before_run(
                request=request,
                error=exc,
                duration_sec=time.perf_counter() - started,
            )

        token_budget = self.config.budget.max_tokens_per_task
        token_budget_exceeded = token_budget is not None and result.total_tokens > token_budget
        return TrainingTaskOutcome.from_benchmark(
            request=request,
            result=result,
            token_budget_exceeded=token_budget_exceeded,
        )


def write_training_report(report: TrainingJobReport, output_dir: Path) -> tuple[Path, Path]:
    """Write JSON and Markdown training reports to an output directory."""

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report.job_id}.json"
    markdown_path = output_dir / f"{report.job_id}.md"
    json_path.write_text(
        json.dumps(_report_to_json(report), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    markdown_path.write_text(_report_to_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _report_to_json(report: TrainingJobReport) -> dict[str, Any]:
    """Convert the training report dataclass into a JSON-safe dictionary."""

    return asdict(report)


def _report_to_markdown(report: TrainingJobReport) -> str:
    """Render a compact training report for manual review."""

    lines = [
        f"# Week 9 Training Report `{report.job_id}`",
        "",
        f"- Status: `{report.status}`",
        f"- Model profile: `{report.model_profile}`",
        f"- Runtime profile: `{report.runtime_profile}`",
        f"- Queue backend: `{report.queue_backend}`",
        f"- Audit backend: `{report.audit_backend}`",
        f"- Seed: `{report.seed}`",
        f"- Tasks: {report.task_count}",
        f"- Success: {report.success_count}/{report.task_count} ({report.success_rate:.1%})",
        f"- Max observed concurrency: {report.max_observed_concurrency}",
        f"- Steps: {report.total_steps}",
        f"- Tokens: {report.total_tokens}",
        f"- Estimated cost: {report.estimated_cost:.6f}",
        "",
        "| Task | Namespace | Status | Success | Steps | Tokens | Error |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for outcome in report.outcomes:
        error = (outcome.error or outcome.verifier.get("reason") or "").replace("|", "\\|")
        lines.append(
            f"| `{outcome.task_id}` | `{outcome.memory_namespace}` | `{outcome.status}` | "
            f"{outcome.success} | {outcome.steps} | {outcome.total_tokens} | {error} |"
        )
    lines.append("")
    return "\n".join(lines)


def _queued_state(request: TrainingTaskRequest) -> TrainingQueueState:
    """Build the initial auditable queue state for a request."""

    return TrainingQueueState(
        task_id=request.task_id,
        attempt=request.attempt,
        memory_namespace=request.memory_namespace,
        status="queued",
        queued_at=_utc_now(),
    )


def _find_state(
    states: list[TrainingQueueState],
    request: TrainingTaskRequest,
) -> TrainingQueueState:
    """Find the mirrored queue state for a request."""

    return next(
        state
        for state in states
        if state.task_id == request.task_id and state.attempt == request.attempt
    )


def _utc_now() -> str:
    """Return a compact UTC timestamp for report and queue state metadata."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
