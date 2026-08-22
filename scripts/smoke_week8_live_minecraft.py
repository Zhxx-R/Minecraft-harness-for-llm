from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
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

from mc_agent_harness.core.config import settings  # noqa: E402
from mc_agent_harness.db.models import Base  # noqa: E402
from mc_agent_harness.harness.action_repair import ActionRepairPolicy  # noqa: E402
from mc_agent_harness.harness.context_manager import ContextManager  # noqa: E402
from mc_agent_harness.harness.persistent_recorder import PersistentEvaluationRecorder  # noqa: E402
from mc_agent_harness.harness.tool_registry import ToolRegistry  # noqa: E402
from mc_agent_harness.models.router import ModelRouter  # noqa: E402
from mc_agent_harness.runtime.mineflayer_client import MineflayerClient  # noqa: E402
from mc_agent_harness.schemas.action import HarnessAction  # noqa: E402


@dataclass(frozen=True, slots=True)
class LiveCheck:
    """One named live E2E check and its pass/fail details."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class LiveE2EReport:
    """JSON-serializable summary for one live Minecraft dashboard E2E test."""

    ok: bool
    run_id: str
    backend_url: str
    worker_url: str
    frontend_url: str | None
    database_url: str
    minecraft: dict[str, Any]
    checks: list[LiveCheck] = field(default_factory=list)
    live_result: dict[str, Any] = field(default_factory=dict)
    api_responses: dict[str, Any] = field(default_factory=dict)
    report_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Convert the live E2E report into a JSON-safe dictionary."""

        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "backend_url": self.backend_url,
            "worker_url": self.worker_url,
            "frontend_url": self.frontend_url,
            "database_url": self.database_url,
            "minecraft": self.minecraft,
            "checks": [_check_to_json(check) for check in self.checks],
            "live_result": self.live_result,
            "api_responses": self.api_responses,
            "report_path": self.report_path,
        }


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the live Minecraft Week 8 E2E smoke test."""

    parser = argparse.ArgumentParser(
        description="Run a live Minecraft -> Mineflayer worker -> audit dashboard E2E smoke test."
    )
    parser.add_argument("--host", default=os.getenv("MINECRAFT_HOST", "localhost"))
    parser.add_argument("--port", type=int, required=True, help="Minecraft LAN port.")
    parser.add_argument("--username", default=os.getenv("MINECRAFT_USERNAME", "Week8E2E"))
    parser.add_argument("--spawn-timeout-ms", type=int, default=20000)
    parser.add_argument("--backend-port", type=int, default=0, help="Backend port; 0 chooses a free port.")
    parser.add_argument("--worker-port", type=int, default=0, help="Worker port; 0 chooses a free port.")
    parser.add_argument("--frontend-port", type=int, default=0, help="Frontend port; 0 chooses a free port.")
    parser.add_argument("--with-frontend", action="store_true", help="Also start Vite and verify dashboard HTML.")
    parser.add_argument(
        "--scripted",
        action="store_true",
        help="Skip the LLM and use a scripted query_inventory action. Default is real LLM mode.",
    )
    parser.add_argument(
        "--keep-services",
        action="store_true",
        help="Leave backend, worker, and optional frontend running after the test.",
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=None,
        help="SQLite database path. Defaults to runs/week8_live_minecraft_e2e_<timestamp>.sqlite3.",
    )
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON report path.")
    return parser.parse_args()


def main() -> None:
    """Run the live Minecraft E2E smoke test and print a compact result summary."""

    args = parse_args()
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    if not args.scripted:
        _validate_model_environment()
    database_path = args.database_path or ROOT / "runs" / f"week8_live_minecraft_e2e_{timestamp}.sqlite3"
    output_path = args.output or ROOT / "runs" / f"week8_live_minecraft_e2e_{timestamp}.json"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    session_factory = _create_database(database_url)

    backend_port = args.backend_port or _free_port()
    worker_port = args.worker_port or _free_port()
    frontend_port = args.frontend_port or _free_port()
    backend_url = f"http://127.0.0.1:{backend_port}"
    worker_url = f"ws://127.0.0.1:{worker_port}"
    frontend_url = f"http://127.0.0.1:{frontend_port}" if args.with_frontend else None
    run_id = f"week8_live_minecraft_{timestamp}"
    report = LiveE2EReport(
        ok=False,
        run_id=run_id,
        backend_url=backend_url,
        worker_url=worker_url,
        frontend_url=frontend_url,
        database_url=database_url,
        minecraft={
            "host": args.host,
            "port": args.port,
            "username": args.username,
            "spawn_timeout_ms": args.spawn_timeout_ms,
            "action_mode": "scripted" if args.scripted else "llm",
            "model": settings.model_default if not args.scripted else None,
        },
    )

    backend_process: subprocess.Popen[str] | None = None
    worker_process: subprocess.Popen[str] | None = None
    frontend_process: subprocess.Popen[str] | None = None

    try:
        worker_process = _start_worker(worker_port)
        _wait_for_tcp("127.0.0.1", worker_port, timeout_sec=20)
        backend_process = _start_backend(database_url, backend_port)
        _wait_for_json(f"{backend_url}/api/health", timeout_sec=20)
        if args.with_frontend:
            frontend_process = _start_frontend(backend_url, frontend_port)
            _wait_for_text(f"{frontend_url}/", timeout_sec=20)

        report.live_result = asyncio.run(
            _run_live_inventory_check(
                session_factory=session_factory,
                run_id=run_id,
                worker_url=worker_url,
                host=args.host,
                port=args.port,
                username=args.username,
                spawn_timeout_ms=args.spawn_timeout_ms,
                use_llm=not args.scripted,
            )
        )
        _run_checks(report, backend_url, frontend_url)
        report.ok = all(check.ok for check in report.checks)
    finally:
        report.report_path = str(output_path)
        _write_report(output_path, report)
        if not args.keep_services:
            _terminate_process(frontend_process)
            _terminate_process(backend_process)
            _terminate_process(worker_process)

    print(json.dumps(_summary(report), indent=2, sort_keys=True))
    if not report.ok:
        raise SystemExit(1)


async def _run_live_inventory_check(
    session_factory: sessionmaker[Session],
    run_id: str,
    worker_url: str,
    host: str,
    port: int,
    username: str,
    spawn_timeout_ms: int,
    use_llm: bool,
) -> dict[str, Any]:
    """Run one live inventory action through Mineflayer, optionally generated by the LLM."""

    recorder = PersistentEvaluationRecorder(session_factory)
    runtime = MineflayerClient(worker_url, request_timeout=(spawn_timeout_ms / 1000) + 10)
    task_id = "week8_live_minecraft_inventory"
    task_spec = {
        "run_id": run_id,
        "task_id": task_id,
        "goal": (
            "Connect to live Minecraft through Mineflayer and query inventory. "
            "Return the query_inventory action with empty args."
        ),
        "allowed_actions": ["query_inventory"],
        "runtime_profile": "live-lan-smoke",
        "runtime": {
            "host": host,
            "port": port,
            "username": username,
            "spawn_timeout_ms": spawn_timeout_ms,
        },
    }
    action = HarnessAction(type="query_inventory", args={})
    try:
        await runtime.reset(task_spec)
        await recorder.record(
            run_id,
            "run_started",
            {
                "task_id": task_id,
                "task_spec": task_spec,
                "allowed_actions": ["query_inventory"],
                "resume": False,
                "start_step_index": 0,
            },
        )
        observation = await runtime.observe()
        await recorder.record(run_id, "observation", {"step_index": 0, "observation": observation})
        if use_llm:
            context = await ContextManager().build(
                observation=observation,
                task_memory=["This smoke test only succeeds with query_inventory and empty args."],
                task_spec=task_spec,
                allowed_actions=["query_inventory"],
            )
            await recorder.record(
                run_id,
                "context_built",
                {
                    "step_index": 0,
                    "resolved_terms": [term.canonical_id for term in context.resolved_terms],
                    "retrieved_docs": [document.id for document in context.retrieved_docs],
                    "retrieved_skills": [
                        {"name": skill.name, "version": skill.version}
                        for skill in context.retrieved_skills
                    ],
                },
            )
            model_result = await ActionRepairPolicy().generate_valid_action(
                model_router=ModelRouter(),
                messages=context.messages,
                registry=ToolRegistry(["query_inventory"]),
                recorder=recorder,
                run_id=run_id,
                step_index=0,
            )
            action = model_result.action
            action_source = str(model_result.raw_response.get("source", "model"))
            raw_model_content = model_result.raw_content
        else:
            await recorder.record(
                run_id,
                "context_built",
                {
                    "step_index": 0,
                    "resolved_terms": [],
                    "retrieved_docs": [],
                    "retrieved_skills": [],
                },
            )
            await recorder.record(
                run_id,
                "model_action",
                {
                    "step_index": 0,
                    "raw_content": json.dumps(action.model_dump(mode="json"), sort_keys=True),
                    "action": action.model_dump(mode="json"),
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    "repair_attempts": 0,
                    "source": "scripted_live_e2e",
                },
            )
            action_source = "scripted_live_e2e"
            raw_model_content = json.dumps(action.model_dump(mode="json"), sort_keys=True)
        action_result = await runtime.act(action)
        await recorder.record(
            run_id,
            "action_result",
            {
                "step_index": 0,
                "action": action.model_dump(mode="json"),
                "result": action_result,
            },
        )
        await recorder.record(run_id, "run_finished", {"task_id": task_id, "steps": 1, "terminated": False})
        return {
            "ok": bool(action_result.get("ok", True)),
            "action_mode": "llm" if use_llm else "scripted",
            "action_source": action_source,
            "raw_model_content": raw_model_content,
            "observation": observation,
            "action": action.model_dump(mode="json"),
            "action_result": action_result,
            "worker_lifecycle_events": [asdict(event) for event in runtime.lifecycle_events],
        }
    except Exception as exc:  # noqa: BLE001 - live smoke must report failures clearly.
        await recorder.record(
            run_id,
            "runtime_error",
            {"phase": "live_e2e", "step_index": None, "error_type": type(exc).__name__, "message": str(exc)},
        )
        raise
    finally:
        await runtime.close()


def _validate_model_environment() -> None:
    """Fail fast when real LLM mode does not have a configured Qwen-compatible endpoint."""

    if not settings.qwen_base_url:
        raise SystemExit("QWEN_BASE_URL is missing. Set it in .env or the environment.")
    if not settings.qwen_api_key:
        raise SystemExit("QWEN_API_KEY is missing. Set it in .env or the environment.")


def _create_database(database_url: str) -> sessionmaker[Session]:
    """Create a SQLite database for the live E2E audit records."""

    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _start_worker(port: int) -> subprocess.Popen[str]:
    """Start a temporary Mineflayer worker bound to a free local port."""

    env = os.environ.copy()
    env["MINEFLAYER_WORKER_PORT"] = str(port)
    return subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=ROOT / "workers" / "mineflayer-worker",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _start_backend(database_url: str, port: int) -> subprocess.Popen[str]:
    """Start a temporary FastAPI backend using the live E2E SQLite database."""

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
    """Start a temporary Vite frontend using the live E2E backend."""

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


def _run_checks(report: LiveE2EReport, backend_url: str, frontend_url: str | None) -> None:
    """Execute dashboard API and optional frontend checks after the live Minecraft action."""

    checks: list[tuple[str, Callable[[], str]]] = [
        ("health", lambda: _expect_field(_get_json(f"{backend_url}/api/health"), "status", "ok")),
        ("live_action", lambda: _check_live_action(report)),
        ("runs", lambda: _check_runs(report, backend_url)),
        ("events", lambda: _check_events(report, backend_url)),
        ("model_calls", lambda: _check_model_calls(report, backend_url)),
        ("replay", lambda: _check_replay(report, backend_url)),
        ("runtime_errors", lambda: _check_runtime_errors(report, backend_url)),
    ]
    if frontend_url is not None:
        checks.append(("frontend", lambda: _check_frontend(frontend_url)))

    for name, check in checks:
        try:
            report.checks.append(LiveCheck(name=name, ok=True, detail=check()))
        except Exception as exc:  # noqa: BLE001 - smoke runner should report all failures.
            report.checks.append(LiveCheck(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}"))


def _check_live_action(report: LiveE2EReport) -> str:
    """Verify the live Mineflayer action returned an observation and result."""

    assert report.live_result["ok"] is True
    if report.minecraft.get("action_mode") == "llm":
        assert report.live_result["action_mode"] == "llm"
        assert report.live_result["action_source"] == "model"
        assert report.live_result["raw_model_content"]
    assert "observation" in report.live_result
    assert "action_result" in report.live_result
    return f"live Mineflayer inventory action completed via {report.live_result['action_mode']}"


def _check_runs(report: LiveE2EReport, backend_url: str) -> str:
    """Verify the live run is visible through the dashboard run list."""

    payload = _get_json(f"{backend_url}/api/runs")
    report.api_responses["runs"] = payload
    run = _find_by_id(payload, report.run_id)
    assert run["status"] == "completed"
    assert run["model_call_count"] == 1
    return "live run is listed with model call count"


def _check_events(report: LiveE2EReport, backend_url: str) -> str:
    """Verify timeline events include live E2E core evidence."""

    payload = _get_json(f"{backend_url}/api/runs/{report.run_id}/events")
    report.api_responses["events"] = payload
    event_types = {event["event_type"] for event in payload}
    assert {"run_started", "observation", "context_built", "model_action", "action_result", "run_finished"} <= event_types
    return "timeline includes live observation and action result"


def _check_model_calls(report: LiveE2EReport, backend_url: str) -> str:
    """Verify live action is exposed as a model-call row for dashboard consistency."""

    payload = _get_json(f"{backend_url}/api/runs/{report.run_id}/model-calls")
    report.api_responses["model_calls"] = payload
    expected_source = "scripted_live_e2e" if report.minecraft.get("action_mode") == "scripted" else "model"
    assert payload[0]["source"] == expected_source
    assert payload[0]["action"]["type"] == "query_inventory"
    return f"model-call view contains {expected_source} action"


def _check_replay(report: LiveE2EReport, backend_url: str) -> str:
    """Verify replay groups live Minecraft evidence into one step."""

    payload = _get_json(f"{backend_url}/api/runs/{report.run_id}/replay")
    report.api_responses["replay"] = payload
    assert payload["summary"]["step_count"] == 1
    step = payload["steps"][0]
    assert step["status"] in {"ok", "completed"}
    assert step["parsed_action"]["type"] == "query_inventory"
    expected_source = "scripted_live_e2e" if report.minecraft.get("action_mode") == "scripted" else "model"
    assert step["model_calls"][0]["source"] == expected_source
    return "replay shows observation-context-action-result chain"


def _check_runtime_errors(report: LiveE2EReport, backend_url: str) -> str:
    """Verify live E2E success does not produce runtime errors."""

    payload = _get_json(f"{backend_url}/api/runs/{report.run_id}/runtime-errors")
    report.api_responses["runtime_errors"] = payload
    assert payload == []
    return "no runtime errors were recorded"


def _check_frontend(frontend_url: str) -> str:
    """Verify the optional frontend is reachable."""

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


def _get_text(url: str) -> str:
    """GET a text endpoint and decode its response body."""

    return _request(url, method="GET")


def _request(url: str, method: str) -> str:
    """Perform one HTTP request using the Python standard library."""

    request = Request(url, method=method)
    with urlopen(request, timeout=5) as response:  # noqa: S310 - local smoke test URLs only.
        return response.read().decode("utf-8")


def _wait_for_json(url: str, timeout_sec: float) -> Any:
    """Wait until a JSON endpoint becomes reachable."""

    return _wait_for(lambda: _get_json(url), timeout_sec=timeout_sec)


def _wait_for_text(url: str, timeout_sec: float) -> str:
    """Wait until a text endpoint becomes reachable."""

    return _wait_for(lambda: _get_text(url), timeout_sec=timeout_sec)


def _wait_for_tcp(host: str, port: int, timeout_sec: float) -> None:
    """Wait until a TCP port accepts connections."""

    def connect_once() -> None:
        with socket.create_connection((host, port), timeout=1):
            return None

    _wait_for(connect_once, timeout_sec=timeout_sec)


def _wait_for(operation: Callable[[], Any], timeout_sec: float) -> Any:
    """Retry an operation until it succeeds or a timeout expires."""

    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return operation()
        except (AssertionError, HTTPError, URLError, ConnectionError, OSError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for service: {last_error}")


def _free_port() -> int:
    """Reserve and return a currently free local TCP port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate_process(process: subprocess.Popen[str] | None) -> None:
    """Terminate a child process if it is still running."""

    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _write_report(path: Path, report: LiveE2EReport) -> None:
    """Write the complete live E2E report to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_json(), indent=2, sort_keys=True), encoding="utf-8")


def _summary(report: LiveE2EReport) -> dict[str, Any]:
    """Build a compact terminal summary from the full live E2E report."""

    return {
        "ok": report.ok,
        "run_id": report.run_id,
        "backend_url": report.backend_url,
        "worker_url": report.worker_url,
        "frontend_url": report.frontend_url,
        "minecraft": report.minecraft,
        "checks": [_check_to_json(check) for check in report.checks],
        "report_path": report.report_path,
    }


def _check_to_json(check: LiveCheck) -> dict[str, Any]:
    """Convert one live check into a JSON-safe dictionary."""

    return {"name": check.name, "ok": check.ok, "detail": check.detail}


if __name__ == "__main__":
    main()
