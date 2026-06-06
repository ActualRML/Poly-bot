"""Standalone Telegram /status listener — runs ALONGSIDE the trading bot.

Run in its own terminal:

    uv run tele_server.py

Long-polls the Telegram Bot API (getUpdates). When the *authorized* chat sends
"/status", it runs the existing read-only state dump and replies with the
result. That's the whole job.

Hard guarantees:
  * READ-ONLY. It calls scripts.check_state._dump() (which opens the DB
    mode=ro) and reuses src.notify.telegram.send_telegram. It never opens a
    writable DB handle, never imports trading/strategy code, never touches
    DRY_RUN.
  * SECURITY. Only messages whose chat.id matches the configured
    telegram_chat_id are acted on. Everything else is ignored silently, so
    anyone who discovers the bot cannot pull state.
  * NEVER CRASHES on a network blip / bad response / Telegram downtime — each
    poll is wrapped; on failure it logs a warning, sleeps briefly, and keeps
    looping. Ctrl+C exits cleanly.
"""
import contextlib
import io
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Project root on sys.path so `scripts.*` (namespace pkg) and `src.*` import
# regardless of cwd — mirrors the idiom in scripts/check_state.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.check_state import _dump  # noqa: E402
from src.config import Settings  # noqa: E402
from src.notify.telegram import send_telegram  # noqa: E402

log = logging.getLogger("tele_server")

# Telegram holds the getUpdates connection open this long waiting for traffic
# (long-poll). The socket timeout is a few seconds longer so a healthy idle
# poll returns on its own rather than tripping the client timeout.
_LONG_POLL_S = 30
_SOCKET_TIMEOUT_S = _LONG_POLL_S + 5
_RETRY_SLEEP_S = 3


def _get_updates(token: str, offset: int | None) -> list[dict]:
    """One long-poll getUpdates call. Returns the (possibly empty) update list.
    Raises on network/HTTP/JSON error — the caller decides how to recover."""
    params: dict[str, object] = {"timeout": _LONG_POLL_S}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=_SOCKET_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates returned not-ok: {data}")
    return data.get("result", []) or []


def _capture_dump() -> str:
    """Run the read-only dump and return its console output as a string.
    Never raises — on failure it returns an error note so the user still gets
    a reply instead of silence."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _dump()
    except Exception as e:  # noqa: BLE001 — a dump error must not kill the loop
        log.warning("dump failed: %s", e)
        buf.write(f"\n[dump error: {e}]")
    return buf.getvalue() or "(no output)"


def _handle_update(upd: dict, authorized_chat_id: str) -> None:
    """Act on a single update. Silently ignores anything from a non-authorized
    chat or any non-/status message."""
    msg = upd.get("message") or upd.get("edited_message")
    if not isinstance(msg, dict):
        return
    chat_id = (msg.get("chat") or {}).get("id")

    # SECURITY GATE: Telegram sends chat.id as an int, config stores it as a
    # str — normalize both sides before comparing. Mismatch => ignore silently.
    if chat_id is None or str(chat_id) != str(authorized_chat_id):
        log.info("ignoring message from unauthorized chat_id=%s", chat_id)
        return

    text = (msg.get("text") or "").strip()
    # Strip any "@botname" suffix Telegram appends to commands.
    command = text.split()[0].split("@")[0] if text else ""
    if command == "/status":
        log.info("/status from authorized chat; running read-only dump")
        send_telegram(_capture_dump())
    else:
        log.info("authorized chat sent non-command %r; ignoring", text)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    token = settings.telegram_bot_token
    authorized = settings.telegram_chat_id
    if not token or not authorized:
        log.error("telegram_bot_token/telegram_chat_id not configured; nothing to do")
        return

    log.info("listener started (authorized chat_id=%s); polling getUpdates...", authorized)
    offset: int | None = None
    try:
        while True:
            try:
                updates = _get_updates(token, offset)
            except Exception as e:  # noqa: BLE001 — network blip / downtime / bad JSON
                log.warning("getUpdates failed (%s); retry in %ds", e, _RETRY_SLEEP_S)
                time.sleep(_RETRY_SLEEP_S)
                continue
            for upd in updates:
                # Advance offset for EVERY update (even ignored ones) so it's
                # confirmed and never redelivered — including across restarts.
                offset = upd.get("update_id", 0) + 1
                try:
                    _handle_update(upd, authorized)
                except Exception as e:  # noqa: BLE001 — one bad update can't stop the loop
                    log.warning("handling update failed: %s", e)
    except KeyboardInterrupt:
        log.info("shutting down (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
