from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """
    Single source of truth for runtime configuration.

    Load order:
      1. .env.local  (non-secret runtime config / overrides)
      2. .env.secret (gitignored — wallet keys live here)
      3. real environment variables (override both)

    pydantic-settings handles all three in that precedence, so a developer
    can keep secrets in one file and tweak runtime knobs in another without
    them tangling.
    """

    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env.secret"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Polymarket credentials (required) ---
    pk_private_key: str = ""
    clob_api_key: str = ""
    clob_secret: str = ""
    clob_pass: str = ""

    # --- Optional integrations ---
    fred_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Runtime ---
    dry_run: bool = True
    log_level: str = "INFO"

    # --- Paths ---
    db_path: Path = Path("data/bot.db")
    log_dir: Path = Path("logs")

    # --- Strategies (comma-separated string in env, list internally) ---
    # NoDecode suppresses pydantic-settings' source-layer JSON parsing so the
    # raw env value reaches _split_csv as a plain CSV string (e.g. "contrarian" or
    # "contrarian,other") instead of being json.loads()'d first.
    active_strategies: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["contrarian"]
    )

    # --- Classifier thresholds (TUNE; defaults are the calibrated values) ---
    # price_zone: ascending YES-price boundaries splitting price into 5 zones
    # (extreme_low/low/uncertain/high/extreme_high). Calibrated on 491 markets —
    # see research/calibrate_zones.py. Must stay strictly ascending.
    zone_extreme_low: float = 0.20
    zone_low: float = 0.40
    zone_uncertain: float = 0.60
    zone_high: float = 0.80
    # volatility: rolling per-symbol spot pstdev -> low/mid/high_vol regime.
    vol_window: int = 60          # recent spot prices kept per symbol
    vol_min_samples: int = 10     # fewer than this -> "unknown"
    vol_low_max: float = 0.00003  # vol <= this -> low_vol
    vol_high_min: float = 0.00008  # vol >= this -> high_vol; between -> mid_vol

    # --- Time-gated stop-loss canary (VALIDATED 2026-06-15; FINDINGS *Time-gated
    # stop-loss*) -------------------------------------------------------------
    # Sell the held side if its value <= sl_threshold within the final
    # sl_window_sec, for sl_symbols only. Mechanism = residual salvage of a
    # clearly-dead longshot's last ~10%, NOT prediction. ON by default because
    # running it forward in paper IS the 2nd-regime test (zero money risk). These
    # are VALIDATED values — the kill-switch is for observability, NOT for tuning
    # (tuning voids the test). sl_canary_enabled=False is a one-line disable.
    sl_canary_enabled: bool = True
    sl_window_sec: int = 120      # act only inside the final 2 min before resolve
    sl_threshold: float = 0.10    # held-side value <= this -> stop out
    # symbols the canary acts on (BNB EXCLUDED: ~30% no_exit empty late bid).
    sl_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC", "ETH"]
    )

    # --- Slow-rise EXIT (VALIDATED 2026-06-15; FINDINGS *Take-profit & exit bake-off*)
    # Sell the held side the FIRST time its value reaches slowrise_value (0.40) IF the
    # climb open->0.40 was SLOW (> slowrise_min_sec); a weak rise reverts. Strongest
    # lever the exit search produced (+$1,068; train/test, 5-fold CV, fill-stress −3¢).
    # OFF by default: unlike the SL (which sells near-certain losers), this caps some
    # would-be winners, so leaving it on would contaminate the IN-FLIGHT contrarian
    # 2nd-regime resolved-WR measurement. Flip True to start its own forward paper test.
    slowrise_enabled: bool = False
    slowrise_value: float = 0.40    # held-side value at which a slow riser is sold
    slowrise_min_sec: int = 600     # climb open->value slower than this (10 min) = weak

    # --- Endpoints ---
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    binance_ws_url: str = "wss://stream.binance.com:9443"

    @field_validator("active_strategies", "sl_symbols", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    def require_credentials(self) -> None:
        """Raise if any required credential is missing. Called once at startup."""
        required = ("pk_private_key", "clob_api_key", "clob_secret", "clob_pass")
        missing = [k.upper() for k in required if not getattr(self, k)]
        if missing:
            raise RuntimeError(
                "Missing required env vars: "
                + ", ".join(missing)
                + ". Put them in .env.local or .env.secret."
            )
