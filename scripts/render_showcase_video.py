from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


@dataclass
class StepEvent:
    """One ReAct step reconstructed from audit trajectory events."""

    run_id: str
    step_index: int
    task_id: str | None = None
    observation_time: float | None = None
    context_time: float | None = None
    model_time: float | None = None
    action_result_time: float | None = None
    action: dict[str, Any] | None = None
    action_result: dict[str, Any] | None = None
    reasoning_summary: str | None = None
    usage: dict[str, Any] | None = None


@dataclass
class RunSummary:
    """Run-level fields shown in the generated markdown report."""

    run_id: str
    task_id: str
    status: str
    started_at: str | None
    finished_at: str | None
    steps: int = 0
    actions: dict[str, int] = field(default_factory=dict)
    tokens: int = 0
    knowledge_calls: int = 0


@dataclass
class AuditTimeline:
    """Audited run timeline aligned to recording seconds."""

    recording_start: datetime
    steps: list[StepEvent]
    runs: list[RunSummary]
    video_duration: float


def parse_args() -> argparse.Namespace:
    """Parse showcase rendering options."""

    parser = argparse.ArgumentParser(
        description="Render a meeting-friendly annotated demo video from Minecraft audit logs."
    )
    parser.add_argument("--video", type=Path, required=True, help="Source Minecraft recording MP4.")
    parser.add_argument("--database", type=Path, required=True, help="Audit SQLite database.")
    parser.add_argument("--live-json", type=Path, default=None, help="Optional live run JSON report.")
    parser.add_argument("--output", type=Path, required=True, help="Annotated MP4 output path.")
    parser.add_argument("--report-md", type=Path, default=None, help="Optional markdown report output.")
    parser.add_argument("--fps", type=int, default=12, help="Output and processing FPS.")
    parser.add_argument("--width", type=int, default=1280, help="Output video width.")
    parser.add_argument("--thinking-speed", type=float, default=8.0)
    parser.add_argument("--idle-speed", type=float, default=4.0)
    parser.add_argument("--runtime-speed", type=float, default=1.5)
    parser.add_argument("--action-speed", type=float, default=1.0)
    parser.add_argument("--title", default="Minecraft Agent Harness")
    parser.add_argument("--max-reasoning-chars", type=int, default=220)
    parser.add_argument("--max-result-chars", type=int, default=180)
    return parser.parse_args()


def main() -> None:
    """Render the annotated video and optional report."""

    args = parse_args()
    if args.fps < 1:
        raise SystemExit("--fps must be >= 1.")
    if not args.video.exists():
        raise SystemExit(f"Video not found: {args.video}")
    if not args.database.exists():
        raise SystemExit(f"Database not found: {args.database}")

    duration = _ffprobe_duration(args.video)
    recording_start = _recording_start(args.live_json, args.database)
    timeline = _load_timeline(args.database, recording_start, duration)
    render_stats = _render_video(args, timeline)
    if args.report_md:
        _write_report(args.report_md, args, timeline, render_stats)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "report_md": str(args.report_md) if args.report_md else None,
                "source_duration_sec": round(duration, 2),
                "rendered_duration_sec": round(render_stats["rendered_duration_sec"], 2),
                "written_frames": render_stats["written_frames"],
                "steps": len(timeline.steps),
                "runs": len(timeline.runs),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _load_timeline(database: Path, recording_start: datetime, video_duration: float) -> AuditTimeline:
    """Load trajectory events and run summaries from SQLite."""

    steps: dict[tuple[str, int], StepEvent] = {}
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "select run_id, event_type, payload, task_id, created_at from trajectory_events order by id"
        ):
            event_type = str(row["event_type"])
            payload = _json_loads(row["payload"])
            if not isinstance(payload, dict):
                continue
            step_index = payload.get("step_index")
            event_time = _seconds_from_start(str(row["created_at"]), recording_start)
            if isinstance(step_index, int):
                run_id = str(row["run_id"])
                step = steps.setdefault((run_id, step_index), StepEvent(run_id=run_id, step_index=step_index))
                if row["task_id"]:
                    step.task_id = str(row["task_id"])
                if event_type == "observation":
                    step.observation_time = event_time
                elif event_type == "context_built":
                    step.context_time = event_time
                elif event_type == "model_action":
                    step.model_time = event_time
                    step.action = _dict_or_none(payload.get("action"))
                    decision = _dict_or_none(payload.get("decision")) or {}
                    step.reasoning_summary = _string_or_none(decision.get("reasoning_summary"))
                    step.usage = _dict_or_none(payload.get("usage"))
                elif event_type == "action_result":
                    step.action_result_time = event_time
                    step.action = _dict_or_none(payload.get("action")) or step.action
                    step.action_result = _dict_or_none(payload.get("result"))

        runs = _load_runs(conn)
        _augment_runs(conn, runs)
    ordered_steps = sorted(
        steps.values(),
        key=lambda step: (_step_start_time(step), step.run_id, step.step_index),
    )
    return AuditTimeline(
        recording_start=recording_start,
        steps=ordered_steps,
        runs=runs,
        video_duration=video_duration,
    )


