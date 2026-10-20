"""PHASE-38 — Telegram notifications for BTCUSDT C3-C only.

The tests are weighted toward two things:

* **the filter**, because the requirement is exclusionary — one asset, one strategy, nothing else —
  and a notifier that leaks is worse than one that does not work, since nobody reviews messages that
  arrive correctly;
* **isolation**, because the feature was asked for on the explicit condition that it cannot disturb
  the running system. Those are asserted structurally (a read-only connection, no imports from the
  engine's modules) rather than by inspection.
"""
import datetime as _dt
import json
from pathlib import Path

import pytest

from notify.config import DEFAULT_ASSET, DEFAULT_STRATEGY, TelegramConfig, normalise_asset
from notify.telegram import MAX_MESSAGE_CHARS, SendResult, check_credentials, send_message
from notify.watcher import (
    STAGE_DETECTED,
    STAGE_FINAL,
    STAGE_POPULATED,
    Watcher,
    WatcherState,
    format_message,
    is_populated,
    ist,
    load_state,
    save_state,
    stage_due,
)
from store import open_store
from store.reader import ReadOnlyStore

# Derived, not hard-coded. A literal here was wrong by a day on the first attempt, and the test
# would then have "passed" against a wrong expectation — which is how a timezone test certifies a
# timezone bug.
T0 = int(_dt.datetime(2026, 10, 12, 6, 21, tzinfo=_dt.UTC).timestamp() * 1000)


def _insert(store, sid, strategy, asset, *, direction="LONG", state="SETUP_DETECTED",
            entry=None, stop=None, targets=(), trail=None, detail=None, rationale=""):
    store.conn.execute(
        """INSERT INTO signals (signal_id, strategy_id, strategy_version, strategy_class, asset,
               product, direction, state, state_since_ms, tier, validated, mode, armed_at_ms,
               entry, stop, targets, trail_level, parameter_hash, code_version, snapshot_id,
               as_of_ms, payload, created_at_ms, updated_at_ms)
           VALUES (?,?,'1.0.0',3,?,'futures',?,?,?,'A',0,'suggestion',?,?,?,?,?,'ph','cv','s',
                   ?,?,?,?)""",
        (sid, strategy, asset, direction, state, T0, T0, entry, stop, json.dumps(list(targets)),
         trail, T0, json.dumps({"raw": {"rationale": rationale}, "detail": detail or {}}), T0, T0))


@pytest.fixture
def seeded(tmp_path):
    """One qualifying signal and four that must never notify."""
    path = tmp_path / "n.db"
    store = open_store(path)
    _insert(store, "btc-c3c", "C3-C", "B-BTC_USDT",
            detail={"pending_direction": True, "boundary": "112750.5"})
    _insert(store, "eth-c3c", "C3-C", "B-ETH_USDT", state="TRIGGERED")   # right strategy, wrong asset
    _insert(store, "sol-c3c", "C3-C", "B-SOL_USDT", state="TARGET_HIT")  # right strategy, wrong asset
    _insert(store, "btc-c4b", "C4-B", "B-BTC_USDT", state="TRIGGERED")   # right asset, wrong strategy
    _insert(store, "btc-c1c", "C1-C", "B-BTC_USDT", state="STOPPED")     # right asset, wrong strategy
    store.conn.commit()
    store.close()
    yield path


def _config(tmp_path, **over):
    return TelegramConfig(bot_token="t" * 40, chat_id="42",
                          state_file=tmp_path / "state.json", **over)


def _watcher(path, tmp_path, *, primed=True, stages=None, **over):
    reader = ReadOnlyStore(path)
    return Watcher(store=reader, config=_config(tmp_path, **over),
                   state=WatcherState(stages={k: list(v) for k, v in (stages or {}).items()},
                                      primed=primed))


def _resolve(path, sid="btc-c3c", *, state="SETUP_DETECTED", direction="SHORT",
             entry="112730", stop="113910", targets='["111500"]'):
    """What the engine does on the pass where C3-C resolves: `pending` clears, the direction is
    assigned and the levels are filled in — **with no requirement that `state` move**, because
    `advance()` only leaves SETUP_DETECTED when price reaches the ACT trigger, a different event.

    Written as its own helper because it is the exact sequence the bug lived in.
    """
    store = open_store(path)
    store.conn.execute(
        """UPDATE signals SET direction=?, state=?, entry=?, stop=?, targets=?,
                  payload=json_set(payload, '$.detail.pending_direction', json('false'))
           WHERE signal_id=?""",
        (direction, state, entry, stop, targets, sid))
    store.conn.commit()
    store.close()


def _set_state(path, state, sid="btc-c3c"):
    store = open_store(path)
    store.conn.execute("UPDATE signals SET state=? WHERE signal_id=?", (state, sid))
    store.conn.commit()
    store.close()


# ---- the filter: one asset, one strategy, nothing else --------------------------------------

def test_only_the_one_asset_and_one_strategy_are_visible_at_all(seeded, tmp_path):
    """The requirement is exclusionary, so this is the test that matters most. Four of the five
    rows are near misses — the right strategy on the wrong asset, and the right asset on the wrong
    strategy — and none of them may reach the notifier."""
    watcher = _watcher(seeded, tmp_path)
    try:
        rows = watcher.qualifying_rows()
        assert [r["signal_id"] for r in rows] == ["btc-c3c"]
        assert rows[0]["asset"] == DEFAULT_ASSET and rows[0]["strategy_id"] == DEFAULT_STRATEGY
    finally:
        watcher.store.close()


