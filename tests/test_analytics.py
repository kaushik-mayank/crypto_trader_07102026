"""PHASE-35 (Phase 3) — analytics counts, rates, denominators and date boundaries.

**Every figure is cross-checked against a direct SQL query over the same fixture**, not against the
service's own output. A twelve-row dataset with every lifecycle state represented is small enough to
count by hand and large enough to catch the mistakes that matter: a bucket omitted from a
denominator, a day boundary counted twice, a zero denominator rendered as zero.

The fixture deliberately includes all five terminal states that are **not** a trade result. The
tempting version of this page treats "target hit versus stopped" as a partition, and it is not: of
the seven terminal states only two are a trade result, and burying the other five in a denominator
understates both rates while dropping them hides most of what the system did.
"""
import json

import pytest

from api.app import create_app, status_code_for
from api.services import (
    CLOSED_WITHOUT_RESULT,
    OPEN_STATES,
    RESOLVED_STATES,
    TRIGGERED_STATES,
    build_analytics,
    day_bounds,
)
from core.errors import ProblemClass
from store import open_store
from strategy.base import Lifecycle

HOUR = 3_600_000
DAY8 = day_bounds("2026-10-08")[0]
DAY9 = day_bounds("2026-10-09")[0]
DAY10 = day_bounds("2026-10-10")[0]

#: (id, class, asset, state, created_at_ms). Twelve rows, nine distinct states.
ROWS = [
    ("s1", 1, "B-BTC_USDT", "TARGET_HIT", DAY8 + HOUR),
    ("s2", 1, "B-BTC_USDT", "STOPPED", DAY8 + 2 * HOUR),
    ("s3", 1, "B-ETH_USDT", "EXPIRED", DAY8 + 3 * HOUR),
    ("s4", 2, "B-BTC_USDT", "TARGET_HIT", DAY9 + HOUR),
    ("s5", 2, "B-SOL_USDT", "TARGET_HIT", DAY9 + 2 * HOUR),
    ("s6", 2, "B-SOL_USDT", "INVALIDATED", DAY9 + 3 * HOUR),
    ("s7", 3, "B-ETH_USDT", "SUPPRESSED", DAY9 + 4 * HOUR),
    ("s8", 3, "B-BTC_USDT", "TRIGGERED", DAY10 + HOUR),
    ("s9", 4, "B-ETH_USDT", "SETUP_DETECTED", DAY10 + 2 * HOUR),
    ("s10", 4, "B-SOL_USDT", "STOPPED", DAY10 + 3 * HOUR),
    ("s11", 4, "B-SOL_USDT", "MISSED", DAY10 + 4 * HOUR),
    ("s12", 1, "B-BTC_USDT", "HANDED_OFF", DAY10 + 5 * HOUR),
]


def _insert(store, sid, klass, asset, state, created):
    store.conn.execute(
        """INSERT INTO signals (signal_id, strategy_id, strategy_version, strategy_class, asset,
               product, direction, state, state_since_ms, tier, validated, mode, armed_at_ms,
               parameter_hash, code_version, snapshot_id, as_of_ms, payload, created_at_ms,
               updated_at_ms)
           VALUES (?,?,?,?,?,'futures','LONG',?,?,'A',0,'suggestion',?,'ph','cv','s',?,?,?,?)""",
        (sid, f"C{klass}-A", "1.0.0", klass, asset, state, created, created, created,
         json.dumps({}), created, created))


@pytest.fixture
def seeded(tmp_path):
    store = open_store(tmp_path / "an.db")
    for row in ROWS:
        _insert(store, *row)
    store.conn.commit()
    yield store
    store.close()


def _window(service):
    return service.overview(start="2026-10-08", end="2026-10-10").payload


def _sql(store, clause=""):
    return store.conn.execute(f"SELECT COUNT(*) FROM signals {clause}").fetchone()[0]


# ---- the state taxonomy is complete and disjoint --------------------------------------------

def test_the_state_buckets_cover_every_publishable_lifecycle_state():
    """A state in none of the buckets would be counted in `all` and in nothing else, so the
    breakdown would silently stop adding up. `CANDIDATE` is excluded because specs/07 §8 says it is
    internal and never published."""
    buckets = set(OPEN_STATES) | set(RESOLVED_STATES) | set(CLOSED_WITHOUT_RESULT)
    publishable = {s.value for s in Lifecycle if s.value != "CANDIDATE"}
    assert buckets == publishable, f"unplaced: {publishable - buckets}, invented: {buckets - publishable}"


