"""The Vercel dashboard duplicates `src/api/render.py`'s display rules. These tests check the copies.

There are two dashboards: the server-rendered one the engine serves on the VM, and the Next.js one
Vercel hosts. They apply the **same** specs/15 honesty rules, which means the rules exist twice —
exactly the kind of duplication this project refuses for ACT, for the trail and for the cost model.

It is accepted for the frontend because the alternative is worse (shipping HTML strings across a
network boundary into `dangerouslySetInnerHTML`, which cannot be type-checked at all), but accepted
duplication still has to be *checked*. Node is not installed in this environment, so the TypeScript
cannot be executed here; instead these tests read the TypeScript as text and assert that its
constants and thresholds match the Python the engine actually uses.

That makes the check run in the suite that runs on every change, rather than in a toolchain that may
not be present. If the two ever disagree, one of these fails and names which file to fix.
"""
import re
from pathlib import Path

import pytest

from api import render

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
HONESTY_TS = FRONTEND / "lib" / "honesty.ts"
GLOBALS_CSS = FRONTEND / "app" / "globals.css"
PROXY_TS = FRONTEND / "app" / "api" / "proxy" / "[...path]" / "route.ts"


def _require(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"{path.relative_to(FRONTEND.parent)} is missing; the Vercel dashboard needs it")
    return path.read_text(encoding="utf-8")


