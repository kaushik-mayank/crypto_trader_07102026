"""The Telegram sender: one HTTP POST, classified failures, and no credential in any message.

Deliberately not routed through :mod:`cdx_http`. That client exists for the exchange — per-host rate
buckets, 429 halving, a distinct 503 maintenance path, raw payload capture for the recorder. None of
it applies to a notification endpoint, and reusing it would put Telegram traffic through the same
rate limiter the market data depends on.

``requests`` is already a project dependency (it is what `cdx_http` is built on), so this adds none.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from core.logging import get_logger
from notify.config import TelegramConfig

log = get_logger("notify.telegram")
COMPONENT = "notify.telegram"

API_BASE = "https://api.telegram.org"
#: Telegram rejects a message over 4096 characters with a 400. A suggestion never comes close, but a
#: long rationale plus an unexpected field could, and a message that can never be delivered would
#: retry for ever. Truncating is the only option that keeps the rest of the queue moving.
MAX_MESSAGE_CHARS = 3900


@dataclass
class SendResult:
    """What happened, in enough detail to act on without reading the log."""

    ok: bool
    status: int | None = None
    error: str | None = None
    #: True when retrying could plausibly succeed: a timeout, a network drop, a 429, a 5xx. False
    #: for a wrong token or a bad chat id, which will fail identically for ever.
    retryable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "error": self.error,
                "retryable": self.retryable}


def _describe(status: int, body: str) -> tuple[str, bool]:
    """A human cause and whether a retry is worth attempting, from Telegram's own status codes."""
    if status == 401:
        return ("Telegram rejected the bot token. Check CT_TELEGRAM__BOT_TOKEN against the value "
                "BotFather gave you — it is the whole string including the digits before the colon."), False
    if status == 400 and "chat not found" in body.lower():
        return ("Telegram says that chat does not exist. Check CT_TELEGRAM__CHAT_ID, and make sure "
                "you have sent the bot at least one message first — a bot cannot open a "
                "conversation."), False
    if status == 403:
        return ("Telegram says the bot is blocked or was never started by this chat. Open the chat "
                "and send /start to the bot."), False
    if status == 400:
        return f"Telegram rejected the request: {body[:200]}", False
    if status == 429:
        return "Telegram is rate limiting this bot; the next poll will retry.", True
    if 500 <= status < 600:
        return f"Telegram returned {status}; the next poll will retry.", True
    return f"Telegram returned an unexpected {status}: {body[:200]}", True


def send_message(config: TelegramConfig, text: str, *,
                 session: Any | None = None) -> SendResult:
    """Send one plain-text message. Returns a result; **never raises**.

    Never raises because the caller is a polling loop whose job is to keep running: a failed
    notification must become a retry and a log line, not a dead process that stops sending the
    *next* one.

    Plain text, with no ``parse_mode``. Markdown would need every price and underscore in an asset
    id escaped, and a missed escape is a 400 that silently drops the message — a formatting nicety
    is not worth a delivery failure.
    """
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "\n[truncated]"

    url = f"{API_BASE}/bot{config.bot_token}/sendMessage"
    payload = {"chat_id": config.chat_id, "text": text,
               "disable_notification": config.disable_notification,
               "disable_web_page_preview": True}
    http = session or requests
    try:
        response = http.post(url, json=payload, timeout=config.timeout_seconds)
    except requests.Timeout:
        log.warning("telegram_timeout", timeout_s=config.timeout_seconds,
                    detail="the next poll will retry")
        return SendResult(ok=False, error="the request to Telegram timed out", retryable=True)
    except requests.RequestException as e:
        # The exception text can contain the URL, and the URL contains the token. Only the type is
        # logged, never the message.
        log.warning("telegram_unreachable", error_type=type(e).__name__,
                    detail="no network path to api.telegram.org; the next poll will retry")
        return SendResult(ok=False, error=f"could not reach Telegram ({type(e).__name__})",
                          retryable=True)

    status = int(getattr(response, "status_code", 0) or 0)
    body = ""
    try:
        body = response.text or ""
    except Exception:                               # noqa: BLE001 - body is only for the message
        body = ""

    if 200 <= status < 300:
        log.info("telegram_sent", status=status, chars=len(text))
        return SendResult(ok=True, status=status)

    reason, retryable = _describe(status, body)
    log.error("telegram_send_failed", status=status, reason=reason, retryable=retryable,
              token=config.redacted_token, chat_id=config.chat_id,
              remediation="see TELEGRAM_HOSTING.md, Troubleshooting")
    return SendResult(ok=False, status=status, error=reason, retryable=retryable)


def check_credentials(config: TelegramConfig, *, session: Any | None = None) -> SendResult:
    """Ask Telegram who this bot is, without sending anyone a message.

    `getMe` is the right preflight: it proves the token works and costs nobody a notification, so
    `python -m notify --check` can be run as often as an operator likes.
    """
    url = f"{API_BASE}/bot{config.bot_token}/getMe"
    http = session or requests
    try:
        response = http.get(url, timeout=config.timeout_seconds)
    except requests.RequestException as e:
        return SendResult(ok=False, error=f"could not reach Telegram ({type(e).__name__})",
                          retryable=True)
    status = int(getattr(response, "status_code", 0) or 0)
    if 200 <= status < 300:
        try:
            username = response.json().get("result", {}).get("username")
        except Exception:                           # noqa: BLE001 - informational only
            username = None
        log.info("telegram_credentials_ok", bot=username)
        return SendResult(ok=True, status=status, error=None)
    reason, retryable = _describe(status, getattr(response, "text", "") or "")
    return SendResult(ok=False, status=status, error=reason, retryable=retryable)
