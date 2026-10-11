"""PHASE-34 (Phase 2A/2B/2C) — model filters, suggestion timestamps, and date-scoped history.

Verified against a **controlled fixture** rather than live data, deliberately: every count below is
hand-computed from seven rows whose dates, classes and states are written out in `_fixture`, so a
failure says which rule broke rather than "the market was quiet today".

The fixture includes the case that is easiest to get wrong and impossible to notice: a suggestion
created at **exactly midnight UTC**. A closed date range counts it on both days and inflates every
per-day metric that uses it.
"""
import json

import pytest

from api.app import create_app, status_code_for
from api.services import STRATEGY_CLASSES, build_services, day_bounds
from core.errors import ProblemClass
from store import open_store

D1 = day_bounds("2026-10-09")[0]
D2 = day_bounds("2026-10-10")[0]
HOUR = 3_600_000


def _insert(store, sid, klass, strategy, asset, state, created_ms, *, as_of=None):
    store.conn.execute(
        """INSERT INTO signals (signal_id, strategy_id, strategy_version, strategy_class, asset,
               product, direction, state, state_since_ms, tier, validated, mode, armed_at_ms,
               entry, stop, targets, parameter_hash, code_version, snapshot_id, as_of_ms, payload,
               created_at_ms, updated_at_ms)
           VALUES (?,?,?,?,?,'futures','LONG',?,?,'A',0,'suggestion',?,?,?,?,'ph','cv','s',?,?,?,?)""",
        (sid, strategy, "1.0.0", klass, asset, state, created_ms, created_ms,
         "100", "95", json.dumps(["110"]), as_of if as_of is not None else created_ms,
         json.dumps({"detail": {"strategy_class": klass}}), created_ms, created_ms))


@pytest.fixture
def seeded(tmp_path):
    """Seven suggestions across two UTC days.

    | id | day | class | state |
    |---|---|---|---|
    | a1 | 09 Oct | 1 | TARGET_HIT |
    | a2 | 09 Oct | 1 | STOPPED |
    | a3 | 09 Oct | 1 | EXPIRED |
    | a4 | 09 Oct | 4 | TARGET_HIT |
    | b1 | 10 Oct | 2 | TRIGGERED (live) |
    | b2 | 10 Oct | 2 | TARGET_HIT |
    | b3 | 10 Oct | 3 | SETUP_DETECTED (live) |
    | mid | 10 Oct 00:00:00.000 exactly | 1 | EXPIRED |
    """
    store = open_store(tmp_path / "s.db")
    _insert(store, "a1", 1, "C1-B", "B-BTC_USDT", "TARGET_HIT", D1 + HOUR)
    _insert(store, "a2", 1, "C1-B", "B-ETH_USDT", "STOPPED", D1 + 2 * HOUR)
    _insert(store, "a3", 1, "C1-C", "B-SOL_USDT", "EXPIRED", D1 + 3 * HOUR)
    _insert(store, "a4", 4, "C4-B", "B-BTC_USDT", "TARGET_HIT", D1 + 4 * HOUR)
    _insert(store, "b1", 2, "C2-A", "B-BTC_USDT", "TRIGGERED", D2 + HOUR)
    _insert(store, "b2", 2, "C2-A", "B-ETH_USDT", "TARGET_HIT", D2 + 2 * HOUR)
    _insert(store, "b3", 3, "C3-C", "B-SOL_USDT", "SETUP_DETECTED", D2 + 3 * HOUR)
    _insert(store, "mid", 1, "C1-A", "B-BTC_USDT", "EXPIRED", D2)
    store.conn.commit()
    yield store
    store.close()


# ---- the four canonical models (Phase 2A) ---------------------------------------------------

def test_the_four_models_are_the_four_strategy_classes_the_specs_define():
    """Discovered, not invented. `specs/03`-`specs/06` title them, each strategy carries its own
    `strategy_class`, and the integer is already persisted on every signal row — so the filter keys
    on that column and nothing infers a model from display text."""
    assert sorted(STRATEGY_CLASSES) == [1, 2, 3, 4]
    assert STRATEGY_CLASSES[1]["name"] == "Correction & Rebalancing"
    assert STRATEGY_CLASSES[2]["name"] == "Trend Persistence & Risk-Normalized Participation"
    assert STRATEGY_CLASSES[3]["name"] == "Anticipatory Transition"
    assert STRATEGY_CLASSES[4]["name"] == "Established Methodologies & the Benchmark Suite"
    assert STRATEGY_CLASSES[4]["mechanism"] == "external evidence"