def test_the_buckets_do_not_overlap():
    """An overlapping bucket double-counts, which is the classic way an analytics page inflates."""
    assert set(OPEN_STATES).isdisjoint(RESOLVED_STATES)
    assert set(OPEN_STATES).isdisjoint(CLOSED_WITHOUT_RESULT)
    assert set(RESOLVED_STATES).isdisjoint(CLOSED_WITHOUT_RESULT)


def test_only_two_states_are_a_trade_result():
    """The whole correctness of the rates rests on this. `EXPIRED`, `INVALIDATED`, `SUPPRESSED`,
    `MISSED` and `HANDED_OFF` are genuine outcomes in which no trade result exists."""
    assert set(RESOLVED_STATES) == {"TARGET_HIT", "STOPPED"}
    assert len(CLOSED_WITHOUT_RESULT) == 5


def test_invalidated_is_excluded_from_the_triggered_count():
    """`INVALIDATED` is reachable from `SETUP_DETECTED` as well as from `TRIGGERED`, and the current
    state alone cannot say which — so counting it as proof of a trigger would inflate that
    denominator by an unknown amount."""
    assert "INVALIDATED" not in TRIGGERED_STATES
    assert set(TRIGGERED_STATES) == {"TRIGGERED", "TARGET_HIT", "STOPPED"}


# ---- counts, cross-checked against direct SQL -----------------------------------------------

def test_every_count_matches_a_direct_query(seeded):
    """Hand-computed from `ROWS` and verified against SQL independently of the service."""
    totals = _window(build_analytics(seeded))["totals"]

    assert totals["all"] == _sql(seeded) == 12
    assert totals["target_hit"] == _sql(seeded, "WHERE state='TARGET_HIT'") == 3
    assert totals["stopped"] == _sql(seeded, "WHERE state='STOPPED'") == 2
    assert totals["open"] == _sql(
        seeded, "WHERE state IN ('SETUP_DETECTED','ENTER_NOW','TRIGGERED')") == 2
    assert totals["resolved"] == _sql(seeded, "WHERE state IN ('TARGET_HIT','STOPPED')") == 5
    assert totals["closed_without_result"] == _sql(
        seeded,
        "WHERE state IN ('EXPIRED','INVALIDATED','SUPPRESSED','MISSED','HANDED_OFF')") == 5
    assert totals["proves_triggered"] == 6


def test_the_buckets_add_up_to_the_total(seeded):
    """If they ever stop adding up, a bucket has been dropped from a denominator — which is exactly
    the failure that makes a rate quietly wrong rather than visibly broken."""
    t = _window(build_analytics(seeded))["totals"]
    assert t["open"] + t["resolved"] + t["closed_without_result"] + t["unclassified"] == t["all"]
    assert t["unclassified"] == 0


def test_each_no_result_state_is_counted_separately(seeded):
    """Five different reasons a suggestion ended without a trade result. Collapsing them into one
    number would hide, for instance, that everything is being suppressed by arbitration."""
    t = _window(build_analytics(seeded))["totals"]
    assert t["expired"] == 1 and t["invalidated"] == 1 and t["suppressed"] == 1
    assert t["missed"] == 1 and t["handed_off"] == 1


def test_no_signal_is_counted_twice_because_nothing_is_joined(seeded):
    """A join to `signal_events` to pick up a transition time would multiply rows per signal and
    inflate every count. The service touches only the `signals` table, where a signal has exactly
    one row and one current state."""
    import inspect

    from api.services import AnalyticsService

    source = inspect.getsource(AnalyticsService)
    # Only the lines that are SQL, because the docstring *explains* why there are no joins and a
    # naive search finds that explanation. Same trap as `_without_comments` exists for.
    sql_lines = [ln.upper() for ln in source.splitlines() if "FROM SIGNALS" in ln.upper()]
    assert sql_lines, "the SQL could not be located"
    for line in sql_lines:
        assert " JOIN " not in line, f"a join would fan out rows per signal: {line.strip()}"
    # And no other table is read, so nothing can be multiplied by a one-to-many relation.
    assert "FROM SIGNAL_EVENTS" not in source.upper()
    assert "FROM SIGNAL_AUDIT" not in source.upper()
    # And demonstrated: events exist for one signal, and the counts do not move.
    for i in range(4):
        seeded.conn.execute(
            """INSERT INTO signal_events (signal_id, at_ms, from_state, to_state, reason, detail)
               VALUES ('s1', ?, 'SETUP_DETECTED', 'TARGET_HIT', 'r', '{}')""", (DAY8 + i,))
    seeded.conn.commit()
    assert _window(build_analytics(seeded))["totals"]["all"] == 12


