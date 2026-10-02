import json
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import pytest

from aura_providers.adapters.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from aura_providers.adapters.openai_compat import OpenAICompatibleProvider
from aura_providers.adapters.stub import StubProvider
from aura_providers.domain.catalog import CatalogError, Tier, load_catalog, parse_catalog
from aura_providers.domain.pricing import cost_usd, estimate_cost_usd
from aura_providers.domain.types import (
    ChatRequest,
    Credentials,
    FinishReason,
    Message,
    ProviderError,
    ProviderErrorKind,
    StreamDone,
    TextDelta,
    Usage,
)

CAT = load_catalog()
OPUS = CAT.get("anthropic/claude-opus-5-5")
HAIKU = CAT.get("anthropic/claude-haiku-4-5")
CREDS = Credentials("sk-test")


def _req(*msgs: tuple[str, str], **kw) -> ChatRequest:  # type: ignore[no-untyped-def]
    return ChatRequest(tuple(Message(r, c) for r, c in msgs), **kw)  # type: ignore[arg-type]


# ---- catalog & pricing ------------------------------------------------------------------


def test_catalog_models_and_resolution() -> None:
    assert OPUS is not None and OPUS.tier is Tier.FRONTIER and OPUS.context_window == 1_000_000
    assert CAT.resolve("claude-sonnet-5-5").id == "anthropic/claude-sonnet-5-5"
    assert CAT.resolve("nope") is None
    assert not OPUS.has("sampling") and HAIKU.has("sampling")
    only_stub = load_catalog(enabled_providers={"stub"})
    assert {m.provider for m in only_stub.enabled()} == {"stub"}
    assert len(CAT.version) == 16


@pytest.mark.parametrize(
    "bad",
    [
        {"providers": {}},
        {
            "providers": {
                "p": {
                    "models": {
                        "m": {
                            "tier": "huge",
                            "context_window": 10,
                            "max_output": 5,
                            "prices": {"input": 1, "output": 1},
                        }
                    }
                }
            }
        },
        {
            "providers": {
                "p": {
                    "models": {
                        "m": {
                            "tier": "small",
                            "context_window": 10,
                            "max_output": 50,
                            "prices": {"input": 1, "output": 1},
                        }
                    }
                }
            }
        },
        {
            "providers": {
                "p": {
                    "models": {
                        "m": {
                            "tier": "small",
                            "context_window": 10,
                            "max_output": 5,
                            "prices": {"input": -1, "output": 1},
                        }
                    }
                }
            }
        },
        {
            "providers": {
                "p": {
                    "models": {
                        "m": {
                            "tier": "small",
                            "context_window": 10,
                            "max_output": 5,
                            "capabilities": ["telepathy"],
                            "prices": {"input": 1, "output": 1},
                        }
                    }
                }
            }
        },
    ],
)
def test_catalog_validation(bad) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(CatalogError):
        parse_catalog(bad)


def test_cost_uses_decimal_and_cache_rates() -> None:
    # 1M plain input + 1M output on Opus 5.5 = $4 + $20
    assert cost_usd(OPUS, Usage(1_000_000, 1_000_000)) == Decimal("24.00000000")
    # Cached input is billed at the cache-read rate ($0.20/MTok)
    assert cost_usd(OPUS, Usage(1_000_000, 0, cached_input_tokens=1_000_000)) == Decimal("0.2")
    # Cache writes at the write rate; subsets never exceed total input
    mixed = Usage(1_000, 0, cached_input_tokens=400, cache_write_tokens=400)
    expected = (Decimal(200) * 4 + Decimal(400) * Decimal("0.2") + Decimal(400) * 5) / 1_000_000
    assert cost_usd(OPUS, mixed) == expected.quantize(Decimal("0.00000001"))
    assert estimate_cost_usd(HAIKU, input_tokens=1000, output_tokens=1000) == Decimal("0.006")


# ---- Anthropic adapter (real SDK, mocked HTTP) ------------------------------------------


