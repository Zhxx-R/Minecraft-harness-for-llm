from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "run_week11_creative_evaluation.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "run_week11_creative_evaluation_script",
    SCRIPT_PATH,
)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
EVALUATION_SCRIPT = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = EVALUATION_SCRIPT
SCRIPT_SPEC.loader.exec_module(EVALUATION_SCRIPT)


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_invalid_recording_skips_mineclip_and_returns_inconclusive(
    tmp_path: Path,
) -> None:
    """Untrusted source evidence must terminate before frame extraction or scorer calls."""

    live_report = tmp_path / "live_training.json"
    live_report.write_text(
        json.dumps(
            {
                "recording": {
                    "validation": {
                        "valid": False,
                        "trusted_minecraft_window": False,
                        "reasons": ["minecraft_window_not_trusted"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        task_id="creative:0",
        manifest_path=ROOT / "tasks" / "executable" / "minedojo_creative_tasks.jsonl",
        video=tmp_path / "not-required-for-invalid-source.mp4",
        frames_dir=None,
        scorer_url="http://127.0.0.1:1",
        scorer_timeout_sec=0.01,
        calibration_file=None,
        threshold=None,
        sample_fps=None,
        max_frames=4096,
        run_id="invalid-capture-run",
        source_validation_report=live_report,
        persist=False,
        database_url=None,
        database_path=None,
        output_dir=tmp_path / "evaluation",
    )

    result = await EVALUATION_SCRIPT.run(args)

    assert result["success"] is False
    assert result["inconclusive"] is True
    assert result["score"] is None
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["result"]["source_validation"]["reasons"] == [
        "minecraft_window_not_trusted"
    ]
