"""Market Maker Method state machine — one state per symbol.

Tracks BTMM Level (L1/L2/L3) and directional Bias, applying transitions
as new indicator signals arrive. Persists state through db.upsert_state
so a restart resumes the correct cycle position.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Optional

from db import get_state, upsert_state
from models import Bias, IndicatorSignal, Level, SignalType

log = logging.getLogger(__name__)


@dataclass
class SymbolState:
    symbol: str
    level: Level = Level.NEUTRAL
    bias: Bias = Bias.NEUTRAL
    anchor_wick: Optional[float] = None


class BTMMEngine:
    def __init__(self) -> None:
        self._states: Dict[str, SymbolState] = {}
        self._lock = asyncio.Lock()

    async def state(self, symbol: str) -> SymbolState:
        async with self._lock:
            return self._load(symbol)

    def _load(self, symbol: str) -> SymbolState:
        if symbol not in self._states:
            row = get_state(symbol)
            if row:
                self._states[symbol] = SymbolState(
                    symbol=symbol,
                    level=Level(row["level"]),
                    bias=Bias(row["bias"]),
                    anchor_wick=row["anchor_wick"],
                )
            else:
                self._states[symbol] = SymbolState(symbol=symbol)
        return self._states[symbol]

    async def apply(self, sig: IndicatorSignal) -> SymbolState:
        """Transition state per Mauro's cycle rules and persist. Returns new state."""
        async with self._lock:
            st = self._load(sig.symbol)
            t = sig.signal_type

            if t == SignalType.PFL:
                st.level, st.bias = Level.L1, Bias.LONG
                st.anchor_wick = sig.anchor_wick or sig.trigger_price

            elif t == SignalType.PFH:
                st.level, st.bias = Level.L1, Bias.SHORT
                st.anchor_wick = sig.anchor_wick or sig.trigger_price

            elif t == SignalType.SUPER_PFL:
                # Super-cycle bottom — exhaustion of extended down move
                st.level = Level.L3
                if st.bias == Bias.NEUTRAL:
                    st.bias = Bias.LONG
                st.anchor_wick = sig.anchor_wick or st.anchor_wick

            elif t == SignalType.SUPER_PFH:
                st.level = Level.L3
                if st.bias == Bias.NEUTRAL:
                    st.bias = Bias.SHORT
                st.anchor_wick = sig.anchor_wick or st.anchor_wick

            elif t == SignalType.EMA_RESET_L3:
                st.level = Level.L3  # tighten stops, no new continuation

            elif t == SignalType.EMA_RESET_L2:
                # Promote L1 → L2 only when a directional bias is established
                if st.bias != Bias.NEUTRAL and st.level in (Level.L1, Level.L2):
                    st.level = Level.L2

            elif t == SignalType.PRIME_LONG:
                if st.bias != Bias.LONG:
                    # Fresh sweep-into-fib reversal — treat as L1 long
                    st.level, st.bias = Level.L1, Bias.LONG
                    st.anchor_wick = sig.anchor_wick or sig.trigger_price

            elif t == SignalType.PRIME_SHORT:
                if st.bias != Bias.SHORT:
                    st.level, st.bias = Level.L1, Bias.SHORT
                    st.anchor_wick = sig.anchor_wick or sig.trigger_price

            self._states[sig.symbol] = st
            upsert_state(sig.symbol, st.level.value, st.bias.value, st.anchor_wick)
            log.info("BTMM %s → level=%s bias=%s anchor=%s (from %s)",
                     st.symbol, st.level.value, st.bias.value,
                     st.anchor_wick, t.value)
            return st

    @staticmethod
    def is_actionable(sig: IndicatorSignal, st: SymbolState) -> Optional[str]:
        """Return None if we should trade this signal, else a rejection reason."""
        # L3: no new continuation trades — trailing/exit only
        if st.level == Level.L3 and sig.signal_type in (
            SignalType.EMA_RESET_L2, SignalType.EMA_RESET_L3,
            SignalType.SUPER_PFH, SignalType.SUPER_PFL,
        ):
            return "L3 exhaustion — no new continuation trades"

        if sig.signal_type == SignalType.EMA_RESET_L2 and st.bias == Bias.NEUTRAL:
            return "L2 continuation requires an established L1 bias"

        if sig.signal_type in (SignalType.PRIME_LONG, SignalType.PRIME_SHORT):
            if sig.fib_618 is None or sig.fib_786 is None:
                return "PRIME setup missing .618/.786 fib zone data"

        if st.bias == Bias.NEUTRAL and sig.signal_type not in (
            SignalType.PFH, SignalType.PFL,
            SignalType.PRIME_LONG, SignalType.PRIME_SHORT,
        ):
            return "no established bias for this signal type"

        return None
