"""Forward BTCUSDT C3-C suggestions to Telegram. A separate, read-only process.

    python -m notify --check      # prove the setup works, send nothing, exit
    python -m notify --once       # one poll, then exit
    python -m notify --dry-run    # print the messages it would send, send nothing
    python -m notify              # run until stopped

**This process cannot affect the suggestion engine.** It opens the database read-only, it runs
outside both existing services, and the only file it writes is its own cursor under
``data/notify/``. Stopping it, breaking it or deleting it changes nothing about how suggestions are
generated, stored or displayed.

Deployment and setup: ``TELEGRAM_HOSTING.md``.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Any

from config.settings import Settings
from core.errors import ALL_ERRORS, ConfigError
from core.logging import get_logger, setup_logging
from notify.config import (
    DEFAULT_ASSET,
    DEFAULT_ENV_FILE,
    DEFAULT_STRATEGY,
    load_config,
    load_env_file,
)
from notify.telegram import check_credentials, send_message
from notify.watcher import Watcher, ist, load_state
from store.reader import ReadOnlyStore

log = get_logger("notify.main")


def _open(settings: Settings) -> ReadOnlyStore:
    """A read-only handle on the engine's database.

    `ReadOnlyStore` refuses a path that does not exist rather than creating one — a notifier that
    created an empty database would then poll it for ever and report, truthfully and uselessly, that
    nothing has happened.
    """
    return ReadOnlyStore(settings.paths.state_db)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="forward BTCUSDT C3-C suggestion state changes to Telegram (read-only)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="verify the configuration, the database and the bot token, then exit. "
                           "Sends no message to anyone")
    mode.add_argument("--once", action="store_true", help="one poll, then exit")
    mode.add_argument("--dry-run", action="store_true",
                      help="print the messages that would be sent, send nothing, and do not record "
                           "them as sent")
    ap.add_argument("--env-file", type=Path, default=None,
                    help=f"where to read CT_TELEGRAM__* from when they are not already in the "
                         f"environment. Defaults to {DEFAULT_ENV_FILE}, which is the file systemd "
                         f"gives the services — so this command sees the same values the service "
                         f"does")
    ap.add_argument("--test-message", action="store_true",
                    help="send one plain test message to the configured chat and exit. The only "
                         "mode that sends anything not derived from a real suggestion")
    args = ap.parse_args(argv)

    settings = Settings()
    setup_logging(settings.paths.log_dir, log_name="notify")

    try:
        config = load_config(env_file=args.env_file)
    except ConfigError as e:
        print(f"TELEGRAM NOTIFIER NOT CONFIGURED: {e.message}", file=sys.stderr)
        print(f"  likely cause: {e.likely_cause}", file=sys.stderr)
        print(f"  fix: {e.remediation}", file=sys.stderr)
        return 2

    log.info("notify_starting", asset=config.asset, strategy=config.strategy_id,
             poll_seconds=config.poll_seconds, state_file=str(config.state_file),
             token=config.redacted_token, chat_id=config.chat_id,
             detail="read-only: this process cannot write to the engine's database")

    if args.test_message:
        result = send_message(config, "crypto-trader: Telegram notifications are connected.\n"
                                      f"Watching {config.asset} {config.strategy_id} only.\n"
                                      "This is a test message, not a suggestion.")
        print("test message sent" if result.ok else f"TEST MESSAGE FAILED: {result.error}")
        return 0 if result.ok else 1

    try:
        store = _open(settings)
    except ALL_ERRORS as e:
        print(f"CANNOT READ THE DATABASE: {e}", file=sys.stderr)
        print("  fix: the engine creates it. Start crypto-ingest first, then this service.",
              file=sys.stderr)
        return 2

    try:
        state = load_state(config.state_file)
        watcher = Watcher(store=store, config=config, state=state)

        if args.check:
            return _preflight(watcher, config, env_file=args.env_file or DEFAULT_ENV_FILE)

        if args.once or args.dry_run:
            report = watcher.poll(dry_run=args.dry_run)
            print(f"qualifying={report['qualifying']} sent={report['sent']} "
                  f"failed={report['failed']} skipped={report['skipped']}"
                  + ("  (primed: first run records what exists and sends nothing)"
                     if report["primed"] else ""))
            return 0

        return _run(watcher, config)
    finally:
        store.close()


def _preflight(watcher: Watcher, config: Any, *, env_file: Path) -> int:
    """Prove every link in the chain without notifying anyone.

    Each line is a separate check so the first failure names the thing to fix, rather than leaving
    an operator with "it does not work".
    """
    ok = True
    print("Telegram notifier preflight")
    print(f"  watching:      {config.asset}  {config.strategy_id}  (and nothing else)")
    print(f"  bot token:     {config.redacted_token}")
    print(f"  chat id:       {config.chat_id}")
    print(f"  state file:    {config.state_file}")
    print(f"  poll interval: {config.poll_seconds:.0f}s")

    # Where the values came from. The original failure was an operator correctly setting the
    # variables in the systemd environment file and this command — which does not read it when run
    # by hand — reporting them unset. Saying which source answered removes that whole class of
    # confusion (F-071).
    import os as _os

    file_values, problem = load_env_file(env_file)
    if "CT_TELEGRAM__BOT_TOKEN" in _os.environ:
        print("  values from:   this shell's environment")
    elif problem:
        print(f"  values from:   nowhere — {problem}")
    else:
        print(f"  values from:   {env_file}  ({len(file_values)} setting(s) in it)")

    if config.asset != DEFAULT_ASSET or config.strategy_id != DEFAULT_STRATEGY:
        print(f"  NOTE: the filter has been overridden. The default is {DEFAULT_ASSET} "
              f"{DEFAULT_STRATEGY}.")

    try:
        rows = watcher.qualifying_rows()
        print(f"  OK   database readable: {len(rows)} {config.strategy_id} "
              f"{config.asset} suggestion(s) on record")
        if rows:
            newest = rows[0]
            print(f"       newest: {newest['state']}, suggested "
                  f"{ist(newest['created_at_ms'])}")
    except Exception as e:                          # noqa: BLE001 - preflight reports, never raises
        ok = False
        print(f"  FAIL database could not be read: {e}")

    credentials = check_credentials(config)
    if credentials.ok:
        print("  OK   Telegram accepted the bot token")
    else:
        ok = False
        print(f"  FAIL {credentials.error}")

    if watcher.state.primed:
        print(f"  OK   state file primed: tracking {len(watcher.state.stages)} signal(s)")
    else:
        print("  NOTE not primed yet. The first real poll records what already exists and sends "
              "nothing, so starting the service will not replay history.")

    print("\nEverything checks out." if ok else
          "\nSomething above failed. See TELEGRAM_HOSTING.md, Troubleshooting.")
    return 0 if ok else 1


def _run(watcher: Watcher, config: Any) -> int:
    """Poll until stopped. One failed poll never ends the run.

    The loop survives everything: a classified error, an unclassified one, a database lock, a
    Telegram outage. A notifier that exits on the first problem stops notifying about the problem it
    exists to notify about.
    """
    stopping = False

    def stop(signum: int, _frame: Any) -> None:
        nonlocal stopping
        log.info("notify_stopping", signal=signum)
        stopping = True

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, stop)
            except (OSError, ValueError):            # pragma: no cover - platform guard
                pass

    log.info("notify_running", poll_seconds=config.poll_seconds)
    while not stopping:
        started = time.monotonic()
        try:
            watcher.poll()
        except ALL_ERRORS as e:
            log.error("notify_poll_failed", error=e.to_dict(),
                      detail="the loop continues; the next poll retries")
        except Exception as e:                       # noqa: BLE001 - the loop must survive
            log.error("notify_poll_crashed", error=str(e), error_type=type(e).__name__,
                      detail="unclassified failure; the loop continues")
        # Sleep the remainder, so a slow poll does not push the cadence out.
        remaining = config.poll_seconds - (time.monotonic() - started)
        while remaining > 0 and not stopping:
            time.sleep(min(1.0, remaining))
            remaining -= 1.0

    log.info("notify_stopped", sent=watcher.sent, failures=watcher.failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
