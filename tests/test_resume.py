"""PHASE-34 (Phase 2D) — resuming armed setups across a restart (specs/13 §11.1).

The gap these tests close is not a subtle one: `AuditTrail.open_setups()` existed, was documented
"for a restart to resume from", and **had no callers**. `AppRunner.states` started empty on every
process start, so every restart abandoned every armed setup — the rows stayed
`SETUP_DETECTED`/`ENTER_NOW`/`TRIGGERED` in the signals table permanently and could never reach
`TARGET_HIT`, `STOPPED` or `EXPIRED`, because nothing held their state any more (F-062).

The tests are weighted toward the **refusals**, because resuming the wrong setup is worse than not
resuming it. A setup carried forward under changed parameters, or without the context that holds a
trailing stop's level, produces a plausible-looking suggestion computed from two different rule
sets — and nothing downstream could detect it.
"""
import json

from app.resume import ResumeReport, rehydrate, resume_setups
from normalize.canonical import Product
from strategy.act import Direction
from strategy.base import Lifecycle

T0 = 1767225600000
PH = "param-hash-current"
VERSIONS = {"C2-A": "1.0.0", "C3-C": "1.0.0"}


def row(**over):
    """A stored `setup_state` row as the audit trail writes it."""
    base = {
        "signal_id": "C2-A-B-BTC_USDT-1-abcd1234", "strategy_id": "C2-A", "asset": "B-BTC_USDT",
        "product": "futures", "direction": "LONG", "armed_at_ms": T0,
        "armed_snapshot_id": "snap-1", "lifecycle": "TRIGGERED",
        "expires_at_ms": T0 + 86_400_000, "x_star": "84000.5", "e_t": "84100.25",
        "original_entry_price": "84100.25", "triggered_at_ms": T0 + 600_000,
        "parameter_hash": PH, "strategy_version": "1.0.0",
        "context": json.dumps({"trail_level": "83500.1", "extreme_since_entry": "85000.9"}),
        "updated_at_ms": T0 + 600_000,
    }
    return {**base, **over}


# ---- rehydration keeps exactly what the lifecycle needs -------------------------------------

def test_a_rehydrated_setup_keeps_its_original_arming_time_and_decimals():
    """The arming time is the whole point of resuming. `X*` spans the window from arming and expiry
    counts from arming, so re-arming with "now" would hand the setup a fresh window and a fresh
    deadline — a different setup wearing the same id.

    Decimals stay `Decimal`: the store keeps them as TEXT precisely so no float touches a price, and
    a resume that parsed them as floats would reintroduce the rounding at the one moment nobody is
    watching.
    """
    from decimal import Decimal

    state = rehydrate(row())
    assert state.armed_at_ms == T0
    assert state.expires_at_ms == T0 + 86_400_000
    assert state.triggered_at_ms == T0 + 600_000
    assert state.lifecycle is Lifecycle.TRIGGERED
    assert state.direction is Direction.LONG
    assert state.product is Product.FUTURES
    assert state.x_star == Decimal("84000.5") and isinstance(state.x_star, Decimal)
    assert state.e_t == Decimal("84100.25")
    assert state.original_entry_price == Decimal("84100.25")


def test_the_context_survives_because_a_trailing_stop_lives_in_it():
    """C2's monotone trail is accumulated in `context`, not recomputed (specs/13 §2), and C3-C's
    `inside_is_below` decides which way its acceptance test reads.

    Resuming without them would reset a trailing stop to its starting width — loosening a stop that
    the spec requires never to loosen — and invert C3-C's judgement. Both silently.
    """
    state = rehydrate(row())
    assert state.context["trail_level"] == "83500.1"
    assert state.context["extreme_since_entry"] == "85000.9"

    pending = rehydrate(row(strategy_id="C3-C", lifecycle="SETUP_DETECTED",
                            context=json.dumps({"inside_is_below": True, "boundary": "85000"})))
    assert pending.context["inside_is_below"] is True


def test_an_open_setup_is_resumed_and_keyed_for_the_runner():
    states, report = resume_setups([row()], now_ms=T0 + 3600_000, parameter_hash=PH,
                                   strategy_versions=VERSIONS)
    assert set(states) == {("C2-A", "B-BTC_USDT")}
    assert report.resumed_count == 1 and report.skipped_count == 0


# ---- the refusals: resuming the wrong setup is worse than not resuming ----------------------

def test_a_setup_past_its_expiry_is_not_resumed():
    """specs/13 §11.1 step 4. Hand-computed: expiry is T0 + 24h, so T0 + 24h exactly is already
    past it (`SetupState.expired` is `>=`)."""
    states, report = resume_setups([row()], now_ms=T0 + 86_400_000, parameter_hash=PH,
                                   strategy_versions=VERSIONS)
    assert states == {}
    assert report.skipped[0]["reason"] == "past_expiry"


