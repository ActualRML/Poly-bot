
import asyncio
import html
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import aiohttp
from dotenv import load_dotenv
load_dotenv(_ROOT / ".env.secret")
load_dotenv(_ROOT / ".env.local")
load_dotenv(_ROOT / ".env")

from pathlib import Path

from src.models.database import (
    get_open_positions,
    get_trade_history,
    get_stats,
    count_open_positions,
)

_TARIK_FLAG = _ROOT / "data" / "tarik.flag"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor_bot")


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _edge_pct(pos: dict) -> float:
    v = float(pos.get("gap_pct") or 0)
    return v * 100 if v <= 1.0 else v


def _resolve_label(resolve_str: str, now: datetime) -> str:
    try:
        resolve = datetime.fromisoformat(resolve_str)
        if resolve.tzinfo is None:
            resolve = resolve.replace(tzinfo=timezone.utc)
        secs_left = max(0, (resolve - now).total_seconds())
        if secs_left < 3600:
            return f"{int(secs_left / 60)}m"
        return f"{secs_left / 3600:.1f}h"
    except Exception:
        return "?"


def build_report() -> str:
    now     = datetime.now(timezone.utc)
    now_wib = now + timedelta(hours=7)

    stats  = get_stats()
    total  = stats.get("total_trades") or 0
    pnl    = stats.get("total_pnl") or 0
    wins   = stats.get("wins") or 0
    losses = stats.get("losses") or 0
    wr     = stats.get("winrate") or 0

    positions = get_open_positions()

    parts: list[str] = []
    parts.append("📡 <b>POLYMARKET BOT — MONITOR</b>")
    parts.append(
        f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')} / {now_wib.strftime('%H:%M WIB')}"
    )
    parts.append("")

    pnl_emoji = "📈" if pnl >= 0 else "📉"
    parts.append("📊 <b>PORTFOLIO</b>")
    parts.append(f"   Closed   : {total} ({wins}W / {losses}L)")
    parts.append(f"   Winrate  : {wr:.1f}%")
    parts.append(f"   Total PnL: {pnl_emoji} <b>${pnl:+.2f}</b>")
    parts.append("")

    parts.append(f"📂 <b>OPEN POSITIONS ({len(positions)}/5)</b>")
    if not positions:
        parts.append("   <i>Tidak ada posisi open.</i>")
    else:
        total_capital = 0.0
        total_unrl    = 0.0
        for pos in positions:
            entry   = float(pos["entry_price"])
            current = float(pos["current_price"])
            shares  = float(pos["shares"])
            capital = float(pos["capital_at_risk"])
            edge    = _edge_pct(pos)

            pnl_pos = (current - entry) * shares
            pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
            profit_if_win = (1.0 - entry) * shares

            total_capital += capital
            total_unrl    += pnl_pos

            if pnl_pct > 5:
                status = "📈"
            elif pnl_pct < -5:
                status = "📉"
            else:
                status = "➡️"

            q_short = html.escape(pos["question"][:52])
            outcome = html.escape(str(pos["outcome"]))
            resolve_label = _resolve_label(pos.get("resolve_date", ""), now)

            parts.append(
                f"\n   {status} <b>{q_short}</b>\n"
                f"      {outcome} @ {entry:.3f} → {current:.3f} "
                f"(edge {edge:.1f}%)\n"
                f"      PnL: <b>${pnl_pos:+.2f}</b> ({pnl_pct:+.1f}%) | "
                f"Cap ${capital:.2f} | Win +${profit_if_win:.2f} | "
                f"⏳ {resolve_label}"
            )
        parts.append("")
        parts.append(f"   ─────────────────────────")
        parts.append(f"   At risk      : ${total_capital:.2f}")
        unrl_emoji = "📈" if total_unrl >= 0 else "📉"
        parts.append(f"   Unrealized   : {unrl_emoji} <b>${total_unrl:+.2f}</b>")

    parts.append("")
    trades = get_trade_history(limit=5)
    parts.append("📋 <b>LAST 5 TRADES</b>")
    if not trades:
        parts.append("   <i>Belum ada trade history.</i>")
    else:
        for t in trades:
            raw_ts = t.get("timestamp", "")
            try:
                ts_dt = datetime.fromisoformat(raw_ts)
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                ts_label = (ts_dt + timedelta(hours=7)).strftime("%m-%d %H:%M")
            except Exception:
                ts_label = raw_ts[:16]

            action = t.get("action", "").upper()
            action_lower = t.get("action", "").lower()
            usdc   = float(t.get("usdc_amount", 0))
            price  = float(t.get("price", 0))
            q      = html.escape(t.get("question", "")[:38])
            outc   = html.escape(str(t["outcome"]))

            if action_lower == "buy":
                tag = "⏳"
            elif action_lower in ("exit", "sell"):
                tag = "✅" if usdc > 0 else "❌"
            else:
                tag = "•"

            parts.append(
                f"   {tag} [{ts_label}] {action} {outc} @ {price:.3f} "
                f"(${usdc:+.2f})\n      <i>{q}</i>"
            )

    if positions:
        parts.append("")
        parts.append("💡 <b>QUICK ANALYSIS</b>")
        high_edge = [p for p in positions if _edge_pct(p) > 15]
        low_edge  = [p for p in positions if _edge_pct(p) < 5]
        if high_edge:
            parts.append(f"   🔥 Edge tinggi (&gt;15%) — hold!")
            for p in high_edge:
                parts.append(
                    f"      • {html.escape(p['question'][:48])} "
                    f"({_edge_pct(p):.1f}%)"
                )
        if low_edge:
            parts.append(f"   ⚠️ Edge rendah (&lt;5%) — monitor ketat")
            for p in low_edge:
                parts.append(
                    f"      • {html.escape(p['question'][:48])} "
                    f"({_edge_pct(p):.1f}%)"
                )
        if not high_edge and not low_edge:
            parts.append("   ✅ Semua posisi normal (5–15% edge)")

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────
# Builders untuk command yang lebih ringkas
# ─────────────────────────────────────────────────────────────────