def test_the_filter_is_in_the_sql_not_in_the_loop(seeded, tmp_path):
    """A `WHERE` clause cannot be bypassed by a later refactor of the polling loop; a Python-side
    filter one careless edit away from notifying on everything can."""
    import inspect

    source = inspect.getsource(Watcher.qualifying_rows)
    assert "WHERE strategy_id = ? AND asset = ?" in source
    assert "self.config.strategy_id, self.config.asset" in source


def test_btcusdt_resolves_to_the_engines_asset_id():
    """The request says "BTCUSDT"; the engine's column holds `B-BTC_USDT`. A plausible spelling that
    silently matched nothing would look exactly like "no suggestions have happened yet"."""
    for spelling in ("BTCUSDT", "btcusdt", "BTC/USDT", "BTC-USDT", "BTC_USDT", "B-BTC_USDT", "btc"):
        assert normalise_asset(spelling) == "B-BTC_USDT"
    # An unrecognised value passes through rather than silently becoming BTC.
    assert normalise_asset("ETHUSDT") == "ETHUSDT"


def test_a_non_qualifying_change_notifies_nothing(seeded, tmp_path):
    """Change every row the notifier must not care about, and nothing is sent."""
    store = open_store(seeded)
    store.conn.execute("UPDATE signals SET state='TARGET_HIT' WHERE signal_id != 'btc-c3c'")
    store.conn.commit()
    store.close()

    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED]})
    try:
        report = watcher.poll(dry_run=True)
        assert report["qualifying"] == 1
        assert report["skipped"] == 0, "nothing changed on the one row it watches"
    finally:
        watcher.store.close()


# ---- what counts as a change ----------------------------------------------------------------

def test_the_first_poll_primes_and_sends_nothing(seeded, tmp_path):
    """A database with a month of history would otherwise produce a month of notifications the
    moment the service starts — useless, and the kind of surprise that gets a feature turned off."""
    watcher = _watcher(seeded, tmp_path, primed=False)
    try:
        report = watcher.poll(dry_run=True)
        assert report["primed"] is True
        assert report["sent"] == 0
        assert watcher.state.stages == {"btc-c3c": [STAGE_DETECTED]}, (
            "the one qualifying row is blank and pending, so only `detected` is already reached"
        )
    finally:
        watcher.store.close()


def test_an_intermediate_state_change_notifies_nothing(seeded, tmp_path, monkeypatch):
    """**The first half of the reported defect, as a regression test.**

    The old trigger was "the state differs from the state last sent", so SETUP_DETECTED ->
    ENTER_NOW -> TRIGGERED produced three messages. A state change on its own is now not an event:
    the setup is still blank and pending, and nothing about it is news.
    """
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))

    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED]})
    try:
        for state in ("ENTER_NOW", "TRIGGERED"):
            _set_state(seeded, state)
            assert watcher.poll()["sent"] == 0, f"{state} is not one of the three notifiable events"
        assert sent == []
    finally:
        watcher.store.close()


def test_no_separate_enter_now_message_is_ever_sent(seeded, tmp_path, monkeypatch):
    """An explicit acceptance criterion, and worth its own test because ENTER_NOW is the transition
    the old code happened to deliver the levels on — so "it worked, just late" was easy to mistake
    for working."""
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))
    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED, STAGE_POPULATED]})
    try:
        _set_state(seeded, "ENTER_NOW")
        assert watcher.poll()["sent"] == 0
        assert not any("ENTER" in t for t in sent)
    finally:
        watcher.store.close()


def test_the_populated_suggestion_is_detected_with_no_state_change(seeded, tmp_path, monkeypatch):
    """**The other half of the reported defect: the message that was missing entirely.**

    C3-C arms blank and pending; the levels arrive on the pass where `_resolve` returns a branch,
    and `lifecycle.py:advance` does not move the state then — it moves on the ACT trigger, which is
    a different event. So the old state-change trigger could not see this at all, and the operator
    first saw the levels on the ENTER_NOW message, late.
    """
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))

    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED]})
    try:
        before = watcher.qualifying_rows()[0]
        assert before["state"] == "SETUP_DETECTED" and not is_populated(before)

        _resolve(seeded)                                  # levels filled in, state untouched

        after = watcher.qualifying_rows()[0]
        assert after["state"] == "SETUP_DETECTED", "the state deliberately did not move"
        assert is_populated(after)

        assert watcher.poll()["sent"] == 1
        assert "SUGGESTION READY" in sent[0]
        assert "112730" in sent[0] and "113910" in sent[0] and "111500" in sent[0], (
            "entry, stop and target are the values the dashboard shows for this row"
        )
        assert "SHORT" in sent[0], "the resolved direction, no longer the placeholder"

        # And not again, however many times the same populated row is observed.
        assert watcher.poll()["sent"] == 0 and watcher.poll()["sent"] == 0
        assert len(sent) == 1
    finally:
        watcher.store.close()


def test_a_signal_first_seen_already_populated_sends_one_message_not_two(seeded, tmp_path,
                                                                        monkeypatch):
    """A setup met mid-life — after a restart, or armed and resolved between two polls.

    It must not get a blank "setup detected" followed by a populated one: the blank-to-populated
    transition is an event this process either witnessed or did not, and manufacturing it would
    report something that never happened.
    """
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))
    store = open_store(seeded)
    _insert(store, "btc-c3c-2", "C3-C", "B-BTC_USDT", state="ENTER_NOW", direction="LONG",
            entry="83000", stop="82500", targets=("84000",))
    store.conn.commit()
    store.close()

    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED]})
    try:
        assert watcher.poll()["sent"] == 1
        assert "83000" in sent[0] and "82500" in sent[0], "one message, carrying the real values"
        assert watcher.state.already("btc-c3c-2") == {STAGE_DETECTED, STAGE_POPULATED}
        assert watcher.poll()["sent"] == 0
        assert len(sent) == 1
    finally:
        watcher.store.close()


