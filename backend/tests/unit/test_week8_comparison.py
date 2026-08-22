from __future__ import annotations

import json

from mc_agent_harness.evaluation.baselines import RawCodegenSandbox, RawCodegenSandboxResult
from mc_agent_harness.evaluation.comparison import build_week8_comparison, write_week8_comparison_report


def test_raw_codegen_sandbox_blocks_disallowed_process_access() -> None:
    """Raw baseline sandbox rejects dangerous process access before syntax checking."""

    result = RawCodegenSandbox().evaluate("process.exit(1)", candidate_id="unsafe")

    assert result.ok is False
    assert result.crashed is True
    assert result.failure_type == "policy_violation"


def test_week8_comparison_loads_week6_and_raw_baseline_rows(tmp_path) -> None:
    """Week 8 comparison combines measured Week 6 metrics with raw sandbox metrics."""

    week6_dir = tmp_path / "week6"
    week6_dir.mkdir()
    (week6_dir / "week6_example.json").write_text(
        json.dumps(
            {
                "task_count": 10,
                "success_count": 8,
                "success_rate": 0.8,
                "invalid_action_rate": 0.1,
                "runtime_crash_rate": 0.0,
                "total_steps": 42,
                "total_tokens": 1200,
                "estimated_cost": 0.12,
            }
        ),
        encoding="utf-8",
    )
    raw_result = RawCodegenSandboxResult(
        candidate_id="candidate",
        ok=False,
        crashed=True,
        failure_type="syntax_error",
        message="failed",
        stdout="",
        stderr="SyntaxError",
        sandbox_mode="policy_and_node_syntax_check",
    )

    report = build_week8_comparison(week6_report_dir=week6_dir, raw_results=[raw_result])
    json_path, markdown_path = write_week8_comparison_report(report, tmp_path / "week8")

    assert report.modes[0].status == "sandbox_measured"
    assert report.modes[0].runtime_crash_rate == 1.0
    assert report.modes[1].status == "measured"
    assert report.modes[1].success_rate == 0.8
    assert report.modes[2].status == "pending_replay"
    assert json_path.exists()
    assert markdown_path.exists()
