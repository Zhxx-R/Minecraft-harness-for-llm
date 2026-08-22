from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "release"
DEFAULT_DATABASE_DUMP = ROOT / "runs" / "backups" / "mc_agent_20260728_pre_trace_spans.dump"

EXCLUDED_TOP_LEVEL = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".runtime",
    ".tmp",
    "release",
}
EXCLUDED_PARTS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
}
EXCLUDED_NAMES = {
    ".DS_Store",
    ".env",
    ".env.local",
    "eula.txt",
    "server.properties",
    "session.lock",
}
EXCLUDED_SUFFIXES = {
    ".pid",
    ".pyc",
    ".pyo",
    ".sqlite-shm",
    ".sqlite-wal",
}
ALREADY_COMPRESSED_SUFFIXES = {
    ".7z",
    ".avi",
    ".docx",
    ".dump",
    ".flac",
    ".gif",
    ".gz",
    ".jar",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mca",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pptx",
    ".pth",
    ".rar",
    ".sqlite",
    ".sqlite3",
    ".webm",
    ".webp",
    ".xlsx",
    ".xz",
    ".zip",
}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".mjs",
    ".properties",
    ".ps1",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
SECRET_PATTERNS = {
    "OpenAI-compatible API key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    "configured QWEN_API_KEY": re.compile(
        rb"(?m)^QWEN_API_KEY=(?!(?:replace-me|<[^>]+>)\s*$)\S+"
    ),
    "configured DASHSCOPE_API_KEY": re.compile(
        rb"(?m)^DASHSCOPE_API_KEY=(?!(?:replace-me|<[^>]+>)\s*$)\S+"
    ),
    "configured RCON password": re.compile(
        rb"(?m)^rcon\.password=(?!(?:__RCON_PASSWORD__|replace-me|<[^>]+>)\s*$)\S+"
    ),
}


@dataclass(frozen=True)
class PackageEntry:
    source: Path
    archive_path: Path
    size: int
    sha256: str


