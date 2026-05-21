
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

    KELLY_MULTIPLIER       : float = _get_float("KELLY_MULTIPLIER", 0.7)
    MAX_KELLY_FRACTION     : float = _get_float("MAX_KELLY_FRACTION", 0.30)
    MIN_BET_USDC           : float = _get_float("MIN_BET_USDC", 5.0)
    MIN_WINRATE            : float = _get_float("MIN_WINRATE", 0.15)
    MAX_OPEN_POSITIONS     : int   = _get_int("MAX_OPEN_POSITIONS", 10)
    MAX_CAPITAL_PER_MARKET : float = _get_float("MAX_CAPITAL_PER_MARKET", 75.0)
    MAX_SAME_DIRECTION     : int   = _get_int("MAX_SAME_DIRECTION", 2)
    TRAILING_STOP_PCT       : float = _get_float("TRAILING_STOP_PCT", 0.15)
    TIGHT_TRAILING_STOP_PCT : float = _get_float("TIGHT_TRAILING_STOP_PCT", 0.07)
    PROFIT_THRESHOLD        : float = _get_float("PROFIT_THRESHOLD", 0.75)
    PROFIT_LOCK_PCT              : float = _get_float("PROFIT_LOCK_PCT", 20.0)
    PROFIT_LOCK_HIGH_PCT         : float = _get_float("PROFIT_LOCK_HIGH_PCT", 35.0)
    UPDOWN_PROFIT_LOCK_PCT       : float = _get_float("UPDOWN_PROFIT_LOCK_PCT", 40.0)
    UPDOWN_PROFIT_LOCK_HIGH_PCT  : float = _get_float("UPDOWN_PROFIT_LOCK_HIGH_PCT", 60.0)

    HOURLY_SL_MIN_AGE_MINUTES          : float = _get_float("HOURLY_SL_MIN_AGE_MINUTES", 10.0)
    HOURLY_LATE_SL_T4_PCT              : float = _get_float("HOURLY_LATE_SL_T4_PCT", -45.0)
    HOURLY_LATE_SL_T4_MAX_REMAINING    : float = _get_float("HOURLY_LATE_SL_T4_MAX_REMAINING", 40.0)

    HOURLY_FLIP_TRIGGER_PCT      : float = _get_float("HOURLY_FLIP_TRIGGER_PCT", -40.0)
    HOURLY_FLIP_MAX_ENTRY        : float = _get_float("HOURLY_FLIP_MAX_ENTRY", 0.72)
    HOURLY_FLIP_MIN_MINUTES      : float = _get_float("HOURLY_FLIP_MIN_MINUTES", 35.0)
    HOURLY_FLIP_PRICE_BUFFER_PCT : float = _get_float("HOURLY_FLIP_PRICE_BUFFER_PCT", 0.02)
    HOURLY_FLIP_MAX_SPREAD       : float = _get_float("HOURLY_FLIP_MAX_SPREAD", 0.03)
    HOURLY_FLIP_COOLDOWN_MINUTES : float = _get_float("HOURLY_FLIP_COOLDOWN_MINUTES", 5.0)

    MAX_DRAWDOWN_PCT       : float = _get_float("MAX_DRAWDOWN_PCT", 0.40)
    MAX_DAILY_LOSS_PCT     : float = _get_float("MAX_DAILY_LOSS_PCT", 0.20)
    MAX_CONSECUTIVE_LOSSES : int   = _get_int("MAX_CONSECUTIVE_LOSSES", 5)

    HOURLY_MIN_MINUTES_TO_RESOLVE : int   = _get_int("HOURLY_MIN_MINUTES_TO_RESOLVE", 30)
    HOURLY_VOL_HOURS              : int   = _get_int("HOURLY_VOL_HOURS", 24)

    UPDOWN_MAX_HOURS            : float = _get_float("UPDOWN_MAX_HOURS", 8.0)
    UPDOWN_HOURLY_MAX_MINUTES         : int   = _get_int("UPDOWN_HOURLY_MAX_MINUTES", 90)
    UPDOWN_HOURLY_CANDLE_OPEN_MIN     : int   = _get_int("UPDOWN_HOURLY_CANDLE_OPEN_MIN", 5)
    MAX_POSITIONS_PER_SLOT            : int   = _get_int("MAX_POSITIONS_PER_SLOT", 5)
    UPDOWN_HOURLY_MOMENTUM_MINUTES    : int   = _get_int("UPDOWN_HOURLY_MOMENTUM_MINUTES", 15)
    UPDOWN_HOURLY_MOMENTUM_MIN        : float = _get_float("UPDOWN_HOURLY_MOMENTUM_MIN", 0.0015)
    UPDOWN_HOURLY_MOMENTUM_VOL_FACTOR : float = _get_float("UPDOWN_HOURLY_MOMENTUM_VOL_FACTOR", 0.75)
    UPDOWN_HOURLY_MOMENTUM_MAX        : float = _get_float("UPDOWN_HOURLY_MOMENTUM_MAX", 0.012)
    UPDOWN_HOURLY_MAX_ENTRY_PRICE     : float = _get_float("UPDOWN_HOURLY_MAX_ENTRY_PRICE", 0.65)
    UPDOWN_HOURLY_STAGNATION_THRESHOLD: float = _get_float("UPDOWN_HOURLY_STAGNATION_THRESHOLD", 0.010)
    UPDOWN_HOURLY_MIN_ENTRY_PRICE     : float = _get_float("UPDOWN_HOURLY_MIN_ENTRY_PRICE", 0.25)
    UPDOWN_HOURLY_SKIP_SYMBOLS        : str   = _get("UPDOWN_HOURLY_SKIP_SYMBOLS", "")
    UPDOWN_HOURLY_ASIA_KELLY_CAP      : float = _get_float("UPDOWN_HOURLY_ASIA_KELLY_CAP", 1.0)
    UPDOWN_HOURLY_US_MAIN_KELLY_CAP   : float = _get_float("UPDOWN_HOURLY_US_MAIN_KELLY_CAP", 0.7)
    CANDLE_ENABLED                    : bool  = _get_bool("CANDLE_ENABLED", False)
    HOURLY_FLIP_ENABLED               : bool  = _get_bool("HOURLY_FLIP_ENABLED", False)
    REENTRY_AFTER_TP_ENABLED          : bool  = _get_bool("REENTRY_AFTER_TP_ENABLED", False)
    FILTER_MOMENTUM_CAP_ENABLED       : bool  = _get_bool("FILTER_MOMENTUM_CAP_ENABLED", False)
    UPDOWN_DAILY_MIN_HOURS_TO_RESOLVE : float = _get_float("UPDOWN_DAILY_MIN_HOURS_TO_RESOLVE", 2.0)
    UPDOWN_VOL_FLOOR                  : float = _get_float("UPDOWN_VOL_FLOOR", 0.50)
    UPDOWN_HOURLY_MIN_T_MINUTES       : int   = _get_int("UPDOWN_HOURLY_MIN_T_MINUTES", 20)
    UPDOWN_HOURLY_CONSENSUS_FLOOR     : float = _get_float("UPDOWN_HOURLY_CONSENSUS_FLOOR", 0.85)

    UPDOWN_HOURLY_BTC_CORR_THR        : float = _get_float("UPDOWN_HOURLY_BTC_CORR_THR", 0.005)
    UPDOWN_HOURLY_MIN_VOL_RATIO       : float = _get_float("UPDOWN_HOURLY_MIN_VOL_RATIO", 0.35)
    UPDOWN_HOURLY_MIN_VOLUME_USD      : float = _get_float("UPDOWN_HOURLY_MIN_VOLUME_USD", 500.0)
    UPDOWN_HOURLY_CONTRARIAN_MIN_T    : float = _get_float("UPDOWN_HOURLY_CONTRARIAN_MIN_T", 20.0)
    CANDLE_MIN_VOLUME_USD             : float = _get_float("CANDLE_MIN_VOLUME_USD", 500.0)
    CANDLE_UPDOWN_MOM_VOL_FACTOR      : float = _get_float("CANDLE_UPDOWN_MOM_VOL_FACTOR", 0.003)
    HOURLY_LOCK_T1_PCT                : float = _get_float("HOURLY_LOCK_T1_PCT", 80.0)
    HOURLY_LOCK_T2_PCT                : float = _get_float("HOURLY_LOCK_T2_PCT", 50.0)
    HOURLY_LOCK_T1_MIN_REMAINING      : float = _get_float("HOURLY_LOCK_T1_MIN_REMAINING", 20.0)
    HOURLY_LOCK_T2_MIN_REMAINING      : float = _get_float("HOURLY_LOCK_T2_MIN_REMAINING", 35.0)
    UPDOWN_HOURLY_MACRO_TREND_GATE    : bool  = _get_bool("UPDOWN_HOURLY_MACRO_TREND_GATE", False)

    UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT: int   = _get_int("UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT", 6)

    UPDOWN_HOURLY_T_TIER_TIGHT_MAX     : float = _get_float("UPDOWN_HOURLY_T_TIER_TIGHT_MAX", 35.0)
    UPDOWN_HOURLY_T_TIER_CRITICAL_MAX  : float = _get_float("UPDOWN_HOURLY_T_TIER_CRITICAL_MAX", 25.0)
    UPDOWN_HOURLY_T_EDGE_MULT_TIGHT    : float = _get_float("UPDOWN_HOURLY_T_EDGE_MULT_TIGHT", 1.5)
    UPDOWN_HOURLY_T_EDGE_MULT_CRITICAL : float = _get_float("UPDOWN_HOURLY_T_EDGE_MULT_CRITICAL", 2.0)
    UPDOWN_HOURLY_CONVICTION_BONUS     : float = _get_float("UPDOWN_HOURLY_CONVICTION_BONUS", 1.5)
    FLASH_CRASH_HARD_SKIP             : bool  = _get_bool("FLASH_CRASH_HARD_SKIP", True)
    CANDLE_HOLD_LIMIT_MIN             : float = _get_float("CANDLE_HOLD_LIMIT_MIN", 5.0)
    CANDLE_HOLD_LIMIT_PNL_PCT         : float = _get_float("CANDLE_HOLD_LIMIT_PNL_PCT", 0.0)
    CANDLE_ONE_SIDED_HIGH             : float = _get_float("CANDLE_ONE_SIDED_HIGH", 0.82)
    CANDLE_ONE_SIDED_LOW              : float = _get_float("CANDLE_ONE_SIDED_LOW", 0.18)
    CANDLE_VELOCITY_THR               : float = _get_float("CANDLE_VELOCITY_THR", 0.05)

    RISK_BASE_SIZE_PCT : float = _get_float("RISK_BASE_SIZE_PCT", 0.25)
    RISK_MIN_SIZE_PCT  : float = _get_float("RISK_MIN_SIZE_PCT", 0.08)
    RISK_MAX_SIZE_PCT  : float = _get_float("RISK_MAX_SIZE_PCT", 0.40)

    CB_ENABLED       : bool    = _get_bool("CB_ENABLED", False)
    SALDO_AWAL       : Decimal = _get_decimal("SALDO_AWAL", "1000")
    POLLING_INTERVAL : int     = _get_int("POLLING_INTERVAL_DETIK", 5)
    LOG_LEVEL        : str     = _get("LOG_LEVEL", "INFO")
    DRY_RUN          : bool    = _get_bool("DRY_RUN", True)

    GEMINI_API_KEY              : str   = _get("GEMINI_API_KEY", "")
    POLYMARKET_GEO_TOKEN        : str   = _get("POLYMARKET_GEO_TOKEN", "")
    SCOUT_MIN_VOLUME_24H        : float = _get_float("SCOUT_MIN_VOLUME_24H", 50_000.0)
    SCOUT_MAX_SPREAD_PCT        : float = _get_float("SCOUT_MAX_SPREAD_PCT", 0.08)
    SCOUT_CATEGORIES            : str   = _get("SCOUT_CATEGORIES", "Crypto,Politics")
    SCOUT_GEMINI_MODEL          : str   = _get("SCOUT_GEMINI_MODEL", "gemini-2.0-flash-lite")
    SCOUT_GEMINI_MAX_CANDIDATES : int   = _get_int("SCOUT_GEMINI_MAX_CANDIDATES", 10)
    SCOUT_INTERVAL_MINUTES      : int   = _get_int("SCOUT_INTERVAL_MINUTES", 5)


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