def test_a_setup_armed_under_different_parameters_is_not_resumed():
    """A setup armed under a different `parameter_hash` is not the setup those parameters would arm
    now. Carrying it forward would put two parameter sets inside one signal's provenance, which
    makes the audit record unreplayable — and `specs/09` treats that as unacceptable output."""
    states, report = resume_setups([row(parameter_hash="param-hash-old")], now_ms=T0 + 1000,
                                   parameter_hash=PH, strategy_versions=VERSIONS)
    assert states == {}
    assert report.skipped[0]["reason"] == "parameter_hash_changed"
    assert report.skipped[0]["stored"] == "param-hash-old"


def test_a_setup_armed_by_a_different_strategy_version_is_not_resumed():
    states, report = resume_setups([row(strategy_version="0.9.0")], now_ms=T0 + 1000,
                                   parameter_hash=PH, strategy_versions=VERSIONS)
    assert states == {}
    assert report.skipped[0]["reason"] == "strategy_version_changed"


def test_a_row_written_before_the_context_column_existed_is_not_resumed():
    """Migration 3 added `context`. A row from the previous build has SQL NULL there, and the facts
    a resume needs were never written — so declining is the conservative answer and guessing is not.

    `{}` is a different case: a strategy that keeps no context writes an empty dict, and that must
    still resume.
    """
    states, report = resume_setups([row(context=None)], now_ms=T0 + 1000, parameter_hash=PH,
                                   strategy_versions=VERSIONS)
    assert states == {}
    assert report.skipped[0]["reason"] == "no_stored_context"

    states2, report2 = resume_setups([row(context="{}")], now_ms=T0 + 1000, parameter_hash=PH,
                                     strategy_versions=VERSIONS)
    assert report2.resumed_count == 1, "an empty context is not a missing context"
    assert states2[("C2-A", "B-BTC_USDT")].context == {}


def test_a_setup_for_an_unregistered_strategy_is_not_resumed():
    states, report = resume_setups([row(strategy_id="C9-Z")], now_ms=T0 + 1000, parameter_hash=PH,
                                   strategy_versions=VERSIONS)
    assert states == {}
    assert report.skipped[0]["reason"] == "strategy_not_registered"


def test_an_unreadable_row_is_reported_rather_than_crashing_startup():
    """One corrupt row must not stop the engine from resuming the others, or from starting at all."""
    good = row()
    bad = row(signal_id="broken", direction="SIDEWAYS")
    states, report = resume_setups([bad, good], now_ms=T0 + 1000, parameter_hash=PH,
                                   strategy_versions=VERSIONS)
    assert report.resumed_count == 1
    assert [s["reason"] for s in report.skipped] == ["unreadable_row"]
    assert report.skipped[0]["signal_id"] == "broken"


def test_two_open_setups_for_one_strategy_and_asset_keep_the_newest_and_name_the_other():
    """The runtime keys state by `(strategy_id, asset)` by design — `specs/04` §6's one-position
    constraint — so only one can be held. The loser is reported **by its own id**, because a dropped
    setup nobody names is a dropped setup nobody finds.

    Hand-checked: the newer arming wins, and the report names the older one as superseded.
    """
    older = row(signal_id="older", armed_at_ms=T0)
    newer = row(signal_id="newer", armed_at_ms=T0 + 7_200_000,
                expires_at_ms=T0 + 7_200_000 + 86_400_000)
    states, report = resume_setups([newer, older], now_ms=T0 + 7_300_000, parameter_hash=PH,
                                   strategy_versions=VERSIONS)

    assert states[("C2-A", "B-BTC_USDT")].signal_id == "newer"
    assert report.resumed == ["newer"]
    assert len(report.skipped) == 1
    assert report.skipped[0]["signal_id"] == "older"
    assert report.skipped[0]["superseded_by"] == "newer"


def test_a_skipped_setup_is_never_transitioned_by_the_loader():
    """specs/13 §11.2's principle, applied here: this process did not observe what happened to a
    setup it is declining to resume, so inventing a terminal state for it would be rewriting history
    it did not witness. The loader reports; the lifecycle engine decides, with a snapshot in hand."""
    import inspect

    from app import resume

    source = inspect.getsource(resume)
    for forbidden in ("Lifecycle.EXPIRED", "Lifecycle.INVALIDATED", "Lifecycle.MISSED",
                      ".transition("):
        assert forbidden not in source, (
            f"the resume loader must not assign {forbidden}: it has no snapshot to justify it"
        )


def test_the_report_summarises_reasons_for_the_log():
    report = ResumeReport(resumed=["a"], skipped=[{"reason": "past_expiry"},
                                                  {"reason": "past_expiry"},
                                                  {"reason": "no_stored_context"}])
    assert report.to_dict()["skipped_reasons"] == {"past_expiry": 2, "no_stored_context": 1}
    assert report.to_dict()["resumed"] == 1


# ---- end to end, through the store ----------------------------------------------------------

