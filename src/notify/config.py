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
from core.logging import get_logger

log = get_logger("notify.config")
COMPONENT = "notify.config"

#: The systemd ``EnvironmentFile`` the deployed services are given.
#:
#: **Read here as a fallback, and that is a fix with a real cause.** systemd loads this file when it
#: *starts a unit*; a shell knows nothing about it. So
#: ``sudo -u crypto ./.venv/bin/python -m notify --check`` -- which `TELEGRAM_HOSTING.md` told an
#: operator to run *before* installing the service -- saw none of the variables and reported the
#: token as unset however correctly it had been entered. The guide contained a step that could not
#: work by construction (F-071).
#:
#: Loading it here makes one command behave the same whether systemd ran it or a person did, which
#: is the property that was missing. **Real environment variables always win**, so nothing about the
#: running service changes: systemd has already injected them by the time this is reached.
DEFAULT_ENV_FILE = Path("/etc/crypto-trader/env")

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


def parse_env_file(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines, the subset systemd's ``EnvironmentFile`` actually supports.

    Comments, blank lines and an optional ``export`` prefix are tolerated, and one layer of matching
    quotes is stripped -- because an operator who writes ``CT_TELEGRAM__CHAT_ID="42"`` has done
    nothing wrong and should not end up with a chat id containing quotation marks.

    An unparseable line is skipped rather than raising: a malformed line elsewhere in a shared file
    must not stop the notifier reading the two values it came for.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", chr(34)):
            value = value[1:-1]
        out[key] = value
    return out


def load_env_file(path: Path | None = None) -> tuple[dict[str, str], str | None]:
    """``(values, problem)`` from the environment file. Never raises.

    ``problem`` is a sentence for a human when the file could not be used, so the error below can
    say *which* of several quite different situations applies -- a missing file, a permissions
    problem, and a file with no such line are three different fixes.
    """
    target = path or DEFAULT_ENV_FILE
    try:
        return parse_env_file(target.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return {}, f"{target} does not exist"
    except PermissionError:
        return {}, (f"{target} exists but this user cannot read it. It is owned by root and "
                    f"readable by the 'crypto' group, so run the command with 'sudo -u crypto ...'")
    except OSError as e:
        return {}, f"{target} could not be read ({e.__class__.__name__})"


def _env(name: str, default: str | None = None, *,
         file_values: dict[str, str] | None = None) -> str | None:
    """A value from the real environment, then the environment file, then the default.

    The order matters: under systemd the variable is already in the environment, so the file is
    never consulted and the service's behaviour is exactly what it was.
    """
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    if file_values:
        from_file = file_values.get(name)
        if from_file and from_file.strip():
            return from_file.strip()
    return default


def _missing(name: str, what: str, hint: str, *, env_file: Path,
             file_values: dict[str, str], problem: str | None) -> ConfigError:
    """The error for an absent value, naming the situation that actually applies.

    Four situations look identical from outside and need four different fixes, so each is
    distinguished rather than collapsed into "it is misconfigured".
    """
    if problem:
        cause = problem
        fix = (f"{hint} Then make sure the file is readable by the user running this: "
               f"prefix the command with 'sudo -u crypto'.")
    elif name in file_values:
        cause = f"{env_file} has a line for {name} but nothing after the '='"
        fix = f"{hint} Put the value immediately after the '=', with no spaces and no quotes."
    else:
        cause = (f"it is not in this shell's environment, and {env_file} has no line for it "
                 f"(the file was read and holds {len(file_values)} other setting(s))")
        fix = hint
    return ConfigError(
        f"{name} is not set, so there is {what}",
        component=COMPONENT, operation="load_config",
        context={"variable": name, "env_file": str(env_file),
                 "env_file_readable": problem is None},
        likely_cause=cause, remediation=fix)


def load_config(*, env_file: Path | None = None) -> TelegramConfig:
    """Build the configuration, failing loudly and specifically when something is missing.

    Values come from the real environment first and the environment file second, so this returns the
    same answer whether systemd started the process or a person ran the command by hand.
    """
    target = env_file or DEFAULT_ENV_FILE
    file_values, problem = load_env_file(target)

    def read(name: str, default: str | None = None) -> str | None:
        return _env(name, default, file_values=file_values)

    token = read("CT_TELEGRAM__BOT_TOKEN")
    chat_id = read("CT_TELEGRAM__CHAT_ID")

    if token and "CT_TELEGRAM__BOT_TOKEN" not in os.environ:
        log.info("notify_env_file_used", env_file=str(target), settings=len(file_values),
                 detail="the token came from the environment file, because it is not in this "
                        "process's environment. Under systemd it always is, and the file is not "
                        "consulted")

    if not token:
        raise _missing(
            "CT_TELEGRAM__BOT_TOKEN", "no bot to send from",
            "Add CT_TELEGRAM__BOT_TOKEN=<the token BotFather gave you> to the file, then restart "
            "crypto-notify. See TELEGRAM_HOSTING.md steps 1 and 4.",
            env_file=target, file_values=file_values, problem=problem)
    if not chat_id:
        raise _missing(
            "CT_TELEGRAM__CHAT_ID", "nobody to send to",
            "Add CT_TELEGRAM__CHAT_ID=<your chat id> to the file. TELEGRAM_HOSTING.md step 3 shows "
            "how to find it.",
            env_file=target, file_values=file_values, problem=problem)

    poll = read("CT_TELEGRAM__POLL_SECONDS", "30")
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

    state_file = Path(read("CT_TELEGRAM__STATE_FILE", "data/notify/telegram_state.json") or "")
    return TelegramConfig(
        bot_token=token,
        chat_id=chat_id,
        asset=normalise_asset(read("CT_TELEGRAM__ASSET", DEFAULT_ASSET) or DEFAULT_ASSET),
        strategy_id=(read("CT_TELEGRAM__STRATEGY", DEFAULT_STRATEGY) or DEFAULT_STRATEGY).upper(),
        poll_seconds=poll_seconds,
        state_file=state_file,
        disable_notification=(read("CT_TELEGRAM__SILENT", "false") or "false").lower()
        in ("1", "true", "yes"),
    )