def _without_comments(source: str) -> str:
    """Strip JS/TS comments, so a rule can be asserted against *code* rather than against prose.

    Needed because the files deliberately explain the hazards they avoid — ``route.ts`` says in a
    comment why a ``NEXT_PUBLIC_`` variable would be wrong — and a naive substring check would read
    the explanation as the offence. The first version of these tests did exactly that.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?<!:)//.*", "", source)


def _number(source: str, name: str) -> float:
    match = re.search(rf"export const {name}\s*=\s*([0-9._]+)\s*;", source)
    if match is None:
        pytest.fail(f"{name} is not exported from honesty.ts; the Python side relies on it existing")
    return float(match.group(1))


# ---- the thresholds must agree --------------------------------------------------------------

def test_the_age_thresholds_are_the_same_in_both_dashboards():
    """specs/15 §9: amber past 60 s, red past 300 s.

    Asserted against ``render.py:age_class``'s actual behaviour rather than against a literal, so
    this test follows the Python if the Python changes — and then fails, which is the point.
    """
    source = _require(HONESTY_TS)
    amber = _number(source, "AGE_AMBER_S")
    red = _number(source, "AGE_RED_S")

    # Derive the Python thresholds by probing the function. If someone edits render.py, these move
    # and the comparison below catches the frontend being left behind.
    assert render.age_class(amber) == "age-ok"
    assert render.age_class(amber + 1) == "age-amber"
    assert render.age_class(red) == "age-amber"
    assert render.age_class(red + 1) == "age-red"


def test_an_unknown_age_is_its_own_state_in_both():
    """The dangerous default: if an absent age rendered as fresh, a dashboard with a broken clock
    would look perfectly healthy."""
    assert render.age_class(None) == "age-unknown"
    assert render.age_text(None) == "age unknown"
    source = _require(HONESTY_TS)
    assert "'unknown'" in source and "'age unknown'" in source


def test_age_is_elapsed_time_in_both_and_never_a_timestamp():
    """"4s ago" is actionable; "14:32:07" makes the reader do arithmetic."""
    for seconds in (4, 90, 7200, 123456):
        text = render.age_text(seconds)
        assert "ago" in text
        assert not re.search(r"\d:\d", text), f"{seconds} rendered as a clock time: {text}"
    source = _require(HONESTY_TS)
    assert "ago" in source
    assert "toFixed(1)}h ago" in source, "the hour branch must match render.py's one decimal place"


def test_the_colour_tokens_are_identical_so_a_colour_cannot_mean_two_things():
    """A colour that means "short" on one dashboard and "stale" on the other is worse than no
    colour at all, because the reader has learned the wrong thing on one of them."""
    css = _require(GLOBALS_CSS)
    for name, value in render.TOKENS.items():
        assert f"{name}: {value};" in css, (
            f"{name} is {value} in render.py but not in frontend/app/globals.css"
        )


# ---- the firewall must exist on the frontend too --------------------------------------------

def test_the_frontend_refuses_to_display_a_control_strategy():
    """The third firewall layer. The engine filters controls out of every response and the controls
    refuse to run outside a backtest; this one is written as if neither did, because a control
    signal reaching a person would be indistinguishable from a real suggestion."""
    source = _require(HONESTY_TS)
    assert "export function displayable" in source
    assert "detail.control" in source, "a signal flagged as a control must be refused"
    assert "tradeable === false" in source, "a non-tradeable signal must be refused"


def test_the_frontend_never_claims_a_confidence_value():
    """``confidence`` is null system-wide until Phase 16 (specs/09 §14), and specs/15 failure mode 2
    says a blank reads as a bug — so the absence is explained, in copy."""
    source = _require(HONESTY_TS)
    assert "breakeven_hit_rate" in source
    assert "no confidence score is published" in source
    # And nothing may invent one.
    assert not re.search(r"confidence\s*[:=]\s*[0-9]", source)


def test_the_frontend_distinguishes_cannot_tell_from_nothing_to_show():
    """specs/14 §10 names rendering an unreachable engine as an empty market the most dangerous
    thing this API can do."""
    source = _require(HONESTY_TS)
    assert "'down'" in source and "'empty'" in source
    assert "'warming'" in source, "warm-up is a third state, not a failure"
    panels = _require(FRONTEND / "components" / "Panels.tsx")
    assert "Cannot tell" in panels
    assert "not &ldquo;no opportunities&rdquo;" in panels or "not \u201cno opportunities\u201d" in panels


# ---- the proxy is what keeps the token off the browser --------------------------------------

def test_the_shared_token_is_never_exposed_to_the_browser():
    """A ``NEXT_PUBLIC_`` variable is compiled into the JavaScript the browser downloads.

    The token is read only inside the proxy route, which runs on Vercel's server. If it ever gained
    that prefix, anyone who opened the page could read the credential that reaches the engine.
    """
    proxy = _require(PROXY_TS)
    assert "process.env.CT_API_TOKEN" in proxy
    for path in (PROXY_TS, FRONTEND / "lib" / "api.ts", FRONTEND / "app" / "page.tsx",
                 FRONTEND / "components" / "Panels.tsx"):
        code = _without_comments(_require(path))
        assert "NEXT_PUBLIC_" not in code, (
            f"{path.name} reads a NEXT_PUBLIC_ variable, which is compiled into the JavaScript the "
            "browser downloads; the engine token must never be one"
        )
    # The template must not suggest one either: a name copied from an example is a name nobody
    # re-reads.
    env_lines = [ln for ln in _require(FRONTEND / ".env.example").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
    assert not any("NEXT_PUBLIC_" in ln for ln in env_lines)


def test_the_proxy_forwards_only_get_and_only_an_allow_list():
    """An allow-list rather than a pattern: ``api/**`` would quietly expose whatever gets added to
    the engine later, and the person adding it would have no reason to look at this file."""
    proxy = _require(PROXY_TS)
    assert "export async function GET" in proxy
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert f"export async function {verb}" not in proxy, (
            f"the proxy exports {verb}; the engine is read-only and the proxy must be too"
        )
    assert "const ALLOWED = new Set([" in proxy
    assert "function permitted" in proxy


def test_every_route_the_frontend_asks_for_exists_in_the_engine():
    """A path on the allow-list that the engine does not serve is a panel that will always be DOWN,
    and it would look like an outage rather than a typo."""
    proxy = _require(PROXY_TS)
    allowed = re.findall(r"'(api/[a-z0-9/_-]+)'", proxy)
    app_source = (Path(__file__).resolve().parents[1] / "src" / "api" / "app.py").read_text(
        encoding="utf-8")
    routes = set(re.findall(r'@app\.get\("(/api/[^"]*)"', app_source))
    declared = {r.strip("/") for r in routes}
    for path in allowed:
        assert path in declared, f"the proxy allows {path}, which the engine does not serve"


def test_the_dashboard_states_that_the_system_never_trades():
    """Stated on every load rather than in an about page: someone reading under time pressure
    should not have to remember what the system is permitted to do."""
    page = _require(FRONTEND / "app" / "page.tsx")
    assert "never places" in page
    assert "Read-only" in page


def test_nothing_in_the_frontend_is_cached_at_the_edge():
    """A cached panel is a stale panel presented as current, which is the one thing specs/15 forbids
    outright. Vercel caches aggressively by default, so this has to be switched off deliberately."""
    config = _require(FRONTEND / "next.config.mjs")
    assert "no-store" in config
    proxy = _require(PROXY_TS)
    assert "'no-store'" in proxy
    assert "force-dynamic" in proxy


# ---- the payload shape the frontend reads must be the shape the engine sends ----------------
#
# This section exists because of a real defect that these tests did not catch: `page.tsx` read the
# signal list as `payload.data` when the engine puts it at `payload.data.signals`. The result was an
# object where an array was expected, so `data.length === 0` was always true and **every suggestion
# was silently dropped** — the dashboard said "no suggestions right now" no matter what the engine
# had found. Node is not installed here, so TypeScript cannot be compiled or executed; the accessor
# is therefore asserted as text, which is weak but not nothing, and is the difference between a
# mismatch being caught in the suite and being caught by a person wondering why nothing ever shows.

def test_the_dashboard_reads_the_signal_list_where_the_engine_actually_puts_it():
    """``services.py:live`` returns ``{"signals": [...], "count": n, ...}`` as the payload, which
    ``ServiceResult.to_dict`` nests under ``data``. So the list is at ``data.signals``."""
    import inspect

    from api.services import SignalService

    source = inspect.getsource(SignalService.live)
    assert '"signals": signals' in source, (
        "services.py no longer returns the list under a `signals` key; the frontend accessor in "
        "app/page.tsx has to change with it"
    )

    page = _without_comments(_require(FRONTEND / "app" / "page.tsx"))
    assert "?.signals" in page or ".signals" in page, (
        "app/page.tsx must read the signal list from `data.signals`; reading `data` itself yields "
        "an object where an array is expected, and every suggestion is silently dropped"
    )
    assert "Array.isArray" in page, (
        "the accessor must be guarded: a shape mismatch has to render as a problem, not as a calm "
        "market"
    )


def test_the_health_panel_reads_components_where_the_engine_puts_them():
    import inspect

    from core.health import HealthReport

    assert '"components"' in inspect.getsource(HealthReport.to_dict)
    panels = _without_comments(_require(FRONTEND / "components" / "Panels.tsx"))
    assert "payload.components" in panels


def test_an_unreachable_engine_does_not_read_as_an_empty_health_list():
    """The first deployment showed "The engine did not report any component health" for an engine
    that could not be reached at all — the same sentence it shows for an engine that answered with
    nothing. Three different nothings, one message, and the operator cannot tell which (F-047)."""
    panels = _require(FRONTEND / "components" / "Panels.tsx")
    assert "no health reading arrived" in panels, "a missing payload needs its own message"
    assert "not the expected shape" in panels, "a wrong shape needs its own message"
    assert "empty health list" in panels, "an empty list needs its own message"

    page = _require(FRONTEND / "app" / "page.tsx")
    assert "HealthSection" in page, (
        "the health reading must be classified before it is rendered, so an unreachable engine "
        "reads as unreachable"
    )


def test_a_reply_that_is_not_json_is_a_transport_failure_not_an_empty_payload():
    """A gateway error page is HTML. Parsed as JSON it yields null, and a null payload reads
    downstream as "the engine said nothing" — indistinguishable from a calm market."""
    api_ts = _require(FRONTEND / "lib" / "api.ts")
    assert "could not be read as JSON" in api_ts
    assert "transportError" in api_ts


# ---- the chart page -------------------------------------------------------------------------

def test_the_chart_page_exists_and_is_reachable_from_the_nav():
    """The chart is the only view where wrong data is obvious at a glance; every other panel reports
    a conclusion that looks identical whether the prices behind it are current, stale or absent."""
    chart = _require(FRONTEND / "app" / "chart" / "page.tsx")
    nav = _require(FRONTEND / "components" / "Nav.tsx")
    assert "'/chart'" in nav, "the chart page must be linked from the nav or nobody will find it"
    layout = _require(FRONTEND / "app" / "layout.tsx")
    assert "<Nav />" in layout, "the nav must be mounted, not merely written"
    for asset in ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"):
        assert asset in chart, f"{asset} must be selectable"


def test_every_interval_the_chart_offers_is_one_the_engine_will_serve():
    """An interval the engine rejects would render as a permanent error on a button the user is
    invited to press, which reads as a broken system rather than as a button that should not exist.
    """
    import inspect
    import re as _re

    from api.services import MarketService

    allowed = set(_re.findall(r'"(\d+[mhd])"', inspect.getsource(MarketService.candles)))
    assert allowed, "the allowed set could not be read out of services.py"

    chart = _require(FRONTEND / "app" / "chart" / "page.tsx")
    offered = set(_re.findall(r"id: '(\d+[mhd])'", chart))
    assert offered, "no intervals were found in the chart page"
    assert offered <= allowed, f"the chart offers intervals the engine refuses: {offered - allowed}"


def test_the_chart_agrees_with_the_engine_about_how_long_an_interval_is():
    """The chart converts a bar's open into its close to measure age, and positions candles by time
    so a gap stays a gap. Both need the interval length, and a disagreement would mis-state
    freshness on the one page a person uses to check freshness."""
    import re as _re

    from api.services import _TIMEFRAME_MS

    chart = _require(FRONTEND / "app" / "chart" / "page.tsx")
    found = dict(_re.findall(r"id: '(\d+[mhd])', label: '[^']*', ms: ([0-9_]+)", chart))
    assert found, "the chart's interval table could not be read"
    for name, ms in found.items():
        assert int(ms.replace("_", "")) == _TIMEFRAME_MS[name], (
            f"the chart thinks {name} is {ms} ms; the engine says {_TIMEFRAME_MS[name]}"
        )


def test_the_chart_distinguishes_a_confirmed_bar_from_an_unconfirmed_one():
    """A bar fetched once is not confirmed, and the feature layer will not compute on it. Drawing it
    identically to a settled bar would show a price the engine is deliberately ignoring."""
    candle = _require(FRONTEND / "components" / "CandleChart.tsx")
    assert "d.final" in candle
    assert "strokeDasharray" in candle, "the difference must be visible, not only in a tooltip"


def test_the_chart_positions_candles_by_time_so_an_outage_stays_visible():
    """Index positioning would close the gap and make a missing hour look like a quiet market —
    which is the exact failure this page exists to catch."""
    candle = _without_comments(_require(FRONTEND / "components" / "CandleChart.tsx"))
    assert "(t - tMin)" in candle, "the x scale must be a function of time, not of array index"


def test_the_chart_separates_an_unreachable_engine_from_an_empty_database():
    chart = _require(FRONTEND / "app" / "chart" / "page.tsx")
    assert "transportError" in chart
    assert "DASHBOARD_UNEXPECTED_SHAPE" in chart, "a 200 of the wrong shape is an application bug"
    assert "No bars stored for this interval yet" in chart
    assert "Showing the last data that arrived" in chart, (
        "a failed refresh must keep the previous candles and mark them, not blank the page"
    )


def test_measuring_the_charts_width_does_not_run_during_render():
    """`useMemo` runs in the render phase, where calling a setter is an update-while-rendering error
    and the returned cleanup is discarded — so the resize listener would accumulate one per render.
    """
    candle = _require(FRONTEND / "components" / "CandleChart.tsx")
    assert "useEffect(() => {\n    const measure" in candle, (
        "the width measurement must be in useEffect, not useMemo"
    )


# ---- a reply that is not the engine's own must not read as a calm market (F-050) ------------

def test_every_engine_response_carries_an_envelope_so_its_absence_is_meaningful():
    """The dashboard now treats a missing envelope as "this did not come from the engine". That is
    only sound if the engine really does attach one to everything, so it is asserted here rather
    than assumed."""
    import inspect

    from api.services import ServiceResult

    assert '"envelope"' in inspect.getsource(ServiceResult.to_dict), (
        "ServiceResult no longer puts the envelope at a fixed key; lib/honesty.ts uses its absence "
        "to detect a reply that did not come from the engine"
    )


def test_a_json_object_with_no_envelope_is_down_not_ready():
    """The hole that produced the first deployment's most misleading screen.

    ``CT_API_BASE`` set to ``http://IP:8000/api`` makes the proxy request ``/api/api/signals/live``,
    which FastAPI answers ``404 {"detail": "Not Found"}``. With no ``error`` key and no
    ``system_status``, the old ``panelState`` returned ``ready``; the age was unknown because there
    was no envelope, and the signal list was absent so it defaulted to empty. The dashboard then
    said "market data age unknown" and "No suggestions right now" — in calm language, about an
    engine it was never talking to.
    """
    honesty = _require(HONESTY_TS)
    assert "p.envelope === undefined" in honesty, (
        "panelState must treat a missing envelope as DOWN; without that check a 404 body renders as "
        "a healthy quiet market"
    )
    assert "did not come from the engine" in honesty
    # And the remediation must name the actual mistake, not just the symptom.
    assert "CT_API_BASE" in honesty and "no path" in honesty


def test_the_proxy_wraps_an_upstream_error_that_is_not_the_engines_own():
    """Belt and braces, on purpose: the dashboard check above is the one that matters, but the proxy
    is where the URL that caused the mistake is in scope, so it can say which path 404'd."""
    proxy = _require(PROXY_TS)
    assert "PROXY_UPSTREAM_NOT_OURS" in proxy
    assert "upstream.ok" in proxy
    assert "upstream_body" in proxy, (
        "the body is the only evidence of what actually answered; hiding it sends the reader to the "
        "logs for one line"
    )
    # A real engine error must still pass through untouched, or its own cause and remediation are
    # replaced by a guess.
    assert 'body.includes(\'"error"\')' in proxy

def test_the_node_mirror_of_panel_state_carries_the_envelope_check_too():
    """``lib/honesty.test.mjs`` re-implements these rules a **third** time, because node:test cannot
    import TypeScript. A third copy that silently falls behind is worse than no copy: it would pass
    while asserting the behaviour that was wrong."""
    mjs = _require(FRONTEND / "lib" / "honesty.test.mjs")
    assert "payload.envelope === undefined" in mjs, (
        "the node mirror still returns `ready` for a reply with no envelope; it would pass while "
        "asserting the F-050 behaviour"
    )
    assert "Not Found" in mjs, "the 404 body that caused it should be the test case"


# ---- the card must not invent a price or a direction (F-051, F-053) -------------------------

def test_a_signal_whose_levels_are_not_prices_is_rendered_as_a_spread_not_an_entry():
    """C1-C's levels are a beta-adjusted spread: small, and negative for about half of all LONG
    setups. The card rendered `entry -0.04145262148598610125` under a heading marked *entry*, on a
    card otherwise identical to a tradeable BTC suggestion — a price that exists on no exchange."""
    import inspect

    from strategy.evaluation import PRICE_BASIS, RESIDUAL_BASIS, build_signal

    # The engine must publish the basis, or the frontend would have to know about C1-C by name.
    assert '"price_basis": priced.price_basis' in inspect.getsource(build_signal)
    assert RESIDUAL_BASIS != PRICE_BASIS

    # The decision lives in `honesty.ts` (one place, with the older-engine fallbacks); the card
    # calls it. Asserted that way round so the refactor cannot quietly move the rule into a
    # component where the server dashboard's copy of it can no longer be compared.
    honesty = _without_comments(_require(HONESTY_TS))
    assert "export function priceBasis" in honesty
    assert "price_basis" in honesty

    panels = _without_comments(_require(FRONTEND / "components" / "Panels.tsx"))
    assert "priceBasis(signal)" in panels
    assert "not a price" in panels, "the reader must be told these are not prices"
    assert "detail.legs" in panels or "legs" in panels, (
        "a two-leg trade must show both legs; the engine's own execution_note says doing one leg "
        "without the other is a different trade with a different risk"
    )
    # And the levels row must be *suppressed*, not merely annotated: a number under the word entry
    # is a price to anyone skimming, whatever the caveat underneath says.
    assert "isPrice && !pendingDirection" in panels


def test_a_pending_direction_setup_is_never_shown_as_a_long():
    """C3-A and C3-C arm *without* a direction and let the market assign it. The engine sets
    `direction` to LONG as a structural placeholder and flags `pending_direction` in the payload.

    The card printed that placeholder in green next to "LONG", so a setup whose entire premise is
    "we do not predict which way this breaks" was displayed as a long suggestion — and counted as
    one in the exposure strip.
    """
    import inspect

    from strategy.c3_transition import C3C

    assert '"pending_direction": True' in inspect.getsource(C3C), (
        "C3-C must flag that its direction is a placeholder, or no consumer can know"
    )

    panels = _require(FRONTEND / "components" / "Panels.tsx")
    assert "pending_direction" in panels
    assert "DIRECTION NOT YET DECIDED" in panels
    assert "whichever way it resolves" in panels


def test_the_exposure_strip_counts_only_genuinely_directional_signals():
    """The concentration warning was wrong in both directions at once.

    It read "long 5, short 4" over a set that included pending-direction setups — which have no
    direction — and beta-neutral spreads, which are *market-neutral by construction* and are the
    opposite of concentrated directional risk. So the warning was inflated, and inflated by exactly
    the signals that add no directional exposure.
    """
    panels = _without_comments(_require(FRONTEND / "components" / "Panels.tsx"))
    assert "const directional" in panels
    assert "!isPending(s) && !isSpread(s)" in panels, (
        "longs and shorts must be counted from directional signals only"
    )
    assert "concentrationNotice(longs, shorts)" in panels
    # The other two kinds are still reported, because they are things to know, not things to hide.
    assert "two-leg spreads" in panels and "awaiting direction" in panels


def test_the_action_bar_never_tells_someone_to_enter_at_an_undecided_level():
    """"ENTER NOW — BTC LONG at —" is an instruction nobody can follow, assembled from a placeholder
    direction and a level that does not exist yet."""
    panels = _without_comments(_require(FRONTEND / "components" / "Panels.tsx"))
    assert "ENTER_NOW' && !isPending(s)" in panels


# ---- freshness: the right number against the right yardstick (F-052) -----------------------

def test_the_market_data_age_comes_from_the_bar_feed_not_from_the_newest_signal():
    """A setup at an hourly horizon legitimately stays armed for hours. Judged against a 300-second
    red threshold it reported "too old to act on" permanently, and a warning that is always on is a
    warning nobody reads."""
    import inspect

    from api.services import SignalService

    assert '"market_data"' in inspect.getsource(SignalService.live)
    page = _without_comments(_require(FRONTEND / "app" / "page.tsx"))
    assert "market_data?.age_s" in page, (
        "the market-data age must come from the engine's market_data block, not the envelope"
    )
    assert "newest suggestion" in page, "the signal's own age is still shown, labelled honestly"


# ---- the data page --------------------------------------------------------------------------

def test_the_data_page_exists_and_is_reachable_from_the_nav():
    data = _require(FRONTEND / "app" / "data" / "page.tsx")
    nav = _require(FRONTEND / "components" / "Nav.tsx")
    assert "'/data'" in nav, "the data page must be linked or nobody will find it"
    assert "api.sources" in data


def test_the_data_page_is_on_the_proxy_allow_list():
    """An allow-listed path the proxy forbids would render as a permanent outage on a page whose
    whole job is telling an outage from a quiet market."""
    proxy = _require(PROXY_TS)
    assert "'api/sources'" in proxy


def test_the_data_page_says_which_sources_can_be_rebuilt_and_which_cannot():
    """The single most important fact about a Tier B gap is that it is **permanent** — there is no
    archive endpoint for yesterday's order book, for us or for anyone with any API key. A page that
    showed a gap without saying so would invite someone to wait for it to fill."""
    data = _require(FRONTEND / "app" / "data" / "page.tsx")
    assert "gaps are permanent" in data
    assert "rebuildable" in data
    assert "no archive" in data


def test_the_data_page_distinguishes_not_collected_from_nothing_arrived():
    """`trades` has a table and an adapter but no poll task, so an empty table is the system working
    as built. A bare zero would read as a broken poller."""
    data = _require(FRONTEND / "app" / "data" / "page.tsx")
    assert "not collected by this system" in data
    assert "collected === false" in data


def test_the_data_page_shows_data_quality_next_to_freshness():
    """A source can be current and wrong at the same time: if rows are being rejected the timestamps
    stay fresh and say nothing. The two readings have to be on one screen or the second never gets
    looked at."""
    data = _require(FRONTEND / "app" / "data" / "page.tsx")
    assert "data_quality" in data
    assert "ROWS_REJECTED" in data and "BAR_MUTATION" in data


def test_the_data_page_separates_an_unreachable_engine_from_an_empty_database():
    data = _require(FRONTEND / "app" / "data" / "page.tsx")
    assert "transportError" in data
    assert "No price series are stored at all" in data
    assert "not a connection problem" in data


# ---- the 404 diagnosis names version skew first (F-055) ------------------------------------

def test_a_404_on_one_path_blames_the_engine_version_not_the_base_url():
    """The first version of this message named the wrong cause first and cost a round trip: it
    blamed `CT_API_BASE` on an installation where that variable was correct, and the real problem was
    an engine three commits behind that had never heard of the route.

    The two are trivially distinguishable, so the message must say how: a wrong base URL breaks
    **every** path, an out-of-date engine breaks only the **new** ones.
    """
    proxy = _require(PROXY_TS)
    assert "older version" in proxy
    assert "would break every page, not one" in proxy
    assert "update.sh" in proxy, "the remediation must be the command that actually fixes it"
    # The base-URL cause is still mentioned, but second, and conditioned on every page failing.
    assert "Only if EVERY page is broken" in proxy


def test_the_signal_detail_accessor_matches_where_the_engine_puts_it():
    """The third accessor bug of the same family, and the quietest.

    `detail` carries `price_basis`, `pending_direction`, `validated`, `legs` and
    `breakeven_hit_rate` — the fields every display rule branches on. The engine nested it inside
    `payload`; the frontend read `signal.detail`; so **every rule in honesty.ts was reading
    `undefined`**. The control-strategy firewall here never fired, and `validationNotice` gave the
    right answer for the wrong reason (`undefined !== true`) — it would have started lying the
    moment anything was validated (F-056).

    The engine lifts it now. The fallback stays because the VM and Vercel are deployed separately.
    """
    import inspect

    from api.services import SignalService

    assert 'out["detail"] = payload.get("detail")' in inspect.getsource(
        SignalService._row_to_signal), "the engine must lift detail to the top level"

    honesty = _without_comments(_require(HONESTY_TS))
    assert "export function signalDetail" in honesty, (
        "one accessor, or the next rule added here reads the wrong place again"
    )
    assert "signal?.payload?.detail" in honesty, (
        "the fallback must stay: the dashboard has to read an engine a few commits behind"
    )
    # No rule may reach for `detail` directly any more.
    assert "signal?.detail?." not in honesty, "every detail read must go through signalDetail"


# ---- nothing the system needs may be un-committable (F-057) ---------------------------------

def test_no_source_file_the_project_needs_is_gitignored():
    """A file that cannot be committed is a file that will never reach the VM or Vercel, and
    **nothing reports it** — an ignored file is not an error, so `git add` succeeds, the commit
    succeeds, the push succeeds, and the deployment is quietly missing a page.

    That happened: `.gitignore` carried a bare `data/` to protect the validation holdout, which
    matches a directory named `data` at *any* depth. It therefore excluded
    `frontend/app/data/page.tsx`. The commit meant to add the Data page added everything except the
    Data page, and Vercel served a 404 for a route whose source had never left the laptop.

    The rule is anchored to the repository root now. This test is the guard, because the next person
    to add a route called `data`, `logs`, `build` or `dist` will not be thinking about `.gitignore`.
    """
    import subprocess

    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("not a git checkout")

    # Everything that has to be deployable: the engine, its tests, and the dashboard's own source.
    candidates: list[Path] = []
    for pattern in ("src/**/*.py", "tests/**/*.py", "frontend/app/**/*", "frontend/components/**/*",
                    "frontend/lib/**/*"):
        candidates += [p for p in root.glob(pattern) if p.is_file()]
    assert candidates, "the glob found nothing; this test would pass vacuously"

    rel = [str(p.relative_to(root)).replace("\\", "/") for p in candidates]
    # `git check-ignore` prints the paths it *would* ignore and exits 1 when there are none.
    result = subprocess.run(["git", "check-ignore", "--no-index", *rel],
                            cwd=root, capture_output=True, text=True, check=False)
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, (
        "these files cannot be committed, so they will never reach the VM or Vercel:\n  "
        + "\n  ".join(ignored)
        + "\nAnchor the offending .gitignore rule to the repository root (/data/, not data/)."
    )


