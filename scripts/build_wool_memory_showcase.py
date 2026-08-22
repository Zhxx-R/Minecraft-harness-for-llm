#!/usr/bin/env python3
"""Build a presentation-ready cut of the wool Memory Update demo.

The source recording is mostly model API latency. This script preserves the
original frame order, accelerates only selected wait ranges, overlays audited
actions and key model-visible context, and writes a concise companion report.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


DEFAULT_RUN_DIR = Path(
    "runs/demos/programmatic_wool_memory_brown_sheep_20260726_run4/with_skill"
)
FONT_PATH = Path("/System/Library/Fonts/Hiragino Sans GB.ttc")


@dataclass(frozen=True)
class Card:
    card_id: str
    eyebrow: str
    title: str
    lines: tuple[str, ...]
    badge: str
    accent: tuple[int, int, int]


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    speed: float
    card_id: str
    purpose: str

    @property
    def edited_duration(self) -> float:
        return (self.end - self.start) / self.speed


CARDS = {
    card.card_id: card
    for card in (
        Card(
            "intro",
            "MEMORY UPDATE · LIVE DEMO",
            "随机出生 · 棕色羊干扰 · 白色羊毛目标",
            (
                "TASK     white_wool  +0 / +1",
                "INPUT    main_hand=shears · nearby sheep #13328",
                "EDIT     模型 API 等待段已加速；Agent Action 保持原始画面",
            ),
            "SHOWCASE CUT",
            (76, 141, 255),
        ),
        Card(
            "api0",
            "MODEL INPUT · STEP 0",
            "发送关键上下文，等待首个 Action",
            (
                "CONTEXT  goal_not_yet_satisfied · white_wool +0/+1",
                "CONTEXT  inventory=shears ×1 · sheep #13328 nearby",
                "SKILL    white_wool workflow（本轮审计保留 legacy id）",
            ),
            "API WAIT ×7",
            (245, 183, 66),
        ),
        Card(
            "move0",
            "AGENT ACTION · STEP 0",
            "move_to(position=(-125.48, 67, 104.93))",
            (
                "ARGS     tolerance=1.5",
                "CONTEXT  target not yet verified · white_wool +0/+1",
                "RESULT   navigation succeeded",
            ),
            "ACTION · REAL TIME",
            (76, 141, 255),
        ),
        Card(
            "api1",
            "MODEL INPUT · STEP 1",
            "根据上一动作结果继续规划",
            (
                "CONTEXT  previous move_to succeeded",
                "CONTEXT  goal remains white_wool +0/+1",
                "CONTEXT  sheep target remains visible",
            ),
            "API WAIT ×10",
            (245, 183, 66),
        ),
        Card(
            "move1",
            "AGENT ACTION · STEP 1",
            "move_to(position=(-130.30, 67, 99.50))",
            (
                "ARGS     tolerance=2.0",
                "CONTEXT  approach the selected moving target",
                "RESULT   navigation succeeded",
            ),
            "ACTION · REAL TIME",
            (76, 141, 255),
        ),
        Card(
            "api2",
            "MODEL INPUT · STEP 2",
            "长耗时调用：一次 timeout 后自动重试",
            (
                "CONTEXT  previous move_to succeeded",
                "CONTEXT  target sheep #13328 is still available",
                "TRACE    model_timeout → model_timeout_retry → response",
            ),
            "81s → 4s",
            (239, 102, 102),
        ),
        Card(
            "follow",
            "AGENT ACTION · STEP 2",
            "follow(entity_id=13328)",
            (
                "ARGS     follow_distance=1.5",
                "CONTEXT  metadata wool.color_id=12 · color=brown",
                "EFFECT   持续跟随，直到下一条 Action 到达",
            ),
            "ACTION · REAL TIME",
            (76, 141, 255),
        ),
        Card(
            "follow_api",
            "ACTIVE ACTION + MODEL INPUT · STEP 3",
            "follow 仍在执行，模型选择下一动作",
            (
                "CONTEXT  target #13328 · brown · is_sheared=false",
                "CONTEXT  verifier requires white_wool +1",
                "DECISION preserve the mismatch as durable memory",
            ),
            "API WAIT ×5",
            (245, 183, 66),
        ),
        Card(
            "memory_rejected",
            "AGENT ACTION · STEP 3",
            "scan_entities(entity=sheep) + Memory Update",
            (
                "MEMORY   REJECTED · path_not_found",
                "CAUSE    pointer omitted /details before metadata_decoded",
                "NEXT     use entity-scoped source_ref returned by scan",
            ),
            "MEMORY · REJECTED",
            (239, 102, 102),
        ),
        Card(
            "api4",
            "MODEL INPUT · STEP 4",
            "利用实体级 source_ref 修复字段路径",
            (
                "CONTEXT  step:3/scan_entities/entity:13328",
                "CONTEXT  /details/metadata_decoded/wool/color",
                "CONTEXT  /details/metadata_decoded/wool/color_id",
            ),
            "API WAIT ×10",
            (245, 183, 66),
        ),
        Card(
            "memory_accepted",
            "AGENT ACTION · STEP 4",
            "scan_entities(entity=sheep) + Memory Update",
            (
                "MEMORY   ACCEPTED · sheep_13328_is_brown_not_white",
                "FACT     entity_id=13328 · color=brown · color_id=12",
                "POLICY   do not target this sheep for white_wool",
            ),
            "MEMORY · STORED",
            (65, 195, 132),
        ),
        Card(
            "api5",
            "MODEL INPUT · STEP 5",
            "新记忆已进入后续上下文",
            (
                "MEMORY   #13328=brown · not a white_wool target",
                "CONTEXT  only nearby sheep is #13328",
                "DECISION scan for another sheep instead of shearing it",
            ),
            "API WAIT ×10",
            (245, 183, 66),
        ),
        Card(
            "memory_used",
            "AGENT ACTION · STEP 5",
            "scan_entities(entity=sheep, max_distance=128)",
            (
                "CONTEXT  memory excludes brown sheep #13328",
                "RESULT   only #13328 found",
                "DECISION continue searching; no repeated shearing",
            ),
            "MEMORY · IN USE",
            (65, 195, 132),
        ),
        Card(
            "api_scan",
            "MODEL INPUT · COMPRESSED SEARCH",
            "相同记忆持续生效，纯等待区间已加速",
            (
                "MEMORY   #13328 remains brown and excluded",
                "PROGRESS white_wool +0/+1",
                "TRACE    repeated scan decisions, no interaction with #13328",
            ),
            "API WAIT ×12–20",
            (245, 183, 66),
        ),
        Card(
            "scan_montage",
            "AGENT ACTION · SEARCH MONTAGE",
            "scan_entities ×4",
            (
                "CONTEXT  memory persisted across compressed trajectory",
                "RESULT   brown sheep #13328 repeatedly recognized",
                "BEHAVIOR no use_item / no repeated shearing",
            ),
            "MEMORY · PERSISTENT",
            (65, 195, 132),
        ),
        Card(
            "scan50",
            "AGENT ACTION · STEP 9",
            "scan_entities(entity=sheep, count=50)",
            (
                "MEMORY   #13328=brown · excluded",
                "RESULT   no valid white sheep selected",
                "CONTEXT  observed white_wool drop enters next prompt",
            ),
            "ACTION · REAL TIME",
            (76, 141, 255),
        ),
        Card(
            "api10",
            "MODEL INPUT · STEP 10",
            "关键上下文出现 white_wool 掉落物",
            (
                "CONTEXT  observed_dropped_items: white_wool ×1",
                "CONTEXT  distance=15.18 · inventory delta still 0/1",
                "DECISION move to the evidence-backed drop coordinate",
            ),
            "API WAIT ×5",
            (245, 183, 66),
        ),
        Card(
            "pickup_move",
            "AGENT ACTION · STEP 10",
            "move_to(position=(-103.20, 74, 147.57))",
            (
                "TARGET   observed white_wool drop",
                "CONTEXT  verifier checks inventory delta +1",
                "RESULT   reached distance 1.68",
            ),
            "ACTION · REAL TIME",
            (76, 141, 255),
        ),
        Card(
            "api11",
            "MODEL INPUT · STEP 11",
            "最后一次决策：进入拾取范围",
            (
                "CONTEXT  white_wool ×1 at distance 1.68",
                "CONTEXT  memory still excludes brown sheep #13328",
                "DECISION move to exact drop coordinate and verify inventory",
            ),
            "API WAIT ×8",
            (245, 183, 66),
        ),
        Card(
            "success",
            "TASK EVALUATOR · FINAL",
            "PASS · white_wool inventory delta +1 / +1",
            (
                "MEMORY   brown sheep #13328 remained excluded",
                "TRACE    no repeated shearing of the mismatched entity",
                "BOUNDARY completion came from picking up the spawned sheep drop",
            ),
            "VERIFIER · PASSED",
            (65, 195, 132),
        ),
    )
}


SEGMENTS = (
    Segment(0.0, 6.0, 1.0, "intro", "开场：任务与随机环境"),
    Segment(6.0, 20.5, 7.0, "api0", "模型调用：首个决策"),
    Segment(20.5, 23.0, 1.0, "move0", "Action 0：靠近候选目标"),
    Segment(23.0, 49.0, 10.0, "api1", "模型调用：根据移动结果规划"),
    Segment(49.0, 52.5, 1.0, "move1", "Action 1：继续接近目标"),
    Segment(52.5, 129.5, 20.0, "api2", "模型调用：超时与自动重试"),
    Segment(129.5, 143.0, 1.0, "follow", "Action 2：持续跟随棕羊"),
    Segment(143.0, 151.5, 5.0, "follow_api", "跟随期间继续模型决策"),
    Segment(151.5, 155.5, 1.0, "memory_rejected", "Memory Update：错误路径被拒绝"),
    Segment(155.5, 187.5, 10.0, "api4", "模型调用：修复字段路径"),
    Segment(187.5, 193.5, 1.0, "memory_accepted", "Memory Update：实体事实写入成功"),
    Segment(193.5, 221.0, 10.0, "api5", "模型调用：首次读取已存记忆"),
    Segment(221.0, 225.0, 1.0, "memory_used", "记忆影响下一条 Action"),
    Segment(225.0, 254.0, 15.0, "api_scan", "模型调用：压缩搜索等待"),
    Segment(254.0, 258.0, 1.0, "scan_montage", "Action：继续搜索白羊"),
    Segment(258.0, 279.0, 12.0, "api_scan", "模型调用：压缩搜索等待"),
    Segment(279.0, 283.0, 1.0, "scan_montage", "Action：继续搜索白羊"),
    Segment(283.0, 302.0, 12.0, "api_scan", "模型调用：压缩搜索等待"),
    Segment(302.0, 306.0, 1.0, "scan_montage", "Action：扩大实体扫描"),
    Segment(306.0, 356.0, 20.0, "api_scan", "模型调用：压缩长等待"),
    Segment(356.0, 361.0, 1.0, "scan50", "Action 9：扩大羊实体扫描"),
    Segment(361.0, 368.0, 5.0, "api10", "模型调用：发现白羊毛掉落物"),
    Segment(368.0, 377.0, 1.0, "pickup_move", "Action 10：移动至白羊毛"),
    Segment(377.0, 387.0, 8.0, "api11", "模型调用：最后一次决策"),
    Segment(387.0, 390.36, 1.0, "success", "Evaluator：任务通过"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe")
    return parser.parse_args()


def run_command(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
    output = run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=width,height,avg_frame_rate",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(output)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    if not FONT_PATH.is_file():
        raise FileNotFoundError(f"Chinese display font not found: {FONT_PATH}")
    return ImageFont.truetype(str(FONT_PATH), size=size)


def draw_card(card: Card, width: int, height: int, output_path: Path) -> None:
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    scale = width / 1708.0
    margin = round(34 * scale)
    panel_width = round(min(width - margin * 2, 1125 * scale))
    panel_height = round(262 * scale)
    left = margin
    top = height - panel_height - margin
    radius = round(20 * scale)
    accent_width = round(8 * scale)

    draw.rounded_rectangle(
        (left, top, left + panel_width, top + panel_height),
        radius=radius,
        fill=(8, 13, 24, 222),
        outline=(255, 255, 255, 32),
        width=max(1, round(1 * scale)),
    )
    draw.rounded_rectangle(
        (left, top, left + accent_width, top + panel_height),
        radius=round(4 * scale),
        fill=(*card.accent, 255),
    )

    eyebrow_font = load_font(max(15, round(20 * scale)))
    title_font = load_font(max(22, round(34 * scale)))
    body_font = load_font(max(16, round(23 * scale)))
    badge_font = load_font(max(14, round(18 * scale)))

    text_left = left + round(30 * scale)
    draw.text(
        (text_left, top + round(19 * scale)),
        card.eyebrow,
        font=eyebrow_font,
        fill=(*card.accent, 255),
    )
    draw.text(
        (text_left, top + round(52 * scale)),
        card.title,
        font=title_font,
        fill=(248, 250, 255, 255),
    )
    line_top = top + round(104 * scale)
    for index, line in enumerate(card.lines):
        draw.text(
            (text_left, line_top + index * round(41 * scale)),
            line,
            font=body_font,
            fill=(211, 220, 235, 255),
        )

    badge_bbox = draw.textbbox((0, 0), card.badge, font=badge_font)
    badge_pad_x = round(18 * scale)
    badge_pad_y = round(10 * scale)
    badge_width = badge_bbox[2] - badge_bbox[0] + badge_pad_x * 2
    badge_height = badge_bbox[3] - badge_bbox[1] + badge_pad_y * 2
    badge_right = width - margin
    badge_top = margin
    draw.rounded_rectangle(
        (
            badge_right - badge_width,
            badge_top,
            badge_right,
            badge_top + badge_height,
        ),
        radius=round(14 * scale),
        fill=(8, 13, 24, 210),
        outline=(*card.accent, 190),
        width=max(1, round(2 * scale)),
    )
    draw.text(
        (badge_right - badge_width + badge_pad_x, badge_top + badge_pad_y - 2),
        card.badge,
        font=badge_font,
        fill=(248, 250, 255, 255),
    )
    canvas.save(output_path)


def escape_movie_path(path: str) -> str:
    return (
        path.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def build_filter(segments: tuple[Segment, ...], duration: float) -> tuple[str, float]:
    usable_segments: list[Segment] = []
    for segment in segments:
        start = min(segment.start, duration)
        end = min(segment.end, duration)
        if end <= start:
            continue
        usable_segments.append(
            Segment(start, end, segment.speed, segment.card_id, segment.purpose)
        )

    labels = "".join(f"[base{index}]" for index in range(len(usable_segments)))
    filters = [f"[0:v]split={len(usable_segments)}{labels}"]
    outputs: list[str] = []
    for index, segment in enumerate(usable_segments):
        card_path = escape_movie_path(f"cards/{segment.card_id}.png")
        filters.append(
            f"[base{index}]"
            f"trim=start={segment.start:.3f}:end={segment.end:.3f},"
            f"setpts=(PTS-STARTPTS)/{segment.speed:.6f},"
            "fps=30,format=rgba"
            f"[video{index}]"
        )
        filters.append(
            f"movie='{card_path}',format=rgba[card{index}]"
        )
        filters.append(
            f"[video{index}][card{index}]"
            "overlay=0:0:eof_action=repeat:shortest=0,"
            "format=yuv420p"
            f"[segment{index}]"
        )
        outputs.append(f"[segment{index}]")

    edited_duration = sum(segment.edited_duration for segment in usable_segments)
    fade_out_start = max(0.0, edited_duration - 0.45)
    filters.append(
        "".join(outputs)
        + f"concat=n={len(outputs)}:v=1:a=0,"
        "scale=in_range=pc:out_range=tv,"
        "format=yuv420p,"
        "fade=t=in:st=0:d=0.35,"
        f"fade=t=out:st={fade_out_start:.3f}:d=0.45"
        "[outv]"
    )
    return ";\n".join(filters), edited_duration


def load_audit_metrics(database_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        steps = connection.execute(
            "SELECT step_index, action, created_at FROM steps ORDER BY step_index"
        ).fetchall()
        model_calls = connection.execute(
            "SELECT step_index, usage, created_at FROM model_calls ORDER BY step_index"
        ).fetchall()
        events = connection.execute(
            """
            SELECT event_type, payload, created_at
            FROM trajectory_events
            WHERE event_type IN (
                'context_built',
                'model_action',
                'model_timeout',
                'memory_update',
                'verifier_result'
            )
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()

    context_times: dict[int, datetime] = {}
    action_times: dict[int, datetime] = {}
    memory_outcomes: list[dict[str, Any]] = []
    timeout_count = 0
    verifier: dict[str, Any] | None = None
    for event in events:
        payload = json.loads(event["payload"])
        event_type = event["event_type"]
        step_index = payload.get("step_index")
        event_time = datetime.fromisoformat(event["created_at"])
        if event_type == "context_built" and isinstance(step_index, int):
            context_times[step_index] = event_time
        elif event_type == "model_action" and isinstance(step_index, int):
            action_times[step_index] = event_time
        elif event_type == "model_timeout":
            timeout_count += 1
        elif event_type == "memory_update":
            memory_outcomes.extend(payload.get("outcomes") or [])
        elif event_type == "verifier_result":
            verifier = payload

    waits: list[dict[str, Any]] = []
    for step_index in sorted(set(context_times) & set(action_times)):
        wait_sec = (action_times[step_index] - context_times[step_index]).total_seconds()
        waits.append({"step_index": step_index, "wait_sec": wait_sec})

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for call in model_calls:
        call_usage = json.loads(call["usage"])
        for key in usage:
            usage[key] += int(call_usage.get(key) or 0)

    accepted = [item for item in memory_outcomes if item.get("accepted") is True]
    rejected = [item for item in memory_outcomes if item.get("accepted") is False]
    created = [item for item in accepted if item.get("operation") == "created"]
    updated = [item for item in accepted if item.get("operation") == "updated"]
    return {
        "step_count": len(steps),
        "model_call_count": len(model_calls),
        "model_usage": usage,
        "model_waits": waits,
        "model_wait_total_sec": sum(item["wait_sec"] for item in waits),
        "model_timeout_count": timeout_count,
        "memory_update_accepted": len(accepted),
        "memory_update_rejected": len(rejected),
        "memory_update_created": len(created),
        "memory_update_updated": len(updated),
        "verifier": verifier,
    }