def test_anthropic_params_respect_model_capabilities() -> None:
    req = _req(
        ("system", "Be terse."), ("user", "hi"), temperature=0.3, effort="low", stop=("END",)
    )
    params, notes = AnthropicProvider.build_params(OPUS, req, 1000)
    assert params["system"] == "Be terse." and params["messages"] == [
        {"role": "user", "content": "hi"}
    ]
    assert "temperature" not in params and any("temperature ignored" in n for n in notes)
    assert params["output_config"] == {"effort": "low"}
    assert params["betas"] == [FALLBACK_BETA] and params["fallbacks"] == "default"
    params, notes = AnthropicProvider.build_params(HAIKU, req, 1000)
    assert params["temperature"] == 0.3 and "betas" not in params and "output_config" not in params
    with pytest.raises(ProviderError) as exc:
        AnthropicProvider.build_params(OPUS, _req(("assistant", "hi")), 10)
    assert exc.value.kind is ProviderErrorKind.BAD_REQUEST


def test_anthropic_result_skips_thinking_and_normalizes_usage() -> None:
    message = SimpleNamespace(
        id="msg_1",
        model="claude-opus-5-5",
        stop_reason="refusal",
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="Hello "),
            SimpleNamespace(type="text", text="world"),
        ],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=7,
            cache_read_input_tokens=50,
            cache_creation_input_tokens=None,
        ),
    )
    r = AnthropicProvider.to_result(message, [])
    assert r.text == "Hello world" and r.finish_reason is FinishReason.CONTENT_FILTER
    assert r.usage == Usage(150, 7, cached_input_tokens=50, cache_write_tokens=0)


def _mock_anthropic(handler) -> AnthropicProvider:  # type: ignore[no-untyped-def]
    client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return AnthropicProvider(http_client=client, max_retries=0)


def _message_json(model: str = "claude-haiku-4-5") -> dict:  # type: ignore[type-arg]
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "Paris."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 3,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


async def test_anthropic_complete_over_http() -> None:
    seen: dict = {}  # type: ignore[type-arg]

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_message_json())

    provider = _mock_anthropic(handler)
    r = await provider.complete(HAIKU, _req(("user", "Capital of France?")), CREDS, max_tokens=64)
    assert r.text == "Paris." and r.usage == Usage(12, 3) and r.model_served == "claude-haiku-4-5"
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["body"]["model"] == "claude-haiku-4-5" and seen["body"]["max_tokens"] == 64


async def test_anthropic_fallback_models_use_beta_header() -> None:
    seen: dict = {}  # type: ignore[type-arg]

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["beta"] = request.headers.get("anthropic-beta")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_message_json("claude-opus-5-5"))

    await _mock_anthropic(handler).complete(OPUS, _req(("user", "x")), CREDS, max_tokens=10)
    assert FALLBACK_BETA in (seen["beta"] or "")
    assert seen["body"]["fallbacks"] == "default"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, ProviderErrorKind.RATE_LIMITED),
        (529, ProviderErrorKind.OVERLOADED),
        (500, ProviderErrorKind.UNAVAILABLE),
        (401, ProviderErrorKind.AUTH),
        (400, ProviderErrorKind.BAD_REQUEST),
        (404, ProviderErrorKind.NOT_FOUND),
    ],
)
async def test_anthropic_error_mapping(status: int, kind: ProviderErrorKind) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": "x", "message": "m"}}
        )

    with pytest.raises(ProviderError) as exc:
        await _mock_anthropic(handler).complete(HAIKU, _req(("user", "x")), CREDS, max_tokens=5)
    assert exc.value.kind is kind
    assert exc.value.retryable == (
        kind
        in {
            ProviderErrorKind.RATE_LIMITED,
            ProviderErrorKind.OVERLOADED,
            ProviderErrorKind.UNAVAILABLE,
        }
    )


def _sse(events: list[tuple[str, dict]]) -> bytes:  # type: ignore[type-arg]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


