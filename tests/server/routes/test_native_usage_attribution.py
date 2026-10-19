"""Per-model cost/token attribution for the native cumulative usage path.

``_persist_native_cumulative_usage`` receives *cumulative* session totals from
native harnesses (claude-native / codex-native). It must attribute each
report's growth to the currently-active model so per-model buckets hold each
model's own spend and sum to the flat session total across mid-session model
switches — rather than SETting every bucket to the whole running total (which
double-counted the shared baseline once the active model changed).
"""

from __future__ import annotations

import pytest

from omnigent.llms.context_window import ModelPricing
from omnigent.server.routes._sessions.orchestration import (
    _persist_native_cumulative_usage,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# Agent ids are stored as 16-byte uuids, so tests use a valid 32-char hex id.
_AGENT_ID = "0123456789abcdef0123456789abcdef"


def _usage(store: SqlAlchemyConversationStore, conv_id: str) -> dict:
    conv = store.get_conversation(conv_id)
    assert conv is not None
    return dict(conv.session_usage)


def test_per_model_cost_sums_across_model_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch", agent_id=_AGENT_ID)

    # Cumulative session cost climbs on opus, then keeps climbing after a
    # /model switch to sonnet (native /cost is a running session total).
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 3.0, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.80, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 11.91, "model": "claude-sonnet-4-5"}, store
    )

    usage = _usage(store, conv.id)
    by_model = usage["by_model"]
    # Each model holds only its own spend (opus's growth 0->10.80, sonnet's
    # 10.80->11.91), and the buckets sum to the flat total — the bug fixed here.
    assert usage["total_cost_usd"] == pytest.approx(11.91)
    assert by_model["claude-opus-4-8"]["total_cost_usd"] == pytest.approx(10.80)
    assert by_model["claude-sonnet-4-5"]["total_cost_usd"] == pytest.approx(1.11)
    assert sum(m["total_cost_usd"] for m in by_model.values()) == pytest.approx(11.91)


def test_per_model_cost_accumulates_when_switching_back(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch-back", agent_id=_AGENT_ID)

    # A -> B -> A: model A's bucket must accumulate across its two separate
    # active spans (not be overwritten on the second visit), so the returning
    # model's growth adds onto its earlier spend.
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.0, "model": "model-a"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 12.50, "model": "model-b"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 15.0, "model": "model-a"}, store
    )

    usage = _usage(store, conv.id)
    by_model = usage["by_model"]
    # A = 10.0 (first span) + 2.50 (15.0 - 12.50 on return); B = 12.50 - 10.0.
    assert usage["total_cost_usd"] == pytest.approx(15.0)
    assert by_model["model-a"]["total_cost_usd"] == pytest.approx(12.50)
    assert by_model["model-b"]["total_cost_usd"] == pytest.approx(2.50)
    assert sum(m["total_cost_usd"] for m in by_model.values()) == pytest.approx(15.0)


def test_single_model_bucket_equals_flat_total(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="single", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 2.0, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 5.5, "model": "claude-opus-4-8"}, store
    )

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(5.5)
    assert usage["by_model"]["claude-opus-4-8"]["total_cost_usd"] == pytest.approx(5.5)


def test_lowered_report_does_not_claw_back_bucket(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="rebased", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0, "model": "m1"}, store)
    # A forged / rebased lower report: the flat total is monotonic-clamped, and
    # the per-model delta is clamped >= 0, so neither is clawed back.
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 2.0, "model": "m1"}, store)

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(10.0)
    assert usage["by_model"]["m1"]["total_cost_usd"] == pytest.approx(10.0)


def test_per_model_tokens_accumulate_from_cumulative(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="tokens", agent_id=_AGENT_ID)

    # codex-native style: cumulative token totals plus an explicit cost.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cost_usd": 1.0,
            "model": "gpt-5.6",
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 50,
            "cumulative_cost_usd": 3.0,
            "model": "gpt-5.6",
        },
        store,
    )

    usage = _usage(store, conv.id)
    bucket = usage["by_model"]["gpt-5.6"]
    # The bucket accumulates deltas to match the flat cumulative session totals.
    assert bucket["input_tokens"] == usage["input_tokens"] == 300
    assert bucket["output_tokens"] == usage["output_tokens"] == 50
    assert bucket["total_tokens"] == usage["total_tokens"] == 350
    assert bucket["total_cost_usd"] == pytest.approx(3.0)