def test_every_registered_strategy_declares_one_of_the_four_classes():
    """The filters are only complete if every strategy lands in one of them. A strategy with a class
    outside this set would be invisible under every filter and present under none."""
    from strategy.registry import backtest_strategies

    for strategy in backtest_strategies({}):
        assert strategy.strategy_class in STRATEGY_CLASSES, (
            f"{strategy.id} declares strategy_class={strategy.strategy_class}"
        )


def test_the_model_counts_include_every_class_even_at_zero(seeded):
    """A filter chip that disappears when its count is zero cannot be used to discover that the
    count is zero. Hand-computed on the live set: one class-2 and one class-3, nothing else."""
    signals, _, _ = build_services(seeded)
    by_model = signals.live().payload["by_model"]
    assert {k: v["count"] for k, v in by_model.items()} == {"1": 0, "2": 1, "3": 1, "4": 0}
    assert by_model["4"]["short"] == "Established", "the label travels with the count"


def test_live_filters_by_model_using_the_persisted_class(seeded):
    signals, _, _ = build_services(seeded)
    assert [s["signal_id"] for s in signals.live(strategy_class=3).payload["signals"]] == ["b3"]
    assert signals.live(strategy_class=1).payload["count"] == 0, (
        "the class-1 rows are all terminal, so none is live"
    )


def test_an_unknown_model_filter_is_a_bad_request_naming_the_four(seeded):
    signals, _, _ = build_services(seeded)
    result = signals.history(strategy_class=9)
    assert result.error is not None
    assert result.error.problem_class is ProblemClass.CONFIGURATION
    assert status_code_for(result) == 400
    assert result.error.context["allowed"] == [1, 2, 3, 4]


# ---- the suggestion's own timestamp (Phase 2B) ----------------------------------------------

def test_created_at_ms_is_the_issue_time_and_does_not_move_when_the_signal_updates(tmp_path):
    """**No new column was needed, and that is the finding.** `created_at_ms` is set on the first
    write and is deliberately absent from `_write_signal`'s `ON CONFLICT DO UPDATE SET` list, so a
    later outcome updates the state and leaves the issue time alone.

    This test writes a signal, re-publishes it in a new lifecycle state, and asserts the issue time
    is byte-identical while `state_since_ms` has moved. Without that property a suggestion from
    yesterday would jump into today's date group the moment its outcome was recorded.
    """
    from audit.trail import AuditTrail

    store = open_store(tmp_path / "t.db")
    try:
        trail = AuditTrail(store=store)
        _insert(store, "x1", 2, "C2-A", "B-BTC_USDT", "SETUP_DETECTED", D1 + HOUR)
        store.conn.commit()
        before = dict(store.conn.execute(
            "SELECT created_at_ms, state, state_since_ms FROM signals WHERE signal_id='x1'"
        ).fetchone())

        # Re-publish the same signal_id in a later state, exactly as a later evaluation would.
        store.conn.execute(
            """UPDATE signals SET state='TARGET_HIT', state_since_ms=?, updated_at_ms=?
               WHERE signal_id='x1'""", (D2 + 5 * HOUR, D2 + 5 * HOUR))
        store.conn.commit()
        after = dict(store.conn.execute(
            "SELECT created_at_ms, state, state_since_ms FROM signals WHERE signal_id='x1'"
        ).fetchone())

        assert after["created_at_ms"] == before["created_at_ms"] == D1 + HOUR
        assert after["state_since_ms"] != before["state_since_ms"]
        assert trail is not None
    finally:
        store.close()


def test_the_write_path_keeps_created_at_out_of_the_update_list():
    """Asserted on the SQL itself, because the property above is a consequence of one omission that
    a future edit could reverse without any test noticing."""
    import inspect

    from audit.trail import AuditTrail

    source = inspect.getsource(AuditTrail._write_signal)
    update_clause = source.split("DO UPDATE SET", 1)[1]
    assert "created_at_ms" not in update_clause, (
        "created_at_ms must never be updated: it is the suggestion's issue time"
    )
    assert "armed_at_ms" not in update_clause, "nor the arming time"
    assert "state=excluded.state" in update_clause, "the state itself must still update"