def test_the_validation_holdout_is_still_ignored():
    """The counterpart. Anchoring the rule must not have opened the one directory it exists for:
    `data/` holds the holdout, and if that reaches a shared repository it can be inspected before
    Stage 10 and the validation is worthless (specs/09 §1.2)."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("not a git checkout")

    for path in ("data/state.db", "data/raw/anything.jsonl", "data/backfill/x.parquet",
                 "logs/app.log", ".env"):
        result = subprocess.run(["git", "check-ignore", "--no-index", path],
                                cwd=root, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"{path} is NOT ignored and it must be"


def test_the_diagnose_script_checks_the_engines_routes_and_its_commit():
    """The check that was missing, and it cost two rounds.

    Every other section of `diagnose.sh` passed on an installation where the chart page was broken,
    because every other section asks "is the engine working" and none asked "is the engine the
    version the dashboard expects". The VM and Vercel deploy separately, so they drift by default,
    and the symptom is one page 404ing while the rest looks perfect (F-059).
    """
    script = (FRONTEND.parent / "deploy" / "azure" / "diagnose.sh").read_text(encoding="utf-8")
    assert "/api/candles/B-BTC_USDT" in script and "/api/sources" in script, (
        "the newest routes are the ones an old engine will be missing, so they are the ones to probe"
    )
    assert "OLDER than the dashboard" in script
    assert "it was never" in script or "never reached" in script, (
        "the message must name the common cause: the code was never pushed"
    )
    assert "4c. Deployed version" in script, "the running commit must be printed"


def test_update_sh_does_not_let_already_up_to_date_read_as_success():
    """"already up to date" is read as success and has twice meant the opposite: nothing had been
    pushed, so there was nothing to pull and the VM stayed old."""
    script = (FRONTEND.parent / "deploy" / "azure" / "update.sh").read_text(encoding="utf-8")
    assert "nothing new has been PUSHED" in script
    assert "git push" in script
    # And it verifies the routes after restarting, while someone is still watching.
    assert "routes the dashboard needs" in script


def test_the_vercel_health_panel_separates_findings_from_faults():
    """Both dashboards, same rule. The Vercel panel counted the two permanent regime findings in
    "2 not healthy", which is where the reported screen came from (F-060)."""
    panels = _without_comments(_require(FRONTEND / "components" / "Panels.tsx"))
    assert "'modelling'" in panels, "the panel must know the modelling problem class by name"
    assert "const operational" in panels and "const faults" in panels
    assert "operational.length - faults.length" in panels, (
        "the healthy count must be over operational components, not over everything"
    )
    # And the finding is still shown, prominently, in its own panel.
    source = _require(FRONTEND / "components" / "Panels.tsx")
    assert "Running with reduced selectivity" in source
    assert "not a fault and there is nothing to fix" in source
    assert "failed_tests" in source, "the specific failing test is the actionable detail"


def test_the_modelling_class_name_matches_the_engines_enum():
    """A string literal in TypeScript against a Python enum. If the enum value changes, the panel
    silently stops recognising findings and they become faults again."""
    from core.errors import ProblemClass

    panels = _require(FRONTEND / "components" / "Panels.tsx")
    assert f"'{ProblemClass.MODELLING.value}'" in panels


# ---- Phase 2: model filters, timestamps, live vs history ------------------------------------

def test_the_dashboards_model_filter_uses_the_persisted_class_not_display_text():
    """The classification is already `signals.strategy_class`. Deriving a model from a strategy id
    or a label would be a second derivation to keep in step with the first, and the task is explicit:
    never infer the model when an authoritative identifier exists."""
    page = _without_comments(_require(FRONTEND / "app" / "page.tsx"))
    api_ts = _without_comments(_require(FRONTEND / "lib" / "api.ts"))
    assert "liveSignalsFiltered" in page and "strategy_class" in api_ts
    assert "strategy_class" not in page or "signal.strategy_class" in _require(
        FRONTEND / "components" / "Suggestions.tsx"), (
        "the class is read from the row, never parsed out of a strategy id"
    )
    suggestions = _require(FRONTEND / "components" / "Suggestions.tsx")
    assert "m.id === String(signal.strategy_class)" in suggestions


def test_the_model_labels_come_from_the_engine():
    """`/api/signals/live` publishes `models`, built from `STRATEGY_CLASSES`, which mirrors the
    canonical spec titles. A hard-coded list in the frontend would be a fifth place to edit."""
    from api.services import STRATEGY_CLASSES

    suggestions = _require(FRONTEND / "components" / "Suggestions.tsx")
    assert "payload?.models" in suggestions, "the engine's map is preferred"
    # The fallback exists only for an older engine, and must agree with the engine where it is used.
    for meta in STRATEGY_CLASSES.values():
        assert meta["short"] in suggestions, f"{meta['short']} missing from the fallback labels"


def test_all_four_models_are_offered_plus_an_explicit_all():
    """An absent chip meaning "all" is a filter nobody can find. Four models and an All."""
    from api.services import STRATEGY_CLASSES

    suggestions = _require(FRONTEND / "components" / "Suggestions.tsx")
    assert len(STRATEGY_CLASSES) == 4
    assert "onChange('')" in suggestions and ">\n          All\n        </button>" in suggestions \
        or "All" in suggestions


def test_the_dashboard_shows_the_suggestion_issue_time_in_utc():
    """The day grouping is UTC, so the timestamp must be too: a suggestion issued at 23:30 UTC would
    otherwise sit under one date in its heading and another in its own stamp, for every reader east
    of Greenwich — and "look at the 10th" would mean different sets to two people."""
    suggestions = _require(FRONTEND / "components" / "Suggestions.tsx")
    assert "export function utcStamp" in suggestions
    assert "getUTCFullYear" in suggestions and "getUTCHours" in suggestions
    assert "UTC" in suggestions, "the label must say which zone, or the number is ambiguous"
    assert "created_at_ms" in suggestions, "the issue time is what is displayed"


def test_the_dashboard_distinguishes_the_issue_time_from_the_outcome_time():
    """Showing one timestamp would make a suggestion issued on Tuesday whose stop was hit on
    Thursday look like a Thursday suggestion."""
    suggestions = _require(FRONTEND / "components" / "Suggestions.tsx")
    assert "state_since_ms" in suggestions
    assert "outcome recorded" in suggestions
    assert ">issued<" in suggestions or "issued" in suggestions


def test_history_is_paged_and_never_fetches_the_whole_table():
    """The task's efficiency constraint, asserted where it would be violated. A browser that fetches
    every suggestion to group them locally grows without bound and moves the date boundary into the
    viewer's timezone."""
    page = _without_comments(_require(FRONTEND / "app" / "page.tsx"))
    assert "HISTORY_PAGE" in page
    assert "api.signalHistory" in page and "api.signalDays" in page
    # The day index is an aggregate from the engine, not a client-side group-by.
    assert "reduce(" not in page, "aggregation belongs in SQL, not in the browser"