def test_signal_events_was_rejected_as_the_source_for_a_reason():
    """`audit/trail.py:_write_events` inserts a row whenever `progress` is in the detail — i.e. on
    every managed pass, not only on a transition. Polling it would notify once a minute for the
    lifetime of a tracked setup, so the state value is the source instead."""
    import inspect

    from audit.trail import AuditTrail

    source = inspect.getsource(AuditTrail._write_events)
    assert "INSERT INTO signal_events" in source
    assert "if transitions is None" in source, (
        "the only guard is whether progress exists, not whether the state changed"
    )
    watcher_source = inspect.getsource(Watcher.qualifying_rows)
    assert "signal_events" not in watcher_source


# ---- failures retry rather than vanish -------------------------------------------------------

def test_a_failed_send_is_not_recorded_so_the_next_poll_retries(seeded, tmp_path, monkeypatch):
    """Recording a send that did not happen loses the notification silently, which is the one
    outcome worse than a duplicate."""
    attempts = {"n": 0}

    def flaky(cfg, text):
        attempts["n"] += 1
        return SendResult(ok=False, status=500, error="Telegram returned 500", retryable=True)

    monkeypatch.setattr("notify.watcher.send_message", flaky)
    _resolve(seeded)

    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED]})
    try:
        assert watcher.poll()["failed"] == 1
        assert watcher.state.already("btc-c3c") == {STAGE_DETECTED}, "not recorded as sent"
        assert watcher.poll()["failed"] == 1
        assert attempts["n"] == 2, "the next poll tried again"
    finally:
        watcher.store.close()


def test_one_undeliverable_signal_does_not_block_the_others(seeded, tmp_path, monkeypatch):
    """Each signal is tracked independently, so a message that can never be delivered cannot wedge
    the queue behind it."""
    def selective(cfg, text):
        return SendResult(ok="undeliverable" not in text, status=200)

    monkeypatch.setattr("notify.watcher.send_message", selective)
    _resolve(seeded)
    store = open_store(seeded)
    _insert(store, "btc-bad", "C3-C", "B-BTC_USDT", direction="undeliverable", state="TRIGGERED")
    store.conn.commit()
    store.close()

    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED]})
    try:
        report = watcher.poll()
        assert report["sent"] == 1 and report["failed"] == 1
        assert watcher.state.already("btc-c3c") == {STAGE_DETECTED, STAGE_POPULATED}, (
            "the deliverable one went through"
        )
        assert "btc-bad" not in watcher.state.stages
    finally:
        watcher.store.close()


def test_the_state_file_is_written_atomically():
    """A kill mid-write would otherwise leave truncated JSON, which reads as "not primed" on the
    next start — silently skipping the notifications that were in flight."""
    import inspect

    source = inspect.getsource(save_state)
    assert ".tmp" in source and "replace(" in source


def test_an_unreadable_state_file_re_primes_rather_than_re_sending(tmp_path):
    """The failure mode of a corrupt cursor must be "you miss one", not "you get three hundred"."""
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    state = load_state(path)
    assert state.stages == {} and state.primed is False


def test_the_state_file_round_trips(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, WatcherState(stages={"a": [STAGE_DETECTED, STAGE_POPULATED]}, primed=True))
    back = load_state(path)
    assert back.stages == {"a": [STAGE_DETECTED, STAGE_POPULATED]} and back.primed is True


def test_a_version_1_state_file_re_primes_rather_than_guessing(tmp_path):
    """The old file recorded `signal_id -> last state sent`, which cannot be converted: it does not
    say whether the populated message ever went out. Guessing wrong either replays a message or
    drops one, so it is discarded and the next poll primes — at most one missed notification for a
    setup mid-flight, never a burst of stale ones."""
    path = tmp_path / "v1.json"
    path.write_text(json.dumps({"version": 1, "primed": True,
                                "seen": {"btc-c3c": "TRIGGERED"}}), encoding="utf-8")
    state = load_state(path)
    assert state.stages == {} and state.primed is False, "re-primes, sends nothing"


def test_stages_are_recorded_in_order_not_in_set_order(tmp_path):
    """So the file reads as a history of what happened to a setup."""
    state = WatcherState()
    for stage in (STAGE_FINAL, STAGE_DETECTED, STAGE_POPULATED):
        state.record("x", stage)
    assert state.stages["x"] == [STAGE_DETECTED, STAGE_POPULATED, STAGE_FINAL]


# ---- the message is read, never computed ----------------------------------------------------

def test_the_message_reports_a_pending_setup_as_undecided(seeded, tmp_path):
    """C3-C arms pending and the stored direction is a structural placeholder (F-067). Printing it
    as a recommendation is exactly the mistake the dashboard made."""
    watcher = _watcher(seeded, tmp_path)
    try:
        row = watcher.qualifying_rows()[0]
        text = format_message(row, stage=STAGE_DETECTED)
        assert "SETUP DETECTED" in text
        assert "direction not yet decided" in text
        assert "112750.5" in text, "the level being watched is useful and is on the row"
        assert "no entry or stop yet" in text
        assert "LONG" not in text.split("status:")[0], "the placeholder must not be printed"
    finally:
        watcher.store.close()