def _load_runs(conn: sqlite3.Connection) -> list[RunSummary]:
    """Load run rows while tolerating old schema versions."""

    rows: list[RunSummary] = []
    columns = {row["name"] for row in conn.execute("pragma table_info(runs)")}
    status_expr = "status" if "status" in columns else "lifecycle_status"
    for row in conn.execute(
        f"select id, task_id, {status_expr} as status, started_at, finished_at from runs order by started_at"
    ):
        rows.append(
            RunSummary(
                run_id=str(row["id"]),
                task_id=str(row["task_id"]),
                status=str(row["status"]),
                started_at=str(row["started_at"]) if row["started_at"] else None,
                finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            )
        )
    return rows


def _augment_runs(conn: sqlite3.Connection, runs: list[RunSummary]) -> None:
    """Attach compact metrics to each run summary."""

    by_id = {run.run_id: run for run in runs}
    for row in conn.execute("select run_id, action from steps order by id"):
        run = by_id.get(str(row["run_id"]))
        if run is None:
            continue
        action = _json_loads(row["action"])
        action_type = action.get("type") if isinstance(action, dict) else None
        if action_type:
            run.actions[str(action_type)] = run.actions.get(str(action_type), 0) + 1
        run.steps += 1
    for row in conn.execute("select run_id, usage from model_calls order by id"):
        run = by_id.get(str(row["run_id"]))
        if run is None:
            continue
        usage = _json_loads(row["usage"])
        if isinstance(usage, dict):
            run.tokens += int(usage.get("total_tokens") or 0)
    for row in conn.execute("select task_id, count(*) as calls from trajectory_events where event_type='knowledge_tool_call' group by task_id"):
        task_id = str(row["task_id"])
        for run in runs:
            if run.task_id == task_id:
                run.knowledge_calls = int(row["calls"])


def _render_video(args: argparse.Namespace, timeline: AuditTimeline) -> dict[str, Any]:
    """Stream source frames through Pillow overlays and ffmpeg encoding."""

    source_width, source_height = _ffprobe_size(args.video)
    out_width = int(args.width)
    out_height = _even_int(source_height * out_width / source_width)
    fps = int(args.fps)
    frame_size = out_width * out_height * 3
    args.output.parent.mkdir(parents=True, exist_ok=True)

    decode = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(args.video),
            "-vf",
            f"scale={out_width}:{out_height},fps={fps},format=rgb24",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    encode = subprocess.Popen(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{out_width}x{out_height}",
            "-r",
            str(fps),
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            str(args.output),
        ],
        stdin=subprocess.PIPE,
    )
    assert decode.stdout is not None
    assert encode.stdin is not None

    fonts = _load_fonts(out_width)
    credit = 0.0
    frame_index = 0
    written_frames = 0
    try:
        while True:
            raw = decode.stdout.read(frame_size)
            if not raw:
                break
            if len(raw) != frame_size:
                break
            source_t = frame_index / fps
            phase = _phase_for_time(timeline.steps, source_t)
            speed = _speed_for_phase(args, phase["kind"])
            credit += 1.0 / max(1.0, speed)
            if credit >= 0.999:
                credit -= 1.0
                image = Image.frombytes("RGB", (out_width, out_height), raw)
                _draw_overlay(image, args, timeline, phase, source_t, speed, fonts)
                encode.stdin.write(image.tobytes())
                written_frames += 1
            frame_index += 1
    finally:
        decode.stdout.close()
        if encode.stdin:
            encode.stdin.close()
        decode.wait(timeout=10)
        encode.wait(timeout=60)

    if decode.returncode not in (0, None):
        raise SystemExit(f"ffmpeg decode failed with exit code {decode.returncode}")
    if encode.returncode not in (0, None):
        raise SystemExit(f"ffmpeg encode failed with exit code {encode.returncode}")
    return {
        "written_frames": written_frames,
        "rendered_duration_sec": written_frames / fps if fps else 0,
        "source_width": source_width,
        "source_height": source_height,
        "output_width": out_width,
        "output_height": out_height,
    }