def test_per_model_tokens_sum_across_model_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch-tokens", agent_id=_AGENT_ID)

    # codex-style: cumulative tokens (with explicit cost to avoid the catalog)
    # climb on opus, then keep climbing after a /model switch to sonnet.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cost_usd": 3.0,
            "model": "claude-opus-4-8",
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 60,
            "cumulative_cost_usd": 11.91,
            "model": "claude-sonnet-4-5",
        },
        store,
    )

    usage = _usage(store, conv.id)
    opus = usage["by_model"]["claude-opus-4-8"]
    sonnet = usage["by_model"]["claude-sonnet-4-5"]
    # Each model holds only its own token growth, and the per-model token +
    # cost buckets sum to the flat session totals across the switch.
    assert opus["input_tokens"] == 100
    assert sonnet["input_tokens"] == 200
    assert opus["input_tokens"] + sonnet["input_tokens"] == usage["input_tokens"] == 300
    assert opus["output_tokens"] + sonnet["output_tokens"] == usage["output_tokens"] == 60
    assert opus["total_tokens"] + sonnet["total_tokens"] == usage["total_tokens"] == 360
    assert opus["total_cost_usd"] + sonnet["total_cost_usd"] == pytest.approx(11.91)


def test_token_priced_bucket_matches_flat_cost(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-priced", agent_id=_AGENT_ID)

    # codex-native reports cumulative tokens but NO explicit cost, so cost is
    # computed from the running token totals. Stub pricing so the test does not
    # depend on the live catalog; assert the per-model cost bucket still tracks
    # the flat token-priced total (the elif-has_tokens branch feeds by_model).
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: {"stub": True},
    )
    monkeypatch.setattr(
        "omnigent.llms.context_window.compute_llm_cost",
        lambda usage, pricing: float(usage.get("total_tokens", 0)) * 0.01,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200, "cumulative_output_tokens": 50, "model": "gpt-5.6"},
        store,
    )

    usage = _usage(store, conv.id)
    # total_tokens = 200 (input) + 0 (cache) + 50 (output) = 250; cost = 2.50.
    assert usage["total_cost_usd"] == pytest.approx(2.50)
    assert usage["by_model"]["gpt-5.6"]["total_cost_usd"] == pytest.approx(2.50)


def test_token_report_without_cache_field_keeps_persisted_cache_split(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-miss", agent_id=_AGENT_ID)

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: ModelPricing(
            input_per_token=2.5e-6,
            output_per_token=10e-6,
            cache_read_per_token=0.25e-6,
            cache_write_per_token=None,
        ),
    )

    # Cache-heavy turn: 100K cumulative input, 90K of them cache reads.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # Cache-miss turn: the cumulative cached count is unchanged, and the
    # report omits it. The persisted split must carry forward rather than
    # re-billing the 90K cache reads at the full input rate.
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200_000, "cumulative_output_tokens": 400, "model": "gpt-5.6"},
        store,
    )

    usage = _usage(store, conv.id)
    expected_cost = (110_000 * 2.5 + 90_000 * 0.25 + 400 * 10) / 1_000_000
    assert usage["cache_read_input_tokens"] == 90_000
    assert usage["input_tokens"] == 110_000
    assert usage["total_tokens"] == 200_400
    assert usage["total_cost_usd"] == pytest.approx(expected_cost)
    bucket = usage["by_model"]["gpt-5.6"]
    assert bucket["input_tokens"] == 110_000
    assert bucket["cache_read_input_tokens"] == 90_000
    assert bucket["total_cost_usd"] == pytest.approx(expected_cost)


def test_explicit_zero_cache_count_replaces_persisted_split(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-zero", agent_id=_AGENT_ID)

    # Only token splits are asserted here; keep the token-pricing branch off
    # the live catalog so the test is deterministic and offline.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # An explicit zero is a report, not an omission: it is honored as-is.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 120_000,
            "cumulative_cache_read_input_tokens": 0,
            "cumulative_output_tokens": 300,
            "model": "gpt-5.6",
        },
        store,
    )

    usage = _usage(store, conv.id)
    assert usage["cache_read_input_tokens"] == 0
    assert usage["input_tokens"] == 120_000


def test_carried_cache_count_is_clamped_to_a_lowered_input_total(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-lowered", agent_id=_AGENT_ID)

    # Only token splits are asserted here; keep the token-pricing branch off
    # the live catalog so the test is deterministic and offline.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # A lowered cumulative total without a cache count keeps the carried split
    # within the reported input, and later reports carry that clamped count.
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 50_000, "cumulative_output_tokens": 250, "model": "gpt-5.6"},
        store,
    )
    assert _usage(store, conv.id)["cache_read_input_tokens"] == 50_000
    assert _usage(store, conv.id)["input_tokens"] == 0

    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200_000, "cumulative_output_tokens": 400, "model": "gpt-5.6"},
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["cache_read_input_tokens"] == 50_000
    assert usage["input_tokens"] == 150_000


