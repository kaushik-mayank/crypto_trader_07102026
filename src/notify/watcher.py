"""Watch the signals table for BTCUSDT C3-C state changes, and forward them to Telegram.

**Read-only, out of process, and structurally unable to affect the engine.**

Three properties, each a deliberate choice rather than a convenience:

1. **The database connection is read-only** (:class:`store.reader.ReadOnlyStore` opens
   ``file:...?mode=ro``). This process cannot write a row, create a table, or run a migration. If
   this module had a bug that tried, SQLite would refuse it.
2. **It is a separate process.** The alternative — a hook inside
   ``AuditTrail.record_evaluation`` — would put an outbound HTTPS call *inside the engine's atomic
   audit transaction*. A slow or unreachable Telegram would then delay the evaluation loop, and a
   raised exception would roll back the audit record for the whole pass. The suggestion engine must
   not be able to fail because a chat app is down.
3. **It keeps its own state file.** Recording "already sent" in the engine's database would mean a
   schema change and a second writer on the file the engine owns. A small JSON file under
   ``data/notify/`` costs nothing and keeps the feature deletable.

**What counts as a notifiable event, and why the state column alone was the wrong answer.**

The first version notified whenever ``signals.state`` differed from the state last sent. That is
wrong in both directions, and the two faults have the same cause:

* **It missed the suggestion.** C3-C arms *pending*: ``_arm`` publishes
  ``Levels(entry=None, stop=None, targets=())`` with ``pending_direction: True`` in state
  ``SETUP_DETECTED``, and the levels are filled in later, on the pass where ``_resolve`` returns a
  branch (``c3_transition.py:_levels`` returns blanks while ``context["pending"]`` is set). The
  UPSERT in ``audit/trail.py`` writes ``entry=excluded.entry, stop=excluded.stop,
  targets=excluded.targets`` on **every** republish, independently of ``state``. And
  ``lifecycle.py:advance`` only leaves ``SETUP_DETECTED`` when ``act_state.triggered`` — price
  reaching ``E(t)``, a *different* event from the resolution. So the blank-to-populated transition
  routinely happens with the state column unchanged, and a state-change trigger cannot see it.
* **It sent everything else.** ``SETUP_DETECTED -> ENTER_NOW -> TRIGGERED`` is three state changes,
  so the operator got three messages — and only the ``ENTER_NOW`` one carried the levels, late.

So the trigger is no longer "the state moved". It is **three stages per signal, each sent at most
once**:

======================  ==========================================================================
``detected``            the signal is seen for the first time (blank, as the dashboard shows it)
``populated``           ``entry`` and ``stop`` are present and the direction is no longer pending
``final``               the state reached ``TARGET_HIT`` or ``STOPPED``
======================  ==========================================================================

Every other transition is observed and deliberately not sent. Nothing upstream is suppressed to
achieve that: the states and events are untouched, and the eligibility decision lives here.

Two tempting change sources were rejected on evidence and remain rejected:

* ``signal_events`` writes a row on **every managed pass**, not only on a transition
  (``audit/trail.py:_write_events`` inserts whenever ``progress`` is present), so it would notify
  once a minute for the lifetime of a tracked setup.
* ``state_since_ms`` is re-stamped on every republish, so it moves without the state changing.

**The values are the dashboard's values.** ``/api/signals/live`` reads the same ``signals`` table
through the same read-only store, and ``services.py:_row_to_signal`` performs *no* value
transformation: it parses ``payload`` and ``targets`` as JSON, lifts ``detail`` to the top level and
reads ``tradeable`` out of the payload. This module does the same four things, so what is sent is
what is displayed. That equivalence is asserted by a test, because it is the whole basis for reading
the table directly instead of adding an HTTP dependency on the app process -- which could not
deliver stage ``final`` anyway, since ``live()`` returns only the three active states.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging import get_logger
from notify.config import TelegramConfig
from notify.telegram import send_message

log = get_logger("notify.watcher")
COMPONENT = "notify.watcher"

#: IST, as a fixed offset. Matches the dashboard (D-63): the operator reading the message and the
#: operator reading the dashboard must see the same clock, or they will not believe they are looking
#: at the same suggestion. India has no daylight saving, so the arithmetic is exact.
IST_OFFSET_MS = int(5.5 * 60 * 60 * 1000)

#: Entries kept in the state file. C3-C on one asset produces a handful of signals a day, so this is
#: years of headroom; the cap exists only so an unforeseen loop cannot grow the file without bound.
MAX_TRACKED = 2000

#: The two terminal states that are a **trade result**. The lifecycle has seven terminal states
#: (`strategy/base.py:TERMINAL_STATES`); the other five -- EXPIRED, INVALIDATED, SUPPRESSED, MISSED,
#: HANDED_OFF -- are a setup ending without a trade, and are not notified. A setup that quietly
#: expires is not news; a stop being hit is.
RESULT_STATES = frozenset({"TARGET_HIT", "STOPPED"})

#: The three stages, in the order they can occur. Each is sent at most once per ``signal_id``.
STAGE_DETECTED = "detected"
STAGE_POPULATED = "populated"
STAGE_FINAL = "final"


def ist(ms: Any) -> str:
    """An epoch-millisecond timestamp as an IST wall clock, with the date."""
    try:
        epoch = int(ms)
    except (TypeError, ValueError):
        return "time unknown"
    import datetime as dt

    moment = dt.datetime.fromtimestamp((epoch + IST_OFFSET_MS) / 1000, dt.UTC)
    return moment.strftime("%Y-%m-%d %H:%M IST")


@dataclass
class WatcherState:
    """What has already been notified: ``signal_id -> the stages already sent for it``."""

    stages: dict[str, list[str]] = field(default_factory=dict)
    #: False until the first poll has recorded what already exists. See :meth:`Watcher.poll`.
    primed: bool = False

    def already(self, signal_id: str) -> set[str]:
        return set(self.stages.get(signal_id) or ())

    def record(self, signal_id: str, stage: str) -> None:
        done = self.already(signal_id)
        done.add(stage)
        # Stored in stage order rather than set order, so the file reads as a history.
        self.stages[signal_id] = [s for s in (STAGE_DETECTED, STAGE_POPULATED, STAGE_FINAL)
                                  if s in done]

    def to_dict(self) -> dict[str, Any]:
        return {"version": 2, "primed": self.primed,
                "stages": {k: list(v) for k, v in self.stages.items()}}

    @classmethod
    def from_dict(cls, raw: Any) -> WatcherState:
        """Rebuild the state. **A version-1 file is discarded, on purpose.**

        A v1 file recorded ``signal_id -> last state sent``, which cannot be converted into stages:
        it does not say whether the populated message ever went out, and guessing wrong either
        replays a message or drops one. Discarding it means the next poll primes -- it records the
        stage every current signal has already reached and sends nothing -- so the upgrade costs at
        most one missed notification for a setup mid-flight, and never a burst of stale ones. That is
        the same trade `load_state` already makes for a corrupt file, and the same direction.
        """
        if not isinstance(raw, dict):
            return cls()
        stages = raw.get("stages")
        if not isinstance(stages, dict):
            if raw.get("seen") is not None:
                log.info("notify_state_v1_discarded",
                         detail="the state file predates the three-stage model and cannot be "
                                "converted, because it does not record whether the populated "
                                "message was sent. The next poll re-primes and sends nothing")
            return cls()
        return cls(stages={str(k): [str(s) for s in v] for k, v in stages.items()
                           if isinstance(v, list)},
                   primed=bool(raw.get("primed", False)))


def load_state(path: Path) -> WatcherState:
    """Read the state file. A missing or unreadable file is a fresh start, never a crash.

    An unreadable file is reported at WARNING and treated as empty, which re-primes rather than
    re-sending: the failure mode of a corrupt state file is "you miss one notification", not "you
    get three hundred".
    """
    try:
        return WatcherState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return WatcherState()
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log.warning("notify_state_unreadable", path=str(path), error=str(e),
                    detail="starting from empty, which re-primes rather than re-sending",
                    remediation=f"delete {path} if this persists")
        return WatcherState()


def save_state(path: Path, state: WatcherState) -> None:
    """Write the state file atomically, so a kill mid-write cannot leave it truncated.

    Truncated JSON would be unreadable on the next start, which re-primes, which silently skips the
    notifications that were in flight. Writing to a temporary file and renaming makes that
    impossible: the rename is atomic on every filesystem this runs on.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        # Not fatal: the notifier keeps working, it just may re-send after a restart. Saying so is
        # better than stopping, because the alternative is no notifications at all.
        log.error("notify_state_unwritable", path=str(path), error=str(e),
                  detail="notifications continue, but a restart may repeat the most recent one",
                  remediation=f"check the owner and permissions of {path.parent}")