def _phase_for_time(steps: list[StepEvent], source_t: float) -> dict[str, Any]:
    """Return the step and phase active at one source timestamp."""

    active: StepEvent | None = None
    active_index: int | None = None
    for index, step in enumerate(steps):
        start = _step_start_time(step)
        if start <= source_t:
            active = step
            active_index = index
        if start > source_t:
            break
    if active is None:
        return {"kind": "idle", "step": None, "label": "Preparing run"}
    next_start = None
    if active_index is not None:
        for step in steps[active_index + 1 :]:
            start = _step_start_time(step)
            if math.isfinite(start):
                next_start = start
                break
    if active.context_time is not None and active.model_time is not None and active.context_time <= source_t < active.model_time:
        return {"kind": "thinking", "step": active, "label": "LLM thinking"}
    if active.model_time is not None and source_t >= active.model_time:
        if active.action_result_time is None or source_t < active.action_result_time:
            return {"kind": "action", "step": active, "label": "Executing action"}
        if next_start is None or source_t < next_start:
            return {"kind": "runtime", "step": active, "label": "Runtime result"}
    return {"kind": "idle", "step": active, "label": "Observing world"}


def _step_start_time(step: StepEvent) -> float:
    """Return the earliest timestamp known for one reconstructed ReAct step."""

    markers = [
        value
        for value in (
            step.observation_time,
            step.context_time,
            step.model_time,
            step.action_result_time,
        )
        if value is not None
    ]
    return min(markers) if markers else math.inf


def _speed_for_phase(args: argparse.Namespace, kind: str) -> float:
    """Return output speed factor for a phase."""

    if kind == "thinking":
        return float(args.thinking_speed)
    if kind == "runtime":
        return float(args.runtime_speed)
    if kind == "action":
        return float(args.action_speed)
    return float(args.idle_speed)


