from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mc_agent_harness.evaluation.baselines import RawCodegenSandboxResult


@dataclass(frozen=True, slots=True)
class ComparisonModeResult:
    """Comparable metric row for one execution mode in the Week 8 demo report."""

    mode: str
    label: str
    status: str
    task_count: int | None
    success_count: int | None
    success_rate: float | None
    invalid_action_rate: float | None
    runtime_crash_rate: float | None
    total_steps: int | None
    total_tokens: int | None
    estimated_cost: float | None
    source: str
    notes: list[str] = field(default_factory=list)
    raw_baseline_results: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class HarnessComparisonReport:
    """Week 8 benchmark comparison report across raw codegen and harness modes."""

    comparison_id: str
    generated_at: str
    modes: list[ComparisonModeResult]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation for API responses and exported reports."""

        return {
            "comparison_id": self.comparison_id,
            "generated_at": self.generated_at,
            "modes": [asdict(mode) for mode in self.modes],
        }


def build_week8_comparison(
    week6_report_dir: Path,
    raw_results: list[RawCodegenSandboxResult] | None = None,
    skill_report_path: Path | None = None,
    comparison_id: str | None = None,
) -> HarnessComparisonReport:
    """Build the Week 8 comparison from measured reports and sandboxed baseline checks."""

    raw_mode = _raw_mode(raw_results or [])
    no_skill_mode = _no_skill_mode(week6_report_dir)
    skill_mode = _skill_mode(skill_report_path)
    return HarnessComparisonReport(
        comparison_id=comparison_id or f"week8_{uuid.uuid4().hex[:12]}",
        generated_at=datetime.now(tz=UTC).isoformat(),
        modes=[raw_mode, no_skill_mode, skill_mode],
    )


def write_week8_comparison_report(report: HarnessComparisonReport, output_dir: Path) -> tuple[Path, Path]:
    """Write the comparison report as JSON and Markdown files."""

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report.comparison_id}.json"
    markdown_path = output_dir / f"{report.comparison_id}.md"
    json_path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_report_to_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _raw_mode(raw_results: list[RawCodegenSandboxResult]) -> ComparisonModeResult:
    """Aggregate raw codegen sandbox checks into one comparison row."""

    if not raw_results:
        return ComparisonModeResult(
            mode="raw_codegen_baseline",
            label="Raw Mineflayer Codegen",
            status="sandbox_ready",
            task_count=None,
            success_count=None,
            success_rate=None,
            invalid_action_rate=None,
            runtime_crash_rate=None,
            total_steps=None,
            total_tokens=None,
            estimated_cost=None,
            source="raw_codegen_sandbox",
            notes=[
                "Sandbox is available, but no generated JS candidates were supplied.",
                "Run scripts/run_week8_comparison.py --raw-js path/to/candidate.js to record baseline checks.",
            ],
        )

    task_count = len(raw_results)
    success_count = sum(1 for result in raw_results if result.ok)
    crashed_count = sum(1 for result in raw_results if result.crashed)
    return ComparisonModeResult(
        mode="raw_codegen_baseline",
        label="Raw Mineflayer Codegen",
        status="sandbox_measured",
        task_count=task_count,
        success_count=success_count,
        success_rate=success_count / task_count if task_count else 0.0,
        invalid_action_rate=None,
        runtime_crash_rate=crashed_count / task_count if task_count else 0.0,
        total_steps=None,
        total_tokens=None,
        estimated_cost=None,
        source="raw_codegen_sandbox",
        notes=["Raw code is checked outside the main harness path; failures count only against this baseline."],
        raw_baseline_results=[result.to_dict() for result in raw_results],
    )


def _no_skill_mode(week6_report_dir: Path) -> ComparisonModeResult:
    """Load the latest measured Week 6 benchmark as the no-skill harness row."""

    latest = _latest_json_report(week6_report_dir)
    if latest is None:
        return ComparisonModeResult(
            mode="no_skill_harness",
            label="Harness Without Skills",
            status="pending",
            task_count=None,
            success_count=None,
            success_rate=None,
            invalid_action_rate=None,
            runtime_crash_rate=None,
            total_steps=None,
            total_tokens=None,
            estimated_cost=None,
            source=str(week6_report_dir),
            notes=["No Week 6 benchmark report was found."],
        )
    payload = _read_json(latest)
    return ComparisonModeResult(
        mode="no_skill_harness",
        label="Harness Without Skills",
        status="measured",
        task_count=_optional_int(payload.get("task_count")),
        success_count=_optional_int(payload.get("success_count")),
        success_rate=_optional_float(payload.get("success_rate")),
        invalid_action_rate=_optional_float(payload.get("invalid_action_rate")),
        runtime_crash_rate=_optional_float(payload.get("runtime_crash_rate")),
        total_steps=_optional_int(payload.get("total_steps")),
        total_tokens=_optional_int(payload.get("total_tokens")),
        estimated_cost=_optional_float(payload.get("estimated_cost")),
        source=str(latest),
        notes=["Loaded from the latest Week 6 deterministic benchmark report."],
    )


def _skill_mode(skill_report_path: Path | None) -> ComparisonModeResult:
    """Load a skill-evolved benchmark row when a measured report is available."""

    if skill_report_path is None or not skill_report_path.exists():
        return ComparisonModeResult(
            mode="skill_evolved_harness",
            label="Skill-Evolved Harness",
            status="pending_replay",
            task_count=None,
            success_count=None,
            success_rate=None,
            invalid_action_rate=None,
            runtime_crash_rate=None,
            total_steps=None,
            total_tokens=None,
            estimated_cost=None,
            source=str(skill_report_path) if skill_report_path else "not_configured",
            notes=[
                "Skill replay benchmark is not measured yet.",
                "After promoted-skill replay is added, point this row at that report file.",
            ],
        )
    payload = _read_json(skill_report_path)
    return ComparisonModeResult(
        mode="skill_evolved_harness",
        label="Skill-Evolved Harness",
        status="measured",
        task_count=_optional_int(payload.get("task_count")),
        success_count=_optional_int(payload.get("success_count")),
        success_rate=_optional_float(payload.get("success_rate")),
        invalid_action_rate=_optional_float(payload.get("invalid_action_rate")),
        runtime_crash_rate=_optional_float(payload.get("runtime_crash_rate")),
        total_steps=_optional_int(payload.get("total_steps")),
        total_tokens=_optional_int(payload.get("total_tokens")),
        estimated_cost=_optional_float(payload.get("estimated_cost")),
        source=str(skill_report_path),
        notes=["Loaded from an externally supplied skill-evolved benchmark report."],
    )


def _latest_json_report(report_dir: Path) -> Path | None:
    """Return the most recently modified JSON report in a directory."""

    if not report_dir.exists():
        return None
    reports = sorted(report_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def _read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object from disk with a defensive object fallback."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _optional_float(value: object) -> float | None:
    """Coerce a JSON value into an optional float metric."""

    if value is None:
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    """Coerce a JSON value into an optional integer metric."""

    if value is None:
        return None
    return int(value)


def _report_to_markdown(report: HarnessComparisonReport) -> str:
    """Render the comparison report as a compact Markdown table."""

    lines = [
        f"# Week 8 Harness Comparison `{report.comparison_id}`",
        "",
        f"- Generated at: `{report.generated_at}`",
        "",
        "| Mode | Status | Success | Invalid Action Rate | Runtime Crash Rate | Steps | Tokens | Source |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for mode in report.modes:
        success = _ratio_label(mode.success_count, mode.task_count, mode.success_rate)
        lines.append(
            f"| {mode.label} | `{mode.status}` | {success} | {_percent(mode.invalid_action_rate)} | "
            f"{_percent(mode.runtime_crash_rate)} | {_number(mode.total_steps)} | {_number(mode.total_tokens)} | "
            f"`{mode.source}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _ratio_label(success_count: int | None, task_count: int | None, success_rate: float | None) -> str:
    """Format success metrics for Markdown output."""

    if success_count is None or task_count is None:
        return "pending"
    return f"{success_count}/{task_count} ({_percent(success_rate)})"


def _percent(value: float | None) -> str:
    """Format an optional ratio as a percentage."""

    return "pending" if value is None else f"{value:.1%}"


def _number(value: int | None) -> str:
    """Format an optional integer metric."""

    return "pending" if value is None else str(value)