def test_the_live_poll_stops_when_the_history_view_is_open():
    """Polling a view nobody is looking at spends the proxy's budget and the VM's cycles to refresh
    a panel that is off screen."""
    page = _require(FRONTEND / "app" / "page.tsx")
    assert "if (view !== 'live') return undefined;" in page


def test_the_day_index_is_not_polled_on_a_timer():
    """Past days do not gain suggestions. Re-polling them is requests spent to receive the same
    answer."""
    page = _require(FRONTEND / "app" / "page.tsx")
    index_effect = page.split("api.signalDays", 1)[1][:400]
    assert "setInterval" not in index_effect


def test_an_empty_filter_combination_reads_as_empty_not_broken():
    """specs/15 failure mode 1 applied to the new filters: an empty result must state why it is
    empty, and must not look like the engine failed."""
    page = _require(FRONTEND / "app" / "page.tsx")
    assert "Nothing matches these filters" in page
    assert "empty result, not a failure" in page
    assert "Other models may have" in page, (
        "a model filter hiding other models' suggestions should say so"
    )


# ---- the analytics page (Phase 3) -----------------------------------------------------------

def test_the_analytics_page_exists_and_is_reachable():
    page = _require(FRONTEND / "app" / "analytics" / "page.tsx")
    nav = _require(FRONTEND / "components" / "Nav.tsx")
    proxy = _require(PROXY_TS)
    assert "'/analytics'" in nav, "the page must be linked or nobody will find it"
    assert "'api/analytics'" in proxy, "and allow-listed or it 404s through the proxy"
    assert "api.analytics" in page