def parse_args() -> argparse.Namespace:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(
        description="Build a complete Windows transfer ZIP for Minecraft Agent Harness."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--name",
        default=f"minecraft-agent-harness-windows-complete-{timestamp}",
    )
    parser.add_argument(
        "--database-dump",
        type=Path,
        default=DEFAULT_DATABASE_DUMP,
        help="PostgreSQL custom-format dump to expose as database/postgres/mc_agent.dump.",
    )
    parser.add_argument(
        "--skip-archive-test",
        action="store_true",
        help="Skip the final full CRC readback (not recommended for handoff builds).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    database_dump = args.database_dump.resolve()
    if not database_dump.is_file():
        raise FileNotFoundError(f"Database dump not found: {database_dump}")
    if not _looks_like_postgres_custom_dump(database_dump):
        raise RuntimeError(f"Not a PostgreSQL custom-format dump: {database_dump}")

    build_dir = output_dir / ".windows-transfer-build" / args.name
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    print("Collecting and hashing package files...", flush=True)
    entries = _collect_entries(database_dump)
    _validate_windows_paths(entries)
    _scan_for_secrets(entries)

    build_info = _build_info(args.name, database_dump, entries)
    build_info_path = build_dir / "PACKAGE_BUILD_INFO.json"
    build_info_path.write_text(
        json.dumps(build_info, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    build_info_entry = _entry(build_info_path, Path("PACKAGE_BUILD_INFO.json"))
    entries.append(build_info_entry)
    entries.sort(key=lambda item: item.archive_path.as_posix())

    manifest_path = build_dir / "PACKAGE_MANIFEST.sha256"
    manifest_path.write_text(
        "".join(
            f"{item.sha256}  {item.archive_path.as_posix()}\n"
            for item in entries
        ),
        encoding="utf-8",
    )
    entries.append(_entry(manifest_path, Path("PACKAGE_MANIFEST.sha256")))
    entries.sort(key=lambda item: item.archive_path.as_posix())

    archive_path = output_dir / f"{args.name}.zip"
    if archive_path.exists():
        archive_path.unlink()
    print(
        f"Writing ZIP64 archive with {len(entries):,} files "
        f"({sum(item.size for item in entries) / (1024**3):.2f} GiB uncompressed)...",
        flush=True,
    )
    _write_archive(archive_path, args.name, entries)

    if not args.skip_archive_test:
        print("Reading the complete archive back for CRC verification...", flush=True)
        with zipfile.ZipFile(archive_path, "r") as archive:
            failed_name = archive.testzip()
        if failed_name:
            raise RuntimeError(f"Archive CRC verification failed: {failed_name}")

    archive_sha256 = _sha256(archive_path)
    checksum_path = archive_path.with_name(f"{archive_path.name}.sha256")
    checksum_path.write_text(
        f"{archive_sha256}  {archive_path.name}\n",
        encoding="utf-8",
    )

    for source_name in (
        "VERIFY_AND_EXTRACT.ps1",
        "README_FIRST.zh-CN.md",
        "PROMPT_FOR_CODEX_WINDOWS.zh-CN.txt",
        "REASSEMBLE.ps1",
    ):
        shutil.copy2(ROOT / "windows" / source_name, output_dir / source_name)

    parts = []
    if archive_path.stat().st_size >= 4_000_000_000:
        print("Archive exceeds 4 GB; writing FAT32-safe transfer parts...", flush=True)
        parts = _split_archive(archive_path, 1_900_000_000)

    report = {
        "schema_version": "mc-agent-harness.windows-transfer-build.v1",
        "package_name": args.name,
        "archive": str(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": archive_sha256,
        "source_file_count": len(entries),
        "source_bytes": sum(item.size for item in entries),
        "database_dump_archive_path": "database/postgres/mc_agent.dump",
        "parts": [
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in parts
        ],
        "crc_verified": not args.skip_archive_test,
    }
    report_path = output_dir / f"{args.name}.build.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), flush=True)


def _collect_entries(database_dump: Path) -> list[PackageEntry]:
    entries: list[PackageEntry] = []
    database_relative = database_dump.relative_to(ROOT)
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
            if relative == database_relative or _is_excluded(relative):
                continue
            if source.is_symlink():
                resolved = source.resolve(strict=True)
                try:
                    resolved.relative_to(ROOT)
                except ValueError as exc:
                    raise RuntimeError(
                        f"Refusing to dereference symlink outside project: {relative}"
                    ) from exc
                if not resolved.is_file():
                    continue
                entries.append(_entry(resolved, relative))
                continue
            entries.append(_entry(source, relative))

    entries.append(
        _entry(database_dump, Path("database") / "postgres" / "mc_agent.dump")
    )
    return entries


def _is_excluded(relative: Path) -> bool:
    if not relative.parts:
        return False
    if relative.parts[0] in EXCLUDED_TOP_LEVEL:
        return True
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return True
    if relative.name in EXCLUDED_NAMES:
        return True
    if relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    return False


def _entry(source: Path, archive_path: Path) -> PackageEntry:
    return PackageEntry(
        source=source,
        archive_path=archive_path,
        size=source.stat().st_size,
        sha256=_sha256(source),
    )


def _scan_for_secrets(entries: list[PackageEntry]) -> None:
    findings: list[str] = []
    for entry in entries:
        if entry.archive_path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if entry.size > 128 * 1024 * 1024:
            continue
        try:
            content = entry.source.read_bytes()
        except OSError:
            continue
        if b"\x00" in content[:4096]:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{entry.archive_path.as_posix()}: {label}")
    if findings:
        raise RuntimeError(
            "Secret scan failed. Sensitive values were not archived:\n"
            + "\n".join(findings)
        )


def _validate_windows_paths(entries: list[PackageEntry]) -> None:
    seen: dict[str, str] = {}
    invalid_characters = re.compile(r'[<>:"|?*]')
    for entry in entries:
        path_text = entry.archive_path.as_posix()
        collision_key = path_text.casefold()
        if collision_key in seen:
            raise RuntimeError(
                f"Case-insensitive Windows path collision: {seen[collision_key]} / {path_text}"
            )
        seen[collision_key] = path_text
        for part in entry.archive_path.parts:
            if invalid_characters.search(part) or part.endswith((" ", ".")):
                raise RuntimeError(f"Windows-incompatible archive path: {path_text}")


def _build_info(
    package_name: str,
    database_dump: Path,
    entries: list[PackageEntry],
) -> dict[str, object]:
    status_lines = _git_output(["status", "--porcelain=v1"]).splitlines()
    dump_stat = database_dump.stat()
    return {
        "schema_version": "mc-agent-harness.windows-transfer.v1",
        "package_name": package_name,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "source_commit": _git_output(["rev-parse", "HEAD"]),
        "source_branch": _git_output(["branch", "--show-current"]),
        "source_dirty": bool(status_lines),
        "source_changes": status_lines,
        "source_file_count": len(entries),
        "source_bytes": sum(item.size for item in entries),
        "database": {
            "archive_path": "database/postgres/mc_agent.dump",
            "source_relative_path": database_dump.relative_to(ROOT).as_posix(),
            "source_modified_at": datetime.fromtimestamp(
                dump_stat.st_mtime, tz=UTC
            ).isoformat(),
            "bytes": dump_stat.st_size,
            "sha256": _sha256(database_dump),
            "format": "PostgreSQL custom dump v1.15",
            "restore_image": "pgvector/pgvector:pg16",
        },
        "included_runtime_state": [
            "runs: JSON, logs, screenshots, recordings, and SQLite audit databases",
            "week10 recovery dumps",
            "Minecraft 1.20.1 server binaries and world data",
            "MineCLIP vendor code, tokenizer cache, and attn checkpoint",
        ],
        "excluded_machine_or_sensitive_state": [
            ".env and .env.local credentials",
            ".git history and remote metadata",
            "macOS Python virtual environments and Node node_modules",
            "generated type/test/build caches",
            "Minecraft eula.txt, server.properties, PID files, and session locks",
            "previous release archives",
        ],
        "known_limitations": [
            "The PostgreSQL source engine was unavailable at package time; the newest local custom dump was used.",
            "Historical records retain original macOS absolute paths as provenance.",
            "macOS window capture must be replaced for Windows-native Week 11 recording.",
        ],
    }


def _git_output(arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _write_archive(
    archive_path: Path,
    package_name: str,
    entries: list[PackageEntry],
) -> None:
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for index, entry in enumerate(entries, start=1):
            compression = (
                zipfile.ZIP_STORED
                if entry.archive_path.suffix.lower() in ALREADY_COMPRESSED_SUFFIXES
                else zipfile.ZIP_DEFLATED
            )
            archive_name = (Path(package_name) / entry.archive_path).as_posix()
            archive.write(entry.source, archive_name, compress_type=compression)
            if index % 5_000 == 0:
                print(f"  archived {index:,}/{len(entries):,} files", flush=True)


def _split_archive(archive_path: Path, part_bytes: int) -> list[Path]:
    parts: list[Path] = []
    with archive_path.open("rb") as source:
        index = 1
        while True:
            chunk = source.read(part_bytes)
            if not chunk:
                break
            part_path = archive_path.with_name(f"{archive_path.name}.part{index:03d}")
            part_path.write_bytes(chunk)
            part_path.with_name(f"{part_path.name}.sha256").write_text(
                f"{_sha256(part_path)}  {part_path.name}\n",
                encoding="utf-8",
            )
            parts.append(part_path)
            index += 1
    return parts


def _looks_like_postgres_custom_dump(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(5) == b"PGDMP"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
