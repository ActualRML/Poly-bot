import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}


def _extras(record: logging.LogRecord) -> dict:
    return {
        k: v for k, v in record.__dict__.items()
        if k not in _RESERVED and not k.startswith("_")
    }


class KVFormatter(logging.Formatter):
    """`HH:MM:SS | LEVEL | logger | msg | key=value ...` — what a human watches live.

    Fixed-width LEVEL/logger columns so every message starts at the same
    column and the live stream stays scannable.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        base = f"{ts} | {record.levelname:<7} | {record.name:<14} | {record.getMessage()}"
        extras = _extras(record)
        if extras:
            base += " | " + " ".join(f"{k}={v!r}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    """One JSON object per line — what you grep later."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        obj.update(_extras(record))
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def _rotating(path: Path, formatter: logging.Formatter, level: str) -> RotatingFileHandler:
    h = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(formatter)
    h.setLevel(level)
    return h


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """
    Channels:
      stdout       — KV, filtered to LOG_LEVEL (human view)
      bot.log      — JSON, filtered to LOG_LEVEL (durable record)
      ws.log       — JSON, always DEBUG (Polymarket WS frames)
      binance.log  — JSON, always DEBUG (Binance ticks)

    Root level is DEBUG so child loggers can emit ticks; the per-handler
    levels do the actual filtering. Without this, the ws/binance per-file
    DEBUG capture wouldn't work even with the loggers set to DEBUG.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    level = level.upper()

    root = logging.getLogger()
    root.setLevel("DEBUG")
    root.handlers.clear()

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(KVFormatter())
    stdout.setLevel(level)
    root.addHandler(stdout)

    root.addHandler(_rotating(log_dir / "bot.log", JsonFormatter(), level))

    ws_logger = logging.getLogger("ws")
    ws_logger.setLevel("DEBUG")
    ws_logger.handlers.clear()
    ws_logger.addHandler(_rotating(log_dir / "ws.log", JsonFormatter(), "DEBUG"))
    # propagate=True so INFO+ lines still reach stdout + bot.log

    binance_logger = logging.getLogger("binance")
    binance_logger.setLevel("DEBUG")
    binance_logger.handlers.clear()
    binance_logger.addHandler(_rotating(log_dir / "binance.log", JsonFormatter(), "DEBUG"))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def separator(label: str = "streaming") -> None:
    """One unprefixed divider line marking startup-config -> runtime-data."""
    print(f"──────────── {label} ────────────", flush=True)
