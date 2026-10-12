"""PHASE-37 — the fields a re-published signal carries, and the C3-C resolution defects.

Three confirmed defects, each reproduced before it was fixed:

* **`direction` was frozen on re-publish.** C3-A and C3-C arm *pending* with a placeholder
  direction; the UPSERT did not update the column, so a resolved SHORT stayed stored as a LONG while
  its levels were updated to the SHORT's — a long whose stop sits above its entry, which is exactly
  the suggestion the owner reported.
* **The C3-C rejection target was not checked against the side of the trade.** `specs/05` §5 offers
  "the opposite side of the range, or SMA_N"; the raw SMA was taken without asking whether it was on
  the opposite side, so a target could land behind the entry — and a target behind the entry is hit
  the instant the position opens.
* **`trail_level` was never written at all.** `specs/06` §4.1 requires it published live for the
  strategies that have no fixed target, and the column was not even in the INSERT.
"""
import json
from decimal import Decimal

import pytest

from audit.trail import AuditTrail
from normalize.canonical import Product, Timeframe
from store import open_store
from strategy.act import Direction
from strategy.base import Lifecycle, RawSignal

T0 = 1791590400000 + 3_600_000


class _Sizing:
    def to_dict(self):
        return {}


class _Signal:
    """The minimum `risk.engine.Signal` surface `_write_signal` touches."""

    def __init__(self, raw):
        self.raw = raw
        self.detail = {}
        self.vetoes = []
        self.sizing = _Sizing()

    def to_dict(self):
        return {"raw": {}}


def _raw(*, direction, entry, stop, targets, lifecycle, detail, as_of=T0, snapshot="snap-1"):
    return RawSignal(
        signal_id="C3-C-B-SOL_USDT-1", strategy_id="C3-C", strategy_version="1.0.0",
        asset="B-SOL_USDT", product=Product.FUTURES, direction=direction, lifecycle=lifecycle,
        as_of_ms=as_of, signal_timeframe=Timeframe.H1, entry=entry, stop=stop, targets=targets,
        snapshot_id=snapshot, detail=detail)


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "fields.db")
    yield s
    s.close()


def _row(store):
    return dict(store.conn.execute("SELECT * FROM signals").fetchone())


# ---- the reported suggestion, reproduced and then fixed -------------------------------------

def test_a_resolved_pending_setup_stores_the_direction_the_market_assigned(store):
    """The owner's exact report: `B-SOL_USDT LONG C3-C`, entry 112.73, stop 113.91, target 114.04 —
    a LONG whose stop sits **above** its entry.

    Those levels are a correct SHORT. C3-C arms pending, publishes `direction=LONG` as a structural
    placeholder, and the resolution assigns the real side; the UPSERT updated the levels and froze
    the direction, so the two halves of the row described different trades.
    """
    trail = AuditTrail(store=store)

    # Pass 1: armed pending. No levels yet, placeholder direction.
    trail._write_signal(_Signal(_raw(
        direction=Direction.LONG, entry=None, stop=None, targets=(),
        lifecycle=Lifecycle.SETUP_DETECTED,
        detail={"pending_direction": True, "boundary": "112.73"})), None, T0, T0)
    store.conn.commit()
    assert _row(store)["direction"] == "LONG", "the placeholder is written on arming"

    # Pass N: resolved as a rejection of an upside break, so the real side is SHORT.
    trail._write_signal(_Signal(_raw(
        direction=Direction.SHORT, entry=Decimal("112.73"), stop=Decimal("113.91"),
        targets=(Decimal("114.04"),), lifecycle=Lifecycle.TRIGGERED,
        detail={"branch": "rejection", "pending_direction": False},
        as_of=T0 + 900_000, snapshot="snap-2")), None, T0 + 900_000, T0 + 900_000)
    store.conn.commit()

    row = _row(store)
    assert row["direction"] == "SHORT", (
        "the resolved direction must replace the placeholder, or the row describes a long with a "
        "stop above its entry"
    )
    # And the levels are consistent with that direction: stop above entry for a SHORT.
    assert Decimal(row["stop"]) > Decimal(row["entry"])