def build_stats() -> str:
    s = get_stats()
    total  = s.get("total_trades") or 0
    pnl    = s.get("total_pnl") or 0
    wins   = s.get("wins") or 0
    losses = s.get("losses") or 0
    wr     = s.get("winrate") or 0
    avg    = s.get("avg_pnl") or 0
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    return (
        f"📊 <b>STATS</b>\n\n"
        f"Closed   : {total} ({wins}W / {losses}L)\n"
        f"Winrate  : <b>{wr:.1f}%</b>\n"
        f"Avg PnL  : ${avg:+.3f}\n"
        f"Total PnL: {pnl_emoji} <b>${pnl:+.2f}</b>"
    )


def build_positions() -> str:
    positions = get_open_positions()
    now = datetime.now(timezone.utc)

    if not positions:
        return "📂 <b>OPEN POSITIONS (0/5)</b>\n\n<i>Tidak ada posisi open.</i>"

    lines = [f"📂 <b>OPEN POSITIONS ({len(positions)}/5)</b>"]
    total_cap = 0.0
    total_unrl = 0.0

    for i, pos in enumerate(positions, 1):
        entry   = float(pos["entry_price"])
        current = float(pos["current_price"])
        shares  = float(pos["shares"])
        capital = float(pos["capital_at_risk"])
        edge    = _edge_pct(pos)

        pnl_pos = (current - entry) * shares
        pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0

        total_cap  += capital
        total_unrl += pnl_pos

        if pnl_pct > 5:
            status = "📈"
        elif pnl_pct < -5:
            status = "📉"
        else:
            status = "➡️"

        q_short = html.escape(pos["question"][:48])
        outcome = html.escape(str(pos["outcome"]))
        resolve_label = _resolve_label(pos.get("resolve_date", ""), now)

        lines.append(
            f"\n<b>{i}.</b> {status} <b>{q_short}</b>\n"
            f"   {outcome} @ {entry:.3f} → {current:.3f}\n"
            f"   PnL: <b>${pnl_pos:+.2f}</b> ({pnl_pct:+.1f}%) | "
            f"Cap ${capital:.2f} | ⏳ {resolve_label}"
        )

    lines.append("")
    lines.append(f"At risk    : ${total_cap:.2f}")
    unrl_emoji = "📈" if total_unrl >= 0 else "📉"
    lines.append(f"Unrealized : {unrl_emoji} <b>${total_unrl:+.2f}</b>")
    now_wib = datetime.now(timezone.utc) + timedelta(hours=7)
    lines.append(f"\n<i>Data per {now_wib.strftime('%H:%M:%S')} WIB — harga update tiap ~30 detik</i>")
    lines.append("<i>Gunakan /tarik 1 3 untuk tutup posisi nomor 1 dan 3</i>")
    return "\n".join(lines)