def test_the_message_shows_the_trail_where_there_is_no_fixed_target(tmp_path):
    """`specs/06` §4.1's design: no T1, trailed from entry. The message must not read as "no exit"
    (F-068)."""
    row = {"signal_id": "x", "strategy_id": "C3-C", "asset": "B-BTC_USDT", "direction": "LONG",
           "state": "TRIGGERED", "entry": "83000", "stop": "82500", "targets": "[]",
           "trail_level": "82900", "created_at_ms": T0, "payload": {}, "detail": {}}
    text = format_message(row, stage=STAGE_POPULATED)
    assert "trailing stop: 82900" in text
    assert "no fixed target by design" in text


def test_the_message_states_that_nothing_is_placed_or_validated(tmp_path):
    """The same caveat the dashboard carries on every load. A notification arriving on a phone is
    read with less context than a dashboard, not more."""
    row = {"signal_id": "x", "strategy_id": "C3-C", "asset": "B-BTC_USDT", "direction": "SHORT",
           "state": "TRIGGERED", "entry": "112730", "stop": "113910", "targets": '["111500"]',
           "created_at_ms": T0, "payload": {}, "detail": {}}
    text = format_message(row, stage=STAGE_POPULATED)
    assert "never places, cancels or transfers" in text
    assert "not been validated" in text or "validated against history" in text


def test_the_message_times_are_ist():
    """The operator reading the message and the operator reading the dashboard must see the same
    clock (D-63), or they will not believe they are looking at the same suggestion."""
    assert ist(T0) == "2026-10-12 11:51 IST"
    # Midnight crossing, so the date moves with it.
    assert ist(T0 + 14 * 3600 * 1000).endswith("IST")
    assert ist(None) == "time unknown"
    assert ist("nonsense") == "time unknown"


def test_the_message_computes_no_trading_value():
    """Read and forward, nothing else. A level derived here would be a second implementation of the
    engine's rules, and the two would diverge."""
    import inspect

    source = inspect.getsource(format_message)
    for arithmetic in (" * atr", "Decimal(", " + s *", " - s *", "atr_1h"):
        assert arithmetic not in source, f"{arithmetic} suggests a computed level"


# ---- the three notifications, and nothing else (PHASE-38b) -----------------------------------

def test_the_whole_sequence_is_exactly_three_messages(seeded, tmp_path, monkeypatch):
    """**The acceptance criterion, end to end**, through every intermediate state the engine really
    passes through: armed blank -> resolved -> ACT trigger -> tracked -> target hit.

    Seven observations, three messages. The four silent ones are the two lifecycle transitions and
    the two repeat polls of an unchanged row.
    """
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))

    watcher = _watcher(seeded, tmp_path, primed=False)
    try:
        # 1. the notifier starts: records what exists, sends nothing.
        assert watcher.poll()["primed"] is True
        assert sent == []

        # A fresh setup arms, blank and pending — the dashboard shows it detected with no levels.
        store = open_store(seeded)
        _insert(store, "btc-seq", "C3-C", "B-BTC_USDT",
                detail={"pending_direction": True, "boundary": "112750.5"})
        store.conn.commit()
        store.close()

        # 2. MESSAGE 1 — setup detected, blank.
        assert watcher.poll()["sent"] == 1
        assert "SETUP DETECTED" in sent[0]
        assert "no entry or stop yet" in sent[0] and "direction not yet decided" in sent[0]

        # 3. nothing changed: silent.
        assert watcher.poll()["sent"] == 0

        # 4. MESSAGE 2 — the boundary resolves. Levels appear; the state does NOT move.
        _resolve(seeded, "btc-seq")
        assert watcher.poll()["sent"] == 1
        assert "SUGGESTION READY" in sent[1]
        assert all(v in sent[1] for v in ("112730", "113910", "111500"))

        # 5. price reaches the ACT trigger, then the entry is tracked: two state changes, silent.
        for state in ("ENTER_NOW", "TRIGGERED"):
            _set_state(seeded, state, "btc-seq")
            assert watcher.poll()["sent"] == 0, f"{state} must not notify"

        # 6. MESSAGE 3 — the target is hit.
        _set_state(seeded, "TARGET_HIT", "btc-seq")
        assert watcher.poll()["sent"] == 1
        assert "TARGET HIT" in sent[2]

        # 7. and it stays quiet afterwards.
        assert watcher.poll()["sent"] == 0

        assert len(sent) == 3, f"exactly three messages, got {len(sent)}"
        assert watcher.state.already("btc-seq") == {STAGE_DETECTED, STAGE_POPULATED, STAGE_FINAL}
    finally:
        watcher.store.close()


def test_a_stop_loss_hit_says_so_in_words(seeded, tmp_path, monkeypatch):
    """The other result. "STOPPED" is the lifecycle's word; a phone preview needs the meaning."""
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))
    _resolve(seeded, state="STOPPED")

    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED, STAGE_POPULATED]})
    try:
        assert watcher.poll()["sent"] == 1
        assert "STOP-LOSS HIT" in sent[0] and "TARGET HIT" not in sent[0]
        assert watcher.poll()["sent"] == 0, "a terminal row is observed forever and sent once"
    finally:
        watcher.store.close()


