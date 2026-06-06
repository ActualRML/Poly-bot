"""Fire-and-forget Telegram notifier.

Plug-and-play: one public function, ``send_telegram(text) -> bool``. Pure
stdlib (urllib), so it adds no dependency to the project.

Defensive by design — the entire body is wrapped in try/except, every failure
path logs a warning and returns False, and it NEVER raises. A broken or slow
notification therefore cannot crash whatever called it (e.g. check_state, or
the bot itself). This is the load-bearing property: the goal is an
uninterrupted multi-day run, so a notifier hiccup must be a no-op, not a fault.
"""
import logging
import urllib.parse
import urllib.request

from src.config import Settings

log = logging.getLogger("notify.telegram")

# Telegram rejects messages over 4096 chars outright; truncate (with a visible
# marker) so an over-long summary still gets delivered minus its tail.
_MAX_LEN = 4096
_TRUNC_MARK = "\n…[truncated]"
_TIMEOUT_S = 5


def send_telegram(text: str) -> bool:
    """POST ``text`` to the configured Telegram chat via the Bot API.

    Returns True on a 2xx response, False on anything else (missing creds,
    network error, timeout, non-2xx HTTP, malformed input — anything at all).
    Never raises.
    """
    try:
        s = Settings()
        token = s.telegram_bot_token
        chat_id = s.telegram_chat_id
        if not token or not chat_id:
            # Missing config is not an error worth crashing for — just skip.
            log.warning("telegram not configured (bot_token/chat_id missing); skipping send")
            return False

        if len(text) > _MAX_LEN:
            text = text[: _MAX_LEN - len(_TRUNC_MARK)] + _TRUNC_MARK

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        # urlopen raises HTTPError on non-2xx (caught below); a clean return
        # here means Telegram accepted the message.
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            if 200 <= resp.status < 300:
                return True
            log.warning("telegram sendMessage returned HTTP %s", resp.status)
            return False
    except Exception as e:  # noqa: BLE001 — fire-and-forget: swallow everything
        log.warning("telegram send failed: %s", e)
        return False
