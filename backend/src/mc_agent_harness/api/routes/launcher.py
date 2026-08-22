from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from mc_agent_harness.core.config import PROJECT_ROOT, settings
from mc_agent_harness.launcher.catalog import ExecutableTaskCatalog
from mc_agent_harness.launcher.service import (
    LaunchConfiguration,
    LaunchConflictError,
    LaunchPreflightError,
    QuickStartService,
)


router = APIRouter(prefix="/launcher", tags=["launcher"])
_CONTROL_HEADER = "local-dashboard-v1"
_catalog = ExecutableTaskCatalog(
    PROJECT_ROOT / "tasks" / "executable" / "minedojo_programmatic_tasks.jsonl",
    PROJECT_ROOT / "tasks" / "executable" / "minedojo_creative_tasks.jsonl",
)
_launcher = QuickStartService(settings)


class LaunchRequest(BaseModel):
    """Validated quick-start settings accepted from the local dashboard."""

    task_id: str = Field(min_length=1, max_length=300)
    view_mode: Literal["agent", "player"] = "agent"
    client_player: str = Field(min_length=1, max_length=64)
    server_host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    server_port: int = Field(default=25565, ge=1, le=65535)
    rcon_host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    rcon_port: int = Field(default=25575, ge=1, le=65535)
    max_steps: int = Field(default=40, ge=1, le=300)
    max_runtime_sec: int = Field(default=900, ge=30, le=7200)
    threat_pause: bool = True
    random_spawn: bool = True
    auto_promote: bool = False

    @field_validator("task_id", "client_player", "server_host", "rcon_host")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """Reject values that only contain whitespace."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    def to_configuration(self) -> LaunchConfiguration:
        """Map the API model onto the launcher's immutable configuration object."""

        return LaunchConfiguration(
            view_mode=self.view_mode,
            client_player=self.client_player,
            server_host=self.server_host,
            server_port=self.server_port,
            rcon_host=self.rcon_host,
            rcon_port=self.rcon_port,
            max_steps=self.max_steps,
            max_runtime_sec=self.max_runtime_sec,
            threat_pause=self.threat_pause,
            random_spawn=self.random_spawn,
            auto_promote=self.auto_promote,
        )


class LaunchPreflightRequest(LaunchRequest):
    """Quick-start request used for readiness checks without process creation."""


class LaunchTaskPage(BaseModel):
    """Paginated task catalog plus stable filter counts."""

    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int
    categories: dict[str, int]
    kinds: dict[str, int]


class LaunchJobLog(BaseModel):
    """One bounded incremental launcher log segment."""

    content: str
    next_offset: int
    complete: bool


def get_task_catalog() -> ExecutableTaskCatalog:
    """Return the process-wide immutable executable task catalog."""

    return _catalog


def get_quick_start_service() -> QuickStartService:
    """Return the process-wide launcher that enforces single-job ownership."""

    return _launcher


def require_local_control(
    x_harness_control: str | None = Header(default=None),
) -> None:
    """Require an explicit local-dashboard header for state-changing controls."""

    if x_harness_control != _CONTROL_HEADER:
        raise HTTPException(status_code=403, detail="local_control_header_required")


@router.get("/config")
def get_launcher_config(
    service: QuickStartService = Depends(get_quick_start_service),
) -> dict[str, Any]:
    """Return safe frontend defaults without exposing runtime credentials."""

    return service.public_config()


@router.get("/tasks", response_model=LaunchTaskPage)
def list_launcher_tasks(
    q: str = Query(default="", max_length=200),
    kind: Literal["all", "programmatic", "creative"] = "all",
    category: str | None = Query(default=None, max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=40, ge=1, le=100),
    catalog: ExecutableTaskCatalog = Depends(get_task_catalog),
) -> LaunchTaskPage:
    """Search and paginate the fixed executable MineDojo snapshots."""

    tasks, total, categories, kinds = catalog.list_tasks(
        query=q,
        kind=kind,
        category=category,
        offset=offset,
        limit=limit,
    )
    return LaunchTaskPage(
        items=[task.summary() for task in tasks],
        total=total,
        offset=offset,
        limit=limit,
        categories=categories,
        kinds=kinds,
    )


@router.get("/tasks/random")
def get_random_launcher_task(
    q: str = Query(default="", max_length=200),
    kind: Literal["all", "programmatic", "creative"] = "all",
    category: str | None = Query(default=None, max_length=100),
    catalog: ExecutableTaskCatalog = Depends(get_task_catalog),
) -> dict[str, Any]:
    """Draw one server-selected task from the currently filtered catalog."""

    try:
        return catalog.random_task(query=q, kind=kind, category=category).summary()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tasks/{task_id}")
def get_launcher_task(
    task_id: str,
    catalog: ExecutableTaskCatalog = Depends(get_task_catalog),
) -> dict[str, Any]:
    """Return complete launch-visible configuration for one trusted task."""

    try:
        return catalog.get(task_id).detail()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc


@router.post("/preflight")
async def preflight_launcher_task(
    request: LaunchPreflightRequest,
    catalog: ExecutableTaskCatalog = Depends(get_task_catalog),
    service: QuickStartService = Depends(get_quick_start_service),
) -> dict[str, Any]:
    """Collect ready, pending, and blocked launch evidence without creating a job."""

    try:
        task = catalog.get(request.task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    return (await service.preflight(task, request.to_configuration())).to_json()


@router.post("/jobs", dependencies=[Depends(require_local_control)])
async def start_launcher_job(
    request: LaunchRequest,
    catalog: ExecutableTaskCatalog = Depends(get_task_catalog),
    service: QuickStartService = Depends(get_quick_start_service),
) -> dict[str, Any]:
    """Start one allowlisted workflow, including managed server and client waiting."""

    try:
        task = catalog.get(request.task_id)
        job, preflight = await service.start_job(task, request.to_configuration())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    except LaunchConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LaunchPreflightError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "preflight_failed",
                "preflight": exc.preflight.to_json(),
            },
        ) from exc
    return {"job": job.to_json(), "preflight": preflight.to_json()}


@router.get("/jobs")
def list_launcher_jobs(
    service: QuickStartService = Depends(get_quick_start_service),
) -> list[dict[str, Any]]:
    """Return launcher jobs known by the current backend process."""

    return [job.to_json() for job in service.list_jobs()]


@router.get("/jobs/{job_id}")
def get_launcher_job(
    job_id: str,
    service: QuickStartService = Depends(get_quick_start_service),
) -> dict[str, Any]:
    """Return current state for one quick-start child process."""

    try:
        return service.get_job(job_id).to_json()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="launch_job_not_found") from exc


@router.get("/jobs/{job_id}/logs", response_model=LaunchJobLog)
def get_launcher_job_logs(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    service: QuickStartService = Depends(get_quick_start_service),
) -> LaunchJobLog:
    """Read one incremental launcher log segment for live progress display."""

    try:
        content, next_offset, complete = service.read_logs(job_id, offset=offset)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="launch_job_not_found") from exc
    return LaunchJobLog(
        content=content,
        next_offset=next_offset,
        complete=complete,
    )


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_local_control)])
def cancel_launcher_job(
    job_id: str,
    service: QuickStartService = Depends(get_quick_start_service),
) -> dict[str, Any]:
    """Cancel one active launcher-owned process group."""

    try:
        return service.cancel_job(job_id).to_json()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="launch_job_not_found") from exc
