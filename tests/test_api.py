"""PHASE-23/24/25 — the service layer, the HTTP boundary and the dashboard.

The tests that matter here are the ones about *not misleading*: a store failure must not render as an
empty dashboard, a cached response must not claim to be fresher than its data, and a control-strategy
signal must never reach a response.
"""
import json
import time
from pathlib import Path

import pytest

from api.app import ResponseCache, assert_bind_allowed, create_app, status_code_for
from api.render import (
    age_class,
    age_text,
    render_action_bar,
    render_empty_panel,
    render_exposure_strip,
    render_health,
    render_page,
    render_signal_card,
    render_signals,
)
from api.services import FreshnessEnvelope, ServiceResult, build_services
from core.errors import StartupError, StoreError
from core.health import ComponentHealth, HealthReport, SystemStatus
from normalize.canonical import Product
from store import open_store

T0 = 1767225600000


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "api.db")
    yield s
    s.close()


def insert_signal(store, *, signal_id="sig-1", strategy="C1-B", asset="B-BTC_USDT",
                  state="SETUP_DETECTED", tradeable=True, as_of=T0, validated=False):
    payload = {"raw": {"rationale": "mean reversion is operative and price is at structure",
                       "direction": "LONG"},
               "tradeable": tradeable,
               "detail": {"k_net": "2.4", "breakeven_hit_rate": "0.31"}}
    store.conn.execute(
        """INSERT INTO signals (signal_id, strategy_id, strategy_version, strategy_class, asset,
               product, direction, state, state_since_ms, tier, validated, mode, armed_at_ms,
               entry, stop, targets, k_net, breakeven_hit_rate, parameter_hash, code_version,
               snapshot_id, as_of_ms, payload, created_at_ms, updated_at_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (signal_id, strategy, "1.0.0", 1, asset, "futures", "LONG", state, as_of, "A",
         int(validated), "suggestion", as_of, "85000", "84500", json.dumps(["86500"]),
         "2.4", "0.31", "ph", "cv", "snap", as_of, json.dumps(payload), as_of, as_of))
    store.conn.commit()


def insert_rejection(store, *, strategy="C1-B", asset="B-BTC_USDT", binding="mean_reversion_operative"):
    store.conn.execute(
        """INSERT INTO rejections (at_ms, as_of_ms, strategy_id, asset, product, binding_condition,
               passed_count, total_count, conditions, rolled_up)
           VALUES (?,?,?,?,?,?,?,?,?,0)""",
        (T0, T0, strategy, asset, "futures", binding, 5, 6,
         json.dumps([{"name": binding, "passed": False, "value": 1.18, "threshold": 1.0}])))
    store.conn.commit()


def healthy_report():
    return HealthReport(components=[ComponentHealth.healthy("store", detail="ok")])


# ---- the envelope -------------------------------------------------------------------------

def test_age_is_computed_from_the_data_not_from_when_it_was_served():
    env = FreshnessEnvelope(served_at_ms=T0 + 5000, data_as_of_ms=T0,
                            system_status=SystemStatus.HEALTHY)
    assert env.age_seconds == 5.0
    assert env.to_dict()["age_seconds"] == 5.0


def test_age_renders_as_age_never_as_a_timestamp():
    """"4s ago" is actionable; "14:32:07" makes the reader do arithmetic to find out if it is alive."""
    assert age_text(4) == "4s ago"
    assert age_text(125) == "2m ago"
    assert age_text(7200) == "2.0h ago"
    assert age_text(None) == "age unknown"
    assert age_class(10) == "age-ok" and age_class(90) == "age-amber" and age_class(600) == "age-red"


def test_a_cached_response_reports_data_age_not_cache_age():
    """specs/14 §3's cache trap. A cached response that reports itself fresh turns a visible outage
    into an invisible one."""
    cache = ResponseCache(ttl_seconds=5.0)
    stale_data_ms = int(time.time() * 1000) - 120_000          # the data is two minutes old
    result = ServiceResult(payload={"signals": []},
                           envelope=FreshnessEnvelope(served_at_ms=int(time.time() * 1000),
                                                      data_as_of_ms=stale_data_ms,
                                                      system_status=SystemStatus.HEALTHY))
    cache.put("live", result)
    time.sleep(0.05)
    served = cache.get("live")
    assert served is not None and cache.hits == 1
    assert served.envelope.data_as_of_ms == stale_data_ms      # unchanged by caching
    assert served.envelope.age_seconds >= 120                  # reports the data's age, not ~0
    assert served.envelope.served_at_ms >= result.envelope.served_at_ms


def test_a_failed_result_is_not_cached():
    """Caching a failure would delay recovery and make an outage look steadier than it is."""
    cache = ResponseCache()
    failed = ServiceResult(payload=None,
                           envelope=FreshnessEnvelope(served_at_ms=T0, data_as_of_ms=None,
                                                      system_status=SystemStatus.DOWN),
                           error=StoreError("db is locked", component="store", operation="query"))
    cache.put("live", failed)
    assert cache.get("live") is None


# ---- services -----------------------------------------------------------------------------

def test_live_signals_are_returned_with_their_envelope(store):
    insert_signal(store)
    signals, _, _ = build_services(store, health_source=healthy_report)
    result = signals.live()
    assert result.ok and result.payload["count"] == 1
    assert result.payload["by_asset"] == {"B-BTC_USDT": ["sig-1"]}
    assert result.envelope.system_status is SystemStatus.HEALTHY
    assert result.envelope.correlation_id


def test_a_control_signal_never_reaches_a_response(store):
    """specs/13 §13: three layers guard this, and the API boundary is the last one."""
    insert_signal(store, signal_id="ctl-1", strategy="C4-X1", tradeable=False)
    insert_signal(store, signal_id="real-1", strategy="C1-B", tradeable=True)
    signals, _, _ = build_services(store)
    ids = [s["signal_id"] for s in signals.live().payload["signals"]]
    assert ids == ["real-1"]
    # and only an explicit opt-in (used by backtest tooling, never by a handler) shows it
    all_ids = [s["signal_id"] for s in signals.live(include_untradeable=True).payload["signals"]]
    assert set(all_ids) == {"ctl-1", "real-1"}


def test_a_store_failure_renders_as_down_not_as_an_empty_market(store):
    """specs/14 §10: the single most dangerous thing this API can do is report a blind system as a
    quiet one."""
    signals, _, _ = build_services(store, health_source=healthy_report)
    store.close()                                    # the query will now fail
    result = signals.live()
    assert not result.ok
    assert result.envelope.system_status is SystemStatus.DOWN
    assert result.payload == {"signals": [], "count": 0, "by_asset": {}}   # shape kept
    body = result.to_dict()
    assert body["error"]["correlation_id"] == result.envelope.correlation_id
    assert "signals.live" in result.envelope.degraded_reasons[0]


def test_an_unknown_signal_id_is_a_structured_error_not_a_crash(store):
    signals, _, _ = build_services(store)
    result = signals.detail("nope")
    assert not result.ok and result.error.code == "API_SERVICE_UNAVAILABLE"
    assert "history" in result.error.remediation


def test_history_is_cursor_paginated(store):
    for i in range(5):
        insert_signal(store, signal_id=f"sig-{i}", as_of=T0 + i * 1000)
    signals, _, _ = build_services(store)
    page = signals.history(limit=2)
    assert len(page.payload["signals"]) == 2
    cursor = page.payload["next_cursor"]
    assert cursor is not None
    second = signals.history(cursor=cursor, limit=2)
    first_ids = {s["signal_id"] for s in page.payload["signals"]}
    second_ids = {s["signal_id"] for s in second.payload["signals"]}
    assert not (first_ids & second_ids)              # no overlap, no skips


def test_rejections_answer_why_there_is_no_signal(store):
    """specs/14 §7 — the endpoint most systems cannot offer, because they never recorded this."""
    insert_rejection(store)
    insert_rejection(store)
    _, market, _ = build_services(store)
    result = market.rejections("B-BTC_USDT")
    assert result.ok and len(result.payload["rejections"]) == 2
    assert result.payload["most_binding"][0]["binding_condition"] == "mean_reversion_operative"
    assert result.payload["most_binding"][0]["n"] == 2
    assert result.payload["rejections"][0]["conditions"][0]["threshold"] == 1.0


def test_health_without_a_source_says_so_rather_than_claiming_healthy(store):
    _, _, health = build_services(store)              # no health_source attached
    result = health.health()
    assert not result.ok
    assert "no health source" in result.error.message
    assert result.envelope.system_status is SystemStatus.DOWN


def test_warmup_is_a_state_not_an_error(store):
    from features.value import FeatureKey, FeatureSet, FeatureStatus, FeatureValue
    from normalize.canonical import Timeframe

    warming = FeatureValue(name="TRD_RET", version="1", status=FeatureStatus.WARMING,
                           reason="need 721 1h bars, have 300", timeframe=Timeframe.H1,
                           horizon="long", detail={"hours_to_warm": 421.0})
    fs = FeatureSet(asset="B-BTC_USDT", product=Product.FUTURES, as_of_ms=T0,
                    values={FeatureKey("TRD_RET", Timeframe.H1, "long"): warming})
    _, _, health = build_services(store)
    result = health.warmup({"B-BTC_USDT": fs})
    assert result.ok                                   # warming is not a failure
    assert result.envelope.system_status is SystemStatus.WARMING
    assert result.payload["assets"]["B-BTC_USDT"]["hours_to_warm"] == 421.0


# ---- the HTTP boundary ---------------------------------------------------------------------

def test_the_app_refuses_to_bind_a_public_address_without_a_token():
    """specs/10 §9: exposure without authentication is not a supported configuration."""
    assert_bind_allowed("127.0.0.1", "")               # localhost is always fine
    assert_bind_allowed("0.0.0.0", "a-token")          # authenticated is fine
    with pytest.raises(StartupError) as ei:
        assert_bind_allowed("0.0.0.0", "")
    assert "refusing to bind" in ei.value.message
    assert "SSH tunnel" in ei.value.remediation
    assert ei.value.problem_class.value == "configuration"


def test_a_service_failure_answers_503_and_a_healthy_one_200():
    ok = ServiceResult(payload={}, envelope=FreshnessEnvelope(T0, T0, SystemStatus.HEALTHY))
    degraded = ServiceResult(payload={}, envelope=FreshnessEnvelope(T0, T0, SystemStatus.DEGRADED))
    failed = ServiceResult(payload={}, envelope=FreshnessEnvelope(T0, None, SystemStatus.DOWN),
                           error=StoreError("x", component="c", operation="o"))
    assert status_code_for(ok) == 200
    assert status_code_for(degraded) == 200            # degraded still answers, with its reasons
    assert status_code_for(failed) == 503


def test_the_http_routes_serve_the_same_service_results(store, tmp_path):
    """Served through ReadOnlyStore, because a sync handler runs in a worker thread and a sqlite3
    connection belongs to the thread that made it — sharing the writer's raises on the first request."""
    from fastapi.testclient import TestClient

    from store.reader import ReadOnlyStore

    insert_signal(store)
    insert_rejection(store)
    reader = ReadOnlyStore(store.path)
    signals, market, health = build_services(reader, health_source=healthy_report)
    app = create_app(signals=signals, market=market, health=health)
    client = TestClient(app)

    live = client.get("/api/signals/live")
    assert live.status_code == 200
    body = live.json()
    assert body["data"]["count"] == 1
    assert body["envelope"]["age_seconds"] is not None
    assert body["envelope"]["system_status"] == "HEALTHY"

    assert client.get("/api/signals/sig-1").status_code == 200
    assert client.get("/api/signals/nope").status_code == 503
    assert client.get("/api/assets/B-BTC_USDT/rejections").json()["data"]["most_binding"]
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200


def test_an_authenticated_app_rejects_an_unauthenticated_request(store):
    from fastapi.testclient import TestClient

    from store.reader import ReadOnlyStore

    signals, market, health = build_services(ReadOnlyStore(store.path),
                                             health_source=healthy_report)
    app = create_app(signals=signals, market=market, health=health, host="0.0.0.0",
                     auth_token="secret-token")
    client = TestClient(app)
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={"x-auth-token": "secret-token"}).status_code == 200


