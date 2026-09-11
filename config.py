"""Central configuration.

All runtime tunables live here — loaded from environment (via .env) and
exposed as frozen dataclasses so downstream modules pass them around
without accidental mutation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from typing import Dict, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v else default


def _env_float(key: str, default: float) -> float:
    v = os.getenv(key)
    return float(v) if v else default


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class KillZone:
    name: str
    start: time
    end: time


@dataclass(frozen=True)
class RiskConfig:
    l1_pct: float = 0.005          # L1 counter-trend reversal — smaller size
    l2_pct: float = 0.02           # L2 max-conviction continuation
    l2_pct_min: float = 0.015      # Lower bound for L2 sizing
    max_daily_loss_pct: float = 0.04
    max_concurrent_positions: int = 4
    sl_buffer_pips: float = 4.0    # 3-5 pip buffer past the anchor wick
    news_freeze_minutes: int = 30
    pre_news_partial_close_pct: float = 0.50
    tp1_close_fraction: float = 0.30
    tp2_close_fraction: float = 0.40
    tp2_fib_extension: float = 1.272


@dataclass(frozen=True)
class ExecConfig:
    exchange_id: str = "oanda"
    account_id: str = ""
    api_key: str = ""
    api_secret: str = ""
    sandbox: bool = True
    default_leverage: int = 30


@dataclass(frozen=True)
class NotificationConfig:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@dataclass(frozen=True)
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    shared_secret: str = ""


# Institutional session windows (UTC)
KILL_ZONES: Tuple[KillZone, ...] = (
    KillZone("London",   time(7, 0),  time(10, 0)),
    KillZone("NewYork",  time(12, 0), time(15, 0)),
)

# USD/pip per 1 standard lot (100k units). Approximations — recomputed from
# live quotes for cross pairs at plan-build time when a ticker is available.
PIP_VALUE_USD_PER_LOT: Dict[str, float] = {
    "EURUSD": 10.0, "GBPUSD": 10.0, "AUDUSD": 10.0, "NZDUSD": 10.0,
    "USDJPY": 9.0,  "USDCAD": 7.5,  "USDCHF": 11.0,
    "EURGBP": 12.5, "EURJPY": 9.0,  "GBPJPY": 9.0,
    "GBPNZD": 6.0,  "AUDNZD": 6.0,  "EURAUD": 6.5,
}

# ADR classification (pips) — controls sizing bands
ADR_HIGH_THRESHOLD: float = 100.0
ADR_LOW_THRESHOLD: float = 40.0

# Tier-1 news keywords for pre-freeze detection
TIER1_KEYWORDS: Tuple[str, ...] = (
    "Non-Farm", "NFP", "Nonfarm", "CPI", "FOMC", "Federal Funds",
    "Interest Rate", "Rate Decision", "Retail Sales", "GDP",
    "Unemployment Rate", "PPI", "ECB", "BoE", "BoJ", "RBA", "RBNZ",
)

RISK = RiskConfig()

EXCHANGE = ExecConfig(
    exchange_id=_env("EXCHANGE_ID", "paper"),
    account_id=_env("EXCHANGE_ACCOUNT_ID"),
    api_key=_env("EXCHANGE_API_KEY"),
    api_secret=_env("EXCHANGE_API_SECRET"),
    sandbox=_env_bool("EXCHANGE_SANDBOX", True),
    default_leverage=_env_int("DEFAULT_LEVERAGE", 30),
)

NOTIFY = NotificationConfig(
    telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
    telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
)

WEBHOOK = WebhookConfig(
    host=_env("WEBHOOK_HOST", "0.0.0.0"),
    # Railway/Heroku/Fly all inject PORT — respect it, otherwise fall back
    port=_env_int("PORT", _env_int("WEBHOOK_PORT", 8000)),
    shared_secret=_env("WEBHOOK_SECRET"),
)

DB_PATH: str = _env("DB_PATH", "trading_agent.db")
NEWS_API_URL: str = _env("NEWS_API_URL",
                          "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
INDICATOR_CONTROL_URL: str = _env("INDICATOR_CONTROL_URL")
