from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from mc_agent_harness.core.config import settings  # noqa: E402
from mc_agent_harness.models.router import ModelRouter, ModelRouterError  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse CLI options for a one-call LLM version and JSON-action smoke test."""

    parser = argparse.ArgumentParser(
        description="Verify configured LLM model metadata and structured action output."
    )
    parser.add_argument("--model", default=None, help="Override MODEL_DEFAULT for this verification call.")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON report path.")
    return parser.parse_args()


def main() -> None:
    """Run the async verifier and exit non-zero on model or JSON-action failure."""

    args = parse_args()
    if not settings.qwen_base_url:
        raise SystemExit("QWEN_BASE_URL is missing. Set it in .env or the environment.")
    if not settings.qwen_api_key:
        raise SystemExit("QWEN_API_KEY is missing. Set it in .env or the environment.")

    payload = asyncio.run(_verify(args.model))
    output_path = args.output or ROOT / "runs" / f"llm_verify_{_timestamp()}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload["output_path"] = str(output_path)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(_summary(payload), indent=2, sort_keys=True))


async def _verify(model: str | None) -> dict[str, Any]:
    """Call the configured model once and return auditable metadata."""

    router = ModelRouter(default_model=model)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a Minecraft agent action planner. Return exactly one JSON object "
                'matching {"type": string, "args": object}. Do not include markdown.'
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "Verify model connectivity by choosing the safest allowed action.",
                    "allowed_actions": ["query_inventory"],
                    "required_action": {"type": "query_inventory", "args": {}},
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
        },
    ]

    try:
        result = await router.generate_action(messages)
    except ModelRouterError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "raw_content": exc.raw_content,
            "usage": asdict(exc.usage),
            "model_metadata": exc.raw_response,
        }

    return {
        "ok": True,
        "action": result.action.model_dump(mode="json"),
        "raw_content": result.raw_content,
        "usage": asdict(result.usage),
        "model_metadata": result.raw_response,
    }


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a compact console summary without printing secrets."""

    metadata = payload.get("model_metadata", {})
    return {
        "ok": payload.get("ok"),
        "request_model": metadata.get("request_model"),
        "response_model": metadata.get("response_model"),
        "provider": metadata.get("provider"),
        "usage": payload.get("usage"),
        "action": payload.get("action"),
        "output_path": payload.get("output_path"),
        "error": payload.get("error"),
    }


def _timestamp() -> str:
    """Return a UTC timestamp suitable for report filenames."""

    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