def test_no_route_accepts_a_parameter_that_reaches_an_engine(store):
    """specs/14 §11: the API is read-only in the strongest sense — it cannot influence a signal."""
    signals, market, health = build_services(store)

    app = create_app(signals=signals, market=market, health=health)
    forbidden = {"delta", "threshold", "risk_per_trade", "param", "size", "equity", "override"}
    for route in app.routes:
        params = set(getattr(route, "param_convertors", {}) or {})
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            import inspect
            params |= set(inspect.signature(endpoint).parameters)
        assert not (params & forbidden), f"{route.path} accepts {params & forbidden}"


# ---- the dashboard --------------------------------------------------------------------------

def result_with(signals, *, status=SystemStatus.HEALTHY, error=None, age_ms=1000):
    env = FreshnessEnvelope(served_at_ms=T0 + age_ms, data_as_of_ms=T0, system_status=status,
                            degraded_reasons=["store: locked"] if error else [],
                            correlation_id="abc123")
    return ServiceResult(payload={"signals": signals, "count": len(signals)}, envelope=env,
                         error=error)


def card(**kw):
    base = {"signal_id": "sig-1", "strategy_id": "C1-B", "asset": "B-BTC_USDT", "direction": "LONG",
            "state": "SETUP_DETECTED", "entry": "85000", "stop": "84500", "targets": ["86500"],
            "validated": False, "k_net": "2.4", "breakeven_hit_rate": "0.31",
            "payload": {"raw": {"rationale": "the variance ratio is below 1"}}}
    return {**base, **kw}