def test_a_setup_written_by_the_audit_trail_round_trips_through_a_resume(tmp_path):
    """The whole path with a real database: write a setup, read it back through `open_setups()`,
    rehydrate it, and confirm the context survived the round trip.

    This is the test that would have failed before migration 3 — the context column did not exist,
    so `_write_setup_state` dropped it and the row came back without the trail level.
    """
    from audit.trail import AuditTrail
    from store import open_store
    from strategy.base import SetupState

    store = open_store(tmp_path / "r.db")
    try:
        trail = AuditTrail(store=store)
        state = SetupState(
            signal_id="C2-A-x", strategy_id="C2-A", asset="B-BTC_USDT", product=Product.FUTURES,
            direction=Direction.LONG, armed_at_ms=T0, armed_snapshot_id="snap",
            expires_at_ms=T0 + 86_400_000, lifecycle=Lifecycle.TRIGGERED,
            triggered_at_ms=T0 + 60_000, parameter_hash=PH, strategy_version="1.0.0",
            context={"trail_level": "83500.1", "extreme_since_entry": "85000.9"})
        trail._write_setup_state(state, T0 + 60_000)
        store.conn.commit()

        rows = trail.open_setups()
        assert len(rows) == 1
        states, report = resume_setups(rows, now_ms=T0 + 3600_000, parameter_hash=PH,
                                       strategy_versions=VERSIONS)
        assert report.resumed_count == 1
        back = states[("C2-A", "B-BTC_USDT")]
        assert back.context["trail_level"] == "83500.1", "the context must survive the round trip"
        assert back.triggered_at_ms == T0 + 60_000
        assert back.armed_at_ms == T0
    finally:
        store.close()


def test_a_terminal_setup_is_not_offered_for_resume(tmp_path):
    """`open_setups()` selects the three active lifecycles only. A resumed terminal setup would be
    re-managed, and re-managing a TARGET_HIT could transition it again — the duplicate-outcome case
    Phase 2D has to make impossible."""
    from audit.trail import AuditTrail
    from store import open_store
    from strategy.base import SetupState

    store = open_store(tmp_path / "t.db")
    try:
        trail = AuditTrail(store=store)
        for i, lifecycle in enumerate((Lifecycle.TARGET_HIT, Lifecycle.STOPPED,
                                       Lifecycle.EXPIRED, Lifecycle.TRIGGERED)):
            trail._write_setup_state(SetupState(
                signal_id=f"s{i}", strategy_id="C2-A", asset=f"A{i}", product=Product.FUTURES,
                direction=Direction.LONG, armed_at_ms=T0 + i, armed_snapshot_id="snap",
                expires_at_ms=T0 + 86_400_000, lifecycle=lifecycle,
                parameter_hash=PH, strategy_version="1.0.0", context={}), T0)
        store.conn.commit()

        rows = trail.open_setups()
        assert [r["lifecycle"] for r in rows] == ["TRIGGERED"]
    finally:
        store.close()


def test_startup_resumes_into_the_runner_and_survives_a_read_failure(tmp_path):
    """`resume_into` must populate `runner.states`, and must not stop startup if it cannot read.

    An engine that refuses to start because it could not resume old setups also stops producing new
    suggestions — and the new ones are the half that cannot be recovered later.
    """
    from app.resume import resume_into

    class Builder:
        parameter_hash = PH

    class Strategy:
        def __init__(self, sid):
            self.id, self.version = sid, "1.0.0"

    class Runner:
        def __init__(self):
            self.states = {}
            self.builder = Builder()
            self.strategies = [Strategy("C2-A"), Strategy("C3-C")]

    class GoodTrail:
        def open_setups(self):
            return [row()]

    class BrokenTrail:
        def open_setups(self):
            raise RuntimeError("the setup_state table is unreadable")

    runner = Runner()
    report = resume_into(runner, GoodTrail(), now_ms=T0 + 1000)
    assert report.resumed_count == 1
    assert ("C2-A", "B-BTC_USDT") in runner.states

    broken_runner = Runner()
    broken_report = resume_into(broken_runner, BrokenTrail(), now_ms=T0 + 1000)
    assert broken_report.resumed_count == 0, "a read failure resumes nothing"
    assert broken_runner.states == {}, "and leaves the runner usable rather than raising"


def test_the_startup_path_actually_calls_resume():
    """The defect being fixed was a missing *caller*, so the caller is what this asserts. Three
    times now the same pattern has cost a session: F-043's serve loop, F-054's tick rounding, and
    this. A test on the wiring is cheap; finding it in production is not."""
    import inspect

    from app import __main__ as app_main

    source = inspect.getsource(app_main)
    assert "resume_into(runner, audit" in source
    assert source.index("resume_into(runner, audit") < source.index("def evaluate_once"), (
        "the resume must happen before the first evaluation, or that pass arms a duplicate"
    )
