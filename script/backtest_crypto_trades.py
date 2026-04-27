"""
script/backtest_crypto_trades.py
================================
End-to-end trade simulator untuk crypto mispricing strategy.

Beda dengan `backtest_mispricing.py` (yang cuma kalibrasi probabilitas model),
script ini SIMULASI FULL: entry → exit → resolve → PnL pakai data historis
Polymarket + Deribit IV + CoinGecko spot.

Jalankan:
    python -m script.backtest_crypto_trades --days 90
    python -m script.backtest_crypto_trades --days 365 --threshold 0.10 --capital 100
    python -m script.backtest_crypto_trades --no-cache    # force refresh

Output:
    data/backtest_results/trades_<timestamp>.csv
    data/backtest_results/summary_<timestamp>.txt
"""

from __future__ import annotations

import sys
import csv
import argparse
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp

from src.backtest.polymarket_history import fetch_closed_crypto_markets
from src.backtest.trade_simulator import (
    SimulatorConfig, run_simulation, SimulatedTrade,
)


RESULTS_DIR = Path(__file__).resolve().parents[1] / "data" / "backtest_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────

def _summary_stats(trades: list[SimulatedTrade]) -> dict:
    if not trades:
        return {"n": 0, "total_pnl": 0.0, "win_rate": 0.0, "avg_pnl_pct": 0.0}

    n = len(trades)
    total_pnl    = sum(t.pnl_usdc for t in trades)
    total_bet    = sum(t.bet_usdc for t in trades)
    wins         = [t for t in trades if t.pnl_usdc > 0]
    losses       = [t for t in trades if t.pnl_usdc <= 0]
    win_rate     = len(wins) / n
    avg_pnl_pct  = sum(t.pnl_pct for t in trades) / n
    avg_win_pct  = sum(t.pnl_pct for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss_pct = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0.0
    avg_days     = sum(t.days_held for t in trades) / n

    return {
        "n":            n,
        "total_pnl":    total_pnl,
        "total_bet":    total_bet,
        "roi":          total_pnl / total_bet if total_bet > 0 else 0.0,
        "win_rate":     win_rate,
        "avg_pnl_pct":  avg_pnl_pct,
        "avg_win_pct":  avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "avg_days":     avg_days,
        "max_win":      max((t.pnl_usdc for t in trades), default=0),
        "max_loss":     min((t.pnl_usdc for t in trades), default=0),
    }


def _per_asset_breakdown(trades: list[SimulatedTrade]) -> dict[str, dict]:
    by_asset: dict[str, list[SimulatedTrade]] = {}
    for t in trades:
        by_asset.setdefault(t.asset, []).append(t)
    return {asset: _summary_stats(group) for asset, group in by_asset.items()}


def _per_signal_breakdown(trades: list[SimulatedTrade]) -> dict[str, dict]:
    by_signal: dict[str, list[SimulatedTrade]] = {}
    for t in trades:
        by_signal.setdefault(t.exit_signal, []).append(t)
    return {sig: _summary_stats(group) for sig, group in by_signal.items()}


def _format_summary(trades: list[SimulatedTrade]) -> str:
    overall = _summary_stats(trades)
    by_asset = _per_asset_breakdown(trades)
    by_signal = _per_signal_breakdown(trades)

    lines = []
    lines.append("=" * 70)
    lines.append("CRYPTO TRADE SIMULATION — SUMMARY")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"Total trades:    {overall['n']}")
    if overall['n'] == 0:
        lines.append("(no trades — coba turunkan threshold atau perpanjang window)")
        return "\n".join(lines)

    lines.append(f"Total PnL:       ${overall['total_pnl']:+.2f}")
    lines.append(f"Total bet:       ${overall['total_bet']:.2f}")
    lines.append(f"ROI:             {overall['roi']:+.2%}")
    lines.append(f"Win rate:        {overall['win_rate']:.1%}")
    lines.append(f"Avg PnL %:       {overall['avg_pnl_pct']:+.2%}")
    lines.append(f"Avg win %:       {overall['avg_win_pct']:+.2%}")
    lines.append(f"Avg loss %:      {overall['avg_loss_pct']:+.2%}")
    lines.append(f"Max win/loss:    ${overall['max_win']:+.2f} / ${overall['max_loss']:+.2f}")
    lines.append(f"Avg days held:   {overall['avg_days']:.1f}")
    lines.append("")

    lines.append("─" * 70)
    lines.append(f"{'Asset':<6} | {'N':>4} | {'PnL':>10} | {'ROI':>8} | {'Win%':>6} | {'AvgDays':>8}")
    lines.append("─" * 70)
    for asset, stats in sorted(by_asset.items()):
        lines.append(
            f"{asset:<6} | {stats['n']:>4} | "
            f"${stats['total_pnl']:>+8.2f} | "
            f"{stats['roi']:>+7.2%} | "
            f"{stats['win_rate']:>5.1%} | "
            f"{stats['avg_days']:>7.1f}"
        )
    lines.append("")

    lines.append("─" * 70)
    lines.append(f"{'Exit Signal':<22} | {'N':>4} | {'PnL':>10} | {'Win%':>6}")
    lines.append("─" * 70)
    for sig, stats in sorted(by_signal.items(), key=lambda x: -x[1]['n']):
        lines.append(
            f"{sig:<22} | {stats['n']:>4} | "
            f"${stats['total_pnl']:>+8.2f} | "
            f"{stats['win_rate']:>5.1%}"
        )
    lines.append("")
    lines.append("=" * 70)

    return "\n".join(lines)


