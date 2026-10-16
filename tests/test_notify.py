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
from notify.watcher import Watcher, WatcherState, format_message, ist, load_state, save_state
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


def _watcher(path, tmp_path, *, primed=True, seen=None, **over):
    reader = ReadOnlyStore(path)
    return Watcher(store=reader, config=_config(tmp_path, **over),
                   state=WatcherState(seen=dict(seen or {}), primed=primed))


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

    watcher = _watcher(seeded, tmp_path, seen={"btc-c3c": "SETUP_DETECTED"})
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
        assert watcher.state.seen == {"btc-c3c": "SETUP_DETECTED"}
    finally:
        watcher.store.close()


def test_a_state_change_is_detected_once_and_not_repeated(seeded, tmp_path, monkeypatch):
    """The whole behaviour: notify on a change, then stay quiet until the next one."""
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))

    store = open_store(seeded)
    store.conn.execute("UPDATE signals SET state='TRIGGERED' WHERE signal_id='btc-c3c'")
    store.conn.commit()
    store.close()

    watcher = _watcher(seeded, tmp_path, seen={"btc-c3c": "SETUP_DETECTED"})
    try:
        assert watcher.poll()["sent"] == 1
        assert len(sent) == 1 and "TRIGGERED" in sent[0] and "was SETUP_DETECTED" in sent[0]
        # Nothing changed since: silent.
        assert watcher.poll()["sent"] == 0
        assert len(sent) == 1
    finally:
        watcher.store.close()


def test_a_new_qualifying_signal_notifies_even_with_no_state_change(seeded, tmp_path, monkeypatch):
    """A suggestion appearing is the event; its state has not "changed" because there was no
    previous one."""
    sent = []
    monkeypatch.setattr("notify.watcher.send_message",
                        lambda cfg, text: sent.append(text) or SendResult(ok=True, status=200))
    store = open_store(seeded)
    _insert(store, "btc-c3c-2", "C3-C", "B-BTC_USDT", state="ENTER_NOW",
            entry="83000", stop="82500", targets=("84000",))
    store.conn.commit()
    store.close()

    watcher = _watcher(seeded, tmp_path, seen={"btc-c3c": "SETUP_DETECTED"})
    try:
        assert watcher.poll()["sent"] == 1
        assert "(new)" in sent[0] and "ENTER_NOW" in sent[0]
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
    store = open_store(seeded)
    store.conn.execute("UPDATE signals SET state='TRIGGERED' WHERE signal_id='btc-c3c'")
    store.conn.commit()
    store.close()

    watcher = _watcher(seeded, tmp_path, seen={"btc-c3c": "SETUP_DETECTED"})
    try:
        assert watcher.poll()["failed"] == 1
        assert watcher.state.seen["btc-c3c"] == "SETUP_DETECTED", "not recorded as sent"
        assert watcher.poll()["failed"] == 1
        assert attempts["n"] == 2, "the next poll tried again"
    finally:
        watcher.store.close()


def test_one_undeliverable_signal_does_not_block_the_others(seeded, tmp_path, monkeypatch):
    """Each signal is tracked independently, so a message that can never be delivered cannot wedge
    the queue behind it."""
    def selective(cfg, text):
        return SendResult(ok=not text.startswith("BTCUSDT · C3-C · bad"), status=200)

    monkeypatch.setattr("notify.watcher.send_message", selective)
    store = open_store(seeded)
    store.conn.execute("UPDATE signals SET state='TRIGGERED' WHERE signal_id='btc-c3c'")
    _insert(store, "btc-bad", "C3-C", "B-BTC_USDT", direction="bad", state="TRIGGERED")
    store.conn.commit()
    store.close()

    watcher = _watcher(seeded, tmp_path, seen={"btc-c3c": "SETUP_DETECTED"})
    try:
        report = watcher.poll()
        assert report["sent"] == 1 and report["failed"] == 1
        assert watcher.state.seen["btc-c3c"] == "TRIGGERED", "the deliverable one went through"
        assert "btc-bad" not in watcher.state.seen
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
    assert state.seen == {} and state.primed is False


def test_the_state_file_round_trips(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, WatcherState(seen={"a": "TRIGGERED"}, primed=True))
    back = load_state(path)
    assert back.seen == {"a": "TRIGGERED"} and back.primed is True


# ---- the message is read, never computed ----------------------------------------------------

def test_the_message_reports_a_pending_setup_as_undecided(seeded, tmp_path):
    """C3-C arms pending and the stored direction is a structural placeholder (F-067). Printing it
    as a recommendation is exactly the mistake the dashboard made."""
    watcher = _watcher(seeded, tmp_path)
    try:
        row = watcher.qualifying_rows()[0]
        text = format_message(row, previous_state=None)
        assert "direction not yet decided" in text
        assert "112750.5" in text, "the level being watched is useful and is on the row"
        assert "no entry or stop yet" in text
        assert "LONG" not in text.split("state:")[0], "the placeholder must not be printed"
    finally:
        watcher.store.close()


def test_the_message_shows_the_trail_where_there_is_no_fixed_target(tmp_path):
    """`specs/06` §4.1's design: no T1, trailed from entry. The message must not read as "no exit"
    (F-068)."""
    row = {"signal_id": "x", "strategy_id": "C3-C", "asset": "B-BTC_USDT", "direction": "LONG",
           "state": "TRIGGERED", "entry": "83000", "stop": "82500", "targets": "[]",
           "trail_level": "82900", "created_at_ms": T0, "payload": {}, "detail": {}}
    text = format_message(row, previous_state="ENTER_NOW")
    assert "trailing stop: 82900" in text
    assert "no fixed target by design" in text


def test_the_message_states_that_nothing_is_placed_or_validated(tmp_path):
    """The same caveat the dashboard carries on every load. A notification arriving on a phone is
    read with less context than a dashboard, not more."""
    row = {"signal_id": "x", "strategy_id": "C3-C", "asset": "B-BTC_USDT", "direction": "SHORT",
           "state": "TRIGGERED", "entry": "112730", "stop": "113910", "targets": '["111500"]',
           "created_at_ms": T0, "payload": {}, "detail": {}}
    text = format_message(row, previous_state=None)
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
