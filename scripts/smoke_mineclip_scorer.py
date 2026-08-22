from __future__ import annotations

import argparse
import base64
import io
import json
from typing import Any
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw


EXPECTED_ATTN_CHECKSUM = "b5ece9198337cfd117a3bfbd921e56da"


def parse_args() -> argparse.Namespace:
    """Parse the isolated scorer smoke-test endpoint."""

    parser = argparse.ArgumentParser(description="Run one real 16-frame MineCLIP inference.")
    parser.add_argument("--scorer-url", default="http://127.0.0.1:8091")
    parser.add_argument("--timeout-sec", type=float, default=180.0)
    return parser.parse_args()


def build_frames() -> list[str]:
    """Generate a deterministic moving Minecraft-like scene as compact PNG frames."""

    frames: list[str] = []
    for index in range(16):
        image = Image.new("RGB", (256, 160), color=(105, 170, 230))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 92, 255, 159), fill=(82, 132, 56))
        draw.rectangle((28 + index * 5, 60, 50 + index * 5, 92), fill=(98, 66, 38))
        draw.rectangle((18 + index * 5, 38, 61 + index * 5, 66), fill=(43, 104, 47))
        draw.rectangle((170, 75, 210, 92), fill=(126, 126, 126))
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=True)
        frames.append(base64.b64encode(stream.getvalue()).decode("ascii"))
    return frames


def score(base_url: str, timeout_sec: float) -> dict[str, Any]:
    """Submit one bounded request and validate official scorer identity metadata."""

    payload = {
        "frames": build_frames(),
        "prompt": "Walk through a grassy Minecraft plains biome.",
        "negative_prompts": [
            "Swim through a deep ocean.",
            "Mine diamonds in a dark cave.",
            "Fight a hostile monster at night.",
        ],
    }
    request = Request(
        f"{base_url.rstrip('/')}/score",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_sec) as response:
        result = json.loads(response.read())
    if result.get("scorer") != "mineclip_official":
        raise RuntimeError(f"Unexpected scorer identity: {result.get('scorer')!r}")
    if result.get("variant") == "attn" and result.get("checkpoint_checksum") != EXPECTED_ATTN_CHECKSUM:
        raise RuntimeError("MineCLIP attn checkpoint checksum was not preserved in the response.")
    probability = result.get("target_probability")
    if not isinstance(probability, (int, float)) or not 0 <= float(probability) <= 1:
        raise RuntimeError(f"Invalid target_probability: {probability!r}")
    return result


def main() -> None:
    """Run and print a compact machine-readable scorer verification report."""

    args = parse_args()
    result = score(args.scorer_url, args.timeout_sec)
    print(
        json.dumps(
            {
                "status": "passed",
                "target_probability": result["target_probability"],
                "variant": result["variant"],
                "checkpoint_checksum": result["checkpoint_checksum"],
                "latency_ms": result["latency_ms"],
                "metadata": result["metadata"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