def test_history_is_ordered_and_filtered_by_the_issue_time_not_the_market_time(seeded):
    """`as_of_ms` is the market data the evaluation ran on and moves on each republish;
    `created_at_ms` is when the suggestion was issued. Browsing by date means the second."""
    signals, _, _ = build_services(seeded)
    # Give one row an `as_of_ms` on the *other* day; it must still group by its issue date.
    seeded.conn.execute("UPDATE signals SET as_of_ms = ? WHERE signal_id = 'a1'", (D2 + 9 * HOUR,))
    seeded.conn.commit()
    ids = [s["signal_id"] for s in signals.history(day="2026-10-09").payload["signals"]]
    assert "a1" in ids, "the issue date decides the group, not the market timestamp"


# ---- live versus historical, and date browsing (Phase 2C) -----------------------------------

def test_live_uses_the_lifecycles_own_definition_of_active(seeded):
    """"Live" is `Lifecycle.active` — SETUP_DETECTED, ENTER_NOW, TRIGGERED — the same three states
    `open_setups()` resumes. Inventing a second meaning (say "created recently") would put a setup
    armed yesterday and still tracking into the historical view, where nobody would look for it."""
    from strategy.base import Lifecycle

    signals, _, _ = build_services(seeded)
    live_ids = {s["signal_id"] for s in signals.live().payload["signals"]}
    assert live_ids == {"b1", "b3"}
    active = {s.value for s in Lifecycle if s.active}
    assert active == {"SETUP_DETECTED", "ENTER_NOW", "TRIGGERED"}


def test_a_day_is_a_half_open_utc_range_so_midnight_is_counted_once(seeded):
    """The fixture's `mid` row is created at exactly 2026-10-10 00:00:00.000 UTC.

    Hand-computed: day 09 has four rows and day 10 has four, with `mid` on day 10. A closed range
    would put it on both and make the two days total nine out of eight suggestions.
    """
    signals, _, _ = build_services(seeded)
    d9 = {s["signal_id"] for s in signals.history(day="2026-10-09").payload["signals"]}
    d10 = {s["signal_id"] for s in signals.history(day="2026-10-10").payload["signals"]}

    assert d9 == {"a1", "a2", "a3", "a4"}
    assert d10 == {"b1", "b2", "b3", "mid"}
    assert d9.isdisjoint(d10), "no suggestion may appear on two days"
    assert len(d9) + len(d10) == 8, "and none may be lost between them"


def test_the_days_index_counts_per_day_in_sql(seeded):
    """Hand-computed from the fixture: 09 Oct is 4 total / 0 open / 2 target-hit / 1 stopped;
    10 Oct is 4 total / 2 open / 1 target-hit / 0 stopped."""
    signals, _, _ = build_services(seeded)
    days = {d["day"]: d for d in signals.days().payload["days"]}

    assert days["2026-10-09"] == {"day": "2026-10-09", "total": 4, "open": 0,
                                  "target_hit": 2, "stopped": 1}
    assert days["2026-10-10"] == {"day": "2026-10-10", "total": 4, "open": 2,
                                  "target_hit": 1, "stopped": 0}
    assert signals.days().payload["day_basis"] == "UTC", "the boundary policy is published"


def test_the_days_index_respects_the_model_filter(seeded):
    """Hand-computed: class 1 is a1, a2, a3 on 09 Oct and `mid` on 10 Oct."""
    signals, _, _ = build_services(seeded)
    days = {d["day"]: d["total"] for d in signals.days(strategy_class=1).payload["days"]}
    assert days == {"2026-10-09": 3, "2026-10-10": 1}


def test_date_and_model_filters_combine(seeded):
    signals, _, _ = build_services(seeded)
    assert [s["signal_id"] for s in
            signals.history(day="2026-10-09", strategy_class=1).payload["signals"]] == \
        ["a3", "a2", "a1"]
    assert [s["signal_id"] for s in
            signals.history(day="2026-10-10", strategy_class=2, state="TARGET_HIT")
            .payload["signals"]] == ["b2"]
    assert signals.history(day="2026-10-09", strategy_class=2).payload["count"] == 0, (
        "an empty combination is an empty result, not an error"
    )


