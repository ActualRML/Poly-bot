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
    # raw env value reaches _split_csv as a plain CSV string (e.g. "noop" or
    # "noop,momentum") instead of being json.loads()'d first.
    active_strategies: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["contrarian"]
    )

    # --- Endpoints ---
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    binance_rest_url: str = "https://api.binance.com"
    binance_ws_url: str = "wss://stream.binance.com:9443"

    @field_validator("active_strategies", mode="before")
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
