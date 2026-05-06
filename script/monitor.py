
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from datetime import datetime, timezone
from src.models.database import (
    get_open_positions,
    get_trade_history,
    get_stats,
)

def main():
    now = datetime.now(timezone.utc)
    print("=" * 65)
    print(f"  POLYMARKET BOT — DAILY MONITOR")
    print(f"  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 65)

    stats  = get_stats()
    total  = stats.get("total_trades") or 0
    pnl    = stats.get("total_pnl") or 0
    wins   = stats.get("wins") or 0
    losses = stats.get("losses") or 0
    wr     = stats.get("winrate") or 0

    print(f"\n📊 PORTFOLIO SUMMARY")
    print(f"   Closed trades : {total}")
    print(f"   Win / Loss    : {wins} / {losses}")
    print(f"   Winrate       : {wr:.1f}%")
    print(f"   Total PnL     : ${pnl:+.2f}")

    positions = get_open_positions()
    print(f"\n📂 OPEN POSITIONS ({len(positions)}/5)")

    if not positions:
        print("   Tidak ada posisi open.")
    else:
        total_capital_at_risk = 0.0
        total_unrealized_pnl  = 0.0

        for pos in positions:
            entry   = float(pos["entry_price"])
            current = float(pos["current_price"])
            shares  = float(pos["shares"])
            capital = float(pos["capital_at_risk"])
            _gap_raw = float(pos.get("gap_pct") or 0)

            gap_pct  = _gap_raw * 100 if _gap_raw <= 1.0 else _gap_raw

            pnl_pos = (current - entry) * shares
            pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0

            profit_if_win = (1.0 - entry) * shares
            profit_if_lose = -capital

            total_capital_at_risk += capital
            total_unrealized_pnl  += pnl_pos

            resolve_str = pos.get("resolve_date", "")
            try:
                resolve = datetime.fromisoformat(resolve_str)
                if resolve.tzinfo is None:
                    resolve = resolve.replace(tzinfo=timezone.utc)
                secs_left = max(0, (resolve - now).total_seconds())
                hours_left = secs_left / 3600
                if hours_left < 1:
                    resolve_label = f"{int(secs_left / 60)}m"
                else:
                    resolve_label = f"{hours_left:.1f}h"
            except Exception:
                resolve_label = "?"

            if pnl_pct > 5:
                status = "📈"
            elif pnl_pct < -5:
                status = "📉"
            else:
                status = "➡️"

            print(f"\n   {status} {pos['question'][:52]}")
            print(f"      Outcome  : {pos['outcome']} | Edge saat entry: {gap_pct:.1f}%")
            print(f"      Entry    : {entry:.3f} | Current: {current:.3f} | "
                  f"PnL: ${pnl_pos:+.2f} ({pnl_pct:+.1f}%)")
            print(f"      Capital  : ${capital:.2f} | "
                  f"Win → +${profit_if_win:.2f} | "
                  f"Lose → -${capital:.2f} | "
                  f"Resolve: {resolve_label}")

        print(f"\n   ──────────────────────────────────────────────────")
        print(f"   Total at risk : ${total_capital_at_risk:.2f}")
        print(f"   Unrealized PnL: ${total_unrealized_pnl:+.2f}")

    trades = get_trade_history(limit=5)
    print(f"\n📋 LAST 5 TRADES")
    if not trades:
        print("   Belum ada trade history.")
    else:
        for t in trades:
            ts     = t.get("timestamp", "")[:16]
            action = t.get("action", "").upper()
            q      = t.get("question", "")[:38]
            price  = float(t.get("price", 0))
            usdc   = float(t.get("usdc_amount", 0))
            action_lower = t.get("action", "").lower()
            if action_lower == "buy":
                result_label = "⏳ OPEN"
            elif action_lower in ("exit", "sell"):
                result_label = "✅ WIN" if usdc > 0 else "❌ LOSE"
            else:
                result_label = ""

            print(f"   [{ts}] {action} {t['outcome']} {result_label} | "
                  f"{q} | @ {price:.3f} | ${usdc:.2f}")

    if positions:
        print(f"\n💡 QUICK ANALYSIS")
        def _edge(p):
            v = float(p.get("gap_pct") or 0)
            return v * 100 if v <= 1.0 else v

        high_edge = [p for p in positions if _edge(p) > 15]
        low_edge  = [p for p in positions if _edge(p) < 5]

        if high_edge:
            print(f"   🔥 {len(high_edge)} posisi dengan edge tinggi (>15%) — hold!")
        if low_edge:
            print(f"   ⚠️  {len(low_edge)} posisi dengan edge rendah (<5%) — monitor ketat")
        if not high_edge and not low_edge:
            print(f"   ✅ Semua posisi dalam range normal (5-15% edge)")

    print("\n" + "=" * 65)
    print("  Jalankan bot : python -m src.main")
    print("  Reset DB     : python -c \"import sqlite3; conn=sqlite3.connect('data/bot_database.db'); conn.execute('DELETE FROM positions'); conn.execute('DELETE FROM trades'); conn.commit()\"")
    print("=" * 65)

if __name__ == "__main__":
    main()
