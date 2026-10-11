"""Rehydrate armed setups on startup, so a restart does not abandon them (specs/13 §11.1).

**The gap this closes.** `AuditTrail.open_setups()` existed, documented "for a restart to resume
from (specs/13 §11.1)", and had **no callers**. `AppRunner.states` started as an empty dict on every
process start, so every restart silently abandoned every armed setup: those rows stayed
`SETUP_DETECTED` / `ENTER_NOW` / `TRIGGERED` in the `signals` table permanently and could never
reach `TARGET_HIT`, `STOPPED` or `EXPIRED`, because nothing held the `SetupState` any more. The
dashboard went on displaying them as live suggestions indefinitely (F-062).

It is the third instance of one pattern — code written, tested in isolation, never wired to a caller
— after F-043's serve loop and F-054's tick rounding. All three were invisible because the thing
that *should* have failed simply never ran.

**What this module does and deliberately does not do.**

It rehydrates state and hands it to the existing evaluation path. It does **not** decide outcomes.
`strategy.evaluate(...)` already receives a state and `strategy/lifecycle.py:progress()` already
applies expiry, invalidation, the ACT trigger, the stop and the first target — so a resumed setup is
managed by exactly the same code that manages one armed a minute ago, which is the only way live and
backtest can agree. A second evaluator here would be a second set of rules to keep in step.

specs/13 §11.1 requires each persisted setup to be **re-evaluated against the current snapshot**
rather than trusted, and this module's refusals implement that:

* past expiry → not resumed; the next pass sees no state and the row is left for the sweep
* a different `parameter_hash` or `strategy_version` → not resumed. A setup armed under different
  parameters is not the setup those parameters would arm now, and carrying it forward would mix two
  parameter sets inside one signal's provenance, which `specs/09` forbids for the audit trail
* no stored `context` → not resumed. Written by a build before migration 3; resuming without the
  trail level or the approach side would reset a trailing stop to its starting width and invert
  C3-C's acceptance test, silently. **Declining is the conservative answer**; guessing is not
* a strategy that is no longer registered → not resumed
* two open setups for one `(strategy, asset)` → the newest is resumed. The runtime keys state by
  `(strategy_id, asset)` by design (`specs/04` §6's one-position constraint), so only one can be
  held, and the older is reported rather than silently dropped

Every refusal is counted and logged with its reason. A setup that is not resumed is **not**
transitioned here: inventing a terminal state for it would be this module rewriting history it did
not witness, and specs/13 §11.2 is explicit that the equivalent case — a trigger that fired while
the process was down — must be `MISSED`, never `TRIGGERED`, precisely because the user had no
opportunity to act. Deciding that belongs to the lifecycle engine with a snapshot in front of it,
not to a loader.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from core.logging import get_logger
from normalize.canonical import Product
from strategy.act import Direction
from strategy.base import Lifecycle, SetupState

log = get_logger("app.resume")
COMPONENT = "app.resume"


@dataclass
class ResumeReport:
    """What happened, in enough detail to act on. Counted, never silent."""

    resumed: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def resumed_count(self) -> int:
        return len(self.resumed)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    def to_dict(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        for s in self.skipped:
            reasons[str(s.get("reason"))] = reasons.get(str(s.get("reason")), 0) + 1
        return {"resumed": self.resumed_count, "skipped": self.skipped_count,
                "skipped_reasons": reasons, "resumed_ids": list(self.resumed)}


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _context(raw: Any) -> dict[str, Any] | None:
    """The stored context, or ``None`` when there is nothing usable to resume from.

    ``None`` and ``{}`` are different answers here. A row written before migration 3 has SQL NULL
    and genuinely has no context; a strategy that keeps no context writes ``{}``. Only the first is
    a reason to decline.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def rehydrate(row: dict[str, Any]) -> SetupState:
    """One stored row back into a :class:`SetupState`, with its original ``armed_at_ms``.

    The arming time is carried through unchanged, and that is the point of resuming at all: `X*`
    spans the window from arming, and expiry counts from arming. Re-arming with "now" would hand the
    setup a fresh window and a fresh deadline, which is a different setup wearing the same id.
    """
    context = _context(row.get("context"))
    return SetupState(
        signal_id=str(row["signal_id"]),
        strategy_id=str(row["strategy_id"]),
        asset=str(row["asset"]),
        product=Product(str(row["product"])),
        direction=Direction(str(row["direction"])),
        armed_at_ms=int(row["armed_at_ms"]),
        armed_snapshot_id=str(row["armed_snapshot_id"]),
        expires_at_ms=int(row["expires_at_ms"]) if row.get("expires_at_ms") is not None else 0,
        lifecycle=Lifecycle(str(row["lifecycle"])),
        x_star=_decimal(row.get("x_star")),
        e_t=_decimal(row.get("e_t")),
        original_entry_price=_decimal(row.get("original_entry_price")),
        triggered_at_ms=(int(row["triggered_at_ms"])
                         if row.get("triggered_at_ms") is not None else None),
        parameter_hash=str(row.get("parameter_hash") or ""),
        strategy_version=str(row.get("strategy_version") or ""),
        context=context if context is not None else {},
    )


