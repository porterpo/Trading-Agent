"""Shared enums and dataclasses used across the agent."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalType(str, Enum):
    PFH = "PFH Confirmed"
    PFL = "PFL Confirmed"
    SUPER_PFH = "SUPER CYCLE PFH"
    SUPER_PFL = "SUPER CYCLE PFL"
    EMA_RESET_L2 = "200 EMA Reset L2"
    EMA_RESET_L3 = "200 EMA Reset L3"
    PRIME_LONG = "PRIME LONG"
    PRIME_SHORT = "PRIME SHORT"


class Level(str, Enum):
    NEUTRAL = "NEUTRAL"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class Bias(str, Enum):
    NEUTRAL = "NEUTRAL"
    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class IndicatorSignal:
    symbol: str
    timeframe: str
    signal_type: SignalType
    trigger_price: float
    active_level: Level
    daily_adr_pips: float
    timestamp: datetime
    anchor_wick: Optional[float] = None
    fib_618: Optional[float] = None
    fib_786: Optional[float] = None
    ema_200: Optional[float] = None


@dataclass
class TradePlan:
    symbol: str
    side: OrderSide
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    lots: float
    risk_pct: float
    level: Level
    signal_type: SignalType
    rationale: str
