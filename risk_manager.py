"""Volatility-aware sizing, structural SL/TP, kill-zone and news gating.

All monetary calculations are made in the base account currency (USD by
default). Pip values for cross pairs are approximate; for maximum accuracy
they should be recomputed live from the counter-pair quote.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import List, Optional

import httpx

from config import (
    ADR_HIGH_THRESHOLD, ADR_LOW_THRESHOLD, KILL_ZONES,
    NEWS_API_URL, PIP_VALUE_USD_PER_LOT, RISK, TIER1_KEYWORDS,
)
from models import Bias, IndicatorSignal, Level, OrderSide, SignalType, TradePlan

log = logging.getLogger(__name__)


# ---------- Kill Zones ----------

def _in_window(now: time, start: time, end: time) -> bool:
    return start <= now <= end


def in_kill_zone(now_utc: Optional[datetime] = None) -> Optional[str]:
    now = (now_utc or datetime.now(timezone.utc)).time()
    for kz in KILL_ZONES:
        if _in_window(now, kz.start, kz.end):
            return kz.name
    return None


def kill_zone_ends_at(now_utc: Optional[datetime] = None) -> Optional[datetime]:
    now = now_utc or datetime.now(timezone.utc)
    for kz in KILL_ZONES:
        if _in_window(now.time(), kz.start, kz.end):
            return now.replace(hour=kz.end.hour, minute=kz.end.minute,
                                second=0, microsecond=0)
    return None


# ---------- Economic Calendar ----------

@dataclass
class NewsEvent:
    title: str
    country: str
    impact: str
    ts: datetime


class NewsCalendar:
    """Polls ForexFactory (faireconomy mirror) for tier-1 events."""

    def __init__(self, url: str = NEWS_API_URL) -> None:
        self.url = url
        self._events: List[NewsEvent] = []
        self.last_refresh: Optional[datetime] = None

    async def refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(self.url)
                r.raise_for_status()
                raw = r.json()
        except Exception as e:
            log.warning("news calendar fetch failed: %s", e)
            return

        events: List[NewsEvent] = []
        for item in raw:
            impact = str(item.get("impact", "")).lower()
            if impact != "high":
                continue
            title = str(item.get("title") or item.get("event") or "")
            date_raw = item.get("date") or item.get("timestamp")
            if not date_raw:
                continue
            try:
                if isinstance(date_raw, (int, float)):
                    ts = datetime.fromtimestamp(int(date_raw), tz=timezone.utc)
                else:
                    ts = datetime.fromisoformat(
                        str(date_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            # Keep all high-impact; keyword list is only a hint
            events.append(NewsEvent(
                title=title,
                country=str(item.get("country", "")),
                impact=impact, ts=ts,
            ))
        self._events = events
        self.last_refresh = datetime.now(timezone.utc)
        log.info("news calendar: %d tier-1 events loaded", len(events))

    def upcoming_within(self, minutes: int,
                        now_utc: Optional[datetime] = None) -> List[NewsEvent]:
        now = now_utc or datetime.now(timezone.utc)
        return [e for e in self._events
                if 0 <= (e.ts - now).total_seconds() <= minutes * 60]

    def within_window(self, minutes: int,
                      now_utc: Optional[datetime] = None) -> Optional[NewsEvent]:
        now = now_utc or datetime.now(timezone.utc)
        for e in self._events:
            if abs((e.ts - now).total_seconds()) / 60.0 <= minutes:
                return e
        return None

    @staticmethod
    def is_tier1(title: str) -> bool:
        t = title.lower()
        return any(k.lower() in t for k in TIER1_KEYWORDS)


# ---------- Sizing & Plan Construction ----------

def pip_size(symbol: str) -> float:
    return 0.01 if symbol.upper().endswith("JPY") else 0.0001


def pip_value_usd(symbol: str) -> float:
    return PIP_VALUE_USD_PER_LOT.get(symbol.upper(), 10.0)


def adr_class(adr_pips: float) -> str:
    if adr_pips >= ADR_HIGH_THRESHOLD:
        return "HIGH_ADR"
    if adr_pips <= ADR_LOW_THRESHOLD:
        return "LOW_ADR"
    return "MID_ADR"


def _risk_pct(sig: IndicatorSignal, level: Level) -> float:
    if level == Level.L2 and sig.signal_type == SignalType.EMA_RESET_L2:
        return RISK.l2_pct
    return RISK.l1_pct


def compute_lots(equity_usd: float, risk_pct: float,
                 sl_pips: float, symbol: str) -> float:
    """Standard fixed-fractional sizing scaled by structural SL."""
    if sl_pips <= 0 or equity_usd <= 0:
        return 0.0
    risk_usd = equity_usd * risk_pct
    lots = risk_usd / (sl_pips * pip_value_usd(symbol))
    return max(round(lots, 2), 0.01)


def build_plan(sig: IndicatorSignal, level: Level, bias: Bias,
               equity_usd: float) -> Optional[TradePlan]:
    """Assemble entry, structural SL, three TPs, and sized lots.

    SL is placed `RISK.sl_buffer_pips` beyond the Anchor Wick as required
    by BTMM structural rules — never a fixed pip stop divorced from context.
    """
    if bias == Bias.NEUTRAL:
        return None

    pip = pip_size(sig.symbol)
    anchor = sig.anchor_wick or sig.trigger_price
    entry = sig.trigger_price

    if bias == Bias.LONG:
        side = OrderSide.BUY
        sl = anchor - RISK.sl_buffer_pips * pip
    else:
        side = OrderSide.SELL
        sl = anchor + RISK.sl_buffer_pips * pip

    sl_pips = abs(entry - sl) / pip
    if sl_pips <= 0:
        log.warning("degenerate SL for %s (entry=%s sl=%s)",
                    sig.symbol, entry, sl)
        return None

    risk_pct = _risk_pct(sig, level)
    lots = compute_lots(equity_usd, risk_pct, sl_pips, sig.symbol)

    # TP1: retest of the White 200 EMA (fallback to 1R if EMA absent)
    if sig.ema_200:
        tp1 = sig.ema_200
    else:
        tp1 = entry + sl_pips * pip if side == OrderSide.BUY \
            else entry - sl_pips * pip

    # TP2: 0.272 Fibonacci extension of the initial swing (entry ↔ anchor)
    swing = abs(entry - anchor)
    if side == OrderSide.BUY:
        tp2 = entry + RISK.tp2_fib_extension * swing
        tp3 = entry + sig.daily_adr_pips * pip
    else:
        tp2 = entry - RISK.tp2_fib_extension * swing
        tp3 = entry - sig.daily_adr_pips * pip

    rationale = (
        f"{sig.signal_type.value} · {level.value}/{bias.value} · "
        f"anchor={anchor:.5f} SL={sl_pips:.1f}p · "
        f"ADR={sig.daily_adr_pips:.0f}p ({adr_class(sig.daily_adr_pips)}) · "
        f"risk={risk_pct*100:.2f}%"
    )

    return TradePlan(
        symbol=sig.symbol, side=side,
        entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        lots=lots, risk_pct=risk_pct,
        level=level, signal_type=sig.signal_type, rationale=rationale,
    )
