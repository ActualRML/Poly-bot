"""
src/utils/logger.py
===================
Logging berbasis Rich untuk output terminal yang informatif dan berwarna.

Semua modul lain mengimpor `log` dari sini.
"""

import logging
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

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