def _conversation_point_reads(statements: list[str]) -> list[str]:
    """Filter captured SQL to point reads of a single conversation row."""
    matches: list[str] = []
    for raw in statements:
        normalized = " ".join(raw.split())
        if "FROM conversations" not in normalized or " WHERE " not in normalized:
            continue
        where = normalized.split(" WHERE ", 1)[1]
        if "conversations.id = " in where and "root_conversation_id" not in where:
            matches.append(normalized)
    return matches


@pytest.mark.asyncio
async def test_flush_with_stale_row_keeps_monotonic_clamp(db_uri: str) -> None:
    """A stale caller-held row must not weaken the forged-low-report clamp.

    The events route reads the conversation at request start (authorization)
    and hands that row to the usage flush for the read-only tree walks. If a
    concurrent report persists a higher cost AFTER that read, a forged low
    report on the stale-row request must still be a no-op: the own-usage
    persist baselines its monotonic clamp on its own FRESH read, never on the
    caller's row (otherwise the replay could roll the persisted/enforced cost
    back and double-count the daily rollup delta).
    """
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="stale-clamp", agent_id=_AGENT_ID)
    # The route's access-check row, read while the persisted cost was $0.
    stale_row = store.get_conversation(conv.id)
    assert stale_row is not None

    # A concurrent legitimate report lands after the access check.
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0, "model": "m1"}, store)

    # The stale-row request then delivers a (forged or delayed) lower report.
    await _persist_external_session_usage(
        conv.id,
        SessionEventInput(
            type="external_session_usage",
            data={"cumulative_cost_usd": 2.0, "model": "m1"},
        ),
        store,
        stale_row,
    )

    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_flush_with_supplied_row_skips_tree_root_rereads(db_uri: str) -> None:
    """The flush's read-only tree walks must reuse the supplied row's root.

    With the route's row in hand, the only conversation point read a flush
    may issue is the own-usage persist's fresh clamp baseline — the subtree
    roll-up and the ancestor publish take the root from the supplied row.
    """
    from sqlalchemy import event as sa_event

    from omnigent.db.utils import _engine_cache
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-reads", agent_id=_AGENT_ID)
    row = store.get_conversation(conv.id)
    assert row is not None

    engine = _engine_cache[db_uri]
    statements: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 3.0, "model": "m1"},
            ),
            store,
            row,
        )
    finally:
        sa_event.remove(engine, "before_cursor_execute", _on_exec)

    point_reads = _conversation_point_reads(statements)
    assert len(point_reads) == 1, (
        f"flush issued {len(point_reads)} conversation point reads (expected "
        f"1: the persist's fresh clamp baseline); statements={point_reads}"
    )
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_flush_telemetry_reports_the_callers_installation_id(db_uri: str) -> None:
    """The usage event names the posting machine from its request header only.

    The header is what makes the attribution free: the server never looks the
    host up, so a flush from a client that sends no header leaves the field
    unset rather than costing a read.
    """
    from unittest.mock import patch

    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-telemetry", agent_id=_AGENT_ID)
    row = store.get_conversation(conv.id)
    assert row is not None

    events: list = []
    with patch(
        "omnigent.server.routes._sessions.orchestration._tel_emit",
        side_effect=events.append,
    ):
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 3.0, "model": "m1"},
            ),
            store,
            row,
            "alice@example.com",
            "inst-host-9",
        )
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 4.0, "model": "m1"},
            ),
            store,
            row,
            "alice@example.com",
        )

    assert [e.host_installation_id for e in events] == ["inst-host-9", None]


def test_concurrent_stale_priced_report_preserves_unpriced_peak(db_uri, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from omnigent.llms import context_window

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="concurrent-reports", agent_id=_AGENT_ID)
    original_get = store.get_conversation
    stale_read = threading.Event()
    new_report_committed = threading.Event()

    def get_with_stale_metadata(sid):
        row = original_get(sid)
        if threading.current_thread().name.startswith("stale-report"):
            stale_read.set()
            assert new_report_committed.wait(10)
        return row

    monkeypatch.setattr(store, "get_conversation", get_with_stale_metadata)
    monkeypatch.setattr(context_window, "fetch_model_pricing_with_provider", lambda *a, **k: None)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-report") as pool:
        replay = pool.submit(
            _persist_native_cumulative_usage,
            conv.id,
            {
                "model": "priced-model",
                "cumulative_cost_usd": 1,
                "cumulative_input_tokens": 10,
                "cumulative_output_tokens": 0,
            },
            store,
        )
        try:
            assert stale_read.wait(10)
            _persist_native_cumulative_usage(
                conv.id,
                {
                    "model": "unknown-model",
                    "cumulative_input_tokens": 100,
                    "cumulative_output_tokens": 0,
                },
                store,
            )
        finally:
            new_report_committed.set()
        replay.result(timeout=10)
    usage = _usage(store, conv.id)
    assert usage["unpriced_tokens"] == 100
    assert usage["cumulative_input_high_water"] == 100
    assert usage["total_cost_usd"] == 1