async def test_anthropic_streaming_over_http() -> None:
    msg = _message_json()
    msg["content"] = []
    body = _sse(
        [
            ("message_start", {"type": "message_start", "message": msg}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Pa"},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ris."},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    events = [
        e
        async for e in _mock_anthropic(handler).stream(
            HAIKU, _req(("user", "x")), CREDS, max_tokens=10
        )
    ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Pa", "ris."]
    done = events[-1]
    assert isinstance(done, StreamDone) and done.result.text == "Paris."
    assert done.result.usage.output_tokens == 3


# ---- OpenAI-compatible adapter ----------------------------------------------------------

SPEC_OAI = parse_catalog(
    {
        "providers": {
            "openrouter": {
                "models": {
                    "vendor/m": {
                        "tier": "small",
                        "context_window": 1000,
                        "max_output": 100,
                        "prices": {"input": 1, "output": 2},
                        "capabilities": ["sampling"],
                    }
                }
            }
        }
    }
).get("openrouter/vendor/m")


async def test_openai_compat_complete_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer sk-test"
        assert body["model"] == "vendor/m" and body["temperature"] == 0.5
        if body["messages"][-1]["content"] == "boom":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "model": "vendor/m",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 4},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        p = OpenAICompatibleProvider("openrouter", http)
        r = await p.complete(SPEC_OAI, _req(("user", "hi"), temperature=0.5), CREDS, max_tokens=5)
        assert r.text == "ok" and r.finish_reason is FinishReason.LENGTH
        assert r.usage == Usage(9, 1, cached_input_tokens=4)
        with pytest.raises(ProviderError) as exc:
            await p.complete(SPEC_OAI, _req(("user", "boom"), temperature=0.5), CREDS, max_tokens=5)
        assert exc.value.kind is ProviderErrorKind.UNAVAILABLE and exc.value.retryable


async def test_openai_compat_streaming() -> None:
    lines = [
        {"id": "c1", "model": "vendor/m", "choices": [{"delta": {"content": "he"}}]},
        {"id": "c1", "choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]},
        {"id": "c1", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        return httpx.Response(200, content=body.encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        p = OpenAICompatibleProvider("openrouter", http)
        events = [e async for e in p.stream(SPEC_OAI, _req(("user", "x")), CREDS, max_tokens=5)]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "hello"
    assert events[-1].result.usage == Usage(3, 2)  # type: ignore[union-attr]


# ---- stub ---------------------------------------------------------------------------------


async def test_stub_failure_injection_refusal_and_truncation() -> None:
    stub = StubProvider()
    spec = load_catalog().get("stub/small")
    stub.fail_next("small", ProviderErrorKind.OVERLOADED)
    with pytest.raises(ProviderError):
        await stub.complete(spec, _req(("user", "hi")), CREDS, max_tokens=10)
    r = await stub.complete(spec, _req(("user", "one two three four")), CREDS, max_tokens=3)
    assert r.finish_reason is FinishReason.LENGTH and r.text == "[small] one two"
    refused = await stub.complete(spec, _req(("user", "please [refuse]")), CREDS, max_tokens=9)
    assert refused.finish_reason is FinishReason.CONTENT_FILTER


async def test_groq_uses_its_openai_compatible_endpoint() -> None:
    groq = load_catalog(enabled_providers={"groq"})
    tiers = {m.tier for m in groq.enabled()}
    assert tiers == {Tier.SMALL, Tier.FRONTIER}  # Balanced uses the nearest (stronger) tier
    spec = groq.get("groq/openai/gpt-oss-20b")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        r = await OpenAICompatibleProvider("groq", http).complete(
            spec, _req(("user", "hi")), Credentials("gsk-test"), max_tokens=5
        )
    assert r.text == "hello"
    assert str(seen[0].url) == "https://api.groq.com/openai/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer gsk-test"
    assert json.loads(seen[0].content)["model"] == "openai/gpt-oss-20b"
