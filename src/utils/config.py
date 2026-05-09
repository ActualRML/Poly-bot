
import os
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env.secret")
load_dotenv(_ROOT / ".env.local")
load_dotenv(_ROOT / ".env")

def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)

def _get_decimal(key: str, default: str) -> Decimal:
    return Decimal(os.getenv(key, default))

def _get_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))

def _get_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")

def _get_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))

class Config:

    PK_PRIVATE_KEY     : str = _get("PK_PRIVATE_KEY")
    API_KEY            : str = _get("CLOB_API_KEY")
    API_SECRET         : str = _get("CLOB_SECRET")
    API_PASSPHRASE     : str = _get("CLOB_PASS")
    TELEGRAM_BOT_TOKEN : str = _get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   : str = _get("TELEGRAM_CHAT_ID", "")

    CLOB_HOST  : str = _get("CLOB_HOST", "https://clob.polymarket.com")
    GAMMA_HOST : str = _get("GAMMA_HOST", "https://gamma-api.polymarket.com")

    KELLY_MULTIPLIER       : float = _get_float("KELLY_MULTIPLIER", 0.5)
    MAX_KELLY_FRACTION     : float = _get_float("MAX_KELLY_FRACTION", 0.30)
    MIN_BET_USDC           : float = _get_float("MIN_BET_USDC", 5.0)
    MIN_WINRATE            : float = _get_float("MIN_WINRATE", 0.15)
    MIN_PROFIT_PCT         : float = _get_float("MIN_PROFIT_PCT", 0.20)
    MAX_OPEN_POSITIONS     : int   = _get_int("MAX_OPEN_POSITIONS", 5)
    MAX_CAPITAL_PER_MARKET : float = _get_float("MAX_CAPITAL_PER_MARKET", 30.0)
    MAX_SAME_DIRECTION     : int   = _get_int("MAX_SAME_DIRECTION", 2)
    TRAILING_STOP_PCT       : float = _get_float("TRAILING_STOP_PCT", 0.15)
    TIGHT_TRAILING_STOP_PCT : float = _get_float("TIGHT_TRAILING_STOP_PCT", 0.07)
    PROFIT_THRESHOLD        : float = _get_float("PROFIT_THRESHOLD", 0.75)
    PROFIT_LOCK_PCT              : float = _get_float("PROFIT_LOCK_PCT", 20.0)
    PROFIT_LOCK_HIGH_PCT         : float = _get_float("PROFIT_LOCK_HIGH_PCT", 35.0)
    UPDOWN_PROFIT_LOCK_PCT       : float = _get_float("UPDOWN_PROFIT_LOCK_PCT", 40.0)
    UPDOWN_PROFIT_LOCK_HIGH_PCT  : float = _get_float("UPDOWN_PROFIT_LOCK_HIGH_PCT", 60.0)
    HOURLY_PROFIT_LOCK_PCT       : float = _get_float("HOURLY_PROFIT_LOCK_PCT", 30.0)
    HOURLY_PROFIT_LOCK_HIGH_PCT  : float = _get_float("HOURLY_PROFIT_LOCK_HIGH_PCT", 50.0)

    MAX_DRAWDOWN_PCT       : float = _get_float("MAX_DRAWDOWN_PCT", 0.40)
    MAX_DAILY_LOSS_PCT     : float = _get_float("MAX_DAILY_LOSS_PCT", 0.20)
    MAX_CONSECUTIVE_LOSSES : int   = _get_int("MAX_CONSECUTIVE_LOSSES", 5)

    HOURLY_MAX_MINUTES_TO_RESOLVE : int   = _get_int("HOURLY_MAX_MINUTES_TO_RESOLVE", 1440)
    HOURLY_MIN_MINUTES_TO_RESOLVE : int   = _get_int("HOURLY_MIN_MINUTES_TO_RESOLVE", 30)
    HOURLY_MISPRICING_THRESHOLD   : float = _get_float("HOURLY_MISPRICING_THRESHOLD", 0.12)
    HOURLY_MIN_WINRATE_STRICT     : float = _get_float("HOURLY_MIN_WINRATE_STRICT", 0.75)
    HOURLY_MIN_MARKET_VOLUME      : float = _get_float("HOURLY_MIN_MARKET_VOLUME", 5000.0)
    HOURLY_MIN_LIQUIDITY          : float = _get_float("HOURLY_MIN_LIQUIDITY", 200.0)
    HOURLY_VOL_HOURS              : int   = _get_int("HOURLY_VOL_HOURS", 4)
    HOURLY_DRIFT_HOURS            : int   = _get_int("HOURLY_DRIFT_HOURS", 4)

    UPDOWN_MIN_WINRATE          : float = _get_float("UPDOWN_MIN_WINRATE", 0.55)
    UPDOWN_HOURLY_MIN_WINRATE   : float = _get_float("UPDOWN_HOURLY_MIN_WINRATE", 0.55)
    UPDOWN_THRESHOLD            : float = _get_float("UPDOWN_THRESHOLD", 0.05)
    UPDOWN_HOURLY_THRESHOLD     : float = _get_float("UPDOWN_HOURLY_THRESHOLD", 0.07)
    UPDOWN_MAX_HOURS            : float = _get_float("UPDOWN_MAX_HOURS", 8.0)
    UPDOWN_HOURLY_MAX_MINUTES         : int   = _get_int("UPDOWN_HOURLY_MAX_MINUTES", 90)
    UPDOWN_HOURLY_CANDLE_OPEN_MIN     : int   = _get_int("UPDOWN_HOURLY_CANDLE_OPEN_MIN", 10)
    MAX_POSITIONS_PER_SLOT            : int   = _get_int("MAX_POSITIONS_PER_SLOT", 2)
    UPDOWN_HOURLY_MOMENTUM_MINUTES    : int   = _get_int("UPDOWN_HOURLY_MOMENTUM_MINUTES", 15)
    UPDOWN_HOURLY_MOMENTUM_THRESHOLD  : float = _get_float("UPDOWN_HOURLY_MOMENTUM_THRESHOLD", 0.002)
    UPDOWN_HOURLY_MOMENTUM_MAX        : float = _get_float("UPDOWN_HOURLY_MOMENTUM_MAX", 0.008)
    UPDOWN_HOURLY_MAX_ENTRY_PRICE     : float = _get_float("UPDOWN_HOURLY_MAX_ENTRY_PRICE", 0.45)
    HOURLY_TRAILING_ACTIVATE_PCT      : float = _get_float("HOURLY_TRAILING_ACTIVATE_PCT", 15.0)
    HOURLY_TRAILING_RETRACE_PCT       : float = _get_float("HOURLY_TRAILING_RETRACE_PCT", 0.30)
    UPDOWN_HOURLY_USE_GBM             : bool  = _get_bool("UPDOWN_HOURLY_USE_GBM", True)
    UPDOWN_HOURLY_GBM_MIN_EDGE        : float = _get_float("UPDOWN_HOURLY_GBM_MIN_EDGE", 0.05)
    UPDOWN_HOURLY_FEE                 : float = _get_float("UPDOWN_HOURLY_FEE", 0.018)
    UPDOWN_HOURLY_OPPOSITE_REENTRY    : bool  = _get_bool("UPDOWN_HOURLY_OPPOSITE_REENTRY", True)
    UPDOWN_HOURLY_OPPOSITE_MIN_MINUTES: int   = _get_int("UPDOWN_HOURLY_OPPOSITE_MIN_MINUTES", 10)

    CB_ENABLED       : bool    = _get_bool("CB_ENABLED", False)
    SALDO_AWAL       : Decimal = _get_decimal("SALDO_AWAL", "1000")
    POLLING_INTERVAL : int     = _get_int("POLLING_INTERVAL_DETIK", 5)
    LOG_LEVEL        : str     = _get("LOG_LEVEL", "INFO")
    DRY_RUN          : bool    = _get_bool("DRY_RUN", True)

config = Config()

if not config.DRY_RUN:
    _REQUIRED = {
        "PK_PRIVATE_KEY": config.PK_PRIVATE_KEY,
        "CLOB_API_KEY":   config.API_KEY,
        "CLOB_SECRET":    config.API_SECRET,
        "CLOB_PASS":      config.API_PASSPHRASE,
    }
    _missing = [k for k, v in _REQUIRED.items() if not v]
    if _missing:
        raise EnvironmentError(
            f"Kredensial wajib tidak ditemukan di .env.secret: {', '.join(_missing)}"
        )
