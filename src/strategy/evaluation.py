"""Shared evaluation mechanics: the ACT unit, the ACT window, signal assembly and setup management.

Everything here is used by every strategy, which is the point: specs/13 §3 requires ACT to have one
implementation, and the same applies to how a setup is armed, managed and retired. A strategy
contributes its conditions, its direction rule and its levels — never its own entry mechanics or its
own lifecycle handling.

**The ACT unit and the 15-minute gap.** specs/03 §2 specifies ``U = ATR_15m`` for the trigger, but
specs/02 §4 defines the shared lookback set for 1h, 4h and 1d only — there is no registered
``lookback_15m_*``, and the parameter budget is full at 30. Rather than add three parameters or invent
an unregistered number, the unit is ATR on the trigger timeframe over **the same bar count as the 1h
short lookback** (24 bars = 6 hours of 15m). That reuses a registered value, keeps the count visible on
every signal as ``act_unit_bars``, and is recorded as open question Q9 for the spec's author.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np

from core.errors import ConfigError, FeatureNotAvailable, SetupNotReady
from core.logging import get_logger
from features.convert import RoundMode, quantize_price
from features.f2_volatility import true_range
from features.value import FeatureSet, FeatureStatus
from features.windows import extract
from normalize.canonical import Product, Timeframe
from regime.model import RegimeState
from snapshot.models import InstrumentKey, MarketSnapshot, SeriesKey
from strategy.act import ActState, Direction, act
from strategy.base import Lifecycle, RawSignal, SetupState
from strategy.conditions import ConditionResult

log = get_logger("strategy.evaluation")
COMPONENT = "strategy.evaluation"

#: The timeframe ACT confirms on (specs/03 §2: "confirmed on the close of a 1-minute bar").
CONFIRM_TF = Timeframe.M1
#: The timeframe the ACT unit is measured on.
TRIGGER_TF = Timeframe.M15


#: What `Levels.price_basis` may say. A basis other than ``price`` means these numbers are **not**
#: tradeable prices, so they are neither tick-rounded nor rendered as an entry anyone could place.
PRICE_BASIS = "price"
RESIDUAL_BASIS = "residual"


@dataclass(frozen=True)
class Levels:
    """What a strategy publishes. Immutable; the risk engine may veto but never alters these."""

    entry: Decimal | None
    stop: Decimal | None
    targets: tuple[Decimal, ...] = ()
    detail: dict[str, Any] | None = None
    #: The space these numbers live in. ``price`` for almost everything; ``residual`` for C1-C,
    #: whose observable is a beta-adjusted spread and whose levels are therefore small and routinely
    #: **negative**. Published because a consumer cannot tell the difference by looking: a dashboard
    #: that renders a residual of ``-0.0414`` under a heading marked *entry* has invented a price
    #: that does not exist, and a reader who acts on it is acting on nothing (F-051).
    price_basis: str = PRICE_BASIS


def signal_id(strategy_id: str, asset: str, armed_at_ms: int, snapshot_id: str) -> str:
    """Deterministic, so a replay of the same arming produces the same id (specs/10 §7)."""
    digest = hashlib.sha256(f"{strategy_id}|{asset}|{armed_at_ms}|{snapshot_id}".encode())
    return f"{strategy_id}-{asset}-{armed_at_ms}-{digest.hexdigest()[:8]}"


def act_unit(snapshot: MarketSnapshot, asset: str, product: Product, params: dict[str, Any],
             *, timeframe: Timeframe = TRIGGER_TF) -> tuple[Decimal, int]:
    """``(unit, bars_used)`` — ATR on the trigger timeframe. See the module docstring on Q9.

    Computed here rather than read from the feature registry because the registry has no 15m lookbacks.
    It reuses :func:`features.f2_volatility.true_range`, so there is still one true-range implementation.
    """
    bars = int(params.get("lookback_1h_short", 24))
    key = SeriesKey(asset, product, timeframe)
    state = snapshot.series.get(key)
    if state is None:
        # The SnapshotRequest did not ask for this timeframe. That is a configuration mistake, not a
        # data gate and not a bug in the strategy: no amount of waiting adds a series nobody requested.
        raise ConfigError(
            f"the {timeframe.value} series for {asset} is not in the snapshot, so the ACT unit cannot "
            "be measured",
            component=COMPONENT, operation="act_unit",
            context={"asset": asset, "timeframe": timeframe.value,
                     "series": sorted(str(k) for k in snapshot.series)},
            likely_cause="the snapshot request does not include the trigger timeframe",
            remediation="add the 15m series to the SnapshotRequest; ACT cannot size its trigger without it",
        )
    outcome = extract(state, lookback=bars, extra_bars=1, min_bars=bars + 1)
    if outcome.window is None:
        raise SetupNotReady(
            f"the {timeframe.value} window is not usable: {outcome.reason}",
            component=COMPONENT, operation="act_unit",
            context={"asset": asset, "timeframe": timeframe.value, "status": outcome.status.value},
            likely_cause=outcome.reason,
            remediation="the setup waits; a trigger sized from a holed or short window would be wrong "
                        "in a way nothing downstream could detect",
        )
    tr = true_range(outcome.window)
    value = float(np.mean(tr[-bars:]))
    if value <= 0:
        raise SetupNotReady(
            f"average true range on {timeframe.value} is {value}",
            component=COMPONENT, operation="act_unit",
            context={"asset": asset, "bars": bars},
            likely_cause="every bar in the window is a single price",
            remediation="a zero unit would make the trigger fire on any tick back; the setup waits",
        )
    return Decimal(repr(value)), bars


def act_window(snapshot: MarketSnapshot, asset: str, product: Product, direction: Direction,
               armed_at_ms: int) -> tuple[list[Decimal], list[Decimal], int]:
    """``(closes, adverse, armed_idx)`` from **closed** 1m bars, for the ACT computation.

    ``X*`` is recomputed from closed bars every time rather than accumulated (specs/13 §2), so a
    restart rebuilds it exactly and live and backtest agree. Closed bars only, because the trigger
    confirms on a 1m close: letting ``X*`` track a forming bar's intra-minute low would make the
    trigger depend on data the confirmation rule does not use.
    """
    key = SeriesKey(asset, product, CONFIRM_TF)
    state = snapshot.series.get(key)
    if state is None or not state.bars:
        raise SetupNotReady(
            f"no closed {CONFIRM_TF.value} bars for {asset} in this snapshot",
            component=COMPONENT, operation="act_window",
            context={"asset": asset, "series": sorted(str(k) for k in snapshot.series)},
            likely_cause="the snapshot request does not include the 1m series",
            remediation="add the 1m series to the SnapshotRequest; ACT confirms on a 1m close",
        )
    bars = [b for b in state.bars if b.open_time_ms >= armed_at_ms]
    if not bars:
        # Nothing has closed since arming: the setup is armed but not yet observable. Using the last
        # closed bar before arming keeps X* defined and is conservative -- it cannot invent a retrace.
        bars = list(state.bars[-1:])
    closes = [b.close for b in bars]
    adverse = [b.low if direction is Direction.LONG else b.high for b in bars]
    return closes, adverse, 0


def compute_act(snapshot: MarketSnapshot, asset: str, product: Product, direction: Direction,
                armed_at_ms: int, params: dict[str, Any]) -> tuple[ActState, int]:
    """The ACT state for an armed setup, with the unit's bar count for the record."""
    unit, bars = act_unit(snapshot, asset, product, params)
    delta = Decimal(str(params["act_trigger_multiple"]))
    closes, adverse, idx = act_window(snapshot, asset, product, direction, armed_at_ms)
    return act(direction, closes, idx, unit, delta, adverse=adverse), bars


