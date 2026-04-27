"""
run_backtest.py
===============
Launcher backtest dari ROOT proyek. Ini solusi paling andal
untuk Windows/MINGW64 karena tidak perlu set PYTHONPATH manual.

Cara jalankan (dari folder polymarket-bot/):
    python run_backtest.py

Atau dengan argumen:
    python run_backtest.py --saldo 500 --csv data/historical/market_log.csv
"""

import sys
import argparse
from pathlib import Path
from decimal import Decimal

# ── Pastikan root proyek ada di sys.path sebelum import apapun ───────────────
# Ini yang menyebabkan ModuleNotFoundError di Windows MINGW64.
# Saat double-click atau jalankan dari folder berbeda, sys.path bisa salah.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Import setelah path difix ─────────────────────────────────────────────────
from src.utils.logger import log, console, tampilkan_header
from src.utils.config import config
from src.logic.strategy import KonfigurasiStrategy
from src.backtest.backtester import Backtester
from src.backtest.collector import generate_csv_contoh


def parse_args():
    p = argparse.ArgumentParser(
        description="Polymarket Bot — Backtest Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Contoh:
  python run_backtest.py
  python run_backtest.py --saldo 500
  python run_backtest.py --csv data/historical/market_log.csv --spread-min 0.0006
        """,
    )
    p.add_argument(
        "--csv",
        default=config.CSV_PATH,
        help=f"Path ke file CSV historical (default: {config.CSV_PATH})",
    )
    p.add_argument(
        "--saldo",
        type=Decimal,
        default=config.SALDO_AWAL,
        help=f"Saldo awal USDC (default: {config.SALDO_AWAL})",
    )
    p.add_argument(
        "--ukuran-order",
        type=Decimal,
        default=config.UKURAN_ORDER,
        dest="ukuran_order",
        help=f"Ukuran tiap order USDC (default: {config.UKURAN_ORDER})",
    )
    p.add_argument(
        "--spread-min",
        type=Decimal,
        default=config.SPREAD_MINIMUM,
        dest="spread_min",
        help=f"Spread minimum strategy (default: {config.SPREAD_MINIMUM})",
    )
    p.add_argument(
        "--tick",
        type=int,
        default=config.JUMLAH_TICK,
        help=f"Jumlah tick frontrun (default: {config.JUMLAH_TICK})",
    )
    p.add_argument(
        "--generate-dummy",
        action="store_true",
        dest="generate_dummy",
        help="Paksa generate ulang CSV dummy meskipun file sudah ada",
    )
    p.add_argument(
        "--n-baris",
        type=int,
        default=300,
        dest="n_baris",
        help="Jumlah baris data dummy yang digenerate (default: 300)",
    )
    return p.parse_args()


def main():
    tampilkan_header()
    args = parse_args()

    # ── Cek / generate CSV ────────────────────────────────────────────────────
    csv_path = Path(args.csv)
    if not csv_path.exists() or args.generate_dummy:
        if args.generate_dummy:
            log.info("[yellow]--generate-dummy aktif: membuat ulang CSV...[/yellow]")
        else:
            log.warning(
                f"[yellow]CSV '{csv_path}' tidak ditemukan.[/yellow] "
                "Membuat data dummy otomatis..."
            )
        generate_csv_contoh(path=csv_path, n_baris=args.n_baris)
    else:
        log.info(f"[cyan]Menggunakan CSV:[/cyan] {csv_path}")

    # ── Konfigurasi strategy ──────────────────────────────────────────────────
    konfigurasi = KonfigurasiStrategy(
        spread_minimum = args.spread_min,
        jumlah_tick    = args.tick,
    )

    log.info(
        f"[dim]Konfigurasi: spread_min={konfigurasi.spread_minimum} | "
        f"tick={konfigurasi.jumlah_tick} | "
        f"saldo={args.saldo} | "
        f"ukuran_order={args.ukuran_order}[/dim]"
    )

    # ── Jalankan backtest ─────────────────────────────────────────────────────
    bt    = Backtester(
        csv_path    = str(csv_path),
        konfigurasi = konfigurasi,
        saldo_awal  = args.saldo,
    )
    hasil = bt.jalankan(ukuran_order=args.ukuran_order)

    # ── Distribusi spread (Task 3) ────────────────────────────────────────────
    if hasil.distribusi_spread:
        console.print("\n[bold cyan]Distribusi Spread Pasar (dalam tick):[/bold cyan]")
        for bucket, jumlah in sorted(hasil.distribusi_spread.items()):
            bar = "█" * min(jumlah // 2, 40)
            console.print(f"  [dim]{bucket:>12}[/dim] │ [cyan]{bar}[/cyan] {jumlah}")

    # ── Ringkasan satu baris ──────────────────────────────────────────────────
    warna_pnl = "green" if hasil.pnl_realisasi >= Decimal("0") else "red"
    console.print(
        f"\n[bold]Selesai.[/bold] "
        f"Fill Rate [cyan]{hasil.fill_rate_pct:.1f}%[/cyan] │ "
        f"PnL [{warna_pnl}]{hasil.pnl_realisasi:+.4f} USDC[/{warna_pnl}] │ "
        f"Spread OK [yellow]{hasil.pct_spread_cukup:.1f}%[/yellow] dari snapshot"
    )


if __name__ == "__main__":
    main()