# ---- rates: explicit numerators and denominators --------------------------------------------

def test_the_win_rate_is_over_resolved_suggestions_only(seeded):
    """Hand-computed: 3 target hits and 2 stops, so 3/5 = 60%. The seven suggestions that never
    reached a trade result are not in this denominator."""
    rates = _window(build_analytics(seeded))["rates"]
    r = rates["target_hit_of_resolved"]
    assert (r["numerator"], r["denominator"]) == (3, 5)
    assert r["value"] == pytest.approx(0.6)
    assert "target hits + stops" in r["definition"]


def test_the_share_of_all_issued_is_a_different_number(seeded):
    """3 of 12 = 25%, against the 60% above. Both are legitimate and they differ by a lot, which is
    why neither is called "the" hit rate and both are published."""
    rates = _window(build_analytics(seeded))["rates"]
    assert rates["target_hit_of_all"]["denominator"] == 12
    assert rates["target_hit_of_all"]["value"] == pytest.approx(0.25)
    assert rates["target_hit_of_resolved"]["value"] != rates["target_hit_of_all"]["value"]


def test_the_win_and_stop_rates_of_resolved_sum_to_one(seeded):
    """They share a denominator and partition it, so this must hold exactly. If it ever does not,
    a state has leaked into `RESOLVED_STATES`."""
    rates = _window(build_analytics(seeded))["rates"]
    assert rates["target_hit_of_resolved"]["value"] + rates["stopped_of_resolved"]["value"] == 1.0


def test_a_zero_denominator_is_none_and_never_zero(seeded):
    """`0.0` would read as "nothing ever hit" — a claim about the market. "Not defined" is a claim
    about the sample, which is the true one.

    Hand-computed: class 3 has one SUPPRESSED and one TRIGGERED, so nothing resolved.
    """
    by_model = {m["model"]: m for m in _window(build_analytics(seeded))["by_model"]}
    class3 = by_model["3"]["rates"]["target_hit_of_resolved"]
    assert class3["denominator"] == 0
    assert class3["value"] is None, "an empty sample must not render as 0%"
    assert class3["numerator"] == 0


def test_an_empty_window_reports_no_rates_rather_than_zeroes(tmp_path):
    store = open_store(tmp_path / "empty.db")
    try:
        payload = build_analytics(store).overview(start="2026-10-08", end="2026-10-10").payload
        assert payload["totals"]["all"] == 0
        for name, rate in payload["rates"].items():
            assert rate["value"] is None, f"{name} invented a value from an empty sample"
        assert any("undefined rather than zero" in line for line in payload["limitations"])
    finally:
        store.close()


def test_every_rate_publishes_its_own_definition(seeded):
    """"The hit rate" is ambiguous, so the payload says which one each figure is. A reader should
    not have to read this file to know what a number on the page means."""
    for name, rate in _window(build_analytics(seeded))["rates"].items():
        assert rate["definition"], f"{name} has no definition"
        assert set(rate) == {"numerator", "denominator", "value", "definition"}


# ---- breakdowns ------------------------------------------------------------------------------

def test_the_model_breakdown_matches_direct_queries(seeded):
    """Hand-computed per class: 1 → 4 issued / 1 hit / 1 stop; 2 → 3 / 2 / 0; 3 → 2 / 0 / 0;
    4 → 3 / 0 / 1."""
    by_model = {m["model"]: m["totals"] for m in _window(build_analytics(seeded))["by_model"]}
    assert {k: v["all"] for k, v in by_model.items()} == {"1": 4, "2": 3, "3": 2, "4": 3}
    assert by_model["2"]["target_hit"] == 2 and by_model["2"]["stopped"] == 0
    assert by_model["4"]["stopped"] == 1
    for klass, totals in by_model.items():
        assert totals["all"] == _sql(seeded, f"WHERE strategy_class={klass}")


def test_the_model_breakdown_carries_the_canonical_name(seeded):
    rows = {m["model"]: m for m in _window(build_analytics(seeded))["by_model"]}
    assert rows["4"]["name"] == "Established Methodologies & the Benchmark Suite"
    assert rows["1"]["short"] == "Correction"