@pytest.mark.parametrize("state", ["EXPIRED", "INVALIDATED", "SUPPRESSED", "MISSED", "HANDED_OFF"])
def test_a_setup_ending_without_a_trade_is_not_notified(seeded, tmp_path, monkeypatch, state):
    """The lifecycle has seven terminal states; only TARGET_HIT and STOPPED are a *result*. A setup
    whose window elapsed is not news, and five extra message types would be exactly the noise the
    requirement is about."""
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))
    _resolve(seeded, state=state)

    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED, STAGE_POPULATED]})
    try:
        assert watcher.poll()["sent"] == 0
        assert sent == []
    finally:
        watcher.store.close()


def test_a_result_state_outranks_a_pending_suggestion(seeded, tmp_path):
    """If a setup is somehow first observed already stopped, the message must be the result — not a
    suggestion to take a trade that is already over."""
    row = {"signal_id": "x", "state": "STOPPED", "entry": "1", "stop": "2", "detail": {}}
    assert stage_due(row, set()) == STAGE_FINAL
    assert stage_due(row, {STAGE_FINAL}) is None
    # And a blank pending row owes only the detected message.
    pending = {"signal_id": "y", "state": "SETUP_DETECTED", "entry": None, "stop": None,
               "detail": {"pending_direction": True}}
    assert stage_due(pending, set()) == STAGE_DETECTED
    assert stage_due(pending, {STAGE_DETECTED}) is None


def test_each_new_setup_gets_its_own_sequence(seeded, tmp_path, monkeypatch):
    """Deduplication is per `signal_id`, so a completed setup cannot silence the next one."""
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))
    watcher = _watcher(seeded, tmp_path, stages={"btc-c3c": [STAGE_DETECTED, STAGE_POPULATED,
                                                             STAGE_FINAL]})
    try:
        store = open_store(seeded)
        _insert(store, "btc-next", "C3-C", "B-BTC_USDT",
                detail={"pending_direction": True, "boundary": "99000"})
        store.conn.commit()
        store.close()
        assert watcher.poll()["sent"] == 1 and "SETUP DETECTED" in sent[0]
        _resolve(seeded, "btc-next")
        assert watcher.poll()["sent"] == 1 and "SUGGESTION READY" in sent[1]
        assert len(sent) == 2
    finally:
        watcher.store.close()


# ---- what "populated" means, read from the dashboard's own fields ----------------------------

def test_a_pending_direction_is_not_populated_even_with_levels_present():
    """The engine stores `Direction.LONG` as a *placeholder* while C3-C waits for the resolution
    (`c3_transition.py:_arm`). Declaring the suggestion ready in that window would state a side the
    system has not chosen — the F-067 mistake, in a message this time."""
    row = {"entry": "112730", "stop": "113910", "detail": {"pending_direction": True}}
    assert is_populated(row) is False
    assert is_populated({**row, "detail": {"pending": True}}) is False
    assert is_populated({**row, "detail": {}}) is True


def test_an_entry_without_a_stop_is_not_populated():
    """Both, not either. A suggestion with an entry and no stop is not actionable, and reporting it
    as ready invites acting on half of one."""
    assert is_populated({"entry": "112730", "stop": None, "detail": {}}) is False
    assert is_populated({"entry": None, "stop": "113910", "detail": {}}) is False
    assert is_populated({"entry": "", "stop": "113910", "detail": {}}) is False
    # The column is TEXT and levels are stored as `str(Decimal)`, so a stringified None would
    # otherwise read as a published level.
    assert is_populated({"entry": "None", "stop": "113910", "detail": {}}) is False


def test_a_trailed_setup_with_no_fixed_target_still_counts_as_populated():
    """`specs/06` §4.1 mandates no fixed T1 for the trailed strategies, so requiring `targets` would
    mean a trailed setup never reached the populated stage and never got its message."""
    row = {"entry": "83000", "stop": "82500", "targets": [], "trail_level": "82900", "detail": {}}
    assert is_populated(row) is True


# ---- the values are the dashboard's values ---------------------------------------------------

def test_the_notifier_projects_a_row_exactly_as_the_live_endpoint_does(seeded, tmp_path):
    """**The basis for reading the table directly instead of calling `/api/signals/live`.**

    `services.py:_row_to_signal` — which is what the endpoint the dashboard polls returns — does no
    value transformation: it parses `payload` and `targets` as JSON, lifts `detail` to the top level
    and reads `tradeable` from the payload. This asserts field-for-field that the notifier's own
    projection of the same row agrees, so a message cannot show a number the dashboard does not.

    Executed against the real service, not asserted about its source, because a comment claiming
    equivalence is exactly what would survive the change that breaks it.
    """
    from api.services import SignalService

    _resolve(seeded)
    reader = ReadOnlyStore(seeded)
    try:
        watcher = Watcher(store=reader, config=_config(tmp_path))
        mine = watcher.qualifying_rows()[0]

        raw = reader.conn.execute("SELECT * FROM signals WHERE signal_id='btc-c3c'").fetchone()
        theirs = SignalService(store=reader)._row_to_signal(raw)

        for field_name in ("signal_id", "strategy_id", "asset", "direction", "state", "entry",
                           "stop", "targets", "trail_level", "created_at_ms", "state_since_ms",
                           "payload", "tradeable"):
            assert mine[field_name] == theirs[field_name], f"{field_name} differs from the dashboard"
        assert mine["detail"] == theirs["detail"], "detail is lifted the same way (F-056)"
    finally:
        reader.close()