def test_the_fields_that_describe_the_current_state_follow_the_republish(store):
    """`as_of_ms`, `snapshot_id` and the regime describe *this* evaluation. Frozen at first write, a
    signal republished for hours reported the snapshot it was armed under while carrying levels from
    a later one — a provenance contradiction. The per-evaluation history lives in `signal_audit`."""
    trail = AuditTrail(store=store)
    trail._write_signal(_Signal(_raw(
        direction=Direction.LONG, entry=None, stop=None, targets=(),
        lifecycle=Lifecycle.SETUP_DETECTED, detail={})), None, T0, T0)
    trail._write_signal(_Signal(_raw(
        direction=Direction.LONG, entry=Decimal("100"), stop=Decimal("95"), targets=(),
        lifecycle=Lifecycle.TRIGGERED, detail={}, as_of=T0 + 60_000,
        snapshot="snap-later")), None, T0 + 60_000, T0 + 60_000)
    store.conn.commit()

    row = _row(store)
    assert row["as_of_ms"] == T0 + 60_000
    assert row["snapshot_id"] == "snap-later"
    assert row["state"] == "TRIGGERED"


def test_the_identity_and_provenance_fields_stay_frozen(store):
    """The counterpart. `created_at_ms` is the issue time and `armed_at_ms` the arming time; both
    must survive a republish, or a suggestion would migrate between date groups whenever its outcome
    was recorded (F-063)."""
    trail = AuditTrail(store=store)
    trail._write_signal(_Signal(_raw(
        direction=Direction.LONG, entry=None, stop=None, targets=(),
        lifecycle=Lifecycle.SETUP_DETECTED, detail={})), None, T0, T0)
    store.conn.commit()
    first = _row(store)

    trail._write_signal(_Signal(_raw(
        direction=Direction.LONG, entry=Decimal("100"), stop=Decimal("95"), targets=(),
        lifecycle=Lifecycle.TRIGGERED, detail={}, as_of=T0 + 86_400_000)), None,
        T0 + 86_400_000, T0 + 86_400_000)
    store.conn.commit()
    second = _row(store)

    for frozen in ("created_at_ms", "armed_at_ms", "strategy_id", "asset", "signal_id"):
        assert first[frozen] == second[frozen], f"{frozen} must not move on a republish"


# ---- the live trail, which replaces T1 for the trailed strategies ---------------------------

def test_the_trail_level_is_persisted_for_a_strategy_with_no_fixed_target(store):
    """`specs/06` §4.1: "No T1: the position is trailed from entry... `trail_level` published live",
    and `c2_participation.py` says in its own docstring that "the UI shows the live trail instead".

    The column was not in the INSERT at all, so it was always NULL — a trailed suggestion showed
    `targets: []` and no trail, i.e. no exit whatsoever.
    """
    trail = AuditTrail(store=store)
    trail._write_signal(_Signal(_raw(
        direction=Direction.LONG, entry=Decimal("100"), stop=Decimal("95"), targets=(),
        lifecycle=Lifecycle.TRIGGERED, detail={"trail_level": "97.5"})), None, T0, T0)
    store.conn.commit()

    row = _row(store)
    assert row["trail_level"] == "97.5"
    assert json.loads(row["targets"]) == [], "no fixed target, by design"


def test_the_trail_level_moves_with_the_position(store):
    """It is monotone in the strategy (`update_trail` never loosens it), and the column has to follow
    it or the displayed exit lags the real one."""
    trail = AuditTrail(store=store)
    for level in ("97.5", "98.9", "99.4"):
        trail._write_signal(_Signal(_raw(
            direction=Direction.LONG, entry=Decimal("100"), stop=Decimal("95"), targets=(),
            lifecycle=Lifecycle.TRIGGERED, detail={"trail_level": level})), None, T0, T0)
    store.conn.commit()
    assert _row(store)["trail_level"] == "99.4"


