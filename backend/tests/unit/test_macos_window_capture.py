from __future__ import annotations

import pytest

import mc_agent_harness.runtime.macos_window_capture as capture
from mc_agent_harness.runtime.macos_window_capture import MacOSWindowTarget


def _window(window_id: int, owner: str, name: str) -> MacOSWindowTarget:
    """Build one layer-0 window candidate with realistic bounds."""

    return MacOSWindowTarget(
        window_id=window_id,
        owner=owner,
        name=name,
        owner_pid=100 + window_id,
        layer=0,
        bounds={"x": 0.0, "y": 0.0, "width": 1280.0, "height": 720.0},
    )


def test_window_selection_rejects_finder_false_positive(monkeypatch) -> None:
    """A Finder folder named minecraft must never outrank the Java game window."""

    monkeypatch.setattr(
        capture,
        "visible_macos_windows",
        lambda: [
            _window(1, "Finder", "minecraft"),
            _window(2, "java", "Minecraft 1.20.1"),
        ],
    )

    selected = capture.select_macos_window(title="Minecraft")

    assert selected.window_id == 2
    assert selected.owner == "java"


def test_window_selection_fails_closed_without_game_window(monkeypatch) -> None:
    """Selection must fail instead of silently recording a similarly named desktop app."""

    monkeypatch.setattr(
        capture,
        "visible_macos_windows",
        lambda: [_window(1, "Finder", "minecraft")],
    )

    with pytest.raises(capture.MacOSWindowCaptureError):
        capture.select_macos_window(title="Minecraft")