def is_populated(row: dict[str, Any]) -> bool:
    """Whether the dashboard would now show this setup's suggestion, rather than a blank.

    The same two conditions the dashboard renders on, read from the same fields:

    * ``detail.pending_direction`` -- true while C3-C is waiting to see which way the boundary
      resolves. The engine stores ``Direction.LONG`` as a *placeholder* in that window
      (``c3_transition.py:_arm``: "placeholder; the resolution assigns it"), so a populated
      suggestion must not be declared while it is set, or the message states a side the system has
      not chosen (F-067).
    * ``entry`` and ``stop`` both present. Both, not either: a suggestion with an entry and no stop
      is not actionable, and reporting it as populated would invite acting on half of one.

    ``targets`` is deliberately **not** required. `specs/06` §4.1 mandates no fixed T1 for the
    trailed strategies, so requiring it would mean a trailed setup never reached the populated stage
    at all.
    """
    detail = _detail(row)
    if detail.get("pending_direction") is True or detail.get("pending") is True:
        return False
    return _present(row.get("entry")) and _present(row.get("stop"))


def _present(value: Any) -> bool:
    """A level the engine actually published. ``None``, ``""`` and the string ``"None"`` are blanks.

    The string form is checked because the column is TEXT -- levels are stored as
    ``str(Decimal)`` -- and a stringified ``None`` would otherwise read as a published level.
    """
    return value is not None and str(value).strip() not in ("", "None")