def test_a_signal_with_a_fixed_target_stores_no_trail(store):
    """Nothing invents a trail where the strategy publishes a target. The two are alternatives."""
    trail = AuditTrail(store=store)
    trail._write_signal(_Signal(_raw(
        direction=Direction.LONG, entry=Decimal("100"), stop=Decimal("95"),
        targets=(Decimal("110"),), lifecycle=Lifecycle.TRIGGERED, detail={})), None, T0, T0)
    store.conn.commit()
    row = _row(store)
    assert row["trail_level"] is None
    assert json.loads(row["targets"]) == ["110"]


# ---- C3-C: the target must be on the profitable side of entry -------------------------------

def test_the_c3c_rejection_target_falls_back_when_the_mean_is_behind_the_entry():
    """`specs/05` §5 gives two options for T1: "the opposite side of the range, or SMA_N". The SMA is
    only an approximation of the first **when it is on that side**.

    Hand-computed. LONG, entry 112.73, ATR 0.60: a trailing mean of 110.00 sits *behind* the entry,
    so the strategy must take the spec's other option — the measured move, entry + 2 x ATR = 113.93.
    Taken raw, the target would have been 110.00, which a long position is already past, and
    `_manage_triggered` would record an immediate false TARGET_HIT.
    """
    import inspect

    from strategy.c3_transition import C3C

    source = inspect.getsource(C3C._levels)
    assert "beyond_entry" in source, "the side of the SMA must be checked"
    assert "measured = entry + 2 * atr if long else entry - 2 * atr" in source

    # The arithmetic itself, independent of the strategy's plumbing.
    entry, atr = Decimal("112.73"), Decimal("0.60")
    for long, sma, expected in (
        (True, Decimal("110.00"), entry + 2 * atr),      # behind a long -> measured move
        (True, Decimal("115.50"), Decimal("115.50")),    # beyond a long -> the mean
        (False, Decimal("115.50"), entry - 2 * atr),     # behind a short -> measured move
        (False, Decimal("110.00"), Decimal("110.00")),   # beyond a short -> the mean
    ):
        measured = entry + 2 * atr if long else entry - 2 * atr
        beyond = sma > entry if long else sma < entry
        assert (sma if beyond else measured) == expected


def test_a_target_behind_the_entry_would_resolve_immediately():
    """Why the fix matters rather than being tidy. `lifecycle._manage_triggered` compares the current
    price against T1 with no sanity check on the side, so a target behind the entry is satisfied by
    the entry price itself — recording TARGET_HIT on the first evaluation after the trigger."""
    import inspect

    from strategy import lifecycle

    source = inspect.getsource(lifecycle._manage_triggered)
    assert "price >= targets[0]" in source and "price <= targets[0]" in source
    # A SHORT at 112.73 with T1 at 114.04 (above it) satisfies `price <= targets[0]` at once.
    price, target = Decimal("112.73"), Decimal("114.04")
    assert price <= target, "which is why the wrong-side target recorded an instant target hit"


def test_the_c3c_trigger_state_is_computed_before_the_levels():
    """`_trigger_state` caches `failed_extreme`, and the rejection stop is defined as "beyond the
    failed extreme". With the calls the other way round the first pass after a resolution measured
    the stop from the boundary instead, and persisted it."""
    import inspect

    from strategy.c3_transition import C3C

    source = inspect.getsource(C3C._manage)
    assert source.index("_trigger_state(") < source.index("_levels("), (
        "the extreme must be cached before the levels read it"
    )


def test_c3c_still_publishes_no_levels_while_pending():
    """Regression guard. A pending C3-C has no entry, stop or target by design — the direction
    decides them — and the dashboard renders that as "DIRECTION NOT YET DECIDED". None of this
    round's changes may start inventing levels for it."""
    import inspect

    from strategy.c3_transition import C3C

    source = inspect.getsource(C3C._levels)
    assert 'if state.context.get("pending") or branch is None:' in source
    assert "return Levels(entry=None, stop=None, targets=()" in source