def atr_1h(features: FeatureSet, horizon: str = "short") -> Decimal:
    """``ATR_1h`` for stop placement. Raises rather than substituting when unavailable."""
    return Decimal(repr(features.value("ATR", Timeframe.H1, horizon)))


def build_signal(*, strategy: Any, features: FeatureSet, snapshot: MarketSnapshot,
                 regime: RegimeState, direction: Direction, lifecycle: Lifecycle,
                 levels: Levels, conditions: tuple[ConditionResult, ...], rationale: str,
                 state: SetupState, act_state: ActState | None = None,
                 detail: dict[str, Any] | None = None) -> RawSignal:
    """Assemble a signal with full provenance.

    Every field here is what makes a signal replayable: the snapshot it was built from, the parameter
    hash, the code version and the strategy version. A signal without them cannot be audited, and
    specs/10 §8 treats that as unacceptable output.

    This is also where levels are **rounded to the instrument's tick** (specs/08 §10), because this
    is the moment a strategy's arithmetic becomes a published number. See :func:`publishable_levels`.
    """
    priced = publishable_levels(levels, snapshot=snapshot, features=features, direction=direction)
    return RawSignal(
        signal_id=state.signal_id, strategy_id=strategy.id, strategy_version=strategy.version,
        asset=features.asset, product=features.product, direction=direction, lifecycle=lifecycle,
        as_of_ms=snapshot.as_of_ms, signal_timeframe=strategy.signal_timeframe,
        entry=priced.entry, stop=priced.stop, targets=priced.targets, act=act_state,
        conditions=conditions, rationale=rationale, snapshot_id=snapshot.snapshot_id,
        parameter_hash=snapshot.parameter_hash, code_version=snapshot.code_version,
        regime=regime.to_dict(),
        detail={**(priced.detail or {}), **(detail or {}),
                "tier": strategy.tier,
                "price_basis": priced.price_basis,
                "validated": False,          # nothing is validated until Phase 16 says so
                "expansion_seeking": strategy.expansion_seeking},
    )