def stage_due(row: dict[str, Any], already: set[str]) -> str | None:
    """Which of the three stages this row owes a message for, or ``None``.

    Ordered, and the order is the point: a result state outranks everything, because a setup that
    has already hit its stop should report that and not a suggestion to take it.
    """
    if str(row.get("state") or "") in RESULT_STATES:
        return None if STAGE_FINAL in already else STAGE_FINAL
    if STAGE_DETECTED not in already:
        return STAGE_DETECTED
    if STAGE_POPULATED not in already and is_populated(row):
        return STAGE_POPULATED
    return None


def stages_reached(row: dict[str, Any]) -> list[str]:
    """Every stage a row has *already* passed, for priming and for a first sighting.

    Used so that a signal first seen already populated gets **one** message describing what is
    actually there, not a blank "setup detected" followed by a populated one. The blank-to-populated
    transition is an event this notifier witnessed or it is not; manufacturing it for a setup met
    mid-life would report something that did not happen while this process was watching.
    """
    reached = [STAGE_DETECTED]
    if is_populated(row):
        reached.append(STAGE_POPULATED)
    if str(row.get("state") or "") in RESULT_STATES:
        reached.append(STAGE_FINAL)
    return reached


def format_message(row: dict[str, Any], *, stage: str) -> str:
    """One notification, built only from fields already on the row.

    Nothing is computed here. No level is derived, no direction inferred, no outcome judged — those
    are the engine's and would be a second implementation of its rules. This function reads and
    formats, which is the whole mandate.
    """
    asset = str(row.get("asset") or "?")
    pretty_asset = asset.replace("B-", "").replace("_", "")
    state = str(row.get("state") or "?")
    direction = str(row.get("direction") or "?")
    detail = _detail(row)

    # C3-A and C3-C arm *pending*: the stored direction is a structural placeholder until the market
    # resolves it (F-067). Printing it as a recommendation is exactly the mistake the dashboard made.
    pending = detail.get("pending_direction") is True or detail.get("pending") is True
    heading = f"{pretty_asset} · {row.get('strategy_id')} · "
    heading += "direction not yet decided" if pending else direction

    lines = [_headline(stage, row), "", heading, f"status: {state}", ""]

    if pending:
        lines.append("This setup is watching a level and will trade whichever way it resolves,")
        lines.append("so it has no entry or stop yet.")
        if detail.get("boundary"):
            lines.append(f"level being watched: {detail['boundary']}")
    else:
        targets = _targets(row)
        trail = row.get("trail_level") or detail.get("trail_level")
        lines.append(f"entry: {row.get('entry') or '—'}")
        lines.append(f"stop:  {row.get('stop') or '—'}")
        if targets:
            lines.append(f"target: {', '.join(targets)}")
        elif trail:
            lines.append(f"trailing stop: {trail}  (no fixed target by design)")
        else:
            lines.append("target: none — trailed from entry; the trail is set once it triggers")

    lines += ["", f"suggested: {ist(row.get('created_at_ms'))}"]
    if row.get("state_since_ms"):
        lines.append(f"state as of: {ist(row.get('state_since_ms'))}")

    rationale = str((row.get("payload") or {}).get("raw", {}).get("rationale") or "")
    if rationale:
        lines += ["", rationale[:600]]

    lines += ["", "Suggestion only — this system never places, cancels or transfers anything,",
              "and nothing in it has been validated against history."]
    return "\n".join(lines)


