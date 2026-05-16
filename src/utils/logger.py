
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

from src.utils.config import config

console = Console()

_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_file_handler = RotatingFileHandler(
    _LOG_DIR / "bot.log",
    maxBytes    = 10_000_000,
    backupCount = 3,
    encoding    = "utf-8",
)
_file_handler.setFormatter(logging.Formatter(
    fmt     = "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt = "%Y-%m-%dT%H:%M:%S",
))

logging.basicConfig(
    level    = getattr(logging, config.LOG_LEVEL, logging.INFO),
    format   = "%(message)s",
    datefmt  = "[%H:%M:%S]",
    handlers = [
        RichHandler(
            console          = console,
            rich_tracebacks  = True,
            show_path        = False,
            markup           = True,
        ),
        _file_handler,
    ]
)

log = logging.getLogger("polymarket-bot")

def tampilkan_header():

    console.print(Panel.fit(
        "[bold cyan]Polymarket Trading Bot[/bold cyan]\n"
        "[dim]Backtest-First | Price Improvement Strategy[/dim]",
        border_style="cyan"
    ))
