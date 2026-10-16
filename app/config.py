"""Environment-driven settings. Every knob has a sane default so the app runs offline."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # Server (used by run.py): set PORT in the environment or .env
    port: int = Field(default=8080, ge=1, le=65535)

    # Storage
    database_path: str = str(BASE_DIR / "signalshift.db")

    # Business locale. All timestamps are stored as naive datetimes in this timezone.
    timezone: str = "Asia/Kolkata"
    currency_symbol: str = "₹"
    calendar_file: str = str(BASE_DIR / "app" / "data" / "calendar_in.json")

    # Synthetic data
    seed: int = 2026

    # Gemini (optional). Model names come only from env, never hardcoded.
    gemini_api_key: str | None = None
    gemini_model: str | None = None
    gemini_fallback_model: str | None = None
    gemini_timeout_seconds: float = 30.0
    gemini_temperature: float = 0.2

    # Telegram (optional)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_min_severity: str = "warning"  # send critical + warning; opportunities go in the daily briefing
    telegram_mode: str = "polling"  # polling (no public URL needed) or webhook
    telegram_webhook_secret: str | None = None
    telegram_api_base: str = "https://api.telegram.org"

    # Engine rules
    ad_pause_lag_hours: float = 4.0  # Critical when cover is shorter than the time it takes to pause ads
    alert_cooldown_hours: float = 6.0
    cooldown_redrop_pct: float = 0.30  # re-alert inside cooldown if cover drops by more than this
    low_cover_hours: float = 24.0  # a size counts as "low" for click-share purposes below this
    warning_buffer_days: float = 2.0  # added to lead time when no restock ETA is known
    opportunity_lookahead_days: int = 30
    opportunity_min_shortfall: int = 5
    budget_shift_min_cover_days: float = 14.0
    budget_shift_max_targets: int = 2
    pause_click_share: float = 0.6  # pause the whole campaign when this much of its clicks are at risk
    waste_horizon_hours: float = 72.0  # wasted-spend projection is bounded to this horizon
    reorder_cover_days: int = 14  # reorder to cover lead time + this many days of projected demand

    # Forecast
    velocity_half_life_hours: float = 72.0
    velocity_lookback_days: int = 14
    size_mix_lookback_days: int = 90
    surge_window_minutes: int = 120
    surge_multiplier: float = 2.0  # anomaly when recent velocity exceeds forecast by this factor
    surge_min_units: int = 3

    # Mock live ad delivery (synthetic; stands in for the ad platform reporting spend while the app runs)
    ad_sim_enabled: bool = True
    ad_sim_interval_seconds: float = 30.0
    ad_sim_speed: float = 1.0  # >1 compresses time so spend counters move visibly during a short demo

    # Scheduler
    scheduler_enabled: bool = True
    startup_sweep: bool = True
    briefing_hour: int = 8
    recalibration_hour: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