def test_every_analytics_rate_is_rendered_with_its_numerator_and_denominator():
    """A percentage on its own is unverifiable and, over a tiny sample, misleading: `100%` from one
    resolved suggestion is not a result. The fraction is what makes the figure checkable against a
    direct query."""
    page = _require(FRONTEND / "app" / "analytics" / "page.tsx")
    assert "rate.numerator" in page and "rate.denominator" in page
    assert "metric-frac" in page


def test_the_analytics_page_never_renders_an_undefined_rate_as_zero():
    """`0%` reads as "nothing ever hit", which is a claim about the market. "Not defined" is a claim
    about the sample, which is the true one."""
    page = _without_comments(_require(FRONTEND / "app" / "analytics" / "page.tsx"))
    assert "'not defined'" in page
    assert "rate.value === null" in page


def test_the_analytics_page_shows_the_limitations_before_the_numbers():
    """A number on a dashboard acquires authority it has not earned. The engine generates the
    limitations from the data so they cannot go stale, and the page must put them above the figures
    rather than in a footnote."""
    page = _require(FRONTEND / "app" / "analytics" / "page.tsx")
    assert "What these numbers are not" in page
    assert page.index("limitations") < page.index("<h2>Rates</h2>"), (
        "the caveat has to come before the figure it qualifies"
    )