def _headline(stage: str, row: dict[str, Any]) -> str:
    """The first line, which is all that shows in a phone's notification preview.

    So it says which of the three things happened, in words, rather than a lifecycle constant.
    """
    if stage == STAGE_FINAL:
        hit = "TARGET HIT" if str(row.get("state")) == "TARGET_HIT" else "STOP-LOSS HIT"
        return f"{hit} - setup closed"
    if stage == STAGE_POPULATED:
        return "SUGGESTION READY - entry and stop now published"
    return "SETUP DETECTED"


def _detail(row: dict[str, Any]) -> dict[str, Any]:
    """The signal's detail, from wherever the engine put it (top level since D-50)."""
    detail = row.get("detail")
    if isinstance(detail, dict) and detail:
        return detail
    payload = row.get("payload")
    nested = payload.get("detail") if isinstance(payload, dict) else None
    return nested if isinstance(nested, dict) else {}


def _targets(row: dict[str, Any]) -> list[str]:
    raw = row.get("targets")
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return [str(t) for t in parsed] if isinstance(parsed, list) else []
        except (ValueError, json.JSONDecodeError):
            return []
    return []


@dataclass
class Watcher:
    """Polls, filters, de-duplicates and sends. Holds no write handle to anything but its own file."""

    store: Any
    config: TelegramConfig
    state: WatcherState = field(default_factory=WatcherState)
    sent: int = 0
    failures: int = 0

    def qualifying_rows(self) -> list[dict[str, Any]]:
        """The rows this notifier is allowed to see at all.

        **The filter is in the SQL**, parameterised on exactly two columns. Reading every signal and
        filtering in Python would work and would mean one careless edit away from notifying on
        everything; a `WHERE` clause cannot be bypassed by a later refactor of the loop below.
        """
        rows = self.store.conn.execute(
            """SELECT signal_id, strategy_id, asset, direction, state, state_since_ms,
                      entry, stop, targets, trail_level, payload, created_at_ms, updated_at_ms
               FROM signals
               WHERE strategy_id = ? AND asset = ?
               ORDER BY updated_at_ms DESC
               LIMIT ?""",
            (self.config.strategy_id, self.config.asset, MAX_TRACKED)).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            payload = record.get("payload")
            if isinstance(payload, str):
                try:
                    record["payload"] = json.loads(payload)
                except (ValueError, json.JSONDecodeError):
                    record["payload"] = {}
            record["detail"] = _detail(record)
            record["targets"] = _targets(record)
            # The same firewall the API applies at its boundary. `specs/13` §13 requires that no
            # endpoint return a signal with `tradeable = False`, and `services.py:live` drops them --
            # so a row this filter let through would be one the dashboard never shows, and the
            # message would describe a suggestion the operator cannot see anywhere. The two control
            # strategies are the only untradeable ones today and neither is C3-C, so this changes
            # nothing now; it is here so that it cannot start to matter silently.
            payload_obj = record.get("payload")
            record["tradeable"] = bool(payload_obj.get("tradeable", True)
                                       if isinstance(payload_obj, dict) else True)
            if not record["tradeable"]:
                continue
            out.append(record)
        return out

    def poll(self, *, dry_run: bool = False) -> dict[str, Any]:
        """One pass: find changes, send them, remember what was sent.

        **The first pass primes and sends nothing.** A database with a month of history would
        otherwise produce a month of notifications the moment the service starts, which is both
        useless and the kind of surprise that gets a feature switched off. Priming records the
        current state of every qualifying signal and logs how many; everything that changes *after*
        that is a notification.
        """
        rows = self.qualifying_rows()
        report: dict[str, Any] = {"qualifying": len(rows), "sent": 0, "failed": 0, "skipped": 0,
                                  "primed": False}

        if not self.state.primed:
            self.state.stages = {r["signal_id"]: stages_reached(r) for r in rows}
            self.state.primed = True
            report["primed"] = True
            report["skipped"] = len(rows)
            if not dry_run:
                save_state(self.config.state_file, self.state)
            log.info("notify_primed", qualifying=len(rows), asset=self.config.asset,
                     strategy=self.config.strategy_id,
                     detail="the first pass records what already exists and sends nothing, so "
                            "starting the service does not replay history. Changes from here on "
                            "are notified")
            return report

        # At most one message per signal per pass: the stages are ordered, so a signal that owes two
        # of them sends the earlier one now and the later one on the next poll. That keeps each
        # message a report of one event instead of a digest of several.
        due = [(r, stage) for r in rows
               if (stage := stage_due(r, self.state.already(r["signal_id"]))) is not None]
        # Oldest first, so a burst arrives in the order it happened.
        due.reverse()

        for row, stage in due:
            signal_id = row["signal_id"]
            first_sighting = not self.state.already(signal_id)
            text = format_message(row, stage=stage)
            if dry_run:
                report["skipped"] += 1
                log.info("notify_dry_run", signal_id=signal_id, stage=stage, state=row["state"],
                         message_chars=len(text))
                print(f"--- would send for {signal_id} ({stage}) ---\n{text}\n")
                continue

            result = send_message(self.config, text)
            if result.ok:
                # Recorded **only on success**, so a failed send is retried on the next poll rather
                # than lost. Each signal is tracked independently, so one undeliverable message
                # cannot block the others.
                #
                # On a *first* sighting every stage the row has already passed is recorded, not just
                # the one sent. A setup met already populated has had its values reported by this
                # very message, so claiming the blank-to-populated transition later would announce an
                # event that never happened while this process was watching.
                for reached in (stages_reached(row) if first_sighting else [stage]):
                    self.state.record(signal_id, reached)
                self.sent += 1
                report["sent"] += 1
                log.info("notify_sent", signal_id=signal_id, stage=stage,
                         strategy=row["strategy_id"], asset=row["asset"], state=row["state"])
            else:
                self.failures += 1
                report["failed"] += 1
                log.error("notify_failed", signal_id=signal_id, stage=stage, state=row["state"],
                          **result.to_dict(),
                          detail="not recorded as sent, so the next poll will try again")

        self._prune()
        if not dry_run and (report["sent"] or report["primed"]):
            save_state(self.config.state_file, self.state)
        return report

    def _prune(self) -> None:
        """Keep the state file bounded. Only reached if the cap is exceeded, which it should not be."""
        if len(self.state.stages) <= MAX_TRACKED:
            return
        keep = {r["signal_id"] for r in self.qualifying_rows()}
        before = len(self.state.stages)
        self.state.stages = {k: v for k, v in self.state.stages.items() if k in keep}
        log.info("notify_state_pruned", before=before, after=len(self.state.stages),
                 detail="entries for signals no longer in the table were dropped")
