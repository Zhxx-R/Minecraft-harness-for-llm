from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_WINDOW_LIST_SWIFT = r"""
import Foundation
import CoreGraphics

let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let infos = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
let rows = infos.compactMap { info -> [String: Any]? in
    guard let owner = info[kCGWindowOwnerName as String] as? String,
          let number = info[kCGWindowNumber as String] as? Int,
          let bounds = info[kCGWindowBounds as String] as? [String: Any] else {
        return nil
    }
    return [
        "owner": owner,
        "name": info[kCGWindowName as String] as? String ?? "",
        "window_id": number,
        "owner_pid": info[kCGWindowOwnerPID as String] as? Int ?? -1,
        "layer": info[kCGWindowLayer as String] as? Int ?? -1,
        "bounds": bounds,
    ]
}
let data = try JSONSerialization.data(withJSONObject: rows)
print(String(data: data, encoding: .utf8)!)
"""

_REJECTED_WINDOW_OWNERS = (
    "finder",
    "访达",
    "terminal",
    "终端",
    "iterm",
    "codex",
    "chatgpt",
    "safari",
    "chrome",
    "preview",
    "预览",
)


class MacOSWindowCaptureError(RuntimeError):
    """Raised when a trusted Minecraft window cannot be selected or captured."""


@dataclass(frozen=True, slots=True)
class MacOSWindowTarget:
    """Stable CoreGraphics identity and bounds for one visible macOS window."""

    window_id: int
    owner: str
    name: str
    owner_pid: int
    layer: int
    bounds: dict[str, float]

    def to_json(self) -> dict[str, Any]:
        """Return JSON-safe target metadata for recording and audit."""

        return {
            "window_id": self.window_id,
            "owner": self.owner,
            "name": self.name,
            "owner_pid": self.owner_pid,
            "layer": self.layer,
            "bounds": dict(self.bounds),
        }