def test_an_untradeable_row_is_filtered_exactly_as_the_endpoint_filters_it(seeded, tmp_path):
    """`specs/13` §13 forbids any endpoint returning `tradeable = False`, and `services.py:live`
    drops them — so a row this notifier let through would describe a suggestion the operator cannot
    see anywhere. Only the two controls are untradeable today and neither is C3-C; the guard is here
    so that cannot start to matter silently."""
    store = open_store(seeded)
    store.conn.execute(
        """UPDATE signals SET payload=json_set(payload, '$.tradeable', json('false'))
           WHERE signal_id='btc-c3c'""")
    store.conn.commit()
    store.close()

    watcher = _watcher(seeded, tmp_path)
    try:
        assert watcher.qualifying_rows() == [], "invisible on the dashboard, so invisible here"
    finally:
        watcher.store.close()


def test_the_live_endpoint_could_not_have_delivered_the_final_message(seeded, tmp_path):
    """Why the fix reads the table rather than switching to the endpoint the request suggested:
    `live()` returns only the three active states, so TARGET_HIT and STOPPED never appear in it.
    Consuming it would have made message 3 impossible."""
    import inspect

    from api.services import SignalService

    source = inspect.getsource(SignalService.live)
    assert "state IN ('SETUP_DETECTED','ENTER_NOW','TRIGGERED')" in source
    assert "TARGET_HIT" not in source and "STOPPED" not in source


# ---- the credential never leaks --------------------------------------------------------------

def test_the_token_is_redacted_wherever_it_could_be_printed():
    config = TelegramConfig(bot_token="123456:AAHsecretsecretsecret", chat_id="1")
    redacted = config.redacted_token
    assert "secret" not in redacted
    assert redacted.endswith("...cret)") or "ends ..." in redacted
    assert TelegramConfig(bot_token="", chat_id="1").redacted_token == "(unset)"


def test_a_transport_failure_never_puts_the_url_in_a_message(monkeypatch):
    """The URL contains the token, and `requests` exception text contains the URL. Only the
    exception *type* is reported."""
    import requests

    class Boom:
        def post(self, *a, **k):
            raise requests.ConnectionError("failed to connect to https://api.telegram.org/bot"
                                           "123456:SECRETTOKEN/sendMessage")

    result = send_message(TelegramConfig(bot_token="123456:SECRETTOKEN", chat_id="1"),
                          "hello", session=Boom())
    assert result.ok is False
    assert "SECRET" not in (result.error or "")
    assert "api.telegram.org" not in (result.error or "")


def test_the_sender_never_raises(monkeypatch):
    """The caller is a polling loop whose job is to keep running. A failed notification must be a
    retry and a log line, not a dead process that stops sending the next one."""
    import requests

    class Timeout:
        def post(self, *a, **k):
            raise requests.Timeout()

    result = send_message(TelegramConfig(bot_token="t", chat_id="1"), "x", session=Timeout())
    assert result.ok is False and result.retryable is True


@pytest.mark.parametrize("status,body,retryable", [
    (401, "", False),
    (403, "bot was blocked", False),
    (400, "Bad Request: chat not found", False),
    (429, "Too Many Requests", True),
    (502, "", True),
])
def test_telegram_failures_are_classified_by_whether_a_retry_could_help(status, body, retryable):
    """A wrong token fails identically for ever; a 429 does not. Retrying the first is noise in the
    log, and not retrying the second loses a notification."""
    class Response:
        status_code = status
        text = body

    class Session:
        def post(self, *a, **k):
            return Response()

    result = send_message(TelegramConfig(bot_token="t", chat_id="1"), "x", session=Session())
    assert result.ok is False
    assert result.retryable is retryable
    assert result.error


def test_a_long_message_is_truncated_rather_than_rejected_for_ever():
    """Telegram 400s anything over 4096 characters. An undeliverable message would retry for ever
    and block nothing else — but it would never arrive, so it is truncated instead."""
    sent = {}

    class Session:
        def post(self, url, json=None, timeout=None):
            sent["text"] = json["text"]

            class R:
                status_code = 200
                text = "{}"
            return R()

    send_message(TelegramConfig(bot_token="t", chat_id="1"), "x" * 9000, session=Session())
    assert len(sent["text"]) <= MAX_MESSAGE_CHARS + 20
    assert sent["text"].endswith("[truncated]")


def test_the_preflight_checks_credentials_without_messaging_anyone():
    """`getMe` proves the token works and costs nobody a notification, so `--check` can be run as
    often as an operator likes."""
    calls = []

    class Session:
        def get(self, url, timeout=None):
            calls.append(url)

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"result": {"username": "my_bot"}}
            return R()

    result = check_credentials(TelegramConfig(bot_token="t", chat_id="1"), session=Session())
    assert result.ok
    assert calls and calls[0].endswith("/getMe"), "getMe, not sendMessage"


# ---- isolation: it cannot disturb the running system ----------------------------------------