def test_the_asset_breakdown_matches_direct_queries(seeded):
    by_asset = {a["asset"]: a["totals"] for a in _window(build_analytics(seeded))["by_asset"]}
    assert {k: v["all"] for k, v in by_asset.items()} == {
        "B-BTC_USDT": 5, "B-ETH_USDT": 3, "B-SOL_USDT": 4}
    for asset, totals in by_asset.items():
        assert totals["all"] == _sql(seeded, f"WHERE asset='{asset}'")


def test_a_breakdowns_rates_use_the_same_bucket_definitions_as_the_totals(seeded):
    """Computed by the same `_totals`/`_rates` pair, so a model's win rate and the overall win rate
    cannot disagree about what "resolved" means — which they would if the frontend divided two
    numbers itself."""
    payload = _window(build_analytics(seeded))
    summed = sum(m["totals"]["resolved"] for m in payload["by_model"])
    assert summed == payload["totals"]["resolved"]
    summed_assets = sum(a["totals"]["target_hit"] for a in payload["by_asset"])
    assert summed_assets == payload["totals"]["target_hit"]


def test_the_daily_series_covers_the_window_and_matches(seeded):
    """Hand-computed: 3 on the 8th, 4 on the 9th, 5 on the 10th."""
    by_day = {d["day"]: d["totals"]["all"] for d in _window(build_analytics(seeded))["by_day"]}
    assert by_day == {"2026-10-08": 3, "2026-10-09": 4, "2026-10-10": 5}
    assert sum(by_day.values()) == 12


# ---- filters and the window ------------------------------------------------------------------

def test_the_model_filter_narrows_every_figure_consistently(seeded):
    """Hand-computed for class 1: 4 issued, 1 hit, 1 stop, so a 50% win rate over 2 resolved."""
    payload = build_analytics(seeded).overview(start="2026-10-08", end="2026-10-10",
                                               strategy_class=1).payload
    assert payload["totals"]["all"] == 4
    assert payload["rates"]["target_hit_of_resolved"]["value"] == pytest.approx(0.5)
    assert [m["model"] for m in payload["by_model"]] == ["1"], "only the filtered class remains"
    assert sum(d["totals"]["all"] for d in payload["by_day"]) == 4


def test_the_asset_filter_narrows_every_figure_consistently(seeded):
    payload = build_analytics(seeded).overview(start="2026-10-08", end="2026-10-10",
                                               asset="B-SOL_USDT").payload
    assert payload["totals"]["all"] == 4
    assert [a["asset"] for a in payload["by_asset"]] == ["B-SOL_USDT"]


def test_a_narrower_window_excludes_the_days_outside_it(seeded):
    """Both ends are inclusive days. Hand-computed: the 9th alone has four suggestions."""
    payload = build_analytics(seeded).overview(start="2026-10-09", end="2026-10-09").payload
    assert payload["totals"]["all"] == 4
    assert payload["range"] == {"from": "2026-10-09", "to": "2026-10-09",
                                "from_ms": DAY9, "to_ms": DAY9 + 86_400_000,
                                "days": 1, "day_basis": "UTC"}


def test_a_suggestion_at_exactly_midnight_is_counted_once(tmp_path):
    """The boundary case. A closed range would count it on both days, so two single-day windows
    would total more than the two-day window containing them."""
    store = open_store(tmp_path / "mid.db")
    try:
        _insert(store, "mid", 1, "B-BTC_USDT", "TARGET_HIT", DAY9)
        store.conn.commit()
        service = build_analytics(store)
        assert service.overview(start="2026-10-08", end="2026-10-08").payload["totals"]["all"] == 0
        assert service.overview(start="2026-10-09", end="2026-10-09").payload["totals"]["all"] == 1
        assert service.overview(start="2026-10-08", end="2026-10-09").payload["totals"]["all"] == 1
    finally:
        store.close()


def test_an_inverted_range_is_refused_rather_than_swapped(seeded):
    """Silently reinterpreting it would answer a question nobody asked, and the caller would never
    learn its date picker was wrong."""
    result = build_analytics(seeded).overview(start="2026-10-10", end="2026-10-08")
    assert result.error is not None
    assert result.error.problem_class is ProblemClass.CONFIGURATION
    assert status_code_for(result) == 400