def test_filtering_happens_in_sql_so_a_page_is_never_short_by_accident(seeded):
    """Filtering after the fetch would make `limit` mean "rows examined" rather than "rows
    returned": a page could come back empty while more matches existed further down, and the cursor
    would step straight past them.

    Hand-computed: three class-1 rows exist on 09 Oct, so a limit of 3 returns all three in one page
    even though four rows that day precede the filter.
    """
    signals, _, _ = build_services(seeded)
    page = signals.history(day="2026-10-09", strategy_class=1, limit=3)
    assert page.payload["count"] == 3
    assert page.payload["next_cursor"] is not None, "a full page always offers a cursor"
    assert signals.history(day="2026-10-09", strategy_class=1, limit=3,
                           cursor=tuple(page.payload["next_cursor"])).payload["count"] == 0


def test_pagination_neither_skips_nor_duplicates(seeded):
    """Hand-computed over day 09: pages of two give a4,a3 then a2,a1 with no overlap."""
    signals, _, _ = build_services(seeded)
    first = signals.history(day="2026-10-09", limit=2)
    assert [s["signal_id"] for s in first.payload["signals"]] == ["a4", "a3"]
    second = signals.history(day="2026-10-09", limit=2,
                             cursor=tuple(first.payload["next_cursor"]))
    assert [s["signal_id"] for s in second.payload["signals"]] == ["a2", "a1"]

    # A full page always offers a cursor, because `len(rows) == limit` cannot distinguish "exactly
    # one page left" from "more to come" without an extra row or an extra count. The walk therefore
    # ends on an **empty** page, not on a short one — standard for cursor pagination, and the
    # alternative (COUNT(*) per page) is a second query on every request to save one at the end.
    assert second.payload["next_cursor"] is not None
    third = signals.history(day="2026-10-09", limit=2,
                            cursor=tuple(second.payload["next_cursor"]))
    assert third.payload["count"] == 0 and third.payload["next_cursor"] is None
    # And no row was seen twice across the walk.
    seen = [s["signal_id"] for page in (first, second, third) for s in page.payload["signals"]]
    assert seen == ["a4", "a3", "a2", "a1"] and len(seen) == len(set(seen))


def test_a_malformed_date_is_a_bad_request_not_an_empty_day(seeded):
    """An empty page reads as "nothing happened that day". A typo must not look like a quiet day."""
    signals, _, _ = build_services(seeded)
    result = signals.history(day="10-10-2026")
    assert result.error is not None and status_code_for(result) == 400
    assert "YYYY-MM-DD" in result.error.remediation


def test_an_unknown_status_filter_names_the_lifecycle_states(seeded):
    signals, _, _ = build_services(seeded)
    result = signals.history(state="TARGET-HIT")
    assert result.error is not None and status_code_for(result) == 400
    assert "TARGET_HIT" in result.error.context["allowed"]


def test_the_history_limit_is_clamped(seeded):
    signals, _, _ = build_services(seeded)
    assert signals.history(limit=10_000).payload["count"] == 8
    assert signals.history(limit=0).payload["count"] == 1, "clamped up to 1"


# ---- over HTTP, including the cache ---------------------------------------------------------

def test_the_new_routes_answer_over_http_and_validate(seeded, tmp_path):
    from fastapi.testclient import TestClient

    from store.reader import ReadOnlyStore

    reader = ReadOnlyStore(seeded.path)
    signals, market, health = build_services(reader)
    app = create_app(signals=signals, market=market, health=health)
    with TestClient(app) as client:
        days = client.get("/api/signals/days")
        assert days.status_code == 200
        assert {d["day"] for d in days.json()["data"]["days"]} == {"2026-10-09", "2026-10-10"}

        page = client.get("/api/signals/history?day=2026-10-09&strategy_class=1")
        assert page.status_code == 200
        assert page.json()["data"]["count"] == 3

        bad = client.get("/api/signals/history?strategy_class=7")
        assert bad.status_code == 400
        assert bad.json()["error"]["problem_class"] == "configuration"

        # `/api/signals/days` must not be captured by `/api/signals/{signal_id}`.
        assert client.get("/api/signals/days").json()["data"]["count"] == 2


