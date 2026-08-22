from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "run_week11_creative_task.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("run_week11_creative_task_script", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
CREATIVE_SCRIPT = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = CREATIVE_SCRIPT
SCRIPT_SPEC.loader.exec_module(CREATIVE_SCRIPT)


def test_creative_cli_selects_reproducible_random_task(tmp_path: Path) -> None:
    """Seeded task selection only considers creative rows and remains reproducible."""

    manifest = tmp_path / "creative.jsonl"
    manifest.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"task_id": "creative:2", "category": "creative"},
                {"task_id": "harvest:1", "category": "harvest"},
                {"task_id": "creative:1", "category": "creative"},
            ]
        ),
        encoding="utf-8",
    )

    first = CREATIVE_SCRIPT._random_task_id(manifest, 42)
    second = CREATIVE_SCRIPT._random_task_id(manifest, 42)

    assert first == second
    assert first in {"creative:1", "creative:2"}


def test_creative_cli_extracts_run_and_redacts_credentials() -> None:
    """Workflow summaries retain run identity without exposing database credentials."""

    run_id = CREATIVE_SCRIPT._first_run_id({"outcomes": [{"run_id": "run-creative"}]})
    redacted_url = CREATIVE_SCRIPT._redacted_database_url(
        "postgresql+psycopg://mc_agent:secret@localhost:5432/mc_agent"
    )
    redacted_command = CREATIVE_SCRIPT._redacted_command(
        ["python", "runner.py", "--database-url", "postgresql://user:secret@host/db"]
    )

    assert run_id == "run-creative"
    assert "secret" not in redacted_url
    assert redacted_command[-1] == "<redacted>"


def test_creative_cli_builds_bounded_local_scorer_control_commands() -> None:
    """Managed offline scoring only exposes supported lifecycle transitions."""

    command = CREATIVE_SCRIPT._local_scorer_command("start")

    assert command[-2].endswith("scripts/mineclip_scorer.sh")
    assert command[-1] == "start"
    try:
        CREATIVE_SCRIPT._local_scorer_command("restart")
    except ValueError as exc:
        assert "Unsupported local scorer action" in str(exc)
    else:
        raise AssertionError("Unsupported scorer action was accepted.")


def test_creative_cli_enables_initial_multimodal_frame_by_default() -> None:
    """The Week11 wrapper should explicitly request a post-reset frame for turn zero."""

    args = argparse.Namespace(
        rcon_reset=False,
        random_teleport=False,
        threat_pause=False,
        spectator_player=None,
        rcon_password=None,
        rcon_port=25575,
        recording_window_title="Minecraft",
        recording_window_owner=None,
        recording_filter=None,
        agent_visual_snapshots=True,
        initial_visual_snapshot=True,
        mineclip_progress_feedback=False,
        live_extra_args=[],
    )
    command = ["python", "runner.py"]

    CREATIVE_SCRIPT._append_live_flags(command, args)

    assert "--agent-visual-snapshots" in command
    assert "--initial-visual-snapshot" in command


def test_managed_scorer_runs_only_after_live_recording(tmp_path: Path, monkeypatch) -> None:
    """The local MPS model stays unloaded until the Minecraft recording has finished."""

    manifest = tmp_path / "creative.jsonl"
    manifest.write_text(
        json.dumps({"task_id": "creative:test", "category": "creative"}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "workflow"
    calls: list[str] = []
    args = argparse.Namespace(
        task_id="creative:test",
        seed=1,
        manifest_path=manifest,
        host="localhost",
        port=25565,
        rcon_reset=False,
        rcon_port=25575,
        rcon_password=None,
        random_teleport=False,
        threat_pause=False,
        spectator_player=None,
        recording_window_title="Minecraft",
        recording_window_owner=None,
        recording_input="Capture screen 0:none",
        recording_filter=None,
        agent_visual_snapshots=True,
        mineclip_progress_feedback=False,
        max_steps=2,
        max_runtime_sec=30.0,
        scorer_url="http://127.0.0.1:8091",
        skip_scorer_preflight=False,
        manage_local_scorer=True,
        keep_local_scorer=False,
        calibration_file=None,
        threshold=None,
        output_dir=output_dir,
        database_url=None,
        database_path=None,
        live_extra_args=[],
    )

    def fake_run_command(
        command: list[str],
        *,
        environment_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Create the artifacts expected at each mocked subprocess boundary."""

        del environment_overrides
        if any(value.endswith("run_week10_live_training.py") for value in command):
            calls.append("live")
            report_path = Path(command[command.index("--output") + 1])
            video_path = Path(command[command.index("--recording-output") + 1])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            video_path.write_bytes(b"video")
            report_path.write_text(
                json.dumps(
                    {
                        "database_url": f"sqlite+pysqlite:///{tmp_path / 'audit.sqlite3'}",
                        "outcomes": [{"run_id": "run-managed"}],
                        "recording": {
                            "validation": {
                                "valid": True,
                                "trusted_minecraft_window": True,
                                "reasons": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
        else:
            calls.append("evaluate")
        return subprocess.CompletedProcess(command, 0)

    def fake_control(action: str) -> None:
        """Retain scorer lifecycle ordering without starting a model process."""

        calls.append(f"scorer:{action}")

    monkeypatch.setattr(CREATIVE_SCRIPT, "parse_args", lambda: args)
    monkeypatch.setattr(CREATIVE_SCRIPT, "_run_command", fake_run_command)
    monkeypatch.setattr(CREATIVE_SCRIPT, "_run_local_scorer_control", fake_control)
    monkeypatch.setattr(CREATIVE_SCRIPT, "_is_scorer_ready", lambda _url: False)
    monkeypatch.setattr(
        CREATIVE_SCRIPT,
        "_require_scorer_ready",
        lambda _url: {"status": "ready", "device": "mps"},
    )

    CREATIVE_SCRIPT.main()

    assert calls == ["scorer:stop", "live", "scorer:start", "evaluate", "scorer:stop"]
    summary = json.loads((output_dir / "workflow_summary.json").read_text(encoding="utf-8"))
    assert summary["mineclip"]["mode"] == "managed_offline"
    assert summary["mineclip"]["kept_running"] is False


def test_creative_cli_passes_nonblocking_mineclip_progress_flags() -> None:
    """The Week11 wrapper can keep online progress feedback explicit and auditable."""

    args = argparse.Namespace(
        rcon_reset=False,
        random_teleport=False,
        threat_pause=False,
        spectator_player=None,
        rcon_password=None,
        rcon_port=25575,
        recording_window_title="Minecraft",
        recording_window_owner=None,
        recording_filter=None,
        agent_visual_snapshots=True,
        initial_visual_snapshot=True,
        mineclip_progress_feedback=True,
        scorer_url="http://127.0.0.1:8091",
        live_extra_args=[],
    )
    command = ["python", "runner.py"]

    CREATIVE_SCRIPT._append_live_flags(command, args)

    assert "--mineclip-progress-feedback" in command
    scorer_index = command.index("--mineclip-progress-scorer-url")
    assert command[scorer_index + 1] == "http://127.0.0.1:8091"