def build_trades(limit: int = 10) -> str:
    trades = get_trade_history(limit=limit)
    if not trades:
        return "📋 <b>TRADE HISTORY</b>\n\n<i>Belum ada trade.</i>"

    lines = [f"📋 <b>LAST {len(trades)} TRADES</b>"]
    for t in trades:
        raw_ts = t.get("timestamp", "")
        try:
            ts_dt = datetime.fromisoformat(raw_ts)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            ts_label = (ts_dt + timedelta(hours=7)).strftime("%m-%d %H:%M")
        except Exception:
            ts_label = raw_ts[:16]

        action_lower = t.get("action", "").lower()
        action = action_lower.upper()
        usdc   = float(t.get("usdc_amount", 0))
        price  = float(t.get("price", 0))
        q      = html.escape(t.get("question", "")[:38])
        outc   = html.escape(str(t["outcome"]))

        if action_lower == "buy":
            tag = "⏳"
        elif action_lower in ("exit", "sell"):
            tag = "✅" if usdc > 0 else "❌"
        else:
            tag = "•"

        lines.append(
            f"{tag} [{ts_label}] {action} {outc} @ {price:.3f} (${usdc:+.2f})\n"
            f"   <i>{q}</i>"
        )
    return "\n".join(lines)


def build_tarik_flag(indices: list[int]) -> str:
    """
    indices: 1-based position numbers from /tarik args.
    Writes selected condition_ids to flag file.
    """
    positions = get_open_positions()
    if not positions:
        return "📭 Tidak ada posisi open."

    # Validate indices
    valid = [i for i in indices if 1 <= i <= len(positions)]
    if not valid:
        listed = "\n".join(
            f"  {i}. {p['outcome']} — {p['question'][:40]}"
            for i, p in enumerate(positions, 1)
        )
        return f"❌ Nomor posisi tidak valid (1–{len(positions)}).\n\n{listed}"

    targets = [positions[i - 1] for i in valid]
    condition_ids = ",".join(p["condition_id"] for p in targets)
    _TARIK_FLAG.write_text(condition_ids)

    now_wib = datetime.now(timezone.utc) + timedelta(hours=7)
    lines = [
        f"🏁 <b>TARIK {len(targets)} posisi</b> — flag dikirim\n"
        f"<i>Harga per {now_wib.strftime('%H:%M:%S')} WIB (mungkin beda dari /positions)</i>\n"
    ]
    for p in targets:
        entry   = float(p["entry_price"])
        current = float(p["current_price"])
        shares  = float(p["shares"])
        pnl     = (current - entry) * shares
        emoji   = "📈" if pnl >= 0 else "📉"
        lines.append(
            f"  {emoji} {p['outcome']} @ {entry:.3f} → <b>{current:.3f}</b> | "
            f"PnL <b>${pnl:+.2f}</b>\n"
            f"     <i>{p['question'][:40]}</i>"
        )
    lines.append("\n⏳ Eksekusi oleh bot utama di cycle berikutnya (~30 detik)")
    return "\n".join(lines)


def build_help() -> str:
    return (
        "🤖 <b>POLYMARKET BOT — COMMANDS</b>\n\n"
        "/status      — full report (portfolio + positions + trades)\n"
        "/positions   — open positions saja\n"
        "/stats       — portfolio summary\n"
        "/trades      — 10 trades terakhir\n"
        "/trades 20   — 20 trades terakhir (max 50)\n"
        "/tarik 1     — tutup posisi #1 (lihat /positions untuk nomornya)\n"
        "/tarik 1 3   — tutup posisi #1 dan #3\n"
        "/tarik       — tutup semua posisi\n"
        "/ping        — cek bot alive\n"
        "/help        — list command\n"
    )


