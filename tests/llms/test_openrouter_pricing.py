import httpx
import pytest

from omnigent.llms import context_window, openrouter_pricing
from omnigent.onboarding.providers import ModelInfo


@pytest.fixture(autouse=True)
def clear_cache():
    openrouter_pricing._cache.clear()
    yield
    openrouter_pricing._cache.clear()


def catalog(monkeypatch, rows):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"data": rows})

    client = httpx.Client
    monkeypatch.setattr(
        openrouter_pricing.httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(handle), **kw),
    )
    return calls


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unknown_catalog_model_uses_exact_cache_aware_openrouter_rates(monkeypatch, ambiguous):
    matches = (
        [
            ModelInfo(name="claude-test-a", provider="anthropic", input_price=1, output_price=2),
            ModelInfo(name="claude-test-b", provider="anthropic", input_price=3, output_price=4),
        ]
        if ambiguous
        else []
    )
    monkeypatch.setattr(context_window, "find_catalog_models", lambda _: matches)
    calls = catalog(
        monkeypatch,
        [
            {
                "id": "anthropic/claude-test",
                "pricing": {
                    "prompt": "0.000004",
                    "completion": "0.00002",
                    "input_cache_read": "0.0000002",
                    "input_cache_write": "0.000005",
                },
            }
        ],
    )
    prices = context_window.fetch_model_pricing("anthropic/claude-test")
    assert prices is not None
    assert context_window.compute_llm_cost(
        {
            "input_tokens": 1000000,
            "output_tokens": 1000000,
            "cache_read_input_tokens": 1000000,
            "cache_creation_input_tokens": 1000000,
        },
        prices,
    ) == pytest.approx(29.2)
    assert context_window.fetch_model_pricing("anthropic/claude-test-other") is None
    assert len(calls) == 1
    assert "authorization" not in calls[0].headers


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-1", True, {}, None])
def test_invalid_rates_leave_model_unpriced(monkeypatch, invalid):
    catalog(
        monkeypatch,
        [{"id": "anthropic/test", "pricing": {"prompt": invalid, "completion": "0.1"}}],
    )
    assert openrouter_pricing.model_rates("anthropic/test") is None


def test_context_tiers_are_not_priced_using_cumulative_tokens(monkeypatch):
    catalog(
        monkeypatch,
        [
            {
                "id": "openai/test",
                "pricing": {
                    "prompt": "0.1",
                    "completion": "0.2",
                    "overrides": [{"min_prompt_tokens": 100}],
                },
            }
        ],
    )
    assert openrouter_pricing.model_rates("openai/test") is None


def test_catalog_outage_is_cached_and_not_reported_as_zero(monkeypatch):
    calls = []

    def fail(**kw):
        calls.append(1)
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(openrouter_pricing.httpx, "Client", fail)
    assert openrouter_pricing.model_rates("anthropic/test") is None
    assert openrouter_pricing.model_rates("anthropic/other") is None
    assert len(calls) == 1