class MacOSWindowCaptureProvider:
    """Capture auditable JPEG frames from one strictly selected Minecraft window."""

    def __init__(
        self,
        *,
        title: str,
        output_dir: str | Path,
        owner: str | None = None,
        minimum_width: int = 320,
        minimum_height: int = 180,
        minimum_bytes: int = 4096,
    ) -> None:
        if not title.strip():
            raise ValueError("A non-empty Minecraft window title is required.")
        self.title = title.strip()
        self.owner = owner.strip() if owner and owner.strip() else None
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.minimum_width = minimum_width
        self.minimum_height = minimum_height
        self.minimum_bytes = minimum_bytes
        self._target: MacOSWindowTarget | None = None
        self._counter = 0
        self._lock = threading.Lock()

    @property
    def target(self) -> MacOSWindowTarget | None:
        """Return the most recently verified target window."""

        return self._target

    def preflight(self) -> dict[str, Any]:
        """Select the window and persist one valid frame before a costly live run."""

        return self.capture_sync(label="preflight", refresh_target=True)

    async def capture(self) -> dict[str, Any]:
        """Capture one frame without blocking the asyncio execution loop."""

        return await asyncio.to_thread(self.capture_sync)

    def capture_sync(
        self,
        *,
        label: str | None = None,
        refresh_target: bool = False,
    ) -> dict[str, Any]:
        """Capture one direct window image and return only bounded audit metadata."""

        if sys.platform != "darwin":
            raise MacOSWindowCaptureError("Direct Minecraft window capture is macOS-only.")
        with self._lock:
            if refresh_target or self._target is None or not _window_is_visible(self._target):
                self._target = select_macos_window(title=self.title, owner=self.owner)
            target = self._target
            assert target is not None
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._counter += 1
            token = label or f"snapshot_{self._counter:06d}"
            destination = self.output_dir / f"{_safe_token(token)}.jpg"
            completed = subprocess.run(
                [
                    "/usr/sbin/screencapture",
                    "-x",
                    "-t",
                    "jpg",
                    "-l",
                    str(target.window_id),
                    str(destination),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0 or not destination.is_file():
                raise MacOSWindowCaptureError(
                    completed.stderr.strip()
                    or f"screencapture returned {completed.returncode} for window {target.window_id}."
                )
            size_bytes = destination.stat().st_size
            width, height = _image_dimensions(destination)
            if (
                size_bytes < self.minimum_bytes
                or width < self.minimum_width
                or height < self.minimum_height
            ):
                destination.unlink(missing_ok=True)
                raise MacOSWindowCaptureError(
                    "Minecraft window capture was blank or too small: "
                    f"{width}x{height}, {size_bytes} bytes."
                )
            checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
            return {
                "available": True,
                "image": str(destination),
                "artifact_path": str(destination),
                "format": "jpeg",
                "mime_type": "image/jpeg",
                "width": width,
                "height": height,
                "size_bytes": size_bytes,
                "sha256": checksum,
                "window": target.to_json(),
            }


def visible_macos_windows() -> list[MacOSWindowTarget]:
    """Return normalized visible CoreGraphics windows without UI scripting permissions."""

    if sys.platform != "darwin":
        raise MacOSWindowCaptureError("macOS window enumeration is unavailable on this platform.")
    completed = subprocess.run(
        ["/usr/bin/swift", "-"],
        input=_WINDOW_LIST_SWIFT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise MacOSWindowCaptureError(
            completed.stderr.strip() or f"Swift window query returned {completed.returncode}."
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MacOSWindowCaptureError("CoreGraphics window query returned invalid JSON.") from exc
    windows: list[MacOSWindowTarget] = []
    for row in payload if isinstance(payload, list) else []:
        target = _normalize_window(row)
        if target is not None:
            windows.append(target)
    return windows


def select_macos_window(*, title: str, owner: str | None = None) -> MacOSWindowTarget:
    """Select a real game window while rejecting Finder and developer-tool false matches."""

    normalized_title = title.casefold().strip()
    normalized_owner = owner.casefold().strip() if owner else None
    candidates: list[tuple[int, MacOSWindowTarget]] = []
    visible = visible_macos_windows()
    for window in visible:
        if window.layer != 0:
            continue
        if window.bounds["width"] < 320 or window.bounds["height"] < 180:
            continue
        owner_name = window.owner.casefold()
        window_name = window.name.casefold()
        if any(rejected in owner_name for rejected in _REJECTED_WINDOW_OWNERS):
            continue
        if normalized_owner and normalized_owner not in owner_name:
            continue
        name_match = normalized_title in window_name
        recognized_minecraft_owner = (
            normalized_title == "minecraft"
            and (owner_name == "java" or "minecraft" in owner_name)
        )
        if not name_match and not recognized_minecraft_owner:
            continue
        score = 0
        if name_match:
            score += 100
        if "minecraft" in window_name:
            score += 30
        if owner_name == "java" or "minecraft" in owner_name:
            score += 20
        score += min(10, int(window.bounds["width"] * window.bounds["height"] / 500_000))
        candidates.append((score, window))
    if not candidates:
        sample = [window.to_json() for window in visible if window.layer == 0][:12]
        raise MacOSWindowCaptureError(
            f"No trusted visible Minecraft window matched title={title!r}, owner={owner!r}. "
            f"Visible layer-0 windows: {json.dumps(sample, ensure_ascii=False)}"
        )
    candidates.sort(key=lambda pair: (pair[0], pair[1].window_id), reverse=True)
    return candidates[0][1]


def crop_filter_for_window(target: MacOSWindowTarget, scale: float) -> str:
    """Convert CoreGraphics point bounds into an even-pixel ffmpeg crop filter."""

    if scale <= 0:
        raise ValueError("Window capture scale must be positive.")
    bounds = target.bounds
    x = max(0, _even_int(bounds["x"] * scale))
    y = max(0, _even_int(bounds["y"] * scale))
    width = max(2, _even_int(bounds["width"] * scale))
    height = max(2, _even_int(bounds["height"] * scale))
    return f"crop={width}:{height}:{x}:{y}"


def _normalize_window(payload: Any) -> MacOSWindowTarget | None:
    """Normalize one CoreGraphics row and reject incomplete bounds."""

    if not isinstance(payload, dict) or not isinstance(payload.get("bounds"), dict):
        return None
    bounds = payload["bounds"]
    try:
        normalized_bounds = {
            "x": float(bounds.get("X", bounds.get("x"))),
            "y": float(bounds.get("Y", bounds.get("y"))),
            "width": float(bounds.get("Width", bounds.get("width"))),
            "height": float(bounds.get("Height", bounds.get("height"))),
        }
        target = MacOSWindowTarget(
            window_id=int(payload["window_id"]),
            owner=str(payload.get("owner") or ""),
            name=str(payload.get("name") or ""),
            owner_pid=int(payload.get("owner_pid") or -1),
            layer=int(payload.get("layer") or 0),
            bounds=normalized_bounds,
        )
    except (KeyError, TypeError, ValueError):
        return None
    if normalized_bounds["width"] <= 0 or normalized_bounds["height"] <= 0:
        return None
    return target


def _window_is_visible(target: MacOSWindowTarget) -> bool:
    """Return whether the exact CoreGraphics window identity is still visible."""

    try:
        return any(window.window_id == target.window_id for window in visible_macos_windows())
    except MacOSWindowCaptureError:
        return False


def _image_dimensions(path: Path) -> tuple[int, int]:
    """Read image pixel dimensions with the macOS-native sips utility."""

    completed = subprocess.run(
        ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise MacOSWindowCaptureError(
            completed.stderr.strip() or f"sips returned {completed.returncode}."
        )
    width = 0
    height = 0
    for line in completed.stdout.splitlines():
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        if key == "pixelWidth":
            width = int(value.strip())
        elif key == "pixelHeight":
            height = int(value.strip())
    if width <= 0 or height <= 0:
        raise MacOSWindowCaptureError("sips did not report valid image dimensions.")
    return width, height


def _safe_token(value: str) -> str:
    """Normalize a caller label into a bounded local artifact filename."""

    normalized = "".join(character if character.isalnum() or character in "-_" else "_" for character in value)
    return normalized[:80] or "snapshot"


def _even_int(value: float) -> int:
    """Round down to an even integer required by yuv420p video crops."""

    rounded = int(round(value))
    return rounded if rounded % 2 == 0 else rounded - 1