def test_the_analytics_page_does_not_aggregate_in_the_browser():
    """The engine returns a few dozen counts. A browser that fetched the archive to group it would
    grow without bound and would compute the date boundary in the viewer's timezone."""
    page = _without_comments(_require(FRONTEND / "app" / "analytics" / "page.tsx"))
    for forbidden in (".reduce(", ".filter((s", "signals.length"):
        assert forbidden not in page, f"{forbidden} suggests client-side aggregation"
    assert "data.totals" in page or "totals." in page


def test_the_analytics_page_does_not_poll():
    """Counts over a window of whole days change when a suggestion resolves, not every fifteen
    seconds. Re-running four GROUP BY queries on a timer would spend the VM's cycles to redraw
    identical numbers."""
    page = _require(FRONTEND / "app" / "analytics" / "page.tsx")
    assert "setInterval" not in page


def test_the_analytics_page_explains_the_two_denominators():
    """The page shows both readings of "hit rate" and must say why they differ, or a reader will
    assume the larger one is the real one."""
    # Whitespace-normalised: prose inside JSX wraps across lines, so a raw substring check fails on
    # text that is present. Collapsing runs of whitespace is how every prose assertion here should
    # be written.
    page = " ".join(_require(FRONTEND / "app" / "analytics" / "page.tsx").split())
    assert "share a denominator" in page
    assert "of the suggestions that reached a trade result" in page