# ─────────────────────────────────────────────────────────────────
# Telegram long-polling
# ─────────────────────────────────────────────────────────────────

class MonitorBot:
    def __init__(self, token: str, allowed_chat_id: str):
        self.token = token
        self.allowed_chat_id = str(allowed_chat_id)
        self.api = f"https://api.telegram.org/bot{token}"
        self._stop = asyncio.Event()
        self._offset = 0

    def request_stop(self):
        self._stop.set()

    async def _send(self, session: aiohttp.ClientSession, chat_id: int, text: str):
        # Chunk biar di bawah 4096
        chunks: list[str] = []
        buf = ""
        for line in text.split("\n"):
            if len(buf) + len(line) + 1 > 3800:
                chunks.append(buf)
                buf = line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf:
            chunks.append(buf)

        for chunk in chunks:
            try:
                async with session.post(
                    f"{self.api}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning(f"sendMessage status={resp.status} body={body[:200]}")
            except Exception as e:
                log.warning(f"sendMessage error: {e}")

    async def _handle(self, session: aiohttp.ClientSession, msg: dict):
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()

        if str(chat_id) != self.allowed_chat_id:
            log.warning(f"Ignored message from unauthorized chat_id={chat_id}")
            return

        if not text.startswith("/"):
            return

        # Strip @botname suffix (kalau di group)
        head, *rest = text.split(maxsplit=1)
        cmd = head.split("@", 1)[0].lower()
        arg = rest[0] if rest else ""

        log.info(f"cmd={cmd} arg={arg!r}")

        try:
            if cmd in ("/start", "/help"):
                reply = build_help()
            elif cmd == "/ping":
                n = count_open_positions()
                reply = f"🟢 alive — {n} posisi open"
            elif cmd == "/status":
                reply = build_report()
            elif cmd == "/positions":
                reply = build_positions()
            elif cmd == "/stats":
                reply = build_stats()
            elif cmd == "/trades":
                try:
                    limit = max(1, min(50, int(arg))) if arg else 10
                except ValueError:
                    limit = 10
                reply = build_trades(limit=limit)
            elif cmd == "/tarik":
                nums = []
                for tok in arg.split():
                    try:
                        nums.append(int(tok))
                    except ValueError:
                        pass
                if not nums:
                    # No args → close all
                    nums = list(range(1, 6))
                reply = build_tarik_flag(nums)
            else:
                reply = f"❓ Unknown command: <code>{html.escape(cmd)}</code>\n\n" + build_help()
        except Exception as e:
            log.exception("handler error")
            reply = f"🔴 Error: <code>{html.escape(str(e)[:200])}</code>"

        await self._send(session, chat_id, reply)

    async def _poll_once(self, session: aiohttp.ClientSession) -> list[dict]:
        params = {"timeout": 30, "allowed_updates": ["message"]}
        if self._offset:
            params["offset"] = self._offset
        try:
            async with session.get(
                f"{self.api}/getUpdates",
                params=params,
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(f"getUpdates status={resp.status} body={body[:200]}")
                    return []
                data = await resp.json()
                if not data.get("ok"):
                    log.warning(f"getUpdates not ok: {data}")
                    return []
                return data.get("result", [])
        except asyncio.TimeoutError:
            return []
        except Exception as e:
            log.warning(f"getUpdates error: {e}")
            await asyncio.sleep(2)
            return []

    async def run(self):
        async with aiohttp.ClientSession() as session:
            log.info("monitor bot online — press Ctrl+C to stop")
            # Notify owner once on startup
            await self._send(
                session,
                int(self.allowed_chat_id),
                "🟢 <b>Monitor bot online</b>\n\nKetik /help untuk daftar command.",
            )

            while not self._stop.is_set():
                updates = await self._poll_once(session)
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if msg:
                        await self._handle(session, msg)


def main() -> int:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("[ERROR] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum di-set.", file=sys.stderr)
        return 1

    bot = MonitorBot(token=token, allowed_chat_id=chat_id)

    # Graceful shutdown (Ctrl+C / SIGTERM)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop_handler(*_):
        log.info("shutdown signal received")
        bot.request_stop()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _stop_handler)
    else:
        signal.signal(signal.SIGINT, _stop_handler)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        bot.request_stop()
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
