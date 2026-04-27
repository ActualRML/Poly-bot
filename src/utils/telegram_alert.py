"""
src/utils/telegram_alert.py
============================
Telegram alert untuk Polymarket bot.

Kirim notifikasi ke Telegram saat:
- Ada signal / posisi baru dibuka
- Posisi di-exit (profit/loss)
- Circuit breaker trigger
- Error critical

Setup:
1. Tambah ke .env:
   TELEGRAM_BOT_TOKEN=xxx
   TELEGRAM_CHAT_ID=xxx

2. Import dan pakai di main.py:
   from src.utils.telegram_alert import TelegramAlert
"""

import asyncio
import aiohttp
import html
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class TelegramAlert:
    """
    Kirim notifikasi ke Telegram secara async.
    Graceful — kalau gagal, bot tetap jalan (tidak crash).
    """

    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self._url    = f"https://api.telegram.org/bot{token}/sendMessage"

        if self.enabled:
            logger.info("[TELEGRAM] Alert aktif ✅")
        else:
            logger.warning("[TELEGRAM] Token/Chat ID tidak ada — alert dinonaktifkan")

    async def send(self, message: str, session: aiohttp.ClientSession) -> bool:
        """
        Kirim pesan ke Telegram.
        Return True kalau berhasil, False kalau gagal.
        """
        if not self.enabled:
            return False

        try:
            async with session.post(
                self._url,
                json={
                    "chat_id":    self.chat_id,
                    "text":       message,
                    "parse_mode": "HTML",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return True
                else:
                    logger.warning(f"[TELEGRAM] Gagal kirim: status {resp.status}")
                    return False

        except Exception as e:
            logger.warning(f"[TELEGRAM] Error: {e}")
            return False

    # ── Alert Templates ───────────────────────────────────────────

    async def alert_signal(
        self,
        question: str,
        outcome: str,
        price: float,
        bet_usdc: float,
        gap_pct: float,
        ev: float,
        session: aiohttp.ClientSession,
        dry_run: bool = True,
    ):
        """Alert saat ada signal / posisi baru dibuka."""
        mode = "🔸 DRY RUN" if dry_run else "🟢 LIVE"
        msg = (
            f"{mode} — <b>POSISI BARU</b>\n\n"
            f"📌 <b>{html.escape(question[:60])}</b>\n\n"
            f"🎯 Outcome  : <b>BUY {html.escape(outcome)}</b> @ {price:.3f}\n"
            f"💰 Bet      : <b>${bet_usdc:.2f}</b>\n"
            f"📊 Edge     : {gap_pct:.1f}%\n"
            f"⚡ EV       : {ev:.3f}\n\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await self.send(msg, session)

    async def alert_exit(
        self,
        question: str,
        outcome: str,
        entry_price: float,
        exit_price: float,
        pnl_usdc: float,
        reason: str,
        session: aiohttp.ClientSession,
    ):
        """Alert saat posisi di-exit."""
        emoji = "✅" if pnl_usdc >= 0 else "❌"
        msg = (
            f"{emoji} — <b>POSISI EXIT</b>\n\n"
            f"📌 <b>{html.escape(question[:60])}</b>\n\n"
            f"🎯 Outcome  : {html.escape(outcome)}\n"
            f"📈 Entry    : {entry_price:.3f} → Exit: {exit_price:.3f}\n"
            f"💰 PnL      : <b>${pnl_usdc:+.2f}</b>\n"
            f"📝 Alasan   : {html.escape(reason)}\n\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await self.send(msg, session)

    async def alert_circuit_breaker(
        self,
        reason: str,
        drawdown_pct: float,
        session: aiohttp.ClientSession,
    ):
        """Alert saat circuit breaker trigger."""
        msg = (
            f"🚨 — <b>CIRCUIT BREAKER TRIGGERED</b>\n\n"
            f"⚠️ Bot berhenti trading!\n\n"
            f"📉 Drawdown : {drawdown_pct:.1f}%\n"
            f"📝 Alasan   : {html.escape(reason)}\n\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await self.send(msg, session)

    async def alert_daily_summary(
        self,
        balance: float,
        open_positions: int,
        closed_trades: int,
        total_pnl: float,
        winrate: float,
        session: aiohttp.ClientSession,
    ):
        """Alert summary harian."""
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        msg = (
            f"📊 — <b>DAILY SUMMARY</b>\n\n"
            f"💵 Balance   : <b>${balance:.2f}</b>\n"
            f"📂 Open      : {open_positions}/5 posisi\n"
            f"✅ Closed    : {closed_trades} trades\n"
            f"{pnl_emoji} Total PnL  : <b>${total_pnl:+.2f}</b>\n"
            f"🎯 Win Rate  : {winrate:.1f}%\n\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        await self.send(msg, session)

    async def alert_error(
        self,
        error_msg: str,
        session: aiohttp.ClientSession,
    ):
        """Alert kalau ada error critical."""
        msg = (
            f"🔴 — <b>BOT ERROR</b>\n\n"
            f"<code>{html.escape(error_msg[:200])}</code>\n\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await self.send(msg, session)


# ─────────────────────────────────────────────
# SINGLETON — shared instance
# ─────────────────────────────────────────────

_alert_instance: Optional[TelegramAlert] = None


def init_telegram(token: str, chat_id: str) -> TelegramAlert:
    """Initialize singleton TelegramAlert instance."""
    global _alert_instance
    _alert_instance = TelegramAlert(token=token, chat_id=chat_id)
    return _alert_instance


def get_alert() -> Optional[TelegramAlert]:
    """Get singleton instance."""
    return _alert_instance


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    async def test():
        alert = TelegramAlert(token=TOKEN, chat_id=CHAT_ID)

        async with aiohttp.ClientSession() as session:
            print("Sending test messages...")

            await alert.alert_signal(
                question  = "Will Bitcoin reach $80,000 in April?",
                outcome   = "Yes",
                price     = 0.665,
                bet_usdc  = 22.71,
                gap_pct   = 12.7,
                ev        = 0.191,
                session   = session,
                dry_run   = True,
            )
            print("✅ Signal alert sent!")

            await alert.alert_daily_summary(
                balance        = 120.00,
                open_positions = 1,
                closed_trades  = 0,
                total_pnl      = 0.0,
                winrate        = 0.0,
                session        = session,
            )
            print("✅ Daily summary sent!")

    asyncio.run(test())