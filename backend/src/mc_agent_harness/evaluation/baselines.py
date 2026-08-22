from __future__ import annotations

import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RawCodegenSandboxConfig:
    """Runtime limits and policy switches for the raw Mineflayer codegen baseline."""

    timeout_sec: float = 3.0
    max_source_bytes: int = 64_000
    node_binary: str = "node"


@dataclass(frozen=True, slots=True)
class RawCodegenSandboxResult:
    """Audit result for one raw JavaScript candidate checked in the sandbox."""

    candidate_id: str
    ok: bool
    crashed: bool
    failure_type: str | None
    message: str
    stdout: str
    stderr: str
    sandbox_mode: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation for reports and API responses."""

        return asdict(self)


class RawCodegenSandbox:
    """Policy and syntax sandbox for raw Mineflayer code generated as a baseline."""

    DISALLOWED_PATTERNS = (
        "child_process",
        "process.",
        "process[",
        "fs.",
        "require('fs')",
        'require("fs")',
        "net.",
        "dgram.",
        "eval(",
        "Function(",
    )

    def __init__(self, config: RawCodegenSandboxConfig | None = None) -> None:
        self.config = config or RawCodegenSandboxConfig()

    def evaluate(self, javascript_source: str, candidate_id: str = "raw_candidate") -> RawCodegenSandboxResult:
        """Check one raw JavaScript candidate without connecting it to the live worker."""

        if len(javascript_source.encode("utf-8")) > self.config.max_source_bytes:
            return RawCodegenSandboxResult(
                candidate_id=candidate_id,
                ok=False,
                crashed=True,
                failure_type="source_too_large",
                message="Raw candidate exceeds sandbox source-size limit.",
                stdout="",
                stderr="",
                sandbox_mode="policy_and_node_syntax_check",
            )

        violation = _first_policy_violation(javascript_source, self.DISALLOWED_PATTERNS)
        if violation is not None:
            return RawCodegenSandboxResult(
                candidate_id=candidate_id,
                ok=False,
                crashed=True,
                failure_type="policy_violation",
                message=f"Disallowed raw-code pattern: {violation}",
                stdout="",
                stderr="",
                sandbox_mode="policy_and_node_syntax_check",
            )

        with tempfile.TemporaryDirectory(prefix="mc-agent-raw-baseline-") as tmp_dir:
            candidate_path = Path(tmp_dir) / "candidate.js"
            candidate_path.write_text('"use strict";\n' + javascript_source, encoding="utf-8")
            try:
                completed = subprocess.run(
                    [self.config.node_binary, "--check", str(candidate_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout_sec,
                )
            except subprocess.TimeoutExpired as exc:
                return RawCodegenSandboxResult(
                    candidate_id=candidate_id,
                    ok=False,
                    crashed=True,
                    failure_type="timeout",
                    message=f"Raw candidate syntax check exceeded {self.config.timeout_sec:.1f}s.",
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                    sandbox_mode="policy_and_node_syntax_check",
                )
            except FileNotFoundError:
                return RawCodegenSandboxResult(
                    candidate_id=candidate_id,
                    ok=False,
                    crashed=True,
                    failure_type="node_not_found",
                    message=f"Node binary not found: {self.config.node_binary}",
                    stdout="",
                    stderr="",
                    sandbox_mode="policy_and_node_syntax_check",
                )

        ok = completed.returncode == 0
        return RawCodegenSandboxResult(
            candidate_id=candidate_id,
            ok=ok,
            crashed=not ok,
            failure_type=None if ok else "syntax_error",
            message="Raw candidate passed syntax and policy checks." if ok else "Raw candidate failed node syntax check.",
            stdout=completed.stdout,
            stderr=completed.stderr,
            sandbox_mode="policy_and_node_syntax_check",
        )


def _first_policy_violation(source: str, disallowed_patterns: tuple[str, ...]) -> str | None:
    """Return the first disallowed source fragment found by the baseline policy scanner."""

    lowered = source.lower()
    for pattern in disallowed_patterns:
        if pattern.lower() in lowered:
            return pattern
    return None
