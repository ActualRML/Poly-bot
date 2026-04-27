"""
src/utils/logger.py
===================
Logging berbasis Rich untuk output terminal yang informatif dan berwarna.

Semua modul lain mengimpor `log` dari sini.
"""

import logging
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint
from decimal import Decimal
from typing import Optional

from src.utils.config import config


# ─────────────────────────────────────────────────────────────────────────────
# SETUP LOGGER
# ─────────────────────────────────────────────────────────────────────────────

console = Console()

logging.basicConfig(
    level    = getattr(logging, config.LOG_LEVEL, logging.INFO),
    format   = "%(message)s",
    datefmt  = "[%H:%M:%S]",
    handlers = [RichHandler(
        console          = console,
        rich_tracebacks  = True,
        show_path        = False,
        markup           = True,
    )]
)

log = logging.getLogger("polymarket-bot")


# ─────────────────────────────────────────────────────────────────────────────
# FUNGSI DISPLAY KHUSUS
# ─────────────────────────────────────────────────────────────────────────────

def tampilkan_header():
    """Tampilkan header saat bot pertama kali dijalankan."""
    console.print(Panel.fit(
        "[bold cyan]Polymarket Trading Bot[/bold cyan]\n"
        "[dim]Backtest-First | Price Improvement Strategy[/dim]",
        border_style="cyan"
    ))


def tampilkan_sinyal(sinyal, snapshot=None):
    """Tampilkan sinyal trading ke console dengan warna."""
    from src.logic.strategy import SinyalTrade
    warna = {
        SinyalTrade.KEDUANYA : "green",
        SinyalTrade.BELI     : "blue",
        SinyalTrade.JUAL     : "yellow",
        SinyalTrade.TIDAK_ADA: "dim",
    }.get(sinyal.sinyal, "white")

    log.info(
        f"[{warna}][{sinyal.sinyal.name}][/{warna}] "
        f"Bid: [cyan]{sinyal.bid_diusulkan}[/cyan] | "
        f"Ask: [cyan]{sinyal.ask_diusulkan}[/cyan] | "
        f"Spread: [yellow]{sinyal.spread_kita}[/yellow] "
        f"([dim]{sinyal.spread_kita_dalam_tick} tick[/dim])"
    )


def tampilkan_trade(sisi: str, harga: Decimal, ukuran: Decimal, pnl: Optional[Decimal] = None):
    """Tampilkan eksekusi trade."""
    warna  = "green" if sisi == "BELI" else "red"
    pnl_str = f" | PnL: [{'green' if pnl and pnl >= 0 else 'red'}]{pnl:+.4f}[/]" if pnl is not None else ""
    log.info(
        f"[{warna}]✓ TERISI {sisi}[/{warna}] "
        f"@ [bold]{harga}[/bold] x {ukuran} USDC{pnl_str}"
    )


def tampilkan_hasil_backtest(state, hasil_analisis: dict):
    """Tampilkan tabel ringkasan hasil backtest."""
    t = Table(title="Hasil Backtest", border_style="cyan", show_lines=True)
    t.add_column("Metrik",  style="bold")
    t.add_column("Nilai",   justify="right")

    t.add_row("Total Sinyal",    str(state.total_sinyal))
    t.add_row("Total Trade",     str(state.total_trade))
    t.add_row("Fill Rate",       f"[green]{state.fill_rate:.2f}%[/green]")
    t.add_row("Trade Beli",      str(state.trade_beli))
    t.add_row("Trade Jual",      str(state.trade_jual))
    t.add_row("Saldo Akhir",     f"[cyan]{state.saldo_usdc:.4f} USDC[/cyan]")
    t.add_row("Inventori",       f"{state.inventori:.4f} shares")
    t.add_row(
        "Realized PnL",
        f"[{'green' if state.pnl_realisasi >= 0 else 'red'}]"
        f"{state.pnl_realisasi:+.4f} USDC[/]"
    )
    t.add_row(
        "Spread Memenuhi Syarat",
        f"{hasil_analisis.get('pct_spread_cukup', 0):.1f}%"
    )
    t.add_row(
        "Spread Rata-rata (tick)",
        f"{hasil_analisis.get('spread_rata_tick', 0):.1f}"
    )

    console.print(t)