def test_stale_policy_engine_usage_keeps_native_guard_metadata(db_uri, monkeypatch):
    from omnigent.llms import context_window
    from omnigent.runtime.policies.engine import PolicyEngine

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="policy-native", agent_id=_AGENT_ID)
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=store,
    )
    monkeypatch.setattr(context_window, "fetch_model_pricing_with_provider", lambda *a, **k: None)
    _persist_native_cumulative_usage(
        conv.id,
        {
            "model": "unknown-model",
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 0,
        },
        store,
    )
    engine.record_usage(input_tokens=10, output_tokens=0, total_tokens=10)
    usage = _usage(store, conv.id)
    assert usage["unpriced_tokens"] == 110
    assert usage["cumulative_input_high_water"] == 100
    assert usage["input_tokens"] == 110


@pytest.mark.parametrize("rates", [(0.01, 0.001), (0.001, 0.01)])
@pytest.mark.parametrize("cache", [None, "read", "write"])
def test_routed_alias_prices_only_new_usage_and_enforces_budget(db_uri, monkeypatch, rates, cache):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from omnigent.inference_pricing import pricing_model_label
    from omnigent.llms import context_window
    from omnigent.server.inference_proxy import InferenceProxy

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed-usage", agent_id=_AGENT_ID)
    monkeypatch.setattr(
        context_window,
        "fetch_model_pricing_with_provider",
        lambda model, **kw: {
            "first-target": ModelPricing(input_per_token=rates[0], output_per_token=rates[0]),
            "second-target": ModelPricing(input_per_token=rates[1], output_per_token=rates[1]),
        }.get(model),
    )
    factor = 0.1 if cache == "read" else 1.25 if cache == "write" else 1.0

    def report(count):
        data = {
            "model": "friendly-alias",
            "cumulative_input_tokens": count,
            "cumulative_output_tokens": 0,
        }
        if cache:
            key = (
                "cumulative_cache_read_input_tokens"
                if cache == "read"
                else "cumulative_cache_creation_input_tokens"
            )
            data[key] = count
        _persist_native_cumulative_usage(conv.id, data, store)

    key = pricing_model_label("friendly-alias")
    store.set_labels(conv.id, {key: "first-target"})
    report(100)
    store.set_labels(conv.id, {key: "second-target"})
    report(200)
    expected = (100 * rates[0] + 100 * rates[1]) * factor
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(expected)
    # Both a replay and an older snapshot must leave recorded spend unchanged.
    report(200)
    report(100)
    report(200)
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(expected)
    assert _usage(store, conv.id)["by_model"]["friendly-alias"]["total_cost_usd"] == pytest.approx(
        expected
    )
    service = SimpleNamespace(
        config=SimpleNamespace(session_cost_limits={"owner": expected - 0.001}),
        _owner_authority=lambda _: SimpleNamespace(user_id="owner"),
    )
    with pytest.raises(HTTPException) as error:
        InferenceProxy._check_cost_limit(service, store.get_conversation(conv.id))
    assert error.value.status_code == 402
    report(300)
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(
        expected + 100 * rates[1] * factor
    )


def test_known_delta_cannot_price_unpriced_legacy_history(db_uri, monkeypatch):
    from omnigent.llms import context_window

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="legacy-usage", agent_id=_AGENT_ID)
    store.set_session_usage(
        conv.id, {"input_tokens": 100, "output_tokens": 0, "total_tokens": 100}
    )
    monkeypatch.setattr(
        context_window,
        "fetch_model_pricing_with_provider",
        lambda *a, **k: ModelPricing(
            input_per_token=0.001,
            output_per_token=0.001,
        ),
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "model": "known-model",
            "cumulative_input_tokens": 200,
            "cumulative_output_tokens": 0,
        },
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(0.1)
    assert usage["unpriced_tokens"] == 100