def _draw_overlay(
    image: Image.Image,
    args: argparse.Namespace,
    timeline: AuditTimeline,
    phase: dict[str, Any],
    source_t: float,
    speed: float,
    fonts: dict[str, ImageFont.FreeTypeFont | ImageFont.ImageFont],
) -> None:
    """Draw task/action/reasoning overlays onto one frame."""

    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    step = phase.get("step")
    task_id = _current_task_id(timeline, source_t, step)
    header_lines = [
        args.title,
        f"Task: {_short_task(task_id)}",
        f"Phase: {phase['label']} | speed x{speed:g} | t={_format_time(source_t)}",
    ]
    _panel(draw, 18, 18, min(width - 36, 760), 128, fill=(8, 14, 28, 190))
    y = 30
    draw.text((34, y), header_lines[0], font=fonts["title"], fill=(255, 255, 255, 255))
    y += 34
    draw.text((34, y), header_lines[1], font=fonts["body"], fill=(216, 232, 255, 255))
    y += 26
    phase_color = (255, 211, 105, 255) if phase["kind"] == "thinking" else (143, 232, 181, 255)
    draw.text((34, y), header_lines[2], font=fonts["body"], fill=phase_color)

    if step is not None:
        action_text = _json_compact(step.action, limit=260)
        reasoning = _truncate(step.reasoning_summary or "", args.max_reasoning_chars)
        result_text = _result_summary(step.action_result, args.max_result_chars)
        body_lines = [
            f"Step {step.step_index}: action schema",
            action_text,
            f"Reasoning: {reasoning or 'waiting for model decision'}",
            f"Result: {result_text}",
        ]
    else:
        body_lines = ["Waiting for agent audit events."]
    panel_h = 176 if step is not None else 70
    _panel(draw, 18, height - panel_h - 26, width - 36, panel_h, fill=(5, 9, 18, 205))
    text_x = 34
    text_y = height - panel_h - 12
    draw.text((text_x, text_y), body_lines[0], font=fonts["subtitle"], fill=(255, 255, 255, 255))
    text_y += 30
    if len(body_lines) > 1:
        for line in _wrap_lines(body_lines[1], width=110)[:2]:
            draw.text((text_x, text_y), line, font=fonts["mono"], fill=(191, 224, 255, 255))
            text_y += 23
        for line in _wrap_lines(body_lines[2], width=120)[:2]:
            draw.text((text_x, text_y), line, font=fonts["small"], fill=(235, 239, 245, 255))
            text_y += 22
        for line in _wrap_lines(body_lines[3], width=120)[:2]:
            draw.text((text_x, text_y), line, font=fonts["small"], fill=(198, 243, 212, 255))
            text_y += 22

    if phase["kind"] == "thinking":
        _panel(draw, width - 350, 18, 332, 68, fill=(92, 65, 8, 205))
        draw.text((width - 332, 34), "API wait compressed", font=fonts["subtitle"], fill=(255, 235, 185, 255))
        draw.text((width - 332, 60), f"LLM deliberation shown at x{speed:g}", font=fonts["small"], fill=(255, 248, 222, 255))

    progress = max(0.0, min(1.0, source_t / max(1.0, timeline.video_duration)))
    draw.rounded_rectangle((18, height - 12, width - 18, height - 6), radius=3, fill=(255, 255, 255, 70))
    draw.rounded_rectangle((18, height - 12, 18 + int((width - 36) * progress), height - 6), radius=3, fill=(68, 190, 255, 210))


