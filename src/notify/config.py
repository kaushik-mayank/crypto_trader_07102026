"""Configuration for the Telegram notifier, read from the environment.

**Why this does not live in `config/settings.py`.** That file is loaded by the ingest process, the
app process, the backtest harness and the probes. Adding a field to it would mean touching a module
every working part of the system imports, for a feature none of them use. Reading the environment
here keeps the notifier genuinely separable: delete `src/notify/` and nothing else notices.

The variable names follow the project's existing `CT_<SECTION>__<FIELD>` convention, so an operator
who has set `CT_SERVER__AUTH_TOKEN` already knows the shape.

**The token is never logged, never printed and never put in an error message.** `redacted()` is the
only way this module will show it, and it shows a length and the last four characters — enough to
tell two tokens apart, not enough to use one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from core.errors import ConfigError

COMPONENT = "notify.config"

#: The asset this notifier is restricted to. "BTCUSDT" in the request; `B-BTC_USDT` is what the
#: engine calls it, and what the `signals.asset` column holds (see `snapshot/request.py:ASSETS`).
DEFAULT_ASSET = "B-BTC_USDT"
#: The strategy this notifier is restricted to.
DEFAULT_STRATEGY = "C3-C"

#: Spellings an operator might reasonably type for the one supported asset, mapped to the engine's.
#: Accepting these is not scope creep — it is refusing to let a plausible typo silently match
#: nothing, which would look exactly like "no suggestions have happened yet".
_ASSET_ALIASES = {
    "BTCUSDT": DEFAULT_ASSET,
    "BTC-USDT": DEFAULT_ASSET,
    "BTC/USDT": DEFAULT_ASSET,
    "BTC_USDT": DEFAULT_ASSET,
    "B-BTC_USDT": DEFAULT_ASSET,
    "BTC": DEFAULT_ASSET,
}


def normalise_asset(value: str) -> str:
    """The engine's asset id for a user-supplied spelling, or the value unchanged if unrecognised.

    Unrecognised values pass through rather than raising: a future operator restricting this to a
    different asset should not have to edit the alias table, and the preflight reports what the
    filter resolved to so a mistake is visible before any polling starts.
    """
    return _ASSET_ALIASES.get(value.strip().upper(), value.strip())


@dataclass(frozen=True)
class TelegramConfig:
    """Everything the notifier needs. Frozen: nothing reconfigures itself while running."""

    bot_token: str
    chat_id: str
    asset: str = DEFAULT_ASSET
    strategy_id: str = DEFAULT_STRATEGY
    poll_seconds: float = 30.0
    #: Where the notifier remembers what it has already sent. Its own file, in its own directory:
    #: no table, no migration, no write to the engine's database.
    state_file: Path = Path("data/notify/telegram_state.json")
    #: Timeout for one Telegram request. Short on purpose — this is a notifier, and a hung HTTP call
    #: should become a retry on the next poll rather than a stalled process.
    timeout_seconds: float = 10.0
    #: Sent to Telegram but muted. The default is off; a quiet night is worth more than a chime.
    disable_notification: bool = False

    @property
    def redacted_token(self) -> str:
        """The token as it may appear in a log: its length and last four characters."""
        if not self.bot_token:
            return "(unset)"
        return f"(len {len(self.bot_token)}, ends ...{self.bot_token[-4:]})"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def load_config() -> TelegramConfig:
    """Build the configuration, failing loudly and specifically when something is missing.

    Two separate checks rather than one, because "Telegram is misconfigured" sends someone to read
    code and "CT_TELEGRAM__CHAT_ID is not set" sends them to the right line of their env file.
    """
    token = _env("CT_TELEGRAM__BOT_TOKEN")
    chat_id = _env("CT_TELEGRAM__CHAT_ID")

    if not token:
        raise ConfigError(
            "CT_TELEGRAM__BOT_TOKEN is not set, so there is no bot to send from",
            component=COMPONENT, operation="load_config",
            likely_cause="the environment variable is missing from /etc/crypto-trader/env",
            remediation="add CT_TELEGRAM__BOT_TOKEN=<the token BotFather gave you> to "
                        "/etc/crypto-trader/env, then restart crypto-notify. See TELEGRAM_HOSTING.md",
        )
    if not chat_id:
        raise ConfigError(
            "CT_TELEGRAM__CHAT_ID is not set, so there is nobody to send to",
            component=COMPONENT, operation="load_config",
            likely_cause="the environment variable is missing from /etc/crypto-trader/env",
            remediation="add CT_TELEGRAM__CHAT_ID=<your chat id> to /etc/crypto-trader/env. "
                        "TELEGRAM_HOSTING.md step 3 shows how to find it",
        )

    poll = _env("CT_TELEGRAM__POLL_SECONDS", "30")
    try:
        poll_seconds = max(5.0, float(poll or 30))
    except ValueError as e:
        raise ConfigError(
            f"CT_TELEGRAM__POLL_SECONDS is {poll!r}, which is not a number of seconds",
            component=COMPONENT, operation="load_config",
            likely_cause="a typo in the environment file",
            remediation="set it to a whole number of seconds, or remove it to use the default of 30",
            cause=e,
        ) from e

    state_file = Path(_env("CT_TELEGRAM__STATE_FILE", "data/notify/telegram_state.json") or "")
    return TelegramConfig(
        bot_token=token,
        chat_id=chat_id,
        asset=normalise_asset(_env("CT_TELEGRAM__ASSET", DEFAULT_ASSET) or DEFAULT_ASSET),
        strategy_id=(_env("CT_TELEGRAM__STRATEGY", DEFAULT_STRATEGY) or DEFAULT_STRATEGY).upper(),
        poll_seconds=poll_seconds,
        state_file=state_file,
        disable_notification=(_env("CT_TELEGRAM__SILENT", "false") or "false").lower()
        in ("1", "true", "yes"),
    )