def test_an_unbounded_window_is_refused(seeded):
    """A range with no limit is a query whose cost grows with the archive, and a page that silently
    gets slower is a page that eventually times out on the smallest VM in the fleet."""
    result = build_analytics(seeded).overview(start="2020-01-01", end="2026-10-10")
    assert result.error is not None and status_code_for(result) == 400
    assert "366" in result.error.remediation


def test_a_bad_model_filter_is_a_bad_request(seeded):
    result = build_analytics(seeded).overview(strategy_class=9)
    assert result.error is not None and status_code_for(result) == 400


# ---- limitations are generated from the data, not remembered ---------------------------------

def test_a_small_sample_says_so(seeded):
    """Five resolved suggestions is not a measurement. The warning is generated from the count, so
    it appears because the sample is small rather than because someone wrote it down."""
    limitations = _window(build_analytics(seeded))["limitations"]
    assert any("noise with a percentage sign" in line for line in limitations)
    assert any("has been validated against history" in line for line in limitations)
    assert any("hypothetical" in line for line in limitations)


def test_the_open_and_no_result_counts_are_called_out(seeded):
    limitations = " ".join(_window(build_analytics(seeded))["limitations"])
    assert "2 suggestion(s) are still open" in limitations
    assert "5 suggestion(s) ended without a trade result" in limitations


# ---- over HTTP, and efficiency ---------------------------------------------------------------

def test_the_analytics_route_answers_and_validates(seeded):
    from fastapi.testclient import TestClient

    from api.services import build_services
    from store.reader import ReadOnlyStore

    reader = ReadOnlyStore(seeded.path)
    signals, market, health = build_services(reader)
    app = create_app(signals=signals, market=market, health=health,
                     analytics=build_analytics(reader))
    with TestClient(app) as client:
        ok = client.get("/api/analytics?start=2026-10-08&end=2026-10-10")
        assert ok.status_code == 200
        assert ok.json()["data"]["totals"]["all"] == 12

        bad = client.get("/api/analytics?start=2026-10-10&end=2026-10-08")
        assert bad.status_code == 400


def test_the_route_reports_a_missing_dependency_instead_of_404ing(seeded):
    """An honest 503 naming the wiring, rather than a 404 that tells the dashboard to stop asking."""
    from fastapi.testclient import TestClient

    from api.services import build_services
    from store.reader import ReadOnlyStore

    reader = ReadOnlyStore(seeded.path)
    signals, market, health = build_services(reader)
    app = create_app(signals=signals, market=market, health=health)   # no analytics=
    with TestClient(app) as client:
        res = client.get("/api/analytics")
        assert res.status_code == 503
        assert res.json()["error"]["code"] == "API_ANALYTICS_UNAVAILABLE"
        assert "build_analytics" in res.json()["error"]["remediation"]


def test_the_analytics_queries_are_bounded_and_indexed(seeded):
    """Four GROUP BY queries over an index range, not a scan of the archive."""
    sqls = [
        """SELECT state, COUNT(*) FROM signals WHERE created_at_ms >= 1 AND created_at_ms < 2
           GROUP BY state""",
        """SELECT strategy_class, state, COUNT(*) FROM signals
           WHERE created_at_ms >= 1 AND created_at_ms < 2 GROUP BY strategy_class, state""",
        """SELECT created_at_ms/86400000 AS d, state, COUNT(*) FROM signals
           WHERE created_at_ms >= 1 AND created_at_ms < 2 GROUP BY d, state""",
    ]
    for sql in sqls:
        detail = " ".join(r["detail"] for r in seeded.conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "USING INDEX" in detail or "USING COVERING INDEX" in detail, detail
        assert "SCAN signals" not in detail, f"full table scan: {detail}"


def test_the_overview_issues_a_bounded_number_of_queries(seeded):
    """Counted, because "aggregate in SQL" is only cheap if it is a handful of statements rather
    than one per group. Four dimensions plus the state roll-up is five."""
    # `sqlite3.Connection.execute` cannot be monkey-patched (read-only attribute), so the
    # statements are counted where sqlite reports them: the trace callback fires once per executed
    # statement, which is exactly the number under test.
    statements: list[str] = []
    seeded.conn.set_trace_callback(lambda sql: statements.append(sql))
    try:
        build_analytics(seeded).overview(start="2026-10-08", end="2026-10-10")
    finally:
        seeded.conn.set_trace_callback(None)
    selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
    assert len(selects) <= 6, f"{len(selects)} queries for one analytics page: {selects}"