def _write_report(
    output_path: Path,
    args: argparse.Namespace,
    timeline: AuditTimeline,
    render_stats: dict[str, Any],
) -> None:
    """Write a concise markdown report for the rendered demo."""

    lines = [
        "# Minecraft Agent Harness Demo Report",
        "",
        "## Video",
        f"- Source: `{args.video}`",
        f"- Showcase: `{args.output}`",
        f"- Source duration: `{timeline.video_duration:.1f}s`",
        f"- Rendered duration: `{render_stats['rendered_duration_sec']:.1f}s`",
        f"- Thinking speed: `x{args.thinking_speed:g}`",
        f"- Idle speed: `x{args.idle_speed:g}`",
        "",
        "## Runs",
    ]
    for run in timeline.runs:
        actions = ", ".join(f"{key}={value}" for key, value in sorted(run.actions.items())) or "none"
        lines.extend(
            [
                f"### {run.task_id}",
                f"- Status: `{run.status}`",
                f"- Steps: `{run.steps}`",
                f"- Model tokens: `{run.tokens}`",
                f"- Knowledge calls: `{run.knowledge_calls}`",
                f"- Actions: {actions}",
                "",
            ]
        )
    lines.extend(
        [
            "## Notes",
            "- The overlay is generated from persisted audit events, not handwritten timestamps.",
            "- LLM waiting intervals are accelerated while action execution segments are kept near real time.",
            "- Action schemas and reasoning summaries are copied from the model call audit payload.",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _recording_start(live_json: Path | None, database: Path) -> datetime:
    """Return recording start time from live JSON or the first audit event."""

    if live_json and live_json.exists():
        payload = json.loads(live_json.read_text(encoding="utf-8"))
        value = payload.get("started_at")
        if isinstance(value, str):
            return _parse_datetime(value)
    with sqlite3.connect(database) as conn:
        row = conn.execute("select min(created_at) from trajectory_events").fetchone()
    if row and row[0]:
        return _parse_datetime(str(row[0]))
    return datetime.now(tz=timezone.utc)


def _seconds_from_start(value: str, start: datetime) -> float:
    """Convert a database timestamp into recording seconds."""

    return max(0.0, (_parse_datetime(value) - start).total_seconds())


def _parse_datetime(value: str) -> datetime:
    """Parse SQLite or ISO timestamp strings as UTC when no timezone is present."""

    cleaned = value.strip().replace("Z", "+00:00")
    if "T" not in cleaned and " " in cleaned:
        cleaned = cleaned.replace(" ", "T", 1)
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ffprobe_duration(video: Path) -> float:
    """Return video duration in seconds."""

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    return float(payload["format"]["duration"])


def _ffprobe_size(video: Path) -> tuple[int, int]:
    """Return source video dimensions."""

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _load_fonts(width: int) -> dict[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    """Load fonts for overlay rendering."""

    font_path = next((Path(path) for path in FONT_CANDIDATES if Path(path).exists()), None)

    def load(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if font_path:
            return ImageFont.truetype(str(font_path), size)
        return ImageFont.load_default()

    scale = max(1.0, width / 1280)
    return {
        "title": load(int(24 * scale)),
        "subtitle": load(int(20 * scale)),
        "body": load(int(17 * scale)),
        "small": load(int(15 * scale)),
        "mono": load(int(15 * scale)),
    }


def _panel(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    fill: tuple[int, int, int, int],
) -> None:
    """Draw one rounded translucent panel."""

    draw.rounded_rectangle((x, y, x + width, y + height), radius=12, fill=fill)
    draw.rounded_rectangle((x, y, x + width, y + height), radius=12, outline=(255, 255, 255, 50), width=1)


def _current_task_id(timeline: AuditTimeline, source_t: float, step: StepEvent | None) -> str:
    """Infer the active task id for one source timestamp."""

    if step and step.task_id:
        return step.task_id
    active = timeline.runs[0].task_id if timeline.runs else "unknown"
    for run in timeline.runs:
        if not run.started_at:
            continue
        started = _seconds_from_start(run.started_at, timeline.recording_start)
        finished = _seconds_from_start(run.finished_at, timeline.recording_start) if run.finished_at else math.inf
        if started <= source_t <= finished:
            active = run.task_id
            break
    return active


def _short_task(task_id: str) -> str:
    """Make a MineDojo task id readable in a compact overlay."""

    text = task_id.replace("_", " ")
    return _truncate(text, 86)


def _json_compact(value: Any, *, limit: int) -> str:
    """Return compact JSON for overlay text."""

    if value is None:
        return "{}"
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _truncate(text, limit)


def _result_summary(result: dict[str, Any] | None, limit: int) -> str:
    """Return a compact result summary for the overlay."""

    if not result:
        return "waiting for runtime result"
    parts = [f"ok={result.get('ok')}"]
    for key in ("action_type", "error_code", "message", "progress_status"):
        if result.get(key) is not None:
            parts.append(f"{key}={result.get(key)}")
    if isinstance(result.get("inventory_delta"), dict) and result["inventory_delta"]:
        parts.append(f"inventory_delta={result['inventory_delta']}")
    if isinstance(result.get("blocks"), list):
        parts.append(f"blocks_found={len(result['blocks'])}")
    if isinstance(result.get("entities"), list):
        parts.append(f"entities_found={len(result['entities'])}")
    return _truncate("; ".join(parts), limit)


def _wrap_lines(text: str, *, width: int) -> list[str]:
    """Wrap overlay text without splitting every JSON token too aggressively."""

    if not text:
        return [""]
    return textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False)


def _truncate(text: str, limit: int) -> str:
    """Truncate overlay text with an ellipsis."""

    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _json_loads(value: Any) -> Any:
    """Decode JSON strings while tolerating already-decoded values."""

    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    """Return value only when it is a dictionary."""

    return value if isinstance(value, dict) else None


def _string_or_none(value: Any) -> str | None:
    """Return value only when it is a string."""

    return value if isinstance(value, str) else None


def _format_time(seconds: float) -> str:
    """Format seconds as mm:ss."""

    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _even_int(value: float) -> int:
    """Round to a positive even integer."""

    rounded = max(2, int(round(value)))
    return rounded if rounded % 2 == 0 else rounded - 1


if __name__ == "__main__":
    main()