def test_priced_relay_preserves_legacy_unpriced_history_atomically(db_uri):
    from omnigent.server.routes._sessions.orchestration import _accumulate_session_usage

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="legacy-relay", agent_id=_AGENT_ID)
    store.set_session_usage(
        conv.id, {"input_tokens": 100, "output_tokens": 0, "total_tokens": 100}
    )
    _accumulate_session_usage(
        {
            "usage": {
                "model": "priced-model",
                "input_tokens": 10,
                "output_tokens": 0,
                "total_tokens": 10,
                "cost_usd": 1.0,
            }
        },
        conv.id,
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["input_tokens"] == 110
    assert usage["total_cost_usd"] == 1
    assert usage["unpriced_tokens"] == 100


def test_relay_preserves_existing_enforcement_cost_delta(db_uri):
    from omnigent.server.routes._sessions.orchestration import _accumulate_session_usage

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="split-relay", agent_id=_AGENT_ID)
    store.set_session_usage(conv.id, {"total_cost_usd": 2.0, "policy_cost_usd": 5.0})
    _accumulate_session_usage(
        {
            "usage": {
                "model": "priced-model",
                "input_tokens": 10,
                "output_tokens": 0,
                "total_tokens": 10,
                "cost_usd": 1.0,
            }
        },
        conv.id,
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == 3
    assert usage["policy_cost_usd"] == 6


@pytest.mark.parametrize("existing_baseline", [False, True])
@pytest.mark.parametrize("reported_cost", [1, 2])
def test_split_cost_and_tokens_do_not_double_charge(
    db_uri, monkeypatch, existing_baseline, reported_cost
):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from omnigent.llms import context_window
    from omnigent.server.inference_proxy import InferenceProxy

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="split-cost", agent_id=_AGENT_ID)
    monkeypatch.setattr(
        context_window,
        "fetch_model_pricing_with_provider",
        lambda *a, **k: ModelPricing(
            input_per_token=0.01,
            output_per_token=0.01,
        ),
    )
    if existing_baseline:
        _persist_native_cumulative_usage(
            conv.id,
            {
                "model": "priced-model",
                "cumulative_input_tokens": 100,
                "cumulative_output_tokens": 0,
                "cumulative_cost_usd": 1,
            },
            store,
        )
    _persist_native_cumulative_usage(
        conv.id, {"model": "priced-model", "cumulative_cost_usd": reported_cost}, store
    )
    for _ in range(2):
        _persist_native_cumulative_usage(
            conv.id,
            {
                "model": "priced-model",
                "cumulative_input_tokens": 200,
                "cumulative_output_tokens": 0,
            },
            store,
        )
        usage = _usage(store, conv.id)
        assert usage["total_cost_usd"] == pytest.approx(reported_cost)
        assert usage["unpriced_tokens"] == (100 if existing_baseline else 200)
    service = SimpleNamespace(
        config=SimpleNamespace(session_cost_limits={"owner": reported_cost + 1}),
        _owner_authority=lambda _: SimpleNamespace(user_id="owner"),
    )
    with pytest.raises(HTTPException) as error:
        InferenceProxy._check_cost_limit(service, store.get_conversation(conv.id))
    assert error.value.status_code == 503
    # Joint trusted coverage resolves the ambiguity without lowering a cost.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "model": "priced-model",
            "cumulative_input_tokens": 200,
            "cumulative_output_tokens": 0,
            "cumulative_cost_usd": reported_cost,
        },
        store,
    )
    assert "unpriced_tokens" not in _usage(store, conv.id)
    assert "pending_cost_coverage" not in _usage(store, conv.id)
    InferenceProxy._check_cost_limit(service, store.get_conversation(conv.id))
    _persist_native_cumulative_usage(
        conv.id,
        {
            "model": "priced-model",
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 0,
        },
        store,
    )
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(reported_cost + 1)


def test_catalog_recovery_replay_does_not_create_a_priced_zero(db_uri, monkeypatch):
    from omnigent.llms import context_window
    from omnigent.policies.builtins.cost import _usage_is_unpriced

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="catalog-replay", agent_id=_AGENT_ID)
    monkeypatch.setattr(context_window, "fetch_model_pricing_with_provider", lambda *a, **k: None)
    report = {
        "model": "priced-model",
        "cumulative_input_tokens": 100,
        "cumulative_output_tokens": 0,
    }
    _persist_native_cumulative_usage(conv.id, report, store)
    assert _usage_is_unpriced(_usage(store, conv.id))
    monkeypatch.setattr(
        context_window,
        "fetch_model_pricing_with_provider",
        lambda *a, **k: ModelPricing(
            input_per_token=0.01,
            output_per_token=0.01,
        ),
    )
    _persist_native_cumulative_usage(conv.id, report, store)
    assert _usage_is_unpriced(_usage(store, conv.id))
    assert "total_cost_usd" not in _usage(store, conv.id)
    assert _usage(store, conv.id)["unpriced_tokens"] == 100
