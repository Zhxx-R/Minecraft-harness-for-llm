from __future__ import annotations

import argparse
import json
from pathlib import Path

from mc_agent_harness.evaluation.baselines import RawCodegenSandbox, RawCodegenSandboxConfig
from mc_agent_harness.evaluation.comparison import (
    build_week8_comparison,
    write_week8_comparison_report,
)


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the Week 8 comparison runner."""

    parser = argparse.ArgumentParser(description="Build the Week 8 harness comparison report.")
    parser.add_argument("--week6-report-dir", type=Path, default=ROOT / "runs" / "week6")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs" / "week8")
    parser.add_argument("--skill-report", type=Path, default=None)
    parser.add_argument("--raw-js", action="append", type=Path, default=[])
    parser.add_argument("--node-binary", default="node")
    parser.add_argument("--timeout-sec", type=float, default=3.0)
    return parser.parse_args()


def main() -> None:
    """Evaluate optional raw-codegen candidates and export the Week 8 comparison."""

    args = parse_args()
    sandbox = RawCodegenSandbox(
        RawCodegenSandboxConfig(node_binary=args.node_binary, timeout_sec=args.timeout_sec)
    )
    raw_results = [
        sandbox.evaluate(path.read_text(encoding="utf-8"), candidate_id=path.stem)
        for path in args.raw_js
    ]
    report = build_week8_comparison(
        week6_report_dir=args.week6_report_dir,
        raw_results=raw_results,
        skill_report_path=args.skill_report,
    )
    json_path, markdown_path = write_week8_comparison_report(report, args.output_dir)
    print(
        json.dumps(
            {
                "comparison_id": report.comparison_id,
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
                "modes": [mode.mode for mode in report.modes],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