def write_edit_map(
    path: Path,
    segments: tuple[Segment, ...],
    source_duration: float,
) -> list[dict[str, Any]]:
    edited_cursor = 0.0
    mapping: list[dict[str, Any]] = []
    for segment in segments:
        start = min(segment.start, source_duration)
        end = min(segment.end, source_duration)
        if end <= start:
            continue
        edited_duration = (end - start) / segment.speed
        mapping.append(
            {
                "source_start_sec": round(start, 3),
                "source_end_sec": round(end, 3),
                "speed": segment.speed,
                "edited_start_sec": round(edited_cursor, 3),
                "edited_end_sec": round(edited_cursor + edited_duration, 3),
                "card_id": segment.card_id,
                "purpose": segment.purpose,
            }
        )
        edited_cursor += edited_duration
    path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return mapping


def format_seconds(value: float) -> str:
    minutes = int(value // 60)
    seconds = value - minutes * 60
    return f"{minutes}:{seconds:04.1f}"


def write_report(
    path: Path,
    *,
    source_video: Path,
    showcase_video: Path,
    source_probe: dict[str, Any],
    showcase_probe: dict[str, Any],
    metrics: dict[str, Any],
    edit_map: list[dict[str, Any]],
) -> None:
    source_duration = float(source_probe["format"]["duration"])
    showcase_duration = float(showcase_probe["format"]["duration"])
    compression_ratio = source_duration / showcase_duration
    usage = metrics["model_usage"]
    waits = metrics["model_waits"]
    wait_summary = ", ".join(
        f"S{item['step_index']}={int(item['wait_sec'])}s" for item in waits
    )

    key_rows = []
    for item in edit_map:
        if item["card_id"] not in {
            "intro",
            "follow",
            "memory_rejected",
            "memory_accepted",
            "memory_used",
            "scan50",
            "pickup_move",
            "success",
        }:
            continue
        key_rows.append(
            "| "
            f"{format_seconds(item['edited_start_sec'])}–"
            f"{format_seconds(item['edited_end_sec'])} | "
            f"{item['purpose']} | "
            f"{item['card_id']} |"
        )

    report = f"""# 白色羊毛 Memory Update 实机演示｜展示版报告

## 一句话结论

Agent 从实体元数据识别出 `entity_id=13328` 是棕色羊，将该事实写入任务记忆；后续上下文持续携带这条记忆，因此没有再次把这只羊当作 `white_wool` 目标。

## 展示指标

| 指标 | 结果 |
|---|---:|
| 原始录像 | {source_duration:.1f} 秒 |
| 展示版录像 | {showcase_duration:.1f} 秒 |
| 时间压缩 | {compression_ratio:.1f}× |
| Agent 步数 | {metrics['step_count']} |
| 模型调用 | {metrics['model_call_count']} |
| 模型等待总计 | 约 {metrics['model_wait_total_sec']:.0f} 秒 |
| 模型超时重试 | {metrics['model_timeout_count']} 次 |
| Token | {usage['total_tokens']:,}（输入 {usage['input_tokens']:,} / 输出 {usage['output_tokens']:,}） |
| Memory Update | {metrics['memory_update_created']} 次创建 + {metrics['memory_update_updated']} 次覆盖更新 / {metrics['memory_update_rejected']} 次拒绝 |
| 最终 verifier | PASS：`white_wool` inventory delta `+1/+1` |

## 推荐讲解顺序

| 展示时间 | 画面含义 | 叠加信息 |
|---|---|---|
{chr(10).join(key_rows)}

## Memory Update 的关键证据

第一次请求引用了 `step:2/action_result`，但 JSON Pointer 少了 `/details`，Harness 返回 `path_not_found`，没有污染记忆。

下一轮模型改用实体级引用：

```json
{{
  "memory_key": "sheep_13328_is_brown_not_white",
  "source_ref": "step:3/scan_entities/entity:13328",
  "paths": [
    "/entity_id",
    "/details/metadata_decoded/wool/color",
    "/details/metadata_decoded/wool/color_id"
  ]
}}
```

Harness 解析并保存：

- `entity_id = 13328`
- `color = brown`
- `color_id = 12`
- 解释：这只羊不能产生目标 `white_wool`，应继续寻找白羊

## 剪辑规则

- 所有画面均来自原始 Agent POV，未重排 Action 的先后顺序。
- `move_to`、`follow`、Memory Update 和拾取过程按原速保留。
- 仅压缩“上下文已经发出、等待模型 API 返回”的静止区间。
- 每段右上角标出加速倍数，左下角显示 Action 与该轮模型可见的关键上下文。
- 原始 API 等待：{wait_summary}。

## 结果边界

这轮 verifier 的最终成功来自拾取一只后补白羊死亡后产生的 `white_wool` 掉落物，并非 Agent 对活白羊执行剪毛。因此：

- 适合展示：实体语义解码、Memory Update、路径校验、记忆进入后续上下文、避免重复处理错误实体。
- 不适合作为严格的“成功剪白羊”动作演示。

另外，本轮审计中的 Skill 仍使用冻结 bundle 的旧标识 `defeat_white_wool`；bundle 已在运行后迁移为 `harvest_white_wool`，展示视频使用中性描述避免改写历史审计事实。

## 文件

- 原始录像：`{source_video}`
- 展示版录像：`{showcase_video}`
- 完整审计：`{source_video.parent / 'audit.sqlite3'}`
- 剪辑映射：`{path.parent / 'edit_map.json'}`
"""
    path.write_text(report, encoding="utf-8")


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else (run_dir / "showcase").resolve()
    )
    source_video = run_dir / "agent_pov.mp4"
    database_path = run_dir / "audit.sqlite3"
    live_report_path = run_dir / "live_training.json"
    for required in (source_video, database_path, live_report_path):
        if not required.is_file():
            raise FileNotFoundError(required)

    output_dir.mkdir(parents=True, exist_ok=True)
    cards_dir = output_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    source_probe = probe_video(args.ffprobe, source_video)
    stream = source_probe["streams"][0]
    width = int(stream["width"])
    height = int(stream["height"])
    source_duration = float(source_probe["format"]["duration"])

    for card in CARDS.values():
        draw_card(card, width, height, cards_dir / f"{card.card_id}.png")

    filter_graph, expected_duration = build_filter(SEGMENTS, source_duration)
    filter_path = output_dir / "filter_complex.txt"
    filter_path.write_text(filter_graph + "\n", encoding="utf-8")

    output_video = output_dir / "wool_memory_update_showcase.mp4"
    run_command(
        [
            args.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(source_video),
            "-filter_complex_script",
            str(filter_path),
            "-map",
            "[outv]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(output_video),
        ],
        cwd=output_dir,
    )

    showcase_probe = probe_video(args.ffprobe, output_video)
    actual_duration = float(showcase_probe["format"]["duration"])
    if abs(actual_duration - expected_duration) > 1.0:
        raise RuntimeError(
            f"Unexpected showcase duration: expected {expected_duration:.2f}s, "
            f"got {actual_duration:.2f}s."
        )

    metrics = load_audit_metrics(database_path)
    edit_map = write_edit_map(output_dir / "edit_map.json", SEGMENTS, source_duration)
    write_report(
        output_dir / "showcase_report.md",
        source_video=source_video,
        showcase_video=output_video,
        source_probe=source_probe,
        showcase_probe=showcase_probe,
        metrics=metrics,
        edit_map=edit_map,
    )
    (output_dir / "media_probe.json").write_text(
        json.dumps(
            {
                "source": source_probe,
                "showcase": showcase_probe,
                "expected_showcase_duration_sec": expected_duration,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "video": str(output_video),
                "report": str(output_dir / "showcase_report.md"),
                "duration_sec": actual_duration,
                "metrics": metrics,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