def test_a_quiet_market_and_a_broken_system_look_different():
    """Failure mode 1 (specs/15 §1): both render as empty panels by default."""
    quiet = render_signals(result_with([]))
    blind = render_signals(result_with([], status=SystemStatus.DOWN,
                                       error=StoreError("locked", component="s", operation="q")))
    assert "no setups match right now" in quiet
    assert "SYSTEM DOWN" not in quiet
    assert "SYSTEM DOWN" in blind
    assert "not</strong> an empty market" in blind
    assert "ref abc123" in blind                       # the correlation id reaches the screen


def test_enter_now_relocates_and_spells_out_the_word():
    """specs/15 §6: position, size and a word — three cues before colour."""
    bar = render_action_bar([card(state="ENTER_NOW"), card(signal_id="s2")])
    assert "action-bar live" in bar
    assert ">ENTER<" in bar
    assert "85000" in bar


def test_an_empty_action_bar_says_so_rather_than_being_blank():
    bar = render_action_bar([card(), card(signal_id="s2")])
    assert "no entries live — 2 setups waiting" in bar
    assert "action-bar empty" in bar


def test_an_unvalidated_signal_is_a_labelled_section_not_a_badge():
    """Failure mode 3: a badge among many gets scanned past."""
    html = render_signals(result_with([card(validated=False)]))
    assert "not yet backtested" in html
    assert "unvalidated" in html                        # dashed border class
    assert "not-backtested-section" in html


def test_confidence_is_never_blank_and_offers_the_breakeven_rate_instead():
    """Failure mode 2: nulls render as blank by default."""
    html = render_signals(result_with([card()]))
    assert "not estimated" in html
    assert "31% win rate to break even" in html


def test_three_correlated_longs_are_described_as_one_position():
    """Failure mode 5: three cards imply three opportunities."""
    signals = [card(signal_id=f"s{i}", asset=a)
               for i, a in enumerate(("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"))]
    strip = render_exposure_strip(signals)
    assert "3 long · 0 short" in strip
    assert "approximately one position" in strip
    assert "0.7-0.9 correlated" in strip


def test_an_empty_panel_states_the_binding_condition():
    """Failure mode 1 again, and the visible payoff of evaluating every condition."""
    panel = render_empty_panel("C1-B", {"binding_condition": "mean_reversion_operative",
                                        "passed_count": 5, "total_count": 6})
    assert "no setup — mean_reversion_operative" in panel
    assert "(5 of 6 conditions pass)" in panel
    assert "no evaluation recorded yet" in render_empty_panel("C1-A", None)


def test_a_stale_envelope_is_visibly_different():
    """Failure mode 6: subtle greying is not sufficient."""
    fresh = render_signals(result_with([card()], age_ms=2_000))
    stale = render_signals(result_with([card()], age_ms=400_000))
    assert "age-ok" in fresh and "age-red" in stale
    assert "7m ago" in stale      # 400 s of envelope age plus the fixture offset


def test_the_page_renders_and_escapes_its_input():
    signals = result_with([card(strategy_id="<script>alert(1)</script>")])
    page = render_page(signals, ServiceResult(payload={"components": []},
                                              envelope=FreshnessEnvelope(T0, T0,
                                                                         SystemStatus.HEALTHY)))
    assert "<!doctype html>" in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "never places orders" in page


def test_health_renders_the_problem_class_so_triage_is_possible():
    report = HealthReport(components=[
        ComponentHealth(name="regime:B-BTC_USDT", status=SystemStatus.DEGRADED,
                        detail="failed validity tests",
                        problem_class=__import__("core.errors", fromlist=["ProblemClass"])
                        .ProblemClass.MODELLING)])
    html = render_health(ServiceResult(payload=report.to_dict(),
                                       envelope=FreshnessEnvelope(T0, T0, SystemStatus.DEGRADED)))
    assert "modelling" in html
    assert "regime:B-BTC_USDT" in html
    assert "DEGRADED" in html


