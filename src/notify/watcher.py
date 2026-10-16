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

**What counts as a change.** The ``signals`` row's ``state`` column, compared against the state this
notifier last sent for that ``signal_id``. Two tempting alternatives were rejected on evidence:

* ``signal_events`` writes a row on **every managed pass**, not only on a transition
  (``audit/trail.py:_write_events`` inserts whenever ``progress`` is present), so it would notify
  once a minute for the lifetime of a tracked setup.
* ``state_since_ms`` is re-stamped on every republish, so it moves without the state changing.

So the state value itself is the only honest signal of "something actually happened", and a new
``signal_id`` is the honest signal of "a suggestion appeared".
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
    """What has already been notified: ``signal_id -> the state last sent for it``."""

    seen: dict[str, str] = field(default_factory=dict)
    #: False until the first poll has recorded what already exists. See :meth:`Watcher.poll`.
    primed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "primed": self.primed, "seen": dict(self.seen)}

    @classmethod
    def from_dict(cls, raw: Any) -> WatcherState:
        if not isinstance(raw, dict):
            return cls()
        seen = raw.get("seen")
        return cls(seen=dict(seen) if isinstance(seen, dict) else {},
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


def format_message(row: dict[str, Any], *, previous_state: str | None) -> str:
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

    lines = [
        f"{heading}",
        f"state: {state}" + (f"  (was {previous_state})" if previous_state else "  (new)"),
        "",
    ]

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
            self.state.seen = {r["signal_id"]: str(r["state"]) for r in rows}
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

        changed = [r for r in rows
                   if self.state.seen.get(r["signal_id"]) != str(r["state"])]
        # Oldest first, so a burst arrives in the order it happened.
        changed.reverse()

        for row in changed:
            signal_id = row["signal_id"]
            previous = self.state.seen.get(signal_id)
            text = format_message(row, previous_state=previous)
            if dry_run:
                report["skipped"] += 1
                log.info("notify_dry_run", signal_id=signal_id, state=row["state"],
                         previous_state=previous, message_chars=len(text))
                print(f"--- would send for {signal_id} ---\n{text}\n")
                continue

            result = send_message(self.config, text)
            if result.ok:
                # Recorded **only on success**, so a failed send is retried on the next poll rather
                # than lost. Each signal is tracked independently, so one undeliverable message
                # cannot block the others.
                self.state.seen[signal_id] = str(row["state"])
                self.sent += 1
                report["sent"] += 1
                log.info("notify_sent", signal_id=signal_id, strategy=row["strategy_id"],
                         asset=row["asset"], state=row["state"], previous_state=previous)
            else:
                self.failures += 1
                report["failed"] += 1
                log.error("notify_failed", signal_id=signal_id, state=row["state"],
                          **result.to_dict(),
                          detail="not recorded as sent, so the next poll will try again")

        self._prune()
        if not dry_run and (report["sent"] or report["primed"]):
            save_state(self.config.state_file, self.state)
        return report

    def _prune(self) -> None:
        """Keep the state file bounded. Only reached if the cap is exceeded, which it should not be."""
        if len(self.state.seen) <= MAX_TRACKED:
            return
        keep = {r["signal_id"] for r in self.qualifying_rows()}
        before = len(self.state.seen)
        self.state.seen = {k: v for k, v in self.state.seen.items() if k in keep}
        log.info("notify_state_pruned", before=before, after=len(self.state.seen),
                 detail="entries for signals no longer in the table were dropped")