def test_the_notifier_cannot_write_to_the_engines_database(seeded, tmp_path):
    """Structural, not a promise. `ReadOnlyStore` opens `file:...?mode=ro`, so SQLite itself refuses
    a write — a bug in this package cannot corrupt the source of truth."""
    import sqlite3

    watcher = _watcher(seeded, tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            watcher.store.conn.execute("UPDATE signals SET state='X' WHERE signal_id='btc-c3c'")
    finally:
        watcher.store.close()


def test_nothing_in_the_existing_system_imports_the_notifier():
    """The feature was asked for on the condition that it cannot disturb what works. If no existing
    module imports it, it cannot — and `src/notify/` can be deleted with no other effect."""
    root = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for path in root.rglob("*.py"):
        if path.parts[-2] == "notify":
            continue
        text = path.read_text(encoding="utf-8")
        if "import notify" in text or "from notify" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"these modules import the notifier: {offenders}"


def test_the_notifier_writes_only_its_own_state_file():
    """The only file this package writes. No table, no migration, no second writer on the engine's
    database."""
    import inspect

    from notify import watcher as module

    source = inspect.getsource(module)
    for write in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE", "ALTER TABLE"):
        assert write not in source.upper().replace("UPDATE A", ""), f"{write} must not appear"


def test_the_engines_own_entrypoints_are_unchanged_by_this_feature():
    """The ingest and app processes must not have gained a Telegram step."""
    root = Path(__file__).resolve().parents[1] / "src"
    for entry in ("ingest/__main__.py", "app/__main__.py", "audit/trail.py"):
        text = (root / entry).read_text(encoding="utf-8")
        assert "telegram" not in text.lower(), f"{entry} references Telegram"


# ---- configuration -------------------------------------------------------------------------

def test_a_missing_token_or_chat_id_names_which_one(monkeypatch):
    """"Telegram is misconfigured" sends someone to read code; naming the variable sends them to the
    right line of their env file."""
    from core.errors import ConfigError
    from notify.config import load_config

    monkeypatch.delenv("CT_TELEGRAM__BOT_TOKEN", raising=False)
    monkeypatch.delenv("CT_TELEGRAM__CHAT_ID", raising=False)
    with pytest.raises(ConfigError, match="CT_TELEGRAM__BOT_TOKEN"):
        load_config()

    monkeypatch.setenv("CT_TELEGRAM__BOT_TOKEN", "123:abc")
    with pytest.raises(ConfigError, match="CT_TELEGRAM__CHAT_ID"):
        load_config()


def test_the_defaults_are_btcusdt_and_c3c(monkeypatch):
    from notify.config import load_config

    monkeypatch.setenv("CT_TELEGRAM__BOT_TOKEN", "123:abc")
    monkeypatch.setenv("CT_TELEGRAM__CHAT_ID", "42")
    monkeypatch.delenv("CT_TELEGRAM__ASSET", raising=False)
    monkeypatch.delenv("CT_TELEGRAM__STRATEGY", raising=False)
    config = load_config()
    assert config.asset == "B-BTC_USDT"
    assert config.strategy_id == "C3-C"
    assert config.poll_seconds == 30.0


def test_a_malformed_poll_interval_is_rejected_rather_than_defaulted(monkeypatch):
    from core.errors import ConfigError
    from notify.config import load_config

    monkeypatch.setenv("CT_TELEGRAM__BOT_TOKEN", "123:abc")
    monkeypatch.setenv("CT_TELEGRAM__CHAT_ID", "42")
    monkeypatch.setenv("CT_TELEGRAM__POLL_SECONDS", "every minute")
    with pytest.raises(ConfigError, match="POLL_SECONDS"):
        load_config()


def test_the_poll_interval_has_a_floor(monkeypatch):
    """A one-second poll would hammer the database the engine is writing to, for no benefit: the
    engine evaluates once a minute."""
    from notify.config import load_config

    monkeypatch.setenv("CT_TELEGRAM__BOT_TOKEN", "123:abc")
    monkeypatch.setenv("CT_TELEGRAM__CHAT_ID", "42")
    monkeypatch.setenv("CT_TELEGRAM__POLL_SECONDS", "1")
    assert load_config().poll_seconds == 5.0


# ---- the environment file (F-071) -----------------------------------------------------------
#
# The original guide told an operator to run `python -m notify --check` *before* installing the
# service. systemd reads `/etc/crypto-trader/env` when it starts a unit; a shell knows nothing about
# it. So the command saw none of the variables and reported the token as unset however correctly it
# had been entered — a step that could not work by construction.

ENV_SAMPLE = """\
# the real file has the API settings in it too
CT_ENVIRONMENT=azure
CT_SERVER__HOST=0.0.0.0
CT_SERVER__AUTH_TOKEN=an-unrelated-api-token

# ---- Telegram notifications: OPTIONAL ----
CT_TELEGRAM__BOT_TOKEN=8012345678:AAFhQ9x_example
CT_TELEGRAM__CHAT_ID=123456789
"""


def test_the_config_reads_the_systemd_environment_file_when_the_shell_has_not(tmp_path,
                                                                              monkeypatch):
    """The reported failure, reproduced and fixed: the values are in the file and not in the shell,
    which is exactly what an operator has after editing `/etc/crypto-trader/env`."""
    from notify.config import load_config

    monkeypatch.delenv("CT_TELEGRAM__BOT_TOKEN", raising=False)
    monkeypatch.delenv("CT_TELEGRAM__CHAT_ID", raising=False)
    env_file = tmp_path / "env"
    env_file.write_text(ENV_SAMPLE, encoding="utf-8")

    config = load_config(env_file=env_file)
    assert config.bot_token == "8012345678:AAFhQ9x_example"
    assert config.chat_id == "123456789"
    assert config.asset == "B-BTC_USDT" and config.strategy_id == "C3-C"


def test_a_real_environment_variable_beats_the_file(tmp_path, monkeypatch):
    """Under systemd the variable is already in the environment, so the service's behaviour must be
    exactly what it was before the file was ever consulted."""
    from notify.config import load_config

    env_file = tmp_path / "env"
    env_file.write_text(ENV_SAMPLE, encoding="utf-8")
    monkeypatch.setenv("CT_TELEGRAM__BOT_TOKEN", "from-the-environment")
    monkeypatch.setenv("CT_TELEGRAM__CHAT_ID", "999")

    config = load_config(env_file=env_file)
    assert config.bot_token == "from-the-environment"
    assert config.chat_id == "999"


def test_the_file_is_only_consulted_for_values_the_environment_lacks(tmp_path, monkeypatch):
    from notify.config import load_config

    env_file = tmp_path / "env"
    env_file.write_text(ENV_SAMPLE, encoding="utf-8")
    monkeypatch.setenv("CT_TELEGRAM__BOT_TOKEN", "from-the-environment")
    monkeypatch.delenv("CT_TELEGRAM__CHAT_ID", raising=False)

    config = load_config(env_file=env_file)
    assert config.bot_token == "from-the-environment"
    assert config.chat_id == "123456789", "the chat id still came from the file"


@pytest.mark.parametrize("line,expected", [
    ("CT_TELEGRAM__CHAT_ID=42", "42"),
    ('CT_TELEGRAM__CHAT_ID="42"', "42"),
    ("CT_TELEGRAM__CHAT_ID='42'", "42"),
    ("export CT_TELEGRAM__CHAT_ID=42", "42"),
    ("CT_TELEGRAM__CHAT_ID = 42", "42"),
    ("CT_TELEGRAM__CHAT_ID=-1001234567890", "-1001234567890"),
])
def test_the_forms_an_operator_might_plausibly_type_all_parse(line, expected):
    """An operator who writes quotes or `export` has done nothing wrong and should not end up with a
    chat id containing quotation marks. A negative group id keeps its minus sign."""
    from notify.config import parse_env_file

    assert parse_env_file(line)["CT_TELEGRAM__CHAT_ID"] == expected


def test_comments_and_junk_lines_are_skipped_not_fatal():
    """The file is shared with the API settings. A malformed line elsewhere in it must not stop the
    notifier reading the two values it came for."""
    from notify.config import parse_env_file

    values = parse_env_file("# a comment\n\nnot-a-pair\nCT_TELEGRAM__CHAT_ID=7\n=novalue\n")
    assert values == {"CT_TELEGRAM__CHAT_ID": "7"}


def test_a_token_with_a_colon_survives_parsing():
    """Telegram tokens contain a colon, and splitting on the wrong character would truncate one."""
    from notify.config import parse_env_file

    token = "8012345678:AAFhQ9x_with:another:colon"
    assert parse_env_file(f"CT_TELEGRAM__BOT_TOKEN={token}")["CT_TELEGRAM__BOT_TOKEN"] == token


def test_a_missing_file_says_so_rather_than_blaming_the_variable(tmp_path, monkeypatch):
    from core.errors import ConfigError
    from notify.config import load_config

    monkeypatch.delenv("CT_TELEGRAM__BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError) as caught:
        load_config(env_file=tmp_path / "absent")
    assert "does not exist" in caught.value.likely_cause
    assert caught.value.context["env_file_readable"] is False


def test_a_line_with_no_value_is_distinguished_from_a_missing_line(tmp_path, monkeypatch):
    """`CT_TELEGRAM__BOT_TOKEN=` with nothing after it is a different mistake from forgetting the
    line, and needs a different sentence to fix it."""
    from core.errors import ConfigError
    from notify.config import load_config

    monkeypatch.delenv("CT_TELEGRAM__BOT_TOKEN", raising=False)
    empty = tmp_path / "empty"
    empty.write_text("CT_TELEGRAM__BOT_TOKEN=\nCT_SERVER__HOST=0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(env_file=empty)
    assert "nothing after the '='" in caught.value.likely_cause

    absent = tmp_path / "absent-line"
    absent.write_text("CT_SERVER__HOST=0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught2:
        load_config(env_file=absent)
    assert "has no line for it" in caught2.value.likely_cause
    assert "1 other setting" in caught2.value.likely_cause, (
        "reporting that the file WAS read rules out a permissions problem at a glance"
    )


def test_an_unreadable_file_points_at_the_service_user(tmp_path, monkeypatch):
    """The real file is `root:crypto` mode 0640, so running the command as the wrong user is a
    plausible mistake with a specific fix."""
    from notify.config import load_env_file

    target = tmp_path / "env"
    target.write_text(ENV_SAMPLE, encoding="utf-8")

    def deny(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", deny)
    values, problem = load_env_file(target)
    assert values == {}
    assert "sudo -u crypto" in (problem or "")


def test_the_cli_accepts_an_env_file_and_the_preflight_names_the_source():
    """The preflight prints where the values came from, because "I set it and it says unset" was the
    entire failure and the answer is which source replied."""
    import inspect

    from notify import __main__ as cli

    source = inspect.getsource(cli)
    assert '"--env-file"' in source
    assert "values from:" in source
    assert "load_config(env_file=args.env_file)" in source


def test_the_default_env_file_is_the_one_systemd_uses():
    """If these two ever disagree, the command and the service read different configuration and the
    preflight stops proving anything about the service."""
    from pathlib import Path as P

    from notify.config import DEFAULT_ENV_FILE

    unit = (P(__file__).resolve().parents[1] / "deploy" / "azure"
            / "crypto-notify.service").read_text(encoding="utf-8")
    assert f"EnvironmentFile={DEFAULT_ENV_FILE.as_posix()}" in unit