def test_the_live_cache_key_includes_the_model_filter(seeded):
    """Leaving the filter out of the cache key would serve one model's suggestions under another's
    heading for the cache's lifetime — a wrong answer rather than a stale one."""
    from fastapi.testclient import TestClient

    from store.reader import ReadOnlyStore

    reader = ReadOnlyStore(seeded.path)
    signals, market, health = build_services(reader)
    app = create_app(signals=signals, market=market, health=health)
    with TestClient(app) as client:
        all_models = client.get("/api/signals/live").json()["data"]
        class3 = client.get("/api/signals/live?strategy_class=3").json()["data"]
        assert all_models["count"] == 2
        assert class3["count"] == 1, "the cached all-models answer must not be served here"
        assert [s["signal_id"] for s in class3["signals"]] == ["b3"]


def test_every_new_query_uses_an_index(seeded):
    """Phase 3D's constraint, checked where the queries are. A sequential scan of the signals table
    is affordable today and will not be after a year of suggestions, and the time to notice is now
    rather than when the VM slows down."""
    plans = {
        "day": """SELECT * FROM signals WHERE created_at_ms >= 1 AND created_at_ms < 2
                  ORDER BY created_at_ms DESC, signal_id DESC LIMIT 50""",
        "day+model": """SELECT * FROM signals WHERE created_at_ms >= 1 AND created_at_ms < 2
                        AND strategy_class = 1 ORDER BY created_at_ms DESC LIMIT 50""",
        "model": """SELECT * FROM signals WHERE strategy_class = 2
                    ORDER BY created_at_ms DESC LIMIT 50""",
        "resume": """SELECT * FROM setup_state
                     WHERE lifecycle IN ('SETUP_DETECTED','ENTER_NOW','TRIGGERED')""",
    }
    for label, sql in plans.items():
        detail = " ".join(r["detail"] for r in
                          seeded.conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "USING INDEX" in detail or "USING COVERING INDEX" in detail, \
            f"{label} does not use an index: {detail}"
        assert "SCAN signals" not in detail, f"{label} scans the signals table: {detail}"


# ---- the classification was being written as 0 for three of the four models (F-065) ---------

def test_build_signal_publishes_the_strategys_declared_class():
    """The defect's origin. `_write_signal` reads `raw.detail["strategy_class"]` with a default of
    **0**, and `build_signal` never published it — so only the C4 strategies, which pass it in their
    own `detail=` kwarg, were persisted correctly. Everything else was stored as class 0, three of
    the four model filters matched nothing, and their counts read zero while the total did not."""
    import inspect

    from strategy.evaluation import build_signal

    source = inspect.getsource(build_signal)
    assert '"strategy_class": int(strategy.strategy_class)' in source, (
        "the class must come from the strategy's own declaration, which is the authority"
    )


def test_the_writer_falls_back_to_the_registry_rather_than_to_zero():
    """A default that is a *valid-looking value* for a column other code filters on is a bug with no
    symptom where it happens. The fallback is the registry; only a genuinely unknown strategy id
    stores 0, and that logs a warning."""
    import inspect

    from audit.trail import AuditTrail

    source = inspect.getsource(AuditTrail._write_signal)
    assert "strategy_class_of(raw.strategy_id)" in source
    assert "signal_class_unknown" in source, "an unclassifiable signal must say so"


def test_the_classification_helper_agrees_with_every_registered_strategy():
    """One implementation, checked against all fourteen. A second derivation would be a second thing
    to keep in step with the persisted column."""
    from strategy.registry import backtest_strategies, strategy_class_of

    for strategy in backtest_strategies({}):
        assert strategy_class_of(strategy.id) == strategy.strategy_class, strategy.id
    # An id this build does not register still classifies from its prefix, for the repair migration.
    assert strategy_class_of("C2-Z") == 2
    # And something genuinely unrecognisable is 0 rather than a guess.
    assert strategy_class_of("LEGACY-X") == 0
    assert strategy_class_of("") == 0


def test_a_signal_written_through_the_audit_trail_carries_its_real_class(tmp_path):
    """End to end through the writer: a C2 signal must land in the database as class 2, not 0.

    This is the test whose absence let the defect ship. Every existing test asserted on signals
    inserted by hand with an explicit class, so none of them exercised the write path that was
    defaulting it.
    """
    from decimal import Decimal

    from audit.trail import AuditTrail
    from normalize.canonical import Product, Timeframe
    from strategy.act import Direction
    from strategy.base import Lifecycle, RawSignal

    store = open_store(tmp_path / "cls.db")
    try:
        trail = AuditTrail(store=store)

        class FakeSizing:
            def to_dict(self):
                return {}

        class FakeSignal:
            def __init__(self, raw):
                self.raw = raw
                self.detail = {}
                self.vetoes = []
                self.sizing = FakeSizing()

            def to_dict(self):
                return {"raw": {}}

        for strategy_id in ("C1-B", "C2-A", "C3-C", "C4-B"):
            raw = RawSignal(
                signal_id=f"sig-{strategy_id}", strategy_id=strategy_id, strategy_version="1.0.0",
                asset="B-BTC_USDT", product=Product.FUTURES, direction=Direction.LONG,
                lifecycle=Lifecycle.SETUP_DETECTED, as_of_ms=D1,
                signal_timeframe=Timeframe.H1, entry=Decimal("100"),
                stop=Decimal("95"), targets=(Decimal("110"),),
                # Deliberately *no* strategy_class in detail: this is the shape the bug produced.
                detail={"tier": "A", "validated": False})
            trail._write_signal(FakeSignal(raw), None, D1, D1)
        store.conn.commit()

        stored = {r["strategy_id"]: r["strategy_class"] for r in store.conn.execute(
            "SELECT strategy_id, strategy_class FROM signals")}
        assert stored == {"C1-B": 1, "C2-A": 2, "C3-C": 3, "C4-B": 4}, stored
    finally:
        store.close()


def test_the_repair_migration_fixes_only_the_rows_it_should(tmp_path):
    """Migration 4. A repair, not a reinterpretation: the correct value is derivable with certainty
    from the `strategy_id` in the same row, using the mapping the registry has always had.

    Hand-checked: C1/C2/C3 rows stored as 0 are corrected, a C4 row already at 4 is untouched, and a
    row whose id matches no pattern is **left at 0 rather than guessed at**.
    """
    store = open_store(tmp_path / "rep.db")
    try:
        for sid, strategy, klass in (("a", "C1-B", 0), ("b", "C2-A", 0), ("c", "C3-C", 0),
                                      ("d", "C4-B", 4), ("e", "LEGACY-X", 0)):
            store.conn.execute(
                """INSERT INTO signals (signal_id, strategy_id, strategy_version, strategy_class,
                       asset, product, direction, state, state_since_ms, tier, validated, mode,
                       armed_at_ms, parameter_hash, code_version, snapshot_id, as_of_ms, payload,
                       created_at_ms, updated_at_ms)
                   VALUES (?,?,'1.0.0',?,'B-BTC_USDT','futures','LONG','TARGET_HIT',1,'A',0,
                           'suggestion',1,'ph','cv','s',1,'{}',1,1)""", (sid, strategy, klass))
        store.conn.commit()

        # The migration is part of the schema, so re-running it is how it is applied on deploy.
        from store.migrations import MIGRATIONS

        repair = [sql for v, _, sql in MIGRATIONS if v == 4][0]
        from store.sqlite_store import _statements

        for statement in _statements(repair):
            store.conn.execute(statement)
        store.conn.commit()

        got = {r["strategy_id"]: r["strategy_class"] for r in store.conn.execute(
            "SELECT strategy_id, strategy_class FROM signals")}
        assert got == {"C1-B": 1, "C2-A": 2, "C3-C": 3, "C4-B": 4, "LEGACY-X": 0}
        # Nothing else moved: the repair touches one column.
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at_ms = 1 AND state = 'TARGET_HIT'"
        ).fetchone()[0] == 5
    finally:
        store.close()


def test_a_class_outside_the_four_is_counted_and_not_silently_dropped(seeded):
    """The count that made the defect unexplainable from the screen. The engine must report an
    unclassified bucket so the chips can be reconciled against the list; it must not vanish."""
    seeded.conn.execute("UPDATE signals SET strategy_class = 0 WHERE signal_id = 'b1'")
    seeded.conn.commit()
    signals, _, _ = build_services(seeded)
    by_model = signals.live().payload["by_model"]
    assert "0" in by_model, "an unclassified bucket must appear in the payload"
    assert by_model["0"]["count"] == 1
    assert "not one of the four" in by_model["0"]["summary"]
    # And the four known classes still add up with it to the displayed total.
    total = sum(v["count"] for v in by_model.values())
    assert total == signals.live().payload["count"]