def resume_setups(rows: list[dict[str, Any]], *, now_ms: int, parameter_hash: str,
                  strategy_versions: dict[str, str]) -> tuple[dict[tuple[str, str], SetupState],
                                                              ResumeReport]:
    """Decide which persisted setups may be resumed, and return them keyed for the runner.

    Pure: takes rows and facts, returns states and a report. No store, no clock, no snapshot — so
    every refusal above is directly testable without a database or a market.
    """
    report = ResumeReport()
    states: dict[tuple[str, str], SetupState] = {}

    for row in sorted(rows, key=lambda r: int(r.get("armed_at_ms") or 0)):
        signal_id = str(row.get("signal_id"))
        strategy_id = str(row.get("strategy_id"))
        key = (strategy_id, str(row.get("asset")))

        # The ids are bound as defaults rather than captured: a closure over a loop variable reads
        # the *last* value, which here would mean every skip reason naming the same setup.
        def skip(reason: str, _sid: str = signal_id, _strat: str = strategy_id,
                 **detail: Any) -> None:
            report.skipped.append({"signal_id": _sid, "strategy_id": _strat,
                                   "reason": reason, **detail})

        if strategy_id not in strategy_versions:
            skip("strategy_not_registered")
            continue
        if _context(row.get("context")) is None:
            # Pre-migration-3 row: the facts a resume needs were never written.
            skip("no_stored_context")
            continue
        if row.get("parameter_hash") and str(row["parameter_hash"]) != parameter_hash:
            skip("parameter_hash_changed", stored=str(row["parameter_hash"]))
            continue
        if (row.get("strategy_version")
                and str(row["strategy_version"]) != strategy_versions[strategy_id]):
            skip("strategy_version_changed", stored=str(row["strategy_version"]),
                 current=strategy_versions[strategy_id])
            continue

        expires = row.get("expires_at_ms")
        if expires is not None and now_ms >= int(expires):
            skip("past_expiry", expires_at_ms=int(expires))
            continue

        try:
            state = rehydrate(row)
        except (KeyError, TypeError, ValueError) as e:
            skip("unreadable_row", detail=str(e))
            continue

        # Rows are sorted oldest-first, so a later one legitimately supersedes an earlier one. The
        # loser is the setup **already** in the dict, and it is reported by its own id rather than
        # vanishing — a dropped setup nobody names is a dropped setup nobody finds.
        existing = states.get(key)
        if existing is not None:
            report.skipped.append({"signal_id": existing.signal_id, "strategy_id": strategy_id,
                                   "reason": "superseded_by_newer_setup",
                                   "superseded_by": state.signal_id})
        states[key] = state

    report.resumed = [s.signal_id for s in states.values()]
    return states, report


def resume_into(runner: Any, trail: Any, *, now_ms: int) -> ResumeReport:
    """Load persisted open setups into ``runner.states``. Returns what happened.

    Failure here is logged and **does not stop startup**: an engine that refuses to start because it
    could not resume old setups is an engine that stops producing new suggestions too, and the new
    ones are the half nobody can recover later. The report says what was lost either way.
    """
    try:
        rows = trail.open_setups()
    except Exception as e:                           # noqa: BLE001 - startup must survive this
        log.error("resume_read_failed", error=str(e), error_type=type(e).__name__,
                  detail="no setups were resumed; the engine continues and will arm new ones. "
                         "Previously armed setups stay in their current state until they expire")
        return ResumeReport()

    versions = {s.id: s.version for s in getattr(runner, "strategies", ())}
    states, report = resume_setups(
        rows, now_ms=now_ms,
        parameter_hash=getattr(runner.builder, "parameter_hash", "") or "",
        strategy_versions=versions)
    runner.states.update(states)

    if report.resumed_count or report.skipped_count:
        log.info("setups_resumed", **report.to_dict(),
                 detail="resumed setups are managed by the same lifecycle code as new ones; a "
                        "skipped setup is NOT transitioned here, because inventing a terminal "
                        "state for a setup this process never observed would rewrite history "
                        "(specs/13 §11.2)")
        for entry in report.skipped:
            log.info("setup_not_resumed", **entry)
    else:
        log.info("no_setups_to_resume", detail="nothing was armed when the engine last stopped")
    return report