def test_the_api_reads_through_a_thread_safe_read_only_connection(store, tmp_path):
    """Two properties at once: a connection per thread (the bug this caught), and read-only enforced
    by SQLite rather than by convention."""
    import sqlite3
    import threading

    from store.reader import ReadOnlyStore

    insert_signal(store)
    reader = ReadOnlyStore(store.path)
    seen = {}

    def read():
        seen[threading.current_thread().name] = reader.conn.execute(
            "SELECT COUNT(*) AS n FROM signals").fetchone()["n"]

    threads = [threading.Thread(target=read, name=f"w{i}") for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(seen.values()) == {1}, seen
    assert reader.stats()["read_connections"] >= 3

    with pytest.raises(sqlite3.OperationalError):
        reader.conn.execute("DELETE FROM signals")          # read-only is enforced by SQLite


def test_pointing_the_reader_at_a_missing_database_fails_clearly(tmp_path):
    from store.reader import ReadOnlyStore

    with pytest.raises(StoreError) as ei:
        ReadOnlyStore(tmp_path / "absent.db")
    assert "never creates the database" in ei.value.remediation


# ---- exposure through a tunnel (the Vercel dashboard) --------------------------------------

def test_a_tunnel_in_front_of_loopback_still_demands_a_token():
    """The guard that the hosted dashboard made necessary.

    Binding ``127.0.0.1`` used to *mean* unreachable, so the token was only demanded for a
    non-loopback bind. With a Cloudflare tunnel the process still binds loopback and is reachable
    from the internet, so the original check would have passed while the API sat open. The operator
    declares the tunnel, because only they know what is in front of the process.
    """
    from api.app import assert_bind_allowed
    from core.errors import StartupError

    # Loopback with no tunnel: fine, and unchanged.
    assert_bind_allowed("127.0.0.1", "")

    with pytest.raises(StartupError, match="without an auth token"):
        assert_bind_allowed("127.0.0.1", "", public_via_tunnel=True)

    assert_bind_allowed("127.0.0.1", "a-long-random-token", public_via_tunnel=True)


def test_the_settings_guard_agrees_with_the_api_guard():
    """Two guards, one rule. They are separate because one runs at configuration load and the other
    at app construction, and a host should fail at the earliest of the two."""
    from config.settings import Settings

    tunnelled = Settings(server={"host": "127.0.0.1", "auth_token": "", "public_via_tunnel": True})
    with pytest.raises(RuntimeError, match="without CT_SERVER__AUTH_TOKEN"):
        tunnelled.assert_bind_allowed()

    ok = Settings(server={"host": "127.0.0.1", "auth_token": "t" * 40, "public_via_tunnel": True})
    ok.assert_bind_allowed()


def test_the_token_is_compared_in_constant_time():
    """A plain ``!=`` returns as soon as two bytes differ, so the time taken leaks how much of the
    token was guessed. Cheap to exploit if measurable, and one import to fix."""
    source = (Path(__file__).resolve().parents[1] / "src" / "api" / "app.py").read_text(
        encoding="utf-8")
    assert "secrets.compare_digest" in source
    assert "supplied != auth_token" not in source


# ---- a directly-exposed port (F-043: no tunnel) --------------------------------------------

def test_a_public_bind_without_a_token_refuses_to_start():
    """The whole safety model of the direct-exposure deployment, in one guard.

    With the Cloudflare tunnel removed, the port is reachable from the internet and the token is the
    only thing in front of it. So there must be no configuration in which the port is open and
    unauthenticated — and this is a startup failure rather than a warning, because a warning is
    something a host carries on past.
    """
    with pytest.raises(StartupError, match="refusing to bind"):
        assert_bind_allowed("0.0.0.0", "")
    assert_bind_allowed("0.0.0.0", "a-long-random-token")       # does not raise


def test_refused_requests_are_counted_and_logged_in_aggregate(tmp_path):
    """One log line per refusal would let a scanner fill the disk — a denial of service handed over
    for free. Once the port is public, refusals arrive as background noise, so they are summarised.
    """
    from api.app import REFUSAL_LOG_EVERY

    store = open_store(tmp_path / "api.db")
    try:
        signals, market, health = build_services(store)
        app = create_app(signals=signals, market=market, health=health,
                         auth_token="the-real-token")
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            # Well under the threshold: counted, not yet logged.
            for _ in range(3):
                assert client.get("/api/health").status_code == 401
            assert app.state.refusals["count"] == 3
            assert app.state.refusals["next_log_at"] == REFUSAL_LOG_EVERY

            # Past the threshold: the summary fires once and the next one is scheduled further out,
            # so the log rate stays flat however hard the port is probed.
            for _ in range(REFUSAL_LOG_EVERY):
                client.get("/api/health")
            assert app.state.refusals["count"] == 3 + REFUSAL_LOG_EVERY
            assert app.state.refusals["next_log_at"] > REFUSAL_LOG_EVERY

        # A valid token is unaffected by any of it. The status is incidental here — these services
        # were built without a health source, so /api/health answers 503 by design — what matters is
        # that the request got past the guard and was not counted.
        with TestClient(app) as client:
            before = app.state.refusals["count"]
            response = client.get("/api/health", headers={"x-auth-token": "the-real-token"})
            assert response.status_code != 401, "a valid token must get past the guard"
            assert app.state.refusals["count"] == before, "a valid request is not a refusal"
    finally:
        store.close()


def test_the_runner_health_can_omit_the_writers_store_for_a_request_thread():
    """F-043's second half: a sync FastAPI handler runs in a worker thread, and the writer's sqlite
    connection belongs to the thread that opened it.

    Querying it from a request raised *"SQLite objects created in a thread can only be used in that
    same thread"*, so the dashboard reported the store as DOWN while the engine was writing happily.
    The API passes ``include_store=False`` and the reader reports its own connection instead — which
    is the more honest number, because the writer being healthy says nothing about whether a request
    can read.
    """
    import inspect

    from app.runner import AppRunner

    signature = inspect.signature(AppRunner.health)
    assert "include_store" in signature.parameters
    assert signature.parameters["include_store"].default is True, \
        "the default must stay True so the engine's own health is unchanged"

    source = inspect.getsource(AppRunner.health)
    assert "include_store" in source
    # The store check must be gated on the flag, not merely accept it.
    assert "if include_store else None" in source


# ---- the candle endpoint: the view that shows whether data is arriving at all ----------------

def insert_bar(store, *, asset="B-BTC_USDT", timeframe="1h", open_time_ms=T0, close="85000",
               is_final=True, product="futures"):
    store.conn.execute(
        """INSERT INTO bars (asset, product, timeframe, open_time_ms, open, high, low, close,
               volume_base, is_final, synthesised, source, content_hash, first_fetched_ms,
               last_fetched_ms, fetch_count)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,'test',?,?,?,1)""",
        (asset, product, timeframe, open_time_ms, "84900", "85100", "84800", close,
         "12.5", int(is_final), f"h{open_time_ms}", open_time_ms, open_time_ms))
    store.conn.commit()


def test_candles_come_back_oldest_first_whatever_order_they_were_written_in(store):
    """Ordering is a property of the data and belongs where the data is produced (specs/10 §5).

    A client that forgot to sort would draw the market mirrored — rising where it fell — and nothing
    on the screen would look wrong.
    """
    for i in (2, 0, 1):                     # written out of order on purpose
        insert_bar(store, open_time_ms=T0 + i * 3_600_000, close=str(85000 + i))
    _, market, _ = build_services(store)
    payload = market.candles("B-BTC_USDT", timeframe="1h").payload
    assert [c["t"] for c in payload["candles"]] == [T0, T0 + 3_600_000, T0 + 7_200_000]
    assert [c["c"] for c in payload["candles"]] == ["85000", "85001", "85002"]


def test_candle_prices_stay_strings_so_nothing_is_rounded_on_the_way_out(store):
    """The store keeps decimals as TEXT precisely so no float ever touches a price. JSON numbers
    *are* floats, so emitting them as numbers would reintroduce the rounding the store exists to
    avoid — on the one screen a person uses to check the data."""
    insert_bar(store, close="85000.12345678")
    _, market, _ = build_services(store)
    candle = market.candles("B-BTC_USDT", timeframe="1h").payload["candles"][0]
    for key in ("o", "h", "l", "c", "v"):
        assert isinstance(candle[key], str), f"{key} must stay a string"
    assert candle["c"] == "85000.12345678", "the exact stored value must survive"


def test_an_unconfirmed_bar_is_returned_and_flagged_rather_than_hidden(store):
    """Both halves matter. Hiding it would make the current interval look missing; returning it
    unflagged would present a bar the feature layer refuses to compute on as settled."""
    insert_bar(store, open_time_ms=T0, is_final=True)
    insert_bar(store, open_time_ms=T0 + 3_600_000, is_final=False)
    _, market, _ = build_services(store)
    payload = market.candles("B-BTC_USDT", timeframe="1h").payload
    assert [c["final"] for c in payload["candles"]] == [True, False]
    assert payload["pending_confirmation"] == 1


def test_candle_age_is_measured_from_the_bars_close_not_its_open(store):
    """A 1h bar that opened 59 minutes ago is current, not an hour old.

    Measuring from the open would put every healthy hourly feed permanently in the amber band, and a
    freshness warning that is always on is a freshness warning nobody reads.
    """
    now_ms = int(time.time() * 1000)
    opened = now_ms - 59 * 60_000           # opened 59 minutes ago, closes in a minute
    insert_bar(store, open_time_ms=opened)
    _, market, _ = build_services(store)
    result = market.candles("B-BTC_USDT", timeframe="1h")
    assert result.envelope.data_as_of_ms == opened + 3_600_000
    # The close is a minute in the future, so the age clamps to zero rather than going negative.
    assert result.envelope.age_seconds == 0.0


def test_an_unknown_interval_is_a_bad_request_not_an_outage(store):
    """The problem class is what tells the operator where to look, and this one must not say
    ``infrastructure``: a chart asking for an interval this system never stores is the caller being
    wrong, and a 503 would send someone to check the VM, the firewall and the service logs for a
    mistake that is entirely in the query string."""
    from core.errors import ProblemClass

    _, market, _ = build_services(store)
    result = market.candles("B-BTC_USDT", timeframe="2h")
    assert result.error is not None
    assert result.error.problem_class is ProblemClass.CONFIGURATION
    assert "2h" in result.error.message
    assert status_code_for(result) == 400, "a caller's mistake is not a service outage"
    # And the payload keeps its shape, so a renderer does not also have to handle None.
    assert result.payload["candles"] == []


def test_a_candle_limit_is_clamped_rather_than_reaching_the_query_unbounded(store):
    for i in range(5):
        insert_bar(store, open_time_ms=T0 + i * 3_600_000)
    _, market, _ = build_services(store)
    assert market.candles("B-BTC_USDT", limit=2).payload["count"] == 2
    assert market.candles("B-BTC_USDT", limit=0).payload["count"] == 1, "clamped up to 1"
    assert market.candles("B-BTC_USDT", limit=10_000).payload["count"] == 5
    # The total is reported separately, so "2 drawn of 5 stored" is sayable.
    assert market.candles("B-BTC_USDT", limit=2).payload["stored_total"] == 5


def test_an_asset_with_no_bars_is_empty_but_not_an_error(store):
    """Nothing stored yet is a real, reportable fact. It must not raise, and it must not claim an
    age it does not have."""
    _, market, _ = build_services(store)
    result = market.candles("B-SOL_USDT", timeframe="1d")
    assert result.ok
    assert result.payload["candles"] == [] and result.payload["stored_total"] == 0
    assert result.payload["newest_ms"] is None
    assert result.envelope.age_seconds is None, "an absent age must not read as fresh"


def test_the_candle_route_is_reachable_and_validated_over_http(store):
    """Through ``ReadOnlyStore``, as production does: a sync handler runs in a worker thread and a
    sqlite3 connection belongs to the thread that made it (F-043b)."""
    from fastapi.testclient import TestClient

    from store.reader import ReadOnlyStore

    insert_bar(store, open_time_ms=T0)
    reader = ReadOnlyStore(store.path)
    signals, market, health = build_services(reader, health_source=healthy_report)
    app = create_app(signals=signals, market=market, health=health)
    with TestClient(app) as client:
        ok = client.get("/api/candles/B-BTC_USDT?timeframe=1h&limit=10")
        assert ok.status_code == 200
        assert ok.json()["data"]["count"] == 1

        bad = client.get("/api/candles/B-BTC_USDT?timeframe=2h")
        assert bad.status_code == 400
        assert bad.json()["error"]["problem_class"] == "configuration"


# ---- the freshness fallback (F-045) ---------------------------------------------------------

def test_with_no_signals_the_age_falls_back_to_the_newest_stored_bar(store):
    """F-045. A quiet market is the normal state, so "no signals" is the common case — and with no
    signal to date the envelope reported ``age_seconds: null``, which the dashboard rendered as
    "market data age unknown".

    That is the one thing a reader must never be left with: unable to tell a healthy quiet market
    from a feed that stopped hours ago. Both show no suggestions; only the age distinguishes them.
    """
    insert_bar(store, open_time_ms=T0)
    signals, _, _ = build_services(store)
    result = signals.live()
    assert result.payload["signals"] == []
    assert result.envelope.data_as_of_ms == T0 + 3_600_000, "the newest bar's close"
    assert result.payload["freshness_basis"] == "newest stored bar"


def test_a_signals_own_timestamp_still_wins_when_there_is_one(store):
    """The fallback is a fallback. A signal's own ``as_of_ms`` is what the suggestion was computed
    from, and that is the honest age for a panel showing suggestions."""
    insert_bar(store, open_time_ms=T0 + 86_400_000)      # a much newer bar
    insert_signal(store, as_of=T0)
    signals, _, _ = build_services(store)
    result = signals.live()
    assert result.envelope.data_as_of_ms == T0
    assert result.payload["freshness_basis"] == "newest signal"


def test_an_empty_database_still_reports_an_unknown_age_rather_than_inventing_one(store):
    """With neither a signal nor a bar there is genuinely nothing to date the data from, and the
    envelope must say so. An invented age would be worse than no age."""
    signals, _, _ = build_services(store)
    result = signals.live()
    assert result.envelope.data_as_of_ms is None
    assert result.envelope.age_seconds is None


# ---- market-data freshness is not the newest setup's age (F-052) ----------------------------

def test_market_data_freshness_is_reported_apart_from_the_newest_signals_age(store):
    """The two were one number, and the result was a permanent false alarm.

    The envelope dates the *payload*, which for this endpoint is the newest signal. A setup at an
    hourly horizon legitimately stays armed for hours, so that number grows with the setup's age and
    says nothing about whether data is arriving. The dashboard labelled it "market data" and judged
    it against a 300-second red threshold, so a healthy system with a 12-minute-old setup reported
    **"too old to act on"**.

    Hand-computed: the signal is stamped at T0 and the newest 1m bar closes a minute after T0, so the
    two ages must differ by exactly that minute and must not be the same field.
    """
    insert_signal(store, as_of=T0)
    insert_bar(store, timeframe="1m", open_time_ms=T0)
    signals, _, _ = build_services(store)
    payload = signals.live().payload

    assert payload["market_data"]["as_of_ms"] == T0 + 60_000, "the 1m bar's close"
    assert payload["market_data"]["basis"] == "newest 1m bar close"
    assert payload["market_data"]["interval_s"] == 60
    # The signal's own age stays on the envelope, unchanged, because that is what it means.
    assert signals.live().envelope.data_as_of_ms == T0


def test_market_data_freshness_is_absent_rather_than_zero_when_no_bars_exist(store):
    """An absent age must stay absent: `Freshness` renders "the engine did not say how old this is",
    which is honest, where a 0 would read as perfectly fresh."""
    insert_signal(store, as_of=T0)
    signals, _, _ = build_services(store)
    market = signals.live().payload["market_data"]
    assert market["as_of_ms"] is None and market["age_s"] is None


# ---- /api/sources: what are we actually fetching? -------------------------------------------

def test_sources_reports_each_series_with_its_rows_newest_bar_and_last_close(store):
    insert_bar(store, timeframe="1h", open_time_ms=T0, close="85000")
    insert_bar(store, timeframe="1h", open_time_ms=T0 + 3_600_000, close="85500", is_final=False)
    insert_bar(store, timeframe="1d", open_time_ms=T0, close="84000")
    _, market, _ = build_services(store)
    payload = market.sources().payload

    by_tf = {s["timeframe"]: s for s in payload["series"]}
    assert by_tf["1h"]["rows"] == 2
    assert by_tf["1h"]["pending_confirmation"] == 1
    assert by_tf["1h"]["last_value"]["c"] == "85500", "the newest bar's close, not the oldest"
    assert by_tf["1h"]["newest_ms"] == T0 + 3_600_000 + 3_600_000, "measured from the bar's close"
    assert by_tf["1d"]["rows"] == 1
    assert by_tf["1h"]["tier"] == "A", "candles are rebuildable from the venue"


def test_a_series_lateness_is_reported_in_intervals_not_only_in_seconds(store):
    """Seconds cannot compare across timeframes: twenty minutes is a dead 1-minute feed and a
    perfectly healthy daily one. One absolute threshold for both is what makes a freshness warning
    permanent, and a permanent warning is one nobody reads.

    Hand-computed: a 1h bar whose close was five hours ago is five intervals behind.
    """
    now_ms = int(time.time() * 1000)
    opened = ((now_ms // 3_600_000) - 6) * 3_600_000      # closed 5 h ago, aligned
    insert_bar(store, timeframe="1h", open_time_ms=opened)
    _, market, _ = build_services(store)
    series = market.sources().payload["series"][0]
    assert series["interval_s"] == 3600
    assert series["intervals_behind"] in (5, 6), "five whole intervals, six if the clock just ticked"


def test_sources_reports_every_point_in_time_table_with_what_it_last_delivered(store):
    store.conn.execute(
        """INSERT INTO futures_rt (asset, ts_ms, funding_rate, predicted_funding_rate, mark_price,
               last_price, received_at_ms, source_ts_ms)
           VALUES ('B-BTC_USDT', ?, '0.00001152', '0.00001', '83481.6', '83473.9', ?, ?)""",
        (T0, T0, T0))
    store.conn.commit()
    _, market, _ = build_services(store)
    by_id = {s["id"]: s for s in market.sources().payload["point_in_time"]}

    rt = by_id["futures_rt"]
    assert rt["tier"] == "B" and rt["rebuildable"] is False
    assert "no archive endpoint" in rt["note"], (
        "the permanence of a Tier B gap is the single most important thing to say about it"
    )
    asset = rt["per_asset"][0]
    assert asset["asset"] == "B-BTC_USDT" and asset["rows"] == 1
    assert asset["last_value"]["funding_rate"] == "0.00001152"
    assert asset["newest_ms"] == T0


def test_a_source_nothing_polls_says_so_rather_than_showing_a_bare_zero(store):
    """Three different readings of an empty table, and only one is true for `trades`: we do not
    collect it. A bare zero would read as a broken poller; omitting the row would leave a reader
    wondering whether trades were being recorded."""
    _, market, _ = build_services(store)
    by_id = {s["id"]: s for s in market.sources().payload["point_in_time"]}
    assert by_id["trades"]["collected"] is False
    assert "does not poll" in by_id["trades"]["note"]
    # And a source that *is* collected does not claim otherwise.
    assert by_id["futures_rt"]["collected"] is True


def test_one_unreadable_source_does_not_hide_the_others(store):
    """The whole point of this page is comparing sources, so one failure must not take the
    comparison with it. The error is attached to **that source** and the rest still report."""
    store.conn.execute("DROP TABLE positioning")
    store.conn.commit()
    insert_bar(store, timeframe="1h", open_time_ms=T0)
    _, market, _ = build_services(store)
    result = market.sources()

    by_id = {s["id"]: s for s in result.payload["point_in_time"]}
    assert by_id["positioning"]["readable"] is False
    assert by_id["positioning"]["error"]["problem_class"] == "application_bug"
    assert by_id["futures_rt"]["readable"] is True, "the other sources must still be reported"
    assert len(result.payload["series"]) == 1, "and the series must still be reported"


def test_sources_reports_data_quality_events_because_fresh_is_not_correct(store):
    """A source can be perfectly current and still be throwing rows away. Timestamps alone would
    call that healthy, so the rejection counts sit on the same page."""
    from store.interface import DQEvent

    store.record_dq_event(DQEvent(kind="ROWS_REJECTED", component="normalize.futures_rt",
                                  operation="futures_rt", at_ms=int(time.time() * 1000),
                                  problem_class="data_quality", severity="warning",
                                  asset="B-BTC_USDT", detail={"reasons": {"STALE": 3}}))
    _, market, _ = build_services(store)
    dq = market.sources().payload["data_quality"]
    assert dq["readable"] is True and dq["window_h"] == 24
    assert dq["events"][0]["kind"] == "ROWS_REJECTED" and dq["events"][0]["count"] == 1


def test_the_sources_route_answers_over_http_and_takes_no_parameters(store):
    """No parameters at all, deliberately: this is the endpoint someone opens when they do not yet
    know what is wrong, so there must be nothing to get wrong about the request."""
    import inspect

    from fastapi.testclient import TestClient

    from store.reader import ReadOnlyStore

    insert_bar(store, timeframe="1h", open_time_ms=T0)
    reader = ReadOnlyStore(store.path)
    signals, market, health = build_services(reader, health_source=healthy_report)
    app = create_app(signals=signals, market=market, health=health)
    with TestClient(app) as client:
        response = client.get("/api/sources")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["series"][0]["rows"] == 1
        assert body["envelope"]["age_seconds"] is not None

    signature = inspect.signature(market.sources)
    assert not [p for p in signature.parameters.values() if p.name != "self"]


# ---- both dashboards must apply the same two rules (F-051, F-053) ---------------------------

def _pending_signal():
    """A C3-C-shaped row: a placeholder direction and no levels."""
    return {"signal_id": "p1", "strategy_id": "C3-C", "asset": "B-BTC_USDT", "direction": "LONG",
            "state": "SETUP_DETECTED", "entry": None, "stop": None, "targets": [],
            "detail": {"pending_direction": True, "boundary": "85000"},
            "payload": {"raw": {"direction": "LONG", "rationale": "watching a boundary"}}}


def _spread_signal(state="SETUP_DETECTED"):
    """A C1-C-shaped row: residual levels and a leg table."""
    return {"signal_id": "s1", "strategy_id": "C1-C", "asset": "B-ETH_USDT", "direction": "LONG",
            "state": state, "entry": "-0.04145262148598610125", "stop": "-0.0451935447161604650",
            "targets": ["0"],
            "detail": {"price_basis": "residual", "two_leg": True,
                       "legs": [{"asset": "B-ETH_USDT", "direction": "LONG", "price": "2575",
                                 "notional_weight": "1.00"},
                                {"asset": "B-BTC_USDT", "direction": "SHORT", "price": "83470",
                                 "notional_weight": "0.9123"}]},
            "payload": {"raw": {"direction": "LONG", "rationale": "residual in the tail"}}}


def test_the_server_dashboard_does_not_print_a_placeholder_direction_as_a_recommendation():
    """C3-C's `direction` is a structural placeholder — the strategy's whole premise is that it does
    not predict which way the level breaks. Printing "LONG" states a recommendation it refuses to
    make, and the Vercel card and this one were getting it wrong identically."""
    html = render_signal_card(_pending_signal())
    assert "direction not yet decided" in html
    assert "85000" in html, "the level being watched must be named"
    assert ">LONG<" not in html, "the placeholder must not be rendered as a direction"
    # And no levels block, because there is nothing to place rather than levels unknown.
    assert "<span>entry</span>" not in html


def test_the_server_dashboard_does_not_print_a_residual_as_an_entry_price():
    html = render_signal_card(_spread_signal())
    assert "not prices" in html
    assert "0.9123" in html and "83470" in html, "both legs must be shown or the trade is executed wrong"
    assert "<span>entry</span>" not in html, (
        "a number under the word entry is a price to anyone skimming, whatever the caveat says"
    )
    # The residual is still shown, labelled by its basis.
    assert "residual entry" in html


def test_the_server_action_bar_refuses_an_undecided_entry():
    pending = {**_pending_signal(), "state": "ENTER_NOW"}
    html = render_action_bar([pending])
    assert "no entries live" in html, "a pending setup is not an entry instruction"

    spread = _spread_signal(state="ENTER_NOW")
    html2 = render_action_bar([spread])
    assert "two-leg spread" in html2
    assert "-0.0414" not in html2, "a residual must never appear as a trigger price"


def test_the_server_exposure_strip_counts_only_directional_signals():
    """Hand-counted: one real long, one pending, one spread. The strip must say 1 long 0 short —
    not 3 long — because the pending setup has no direction and the spread is beta-neutral by
    construction, which is the opposite of concentrated directional risk."""
    real = {"signal_id": "r1", "asset": "B-SOL_USDT", "direction": "LONG",
            "state": "SETUP_DETECTED", "detail": {}, "payload": {}}
    html = render_exposure_strip([real, _pending_signal(), _spread_signal()])
    assert "1 long" in html and "0 short" in html


def test_both_dashboards_read_the_same_two_flags():
    """The parity is the point: the two dashboards claim the same specs/15 rules, so a fix in one is
    half a fix. Asserted on both sides rather than trusted."""
    import inspect

    from api import render

    server = inspect.getsource(render)
    assert "pending_direction" in server and "price_basis" in server

    frontend = (Path(__file__).resolve().parents[1] / "frontend" / "components" / "Panels.tsx"
                ).read_text(encoding="utf-8")
    assert "pending_direction" in frontend and "price_basis" in frontend


# ---- the display must stay correct against an OLDER engine (F-058) -------------------------

def test_an_old_engines_two_leg_signal_is_still_not_rendered_as_a_price():
    """The fix for F-051 added `price_basis`. The VM and the dashboard deploy separately, so for as
    long as the engine lags, that field is absent — and without a fallback the fix would have looked
    deployed while a C1-C residual was still printed under the word *entry*.

    `two_leg` and `levels_are` have been in C1-C's payload since it was written, so an old signal is
    still identifiable. This is also the after-upgrade case: the signals table still holds setups
    armed by the previous version.
    """
    old = {"signal_id": "o1", "strategy_id": "C1-C", "asset": "B-BTC_USDT", "direction": "LONG",
           "state": "SETUP_DETECTED", "entry": "-0.0414", "stop": "-0.0451", "targets": ["0"],
           # No `price_basis` anywhere, exactly as the deployed engine writes it.
           "payload": {"raw": {"direction": "LONG", "rationale": "residual"},
                       "detail": {"two_leg": True,
                                  "levels_are": "residual (spread) units, not price",
                                  "legs": [{"asset": "B-BTC_USDT", "direction": "LONG",
                                            "price": "83470", "notional_weight": "1.00"}]}}}
    html = render_signal_card(old)
    assert "not prices" in html
    assert "<span>entry</span>" not in html, (
        "without the fallback the old signal renders a residual as an entry price"
    )
    assert "83470" in html, "the executable leg price must still be shown"


def test_an_old_engines_pending_setup_is_still_not_rendered_as_a_long():
    """`pending` in the setup context, or a two-sided bracket with no entry, is the same statement
    `pending_direction` makes — from a version that did not have the field."""
    for detail in ({"pending": True, "boundary": "85000"},
                   {"entry_triggers": ["84000", "86000"]}):
        old = {"signal_id": "o2", "strategy_id": "C3-C", "asset": "B-BTC_USDT",
               "direction": "LONG", "state": "SETUP_DETECTED", "entry": None, "stop": None,
               "targets": [], "payload": {"raw": {"direction": "LONG"}, "detail": detail}}
        html = render_signal_card(old)
        assert "direction not yet decided" in html, f"missed for {detail}"
        assert ">LONG<" not in html, f"placeholder rendered as a direction for {detail}"


def test_the_two_dashboards_use_the_same_fallbacks():
    """Parity again: a fallback on one side only means the two dashboards disagree about the same
    signal, which is worse than both being wrong in the same way."""
    import inspect

    from api import render

    server = inspect.getsource(render)
    frontend = (Path(__file__).resolve().parents[1] / "frontend" / "lib" / "honesty.ts"
                ).read_text(encoding="utf-8")
    for token in ("two_leg", "levels_are", "entry_triggers"):
        assert token in server, f"{token} fallback missing from render.py"
        assert token in frontend, f"{token} fallback missing from honesty.ts"