def test_the_four_pages_are_all_in_the_nav():
    """The navigation is the only way to reach three of them."""
    nav = _require(FRONTEND / "components" / "Nav.tsx")
    for href in ("'/'", "'/chart'", "'/data'", "'/analytics'"):
        assert href in nav, f"{href} is missing from the nav"
    for path in ("app/page.tsx", "app/chart/page.tsx", "app/data/page.tsx",
                 "app/analytics/page.tsx"):
        assert (FRONTEND / path).exists(), f"{path} does not exist"


# ---- the live card's issue time, and the unexplainable count (F-065, F-066) -----------------

def test_a_live_suggestion_card_shows_when_it_was_issued():
    """The history view showed the issue time and the live cards did not, which left the most
    time-sensitive view as the one that could not answer "how old is this?" — and a setup at an
    hourly horizon legitimately stays live for hours, so being on screen says nothing about age."""
    panels = _require(FRONTEND / "components" / "Panels.tsx")
    card = panels.split("export function SignalCard", 1)[1]
    assert "created_at_ms" in card, "the live card must read the issue time"
    assert "utcStamp(signal.created_at_ms)" in card
    assert ">suggested<" in card or "suggested" in card


def test_the_live_card_distinguishes_the_issue_time_from_the_last_state_change():
    """Both are useful and they are different questions: when it was first suggested, and when it
    last moved. One number shown for both would make a setup detected yesterday and triggered an
    hour ago look like an hour-old suggestion."""
    card = _require(FRONTEND / "components" / "Panels.tsx").split(
        "export function SignalCard", 1)[1]
    assert "state_since_ms" in card
    assert "state changed" in card


