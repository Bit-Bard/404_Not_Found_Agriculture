from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _require(name: str, value: Optional[str]) -> str:
    if value is None or not value.strip():
        raise ValueError(f"Missing required env var: {name}")
    return value.strip()


def _as_int(name: str, value: Optional[str], default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as e:
        raise ValueError(f"Invalid int for {name}: {value}") from e


@dataclass(frozen=True)
class Settings:
    # Core
    app_env: str
    log_level: str
    timezone: str

    # Storage
    store_backend: str  # "json" | "mysql"
    data_dir: Path
    store_file: Path

    # Media
    media_dir: Path

    # OpenAI
    openai_api_key: str
    openai_model: str
    openai_base_url: Optional[str]

    # Tavily
    tavily_api_key: str
    tavily_max_results: int

    # OpenWeather
    openweather_api_key: str
    openweather_units: str  # metric/imperial/standard

    # Telegram
    telegram_bot_token: str

    # MySQL
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str

    # Table names (THIS fixes your crash)
    mysql_sessions_table: str
    mysql_farmers_table: str
    mysql_images_table: str

    @classmethod
    def from_env(cls) -> "Settings":
        import os

        app_env = os.getenv("APP_ENV", "dev").strip()
        log_level = os.getenv("LOG_LEVEL", "INFO").strip()
        timezone = os.getenv("TIMEZONE", "Asia/Kolkata").strip()

        store_backend = os.getenv("STORE_BACKEND", "mysql").strip().lower()
        if store_backend not in {"json", "mysql"}:
            raise ValueError("STORE_BACKEND must be one of: json, mysql")

        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        store_file = Path(os.getenv("STORE_FILE", str(data_dir / "state_store.json"))).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        media_dir = Path(os.getenv("MEDIA_DIR", str(data_dir / "media"))).resolve()
        media_dir.mkdir(parents=True, exist_ok=True)

        openai_api_key = _require("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))
        openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
        openai_base_url = os.getenv("OPENAI_BASE_URL")
        openai_base_url = openai_base_url.strip() if openai_base_url and openai_base_url.strip() else None

        tavily_api_key = _require("TAVILY_API_KEY", os.getenv("TAVILY_API_KEY"))
        tavily_max_results = _as_int("TAVILY_MAX_RESULTS", os.getenv("TAVILY_MAX_RESULTS"), default=5)

        openweather_api_key = _require("OPENWEATHER_API_KEY", os.getenv("OPENWEATHER_API_KEY"))
        openweather_units = os.getenv("OPENWEATHER_UNITS", "metric").strip().lower()
        if openweather_units not in {"metric", "imperial", "standard"}:
            raise ValueError("OPENWEATHER_UNITS must be one of: metric, imperial, standard")

        telegram_bot_token = _require("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN"))

        mysql_host = os.getenv("MYSQL_HOST", "127.0.0.1").strip()
        mysql_port = _as_int("MYSQL_PORT", os.getenv("MYSQL_PORT"), default=3306)
        mysql_user = os.getenv("MYSQL_USER", "root").strip()
        mysql_password = os.getenv("MYSQL_PASSWORD", "") or ""
        mysql_database = os.getenv("MYSQL_DATABASE", "agentic_crop_advisor").strip()

        # These env names MUST match what db.py expects (and your .env)
        mysql_sessions_table = os.getenv("MYSQL_SESSIONS_TABLE", "sessions").strip()
        mysql_farmers_table = os.getenv("MYSQL_FARMERS_TABLE", "farmers").strip()
        mysql_images_table = os.getenv("MYSQL_IMAGES_TABLE", "crop_images").strip()

        if store_backend == "mysql":
            if not mysql_host:
                raise ValueError("MYSQL_HOST is required when STORE_BACKEND=mysql")
            if not mysql_user:
                raise ValueError("MYSQL_USER is required when STORE_BACKEND=mysql")
            if not mysql_database:
                raise ValueError("MYSQL_DATABASE is required when STORE_BACKEND=mysql")
            if not mysql_sessions_table:
                raise ValueError("MYSQL_SESSIONS_TABLE is required when STORE_BACKEND=mysql")
            if not mysql_farmers_table:
                raise ValueError("MYSQL_FARMERS_TABLE is required when STORE_BACKEND=mysql")
            if not mysql_images_table:
                raise ValueError("MYSQL_IMAGES_TABLE is required when STORE_BACKEND=mysql")

        return cls(
            app_env=app_env,
            log_level=log_level,
            timezone=timezone,
            store_backend=store_backend,
            data_dir=data_dir,
            store_file=store_file,
            media_dir=media_dir,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            openai_base_url=openai_base_url,
            tavily_api_key=tavily_api_key,
            tavily_max_results=tavily_max_results,
            openweather_api_key=openweather_api_key,
            openweather_units=openweather_units,
            telegram_bot_token=telegram_bot_token,
            mysql_host=mysql_host,
            mysql_port=mysql_port,
            mysql_user=mysql_user,
            mysql_password=mysql_password,
            mysql_database=mysql_database,
            mysql_sessions_table=mysql_sessions_table,
            mysql_farmers_table=mysql_farmers_table,
            mysql_images_table=mysql_images_table,
        )
