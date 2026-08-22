import pytest

from mc_agent_harness.models.router import (
    ModelActionResult,
    ModelCompletion,
    ModelProfile,
    ModelProviderHTTPError,
    ModelRouter,
    ModelRouterError,
    ModelRouterTimeout,
    ModelUsage,
    ResilientModelProvider,
    parse_action_decision,
    parse_harness_action,
)


class FakeProvider:
    """Deterministic model provider used to test router parsing without network calls."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[dict] | None = None

    async def complete(
        self,
        messages: list[dict],
        profile: ModelProfile,
        response_schema: dict | None = None,
    ) -> ModelCompletion:
        """Return the configured content and capture request metadata."""

        self.messages = messages
        _ = (profile, response_schema)
        return ModelCompletion(
            content=self.content,
            usage=ModelUsage(input_tokens=12, output_tokens=6, total_tokens=18),
            raw_response={"id": "chatcmpl_fake", "model": profile.id, "object": "chat.completion"},
        )


class TimeoutProvider:
    """Fake provider that simulates a model read timeout before any content exists."""

    async def complete(
        self,
        messages: list[dict],
        profile: ModelProfile,
        response_schema: dict | None = None,
    ) -> ModelCompletion:
        """Raise a built-in timeout that ModelRouter should classify."""

        _ = (messages, profile, response_schema)
        raise TimeoutError("read timed out")


class RateLimitThenSuccessProvider:
    """Fake provider that returns HTTP 429 once and succeeds on retry."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        messages: list[dict],
        profile: ModelProfile,
        response_schema: dict | None = None,
    ) -> ModelCompletion:
        """Raise one retryable provider failure before returning valid JSON."""

        _ = (messages, response_schema)
        self.calls += 1
        if self.calls == 1:
            raise ModelProviderHTTPError(
                "rate limited",
                status_code=429,
                retry_after_sec=0.0,
                raw_response={"status_code": 429},
            )
        return ModelCompletion(
            content='{"type":"query_inventory","args":{}}',
            raw_response={"id": "recovered", "model": profile.id},
        )


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_model_router_generates_valid_harness_action() -> None:
    provider = FakeProvider('{"type":"query_inventory","args":{}}')
    router = ModelRouter(provider=provider)

    result = await router.generate_action([{"role": "user", "content": "{}"}])

    assert isinstance(result, ModelActionResult)
    assert result.action.type == "query_inventory"
    assert result.decision is not None
    assert result.decision.action.type == "query_inventory"
    assert result.usage.total_tokens == 18
    assert result.raw_response == {
        "request_model": router.default_model,
        "provider": "qwen",
        "profile_vision": True,
        "request_vision_input": False,
        "response_id": "chatcmpl_fake",
        "response_model": router.default_model,
        "response_object": "chat.completion",
    }


@pytest.mark.anyio
async def test_model_router_audits_multimodal_image_input() -> None:
    """A model call carrying an image part must be distinguishable in persisted metadata."""

    provider = FakeProvider('{"type":"query_inventory","args":{}}')
    router = ModelRouter(provider=provider)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this Minecraft view."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,ZmFrZQ=="},
                },
            ],
        }
    ]

    result = await router.generate_action(messages)

    assert provider.messages == messages
    assert result.raw_response["profile_vision"] is True
    assert result.raw_response["request_vision_input"] is True


def test_parse_harness_action_accepts_json_markdown_fence() -> None:
    action = parse_harness_action(
        '```json\n{"type":"dig_block_at","args":{"block":"oak_log","position":{"x":0,"y":64,"z":0}}}\n```'
    )

    assert action.type == "dig_block_at"
    assert action.args == {"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}}


def test_parse_action_decision_accepts_auditable_envelope() -> None:
    decision = parse_action_decision(
        (
            '{"reasoning_summary":"Inventory is unknown, so inspect it first.",'
            '"evidence":["task requires checking current inventory"],'
            '"knowledge_need":{"needed":false,"query":null,"reason":null},'
            '"action":{"type":"query_inventory","args":{}}}'
        )
    )

    assert decision.reasoning_summary == "Inventory is unknown, so inspect it first."
    assert decision.evidence == ["task requires checking current inventory"]
    assert decision.knowledge_need.needed is False
    assert decision.action.type == "query_inventory"


def test_parse_action_decision_accepts_source_grounded_memory_updates() -> None:
    decision = parse_action_decision(
        (
            '{"reasoning_summary":"Avoid the already-sheared sheep.",'
            '"evidence":["step 2 reports brown wool and is_sheared=true"],'
            '"knowledge_need":{"needed":false},'
            '"memory_update":[{"memory_key":"entity:68/wool_state",'
            '"source_ref":"step:2/scan_entities/entity:68",'
            '"paths":["/entity_id","/details/metadata_decoded/wool/color",'
            '"/details/metadata_decoded/wool/is_sheared"],'
            '"note":"This sheep is brown and already sheared."}],'
            '"action":{"type":"scan_entities","args":{"entity":"sheep"}}}'
        )
    )

    assert decision.memory_update[0].memory_key == "entity:68/wool_state"
    assert decision.memory_update[0].paths[-1].endswith("/is_sheared")
    assert decision.action.type == "scan_entities"


def test_parse_harness_action_rejects_raw_code() -> None:
    with pytest.raises(ModelRouterError, match="valid JSON"):
        parse_harness_action("bot.chat('hello')")


@pytest.mark.anyio
async def test_model_router_error_preserves_raw_completion_for_repair() -> None:
    provider = FakeProvider("bot.chat('hello')")
    router = ModelRouter(provider=provider)

    with pytest.raises(ModelRouterError) as error:
        await router.generate_action([{"role": "user", "content": "{}"}])

    assert error.value.raw_content == "bot.chat('hello')"
    assert error.value.usage.total_tokens == 18
    assert error.value.raw_response["request_model"] == router.default_model
    assert error.value.raw_response["response_model"] == router.default_model


@pytest.mark.anyio
async def test_model_router_wraps_provider_timeout() -> None:
    router = ModelRouter(provider=TimeoutProvider())

    with pytest.raises(ModelRouterTimeout) as error:
        await router.generate_action([{"role": "user", "content": "{}"}])

    assert "timed out" in str(error.value)
    assert error.value.raw_response["timeout"] is True
    assert error.value.raw_response["request_model"] == router.default_model


@pytest.mark.anyio
async def test_resilient_model_provider_retries_rate_limit_and_audits_recovery() -> None:
    """HTTP 429 should be retried inside the shared provider without consuming an agent step."""

    delegate = RateLimitThenSuccessProvider()
    provider = ResilientModelProvider(
        delegate,
        max_concurrency=2,
        transient_retries=1,
        backoff_sec=(0.0,),
    )
    router = ModelRouter(provider=provider)

    result = await router.generate_action([{"role": "user", "content": "{}"}])

    assert result.action.type == "query_inventory"
    assert delegate.calls == 2
    recovery = result.raw_response["harness_provider_recovery"]
    assert recovery["recovered"] is True
    assert recovery["provider_attempts"] == [
        {"attempt_index": 0, "status_code": 429, "retry_after_sec": 0.0}
    ]
    assert recovery["max_concurrency"] == 2
