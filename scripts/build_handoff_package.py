from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "release"
EXCLUDED_TOP_LEVEL = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".runtime",
    "release",
    "runs",
}
EXCLUDED_PARTS = {
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "coverage",
    "dist",
    "logs",
    "node_modules",
}
EXCLUDED_NAMES = {".DS_Store", ".env"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".sqlite3", ".pid"}
SECRET_PATTERNS = {
    "OpenAI-compatible API key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "configured QWEN_API_KEY": re.compile(
        r"(?m)^QWEN_API_KEY=(?!replace-me\s*$|<[^>]+>\s*$)(\S+)"
    ),
    "configured RCON password": re.compile(
        r"(?m)^rcon\.password=(?!__RCON_PASSWORD__\s*$|<[^>]+>\s*$)(\S+)"
    ),
}


def parse_args() -> argparse.Namespace:
    """Parse handoff package build options."""

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(
        description="Build a sanitized Minecraft Agent Harness handoff archive."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--name",
        default=f"minecraft-agent-harness-handoff-{timestamp}",
        help="Top-level directory and archive basename.",
    )
    parser.add_argument("--skip-zip", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Stage sanitized sources, scan them, and create checksummed archives."""

    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_root = output_dir / args.name
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True)

    copied_files = _copy_project(stage_root)
    build_info = _build_info(args.name, copied_files)
    (stage_root / "HANDOFF_BUILD_INFO.json").write_text(
        json.dumps(build_info, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _scan_for_secrets(stage_root)
    _write_file_manifest(stage_root)

    tar_path = output_dir / f"{args.name}.tar.gz"
    zip_path = output_dir / f"{args.name}.zip"
    for path in (tar_path, zip_path):
        if path.exists():
            path.unlink()
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(stage_root, arcname=args.name, recursive=True)
    artifacts = [tar_path]
    if not args.skip_zip:
        _write_zip(stage_root, zip_path, args.name)
        artifacts.append(zip_path)

    for artifact in artifacts:
        checksum = _sha256(artifact)
        artifact.with_name(f"{artifact.name}.sha256").write_text(
            f"{checksum}  {artifact.name}\n",
            encoding="utf-8",
        )

    quickstart_source = ROOT / "docs" / "handoff" / "README_FIRST.zh.md"
    shutil.copy2(quickstart_source, output_dir / "README_FIRST.zh.md")
    installer_target = output_dir / "install_handoff.sh"
    shutil.copy2(ROOT / "scripts" / "install_handoff_archive.sh", installer_target)
    installer_target.chmod(installer_target.stat().st_mode | stat.S_IXUSR)

    report = {
        "package_name": args.name,
        "stage_root": str(stage_root),
        "source_file_count": len(copied_files),
        "artifacts": [
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in artifacts
        ],
    }
    report_path = output_dir / f"{args.name}.build.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _copy_project(stage_root: Path) -> list[str]:
    """Copy source and reproducibility assets while excluding local state."""

    copied: list[str] = []
    for current_root, directory_names, file_names in os.walk(ROOT):
        current_path = Path(current_root)
        directory_names[:] = sorted(
            directory_name
            for directory_name in directory_names
            if not _is_excluded((current_path / directory_name).relative_to(ROOT))
        )
        for file_name in sorted(file_names):
            source = current_path / file_name
            relative = source.relative_to(ROOT)
            if _is_excluded(relative):
                continue
            if source.is_symlink():
                raise RuntimeError(f"Refusing to package symlink: {relative}")
            destination = stage_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative.as_posix())
    return copied


def _is_excluded(relative: Path) -> bool:
    """Return whether a repository path contains local, generated, or sensitive state."""

    if not relative.parts:
        return False
    if relative.parts[0] in EXCLUDED_TOP_LEVEL:
        return True
    if relative.parts[:2] == ("infra", "minecraft-server"):
        return True
    if relative.parts[:2] == ("infra", "minecraft-server-pool"):
        return True
    if relative.parts[:3] in {
        ("services", "mineclip-scorer", "cache"),
        ("services", "mineclip-scorer", "checkpoints"),
        ("services", "mineclip-scorer", "vendor"),
    }:
        return True
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return True
    if relative.name in EXCLUDED_NAMES or relative.suffix in EXCLUDED_SUFFIXES:
        return True
    return False


def _build_info(package_name: str, copied_files: list[str]) -> dict[str, object]:
    """Build source provenance without including credentials or machine paths."""

    status_lines = _git_output(["status", "--porcelain"]).splitlines()
    return {
        "schema_version": "mc-agent-harness.handoff.v1",
        "package_name": package_name,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "source_commit": _git_output(["rev-parse", "HEAD"]),
        "source_branch": _git_output(["branch", "--show-current"]),
        "source_dirty": bool(status_lines),
        "source_changes": status_lines,
        "source_file_count": len(copied_files),
        "excluded_runtime_state": [
            ".env and credentials",
            ".git history and private remote metadata",
            "Python virtual environments and Node node_modules",
            "Minecraft jars, mods, libraries, worlds, logs, and player data",
            "run databases, recordings, screenshots, and audit logs",
        ],
    }


def _git_output(arguments: list[str]) -> str:
    """Return compact Git metadata, or an empty string outside a Git checkout."""

    result = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _scan_for_secrets(stage_root: Path) -> None:
    """Fail closed when common API key or concrete RCON password patterns remain."""

    findings: list[str] = []
    for path in _iter_files(stage_root):
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{path.relative_to(stage_root)}: {label}")
    if findings:
        raise RuntimeError("Secret scan failed:\n" + "\n".join(findings))


def _write_file_manifest(stage_root: Path) -> None:
    """Write SHA-256 hashes for every staged file except the manifest itself."""

    manifest_path = stage_root / "PACKAGE_MANIFEST.sha256"
    lines = []
    for path in _iter_files(stage_root):
        if path == manifest_path:
            continue
        lines.append(f"{_sha256(path)}  {path.relative_to(stage_root).as_posix()}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_zip(stage_root: Path, zip_path: Path, package_name: str) -> None:
    """Create a ZIP archive while preserving Unix executable permission bits."""

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in _iter_files(stage_root):
            relative = Path(package_name) / path.relative_to(stage_root)
            info = zipfile.ZipInfo.from_file(path, arcname=relative.as_posix())
            with path.open("rb") as handle:
                archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED)


def _iter_files(root: Path) -> Iterable[Path]:
    """Yield regular files in stable path order."""

    return (path for path in sorted(root.rglob("*")) if path.is_file())


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file without loading it entirely into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
