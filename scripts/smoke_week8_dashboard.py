from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from mc_agent_harness.db.models import (  # noqa: E402
    Base,
    ModelCallRecord,
    RunRecord,
    RuntimeErrorRecord,
    SkillRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.schemas.action import HarnessAction  # noqa: E402
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus  # noqa: E402


SMOKE_RUN_ID = "week8_smoke_run"


@dataclass(frozen=True, slots=True)
class SmokeCheck:
    """One named smoke check and its pass/fail details."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class SmokeReport:
    """JSON-serializable summary for one Week 8 dashboard smoke run."""

    ok: bool
    backend_url: str
    frontend_url: str | None
    database_url: str
    checks: list[SmokeCheck] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)
    report_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Convert the smoke report into a JSON-safe dictionary."""

        return {
            "ok": self.ok,
            "backend_url": self.backend_url,
            "frontend_url": self.frontend_url,
            "database_url": self.database_url,
            "checks": [_check_to_json(check) for check in self.checks],
            "responses": self.responses,
            "report_path": self.report_path,
        }


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the Week 8 dashboard smoke test."""

    parser = argparse.ArgumentParser(description="Run an automated Week 8 dashboard smoke test.")
    parser.add_argument(
        "--with-frontend",
        action="store_true",
        help="Also start Vite and verify the dashboard HTML is reachable.",
    )
    parser.add_argument(
        "--keep-services",
        action="store_true",
        help="Leave temporary backend/frontend services running after the smoke test.",
    )
    parser.add_argument("--backend-port", type=int, default=0, help="Backend port; 0 chooses a free port.")
    parser.add_argument("--frontend-port", type=int, default=0, help="Frontend port; 0 chooses a free port.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON report path. Defaults to runs/week8_dashboard_smoke_<timestamp>.json.",
    )
    return parser.parse_args()


def main() -> None:
    """Run the Week 8 dashboard smoke test and print a compact result summary."""

    args = parse_args()
    with tempfile.TemporaryDirectory(prefix="mc-agent-week8-smoke-") as tmp_dir:
        database_path = Path(tmp_dir) / "week8_smoke.sqlite3"
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        _seed_database(database_url)

        backend_port = args.backend_port or _free_port()
        frontend_port = args.frontend_port or _free_port()
        backend_url = f"http://127.0.0.1:{backend_port}"
        frontend_url = f"http://127.0.0.1:{frontend_port}" if args.with_frontend else None
        backend_process: subprocess.Popen[str] | None = None
        frontend_process: subprocess.Popen[str] | None = None
        report = SmokeReport(
            ok=False,
            backend_url=backend_url,
            frontend_url=frontend_url,
            database_url=database_url,
        )

        try:
            backend_process = _start_backend(database_url, backend_port)
            _wait_for_json(f"{backend_url}/api/health", timeout_sec=20)
            if args.with_frontend:
                frontend_process = _start_frontend(backend_url, frontend_port)
                _wait_for_text(f"{frontend_url}/", timeout_sec=20)

            _run_checks(report, backend_url, frontend_url)
            report.ok = all(check.ok for check in report.checks)
        finally:
            output_path = args.output or _default_report_path()
            report.report_path = str(output_path)
            _write_report(output_path, report)
            if not args.keep_services:
                _terminate_process(frontend_process)
                _terminate_process(backend_process)

        print(json.dumps(_summary(report), indent=2, sort_keys=True))
        if not report.ok:
            raise SystemExit(1)


def _seed_database(database_url: str) -> None:
    """Create a temporary SQLite database with representative Week 8 audit rows."""

    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    action = HarnessAction(type="dig_block_at", args={"block": "oak_log", "position": {"x": 1, "y": 64, "z": 1}})
    skill = SkillSpec(
        name="collect_wood",
        version="0.1.0",
        description="Collect starter oak logs.",
        triggers=["oak_log", "wood"],
        action_plan=[action],
        source_run_id=SMOKE_RUN_ID,
        status=SkillStatus.draft,
    )
    with session_factory() as session:
        _insert_run(session, action, skill)
        session.commit()


def _insert_run(session: Session, action: HarnessAction, skill: SkillSpec) -> None:
    """Insert one run with trajectory, model, runtime-error, and skill review rows."""

    started_at = datetime.now(tz=UTC)
    session.add(
        RunRecord(
            id=SMOKE_RUN_ID,
            task_id="week8_dashboard_smoke",
            status="completed",
            task_spec={
                "task_id": "week8_dashboard_smoke",
                "goal": "Validate Week 8 dashboard audit and replay views.",
                "allowed_actions": ["query_inventory", "scan_blocks", "move_to", "dig_block_at", "wait_ticks"],
            },
            started_at=started_at,
            finished_at=started_at,
        )
    )
    session.add(
        StepRecord(
            run_id=SMOKE_RUN_ID,
            step_index=0,
            observation={"inventory": [], "nearby_blocks": [{"name": "oak_log"}]},
            action=action.model_dump(mode="json"),
            action_result={"ok": True, "inventory_delta": {"oak_log": 1}},
        )
    )
    for event_type, payload in (
        ("run_started", {"task_id": "week8_dashboard_smoke"}),
        (
            "observation",
            {"step_index": 0, "observation": {"inventory": [], "nearby_blocks": [{"name": "oak_log"}]}},
        ),
        (
            "context_built",
            {
                "step_index": 0,
                "resolved_terms": ["oak_log"],
                "retrieved_docs": ["minecraft.blocks.oak_log"],
                "retrieved_skills": [{"name": "collect_wood", "version": "0.1.0"}],
            },
        ),
        (
            "model_action",
            {
                "step_index": 0,
                "raw_content": '{"type":"dig_block_at","args":{"block":"oak_log","position":{"x":1,"y":64,"z":1}}}',
                "action": action.model_dump(mode="json"),
                "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
                "source": "smoke",
            },
        ),
        (
            "action_result",
            {
                "step_index": 0,
                "action": action.model_dump(mode="json"),
                "result": {"ok": True, "inventory_delta": {"oak_log": 1}},
            },
        ),
        (
            "runtime_error",
            {"step_index": 1, "error_type": "worker_timeout", "message": "synthetic timeout"},
        ),
        ("run_finished", {"task_id": "week8_dashboard_smoke", "steps": 1, "terminated": False}),
    ):
        session.add(TrajectoryEventRecord(run_id=SMOKE_RUN_ID, event_type=event_type, payload=payload))

    session.add(
        ModelCallRecord(
            run_id=SMOKE_RUN_ID,
            step_index=0,
            raw_content='{"type":"dig_block_at","args":{"block":"oak_log","position":{"x":1,"y":64,"z":1}}}',
            action=action.model_dump(mode="json"),
            usage={"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
            raw_response={"source": "smoke"},
            source="smoke",
        )
    )
    session.add(
        RuntimeErrorRecord(
            run_id=SMOKE_RUN_ID,
            step_index=1,
            error_type="worker_timeout",
            message="synthetic timeout",
            payload={"step_index": 1, "timeout_ms": 1000},
        )
    )
    session.add(
        SkillRecord(
            name=skill.name,
            version=skill.version,
            status=skill.status.value,
            spec=skill.model_dump(mode="json"),
            source_run_id=SMOKE_RUN_ID,
        )
    )


def _start_backend(database_url: str, port: int) -> subprocess.Popen[str]:
    """Start a temporary FastAPI backend process bound to a free local port."""

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(BACKEND_SRC)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mc_agent_harness.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT / "backend",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _start_frontend(backend_url: str, port: int) -> subprocess.Popen[str]:
    """Start a temporary Vite dashboard process bound to a free local port."""

    env = os.environ.copy()
    env["VITE_API_BASE_URL"] = backend_url
    return subprocess.Popen(
        ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
        cwd=ROOT / "frontend",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run_checks(report: SmokeReport, backend_url: str, frontend_url: str | None) -> None:
    """Execute API and optional frontend smoke checks against temporary services."""

    checks: list[tuple[str, Callable[[], str]]] = [
        ("health", lambda: _expect_field(_get_json(f"{backend_url}/api/health"), "status", "ok")),
        ("runs", lambda: _check_runs(report, backend_url)),
        ("run_detail", lambda: _check_run_detail(report, backend_url)),
        ("events", lambda: _check_events(report, backend_url)),
        ("model_calls", lambda: _check_model_calls(report, backend_url)),
        ("runtime_errors", lambda: _check_runtime_errors(report, backend_url)),
        ("replay", lambda: _check_replay(report, backend_url)),
        ("skills", lambda: _check_skills(report, backend_url)),
        ("skill_review", lambda: _check_skill_review(report, backend_url)),
        ("benchmark_comparison", lambda: _check_comparison(report, backend_url)),
    ]
    if frontend_url is not None:
        checks.append(("frontend", lambda: _check_frontend(frontend_url)))

    for name, check in checks:
        try:
            report.checks.append(SmokeCheck(name=name, ok=True, detail=check()))
        except Exception as exc:  # noqa: BLE001 - smoke runner should report every check failure.
            report.checks.append(SmokeCheck(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}"))


def _check_runs(report: SmokeReport, backend_url: str) -> str:
    """Verify the run list exposes audit counts for the seeded run."""

    payload = _get_json(f"{backend_url}/api/runs")
    report.responses["runs"] = payload
    run = _find_by_id(payload, SMOKE_RUN_ID)
    assert run["event_count"] >= 7
    assert run["model_call_count"] == 1
    assert run["runtime_error_count"] == 1
    return "seeded run is listed with audit counts"


def _check_run_detail(report: SmokeReport, backend_url: str) -> str:
    """Verify the run detail endpoint returns task metadata."""

    payload = _get_json(f"{backend_url}/api/runs/{SMOKE_RUN_ID}")
    report.responses["run_detail"] = payload
    assert payload["task_id"] == "week8_dashboard_smoke"
    assert payload["status"] == "completed"
    return "run detail is available"


def _check_events(report: SmokeReport, backend_url: str) -> str:
    """Verify the timeline endpoint returns expected event types."""

    payload = _get_json(f"{backend_url}/api/runs/{SMOKE_RUN_ID}/events")
    report.responses["events"] = payload
    event_types = {event["event_type"] for event in payload}
    assert {"run_started", "context_built", "model_action", "action_result", "runtime_error"} <= event_types
    return "timeline contains core audit events"


def _check_model_calls(report: SmokeReport, backend_url: str) -> str:
    """Verify the model-call endpoint returns parsed action and usage."""

    payload = _get_json(f"{backend_url}/api/runs/{SMOKE_RUN_ID}/model-calls")
    report.responses["model_calls"] = payload
    assert payload[0]["action"]["type"] == "dig_block_at"
    assert payload[0]["usage"]["total_tokens"] == 18
    return "model call includes action and usage"


def _check_runtime_errors(report: SmokeReport, backend_url: str) -> str:
    """Verify runtime errors are exposed separately from the raw timeline."""

    payload = _get_json(f"{backend_url}/api/runs/{SMOKE_RUN_ID}/runtime-errors")
    report.responses["runtime_errors"] = payload
    assert payload[0]["error_type"] == "worker_timeout"
    return "runtime error endpoint exposes worker timeout"


def _check_replay(report: SmokeReport, backend_url: str) -> str:
    """Verify the Week 8.5 replay evidence-chain endpoint."""

    payload = _get_json(f"{backend_url}/api/runs/{SMOKE_RUN_ID}/replay")
    report.responses["replay"] = payload
    assert payload["summary"]["step_count"] == 2
    first_step = payload["steps"][0]
    assert first_step["status"] == "ok"
    assert first_step["parsed_action"]["type"] == "dig_block_at"
    assert first_step["resolved_terms"] == ["oak_log"]
    assert first_step["model_calls"][0]["source"] == "smoke"
    assert payload["steps"][1]["status"] == "error"
    return "replay groups observation-context-model-action-result evidence"


def _check_skills(report: SmokeReport, backend_url: str) -> str:
    """Verify the skill review list exposes the seeded draft skill."""

    payload = _get_json(f"{backend_url}/api/skills")
    report.responses["skills"] = payload
    skill = next(item for item in payload if item["name"] == "collect_wood")
    assert skill["status"] == "draft"
    assert skill["action_count"] == 1
    return "skill review list includes draft skill"


def _check_skill_review(report: SmokeReport, backend_url: str) -> str:
    """Verify promote and deprecate review actions mutate skill status safely."""

    skills = report.responses.get("skills") or _get_json(f"{backend_url}/api/skills")
    skill_id = next(item["id"] for item in skills if item["name"] == "collect_wood")
    promoted = _post_json(f"{backend_url}/api/skills/{skill_id}/promote")
    deprecated = _post_json(
        f"{backend_url}/api/skills/{skill_id}/deprecate",
        {"reason": "week8 smoke test"},
    )
    report.responses["skill_review"] = {"promoted": promoted, "deprecated": deprecated}
    assert promoted["status"] == "promoted"
    assert deprecated["status"] == "deprecated"
    return "skill promote/deprecate endpoints work"


def _check_comparison(report: SmokeReport, backend_url: str) -> str:
    """Verify the Week 8 benchmark comparison endpoint returns all expected modes."""

    payload = _get_json(f"{backend_url}/api/benchmark-comparison")
    report.responses["benchmark_comparison"] = payload
    modes = {mode["mode"] for mode in payload["modes"]}
    assert {"raw_codegen_baseline", "no_skill_harness", "skill_evolved_harness"} <= modes
    return "comparison endpoint returns three harness modes"


def _check_frontend(frontend_url: str) -> str:
    """Verify the Vite dashboard root is reachable."""

    html = _get_text(f"{frontend_url}/")
    assert '<div id="root">' in html
    return "frontend HTML is reachable"


def _expect_field(payload: dict[str, Any], field: str, expected: Any) -> str:
    """Assert a JSON response field equals the expected value."""

    assert payload[field] == expected
    return f"{field}={expected}"


def _find_by_id(items: Any, expected_id: str) -> dict[str, Any]:
    """Find a dictionary by id in a JSON list."""

    assert isinstance(items, list)
    return next(item for item in items if isinstance(item, dict) and item.get("id") == expected_id)


def _get_json(url: str) -> Any:
    """GET a JSON endpoint and decode its response body."""

    return json.loads(_request(url, method="GET"))


def _post_json(url: str, payload: dict[str, Any] | None = None) -> Any:
    """POST an optional JSON payload and decode the JSON response body."""

    body = json.dumps(payload or {}).encode("utf-8")
    return json.loads(_request(url, method="POST", body=body, content_type="application/json"))


def _get_text(url: str) -> str:
    """GET a text endpoint and decode its response body."""

    return _request(url, method="GET")


def _request(url: str, method: str, body: bytes | None = None, content_type: str | None = None) -> str:
    """Perform one HTTP request using the Python standard library."""

    headers = {"Content-Type": content_type} if content_type else {}
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:  # noqa: S310 - local smoke test URLs only.
        return response.read().decode("utf-8")


def _wait_for_json(url: str, timeout_sec: float) -> Any:
    """Wait until a JSON endpoint becomes reachable."""

    return _wait_for(lambda: _get_json(url), timeout_sec=timeout_sec)


def _wait_for_text(url: str, timeout_sec: float) -> str:
    """Wait until a text endpoint becomes reachable."""

    return _wait_for(lambda: _get_text(url), timeout_sec=timeout_sec)


def _wait_for(operation: Callable[[], Any], timeout_sec: float) -> Any:
    """Retry an operation until it succeeds or a timeout expires."""

    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return operation()
        except (AssertionError, HTTPError, URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for service: {last_error}")


def _free_port() -> int:
    """Reserve and return a currently free local TCP port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate_process(process: subprocess.Popen[str] | None) -> None:
    """Terminate a child process and drain a small amount of output for cleanup."""

    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _default_report_path() -> Path:
    """Return the default timestamped smoke report path."""

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "runs" / f"week8_dashboard_smoke_{timestamp}.json"


def _write_report(path: Path, report: SmokeReport) -> None:
    """Write the complete smoke report to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_json(), indent=2, sort_keys=True), encoding="utf-8")


def _summary(report: SmokeReport) -> dict[str, Any]:
    """Build a compact terminal summary from the full smoke report."""

    return {
        "ok": report.ok,
        "backend_url": report.backend_url,
        "frontend_url": report.frontend_url,
        "checks": [_check_to_json(check) for check in report.checks],
        "report_path": report.report_path,
    }


def _check_to_json(check: SmokeCheck) -> dict[str, Any]:
    """Convert one smoke check into a JSON-safe dictionary."""

    return {"name": check.name, "ok": check.ok, "detail": check.detail}


if __name__ == "__main__":
    main()