def publishable_levels(levels: Levels, *, snapshot: MarketSnapshot, features: FeatureSet,
                       direction: Direction) -> Levels:
    """Round published levels to the instrument's tick, erring conservatively (specs/08 §10).

    **Why this has to happen before the risk engine sees them, not at the display boundary.** The
    risk engine computes `k_net`, the breakeven hit rate and the R:R gate *from these numbers*. Round
    them later and the reader sees a price that is not the one the arithmetic used, so a trade
    displayed as clearing its R:R floor may not — the two would disagree by a tick, silently, in the
    direction nobody checked. Rounding here means the number on screen, the number in the audit
    record and the number the cost model used are the same number.

    The direction of rounding is not cosmetic. A stop rounds **away** from entry and a target
    **toward** it, so tick rounding can only ever make a suggestion more conservative. Rounding to
    nearest would let it flatter the trade by half a tick on both ends.

    Three cases decline to round, each for its own reason:

    * ``price_basis != "price"`` — the numbers are not prices. A residual has no tick (F-051).
    * no instrument metadata — the tick is unknown, and **inventing one is forbidden** (invariant 1).
      The risk engine already vetoes this signal on ``DQ_META``, so returning it unrounded loses
      nothing: it cannot be acted on either way.
    * ``tick <= 0`` — defaulted rather than fetched metadata, which must not be trusted.
    """
    if levels.price_basis != PRICE_BASIS:
        return levels
    meta = snapshot.instruments.get(InstrumentKey(features.asset, features.product))
    tick = getattr(meta, "tick", None) if meta is not None else None
    if tick is None or tick <= 0:
        return levels

    long_side = direction is Direction.LONG
    entry = (quantize_price(levels.entry, tick=tick, mode=RoundMode.NEAREST)
             if levels.entry is not None else None)
    stop = (quantize_price(levels.stop, tick=tick,
                           mode=RoundMode.STOP_LONG if long_side else RoundMode.STOP_SHORT)
            if levels.stop is not None else None)
    targets = tuple(
        quantize_price(t, tick=tick,
                       mode=RoundMode.TARGET_LONG if long_side else RoundMode.TARGET_SHORT)
        for t in levels.targets)
    return Levels(entry=entry, stop=stop, targets=targets, detail=levels.detail,
                  price_basis=levels.price_basis)


def update_trail(state: SetupState, *, atr: Decimal, s: Decimal, price: Decimal) -> None:
    """``extreme since entry -/+ s * ATR``, **monotone**: the trail never loosens.

    Shared by C2-A/B/C, C3-B and C4-A/B for the same reason ACT is shared — a trail that loosens in
    one strategy and not another is not a design choice, it is a bug that only shows up in one
    backtest. The extreme is accumulated in ``state.context`` rather than recomputed, because it is
    defined from the entry *moment*, and the moment is state rather than a window boundary; a restart
    therefore reloads it with the setup (specs/13 §2).

    No-ops unless the setup is ``TRIGGERED``: there is nothing to trail before entry.
    """
    if state.lifecycle is not Lifecycle.TRIGGERED:
        return
    extreme = state.context.get("extreme_since_entry")
    extreme_d = Decimal(str(extreme)) if extreme is not None else price
    current = state.context.get("trail_level")
    if state.direction is Direction.LONG:
        extreme_d = max(extreme_d, price)
        candidate = extreme_d - s * atr
        state.context["trail_level"] = str(max(candidate, Decimal(str(current)))
                                           if current is not None else candidate)
    else:
        extreme_d = min(extreme_d, price)
        candidate = extreme_d + s * atr
        state.context["trail_level"] = str(min(candidate, Decimal(str(current)))
                                           if current is not None else candidate)
    state.context["extreme_since_entry"] = str(extreme_d)


def effective_stop(state: SetupState, stop: Decimal | None) -> Decimal | None:
    """The trail once triggered, otherwise the published stop — and never looser than it.

    The ``max``/``min`` is the load-bearing part: early in a trade the trail sits *behind* the
    structural stop, and taking the trail unconditionally would widen risk below the ``R`` the signal
    was sized against.
    """
    trail = state.context.get("trail_level")
    if state.lifecycle is Lifecycle.TRIGGERED and trail is not None and stop is not None:
        trail_d = Decimal(str(trail))
        return max(trail_d, stop) if state.direction is Direction.LONG else min(trail_d, stop)
    return stop


def price_now(features: FeatureSet, snapshot: MarketSnapshot) -> Decimal:
    """The most recent closed 1m close — the price every level is measured against."""
    key = SeriesKey(features.asset, features.product, CONFIRM_TF)
    state = snapshot.series.get(key)
    if state is None or not state.bars:
        raise SetupNotReady(
            f"no closed {CONFIRM_TF.value} bar for {features.asset}, so there is no current price",
            component=COMPONENT, operation="price_now",
            context={"asset": features.asset},
            likely_cause="the 1m series is missing from the snapshot",
            remediation="add the 1m series to the SnapshotRequest",
        )
    return state.bars[-1].close


def feature_or_none(features: FeatureSet, name: str, timeframe: Timeframe | None = None,
                    horizon: str = "-") -> float | None:
    try:
        return features.value(name, timeframe, horizon)
    except FeatureNotAvailable:
        return None


def detail_of(features: FeatureSet, name: str, timeframe: Timeframe | None = None,
              horizon: str = "-") -> dict[str, Any]:
    """A feature's structured payload, empty when the feature is not OK."""
    fv = features.get(name, timeframe, horizon)
    return dict(fv.detail) if fv.status is FeatureStatus.OK else {}