def test_an_unclassified_model_bucket_is_rendered_rather_than_dropped():
    """This is what made F-065 unexplainable from the screen. Every C1/C2/C3 signal was persisted
    with `strategy_class = 0`, the engine counted them under a `"0"` key, and `modelList` only ever
    looked at keys 1-4 — so the chips read `Correction 0, Persistence 0, Transition 0,
    Established 13` while the list below showed far more than 13.

    A count that appears nowhere cannot be reconciled against the list, so now it appears.
    """
    suggestions = _without_comments(_require(FRONTEND / "components" / "Suggestions.tsx"))
    assert "export function unclassifiedCount" in suggestions
    assert "known.has(k)" in suggestions, "keys outside the four must be appended, not filtered out"

    page = _require(FRONTEND / "app" / "page.tsx")
    assert "unclassifiedCount" in page
    assert "no valid model class" in page
    assert "counts will not add up" in page, (
        "the reader must be told the chips and the list disagree, not left to add them up"
    )


def test_the_engine_reports_an_unclassified_bucket_at_all():
    """The frontend can only render it if the engine sends it. Both halves asserted, because the
    defect needed both to be wrong."""
    import inspect

    from api.services import SignalService

    source = inspect.getsource(SignalService._group_by_model)
    assert "unclassified" in source
    assert "out.setdefault" in source, "a class outside the four must be added, not skipped"