def _write_csv(trades: list[SimulatedTrade], path: Path) -> None:
    if not trades:
        return
    rows = [t.to_row() for t in trades]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

async def main_async(args):
    cfg = SimulatorConfig(
        threshold              = args.threshold,
        capital_per_trade      = args.capital,
        trailing_stop_pct      = args.trailing_stop,
        profit_threshold       = args.profit_threshold,
        tight_trailing_pct     = args.tight_trailing,
        days_hold_to_resolve   = args.hold_to_resolve,
        max_days_stale         = args.max_stale,
    )

    print("=" * 70)
    print(f"CRYPTO TRADE BACKTEST — {args.days}d window")
    print(f"Threshold: {cfg.threshold:.0%} | Capital/trade: ${cfg.capital_per_trade:.0f} | "
          f"Trailing: {cfg.trailing_stop_pct:.0%}")
    print("=" * 70)

    async with aiohttp.ClientSession() as session:
        markets = await fetch_closed_crypto_markets(
            days_back=args.days, session=session, use_cache=not args.no_cache
        )
        print(f"\n→ {len(markets)} closed crypto markets ditemukan")
        if not markets:
            print("Tidak ada market di window ini. Cek koneksi atau perpanjang --days.")
            return

        trades = await run_simulation(markets, cfg, session)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path  = RESULTS_DIR / f"trades_{timestamp}.csv"
    txt_path  = RESULTS_DIR / f"summary_{timestamp}.txt"

    _write_csv(trades, csv_path)
    summary = _format_summary(trades)
    txt_path.write_text(summary, encoding="utf-8")

    print()
    print(summary)
    print()
    print(f"→ Trades CSV : {csv_path}")
    print(f"→ Summary    : {txt_path}")


def parse_args():
    p = argparse.ArgumentParser(description="Crypto Trade Simulator (full lifecycle)")
    p.add_argument("--days", type=int, default=90,
                   help="Window historis (hari) — default 90, max useful ~365")
    p.add_argument("--threshold", type=float, default=0.15,
                   help="Min |gap| untuk entry (default 0.15)")
    p.add_argument("--capital", type=float, default=100.0,
                   help="Virtual capital per trade USDC (default 100)")
    p.add_argument("--trailing-stop", type=float, default=0.15,
                   help="Trailing stop pct dari peak (default 0.15)")
    p.add_argument("--profit-threshold", type=float, default=0.85,
                   help="Harga profit zone (default 0.85)")
    p.add_argument("--tight-trailing", type=float, default=0.07,
                   help="Tight trailing pct di profit zone (default 0.07)")
    p.add_argument("--hold-to-resolve", type=int, default=3,
                   help="Days threshold for HOLD_TO_RESOLVE (default 3)")
    p.add_argument("--max-stale", type=int, default=21,
                   help="Max days stale (default 21)")
    p.add_argument("--no-cache", action="store_true",
                   help="Force refresh data (bypass disk cache)")
    p.add_argument("--verbose", action="store_true",
                   help="Verbose logging")
    return p.parse_args()


def main():
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
