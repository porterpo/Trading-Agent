"""Async order routing with a three-stage TP ladder.

Three backends:
    * MT5 native adapter for MetaTrader 5 brokers (HFM, EasyMarkets, etc.) —
      selected when `EXCHANGE_ID=mt5`. Windows only.
    * CCXT for crypto venues (Binance, Bybit, etc.)
    * A paper-trading simulator used when the configured exchange id is
      `paper` or an unknown value that isn't in ccxt.

Position lifecycle:
    Open  → market entry with bracket (SL + TP1)
    TP1   → close 30%, move SL to breakeven
    TP2   → close 40%
    TP3   → close remaining 30% (or trail behind Red 13 EMA when available)
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from typing import Dict, Optional

import ccxt.async_support as ccxt

from config import EXCHANGE, RISK
from db import log_trade, update_trade_status
from models import OrderSide, TradePlan

log = logging.getLogger(__name__)


PAPER_EXCHANGE_IDS = {"paper"}
MT5_EXCHANGE_ID = "mt5"


@dataclass
class OpenPosition:
    trade_id: int
    plan: TradePlan
    broker_order_id: str
    remaining_lots: float
    tp1_hit: bool = False
    tp2_hit: bool = False


class _PaperBroker:
    """Minimal in-memory broker used when no ccxt venue is configured.

    Simulates: fetch_balance (fixed equity), fetch_ticker (last-seen price),
    create_order (assigns id), edit_order (no-op), close (no-op). Prices for
    fetch_ticker come from the most recent order's price so TP checks work.
    """

    def __init__(self, starting_equity: float = 10_000.0) -> None:
        self._equity = starting_equity
        self._last_price: Dict[str, float] = {}
        self._counter = itertools.count(1)

    async def fetch_balance(self) -> dict:
        return {"total": {"USD": self._equity}}

    async def fetch_ticker(self, symbol: str) -> dict:
        return {"last": self._last_price.get(symbol, 0.0)}

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list:
        return []

    async def create_order(self, symbol: str, type: str, side: str,
                           amount: float, price: Optional[float] = None,
                           params: Optional[dict] = None) -> dict:
        params = params or {}
        px = float(price or params.get("price") or self._last_price.get(symbol, 0.0))
        if px:
            self._last_price[symbol] = px
        oid = f"paper-{next(self._counter)}"
        log.info("[paper] %s %s %s %.4f @ %s params=%s",
                 type, side, symbol, amount, px, params)
        return {"id": oid, "symbol": symbol, "side": side,
                "amount": amount, "price": px, "params": params}

    async def edit_order(self, order_id: str, symbol: str,
                         params: Optional[dict] = None) -> dict:
        log.info("[paper] edit %s %s %s", order_id, symbol, params)
        return {"id": order_id, "params": params or {}}

    async def close(self) -> None:
        pass

    def set_sandbox_mode(self, on: bool) -> None:
        pass


class ExchangeClient:
    """Async wrapper — dispatches to CCXT or the paper broker."""

    def __init__(self) -> None:
        eid = EXCHANGE.exchange_id.lower()
        if eid == MT5_EXCHANGE_ID:
            from mt5_broker import MT5Broker
            self.is_paper = False
            self.ex = MT5Broker()
        elif eid in PAPER_EXCHANGE_IDS or not hasattr(ccxt, eid):
            if eid not in PAPER_EXCHANGE_IDS:
                log.warning(
                    "ccxt has no exchange '%s' — falling back to paper broker",
                    EXCHANGE.exchange_id,
                )
            self.is_paper = True
            self.ex = _PaperBroker()
        else:
            self.is_paper = False
            cls = getattr(ccxt, eid)
            self.ex = cls({
                "apiKey": EXCHANGE.api_key,
                "secret": EXCHANGE.api_secret,
                "enableRateLimit": True,
                "options": {"accountId": EXCHANGE.account_id},
            })
            if EXCHANGE.sandbox and hasattr(self.ex, "set_sandbox_mode"):
                try:
                    self.ex.set_sandbox_mode(True)
                except Exception as e:
                    log.warning("sandbox mode not supported: %s", e)

    async def close(self) -> None:
        try:
            await self.ex.close()
        except Exception:
            pass

    async def fetch_equity_usd(self) -> float:
        try:
            bal = await self.ex.fetch_balance()
        except Exception as e:
            log.warning("fetch_balance failed: %s", e)
            return 0.0
        total = bal.get("total", {}) if isinstance(bal, dict) else {}
        return float(
            total.get("USD")
            or total.get("USDT")
            or bal.get("info", {}).get("balance", 0.0)
            or 0.0
        )

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "1d",
                          limit: int = 20) -> list:
        return await self.ex.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def fetch_last_price(self, symbol: str) -> float:
        t = await self.ex.fetch_ticker(symbol)
        return float(t.get("last") or t.get("close") or 0.0)

    async def place_bracket(self, plan: TradePlan) -> str:
        """Market order with attached SL + TP1. TP2/TP3 managed in-agent."""
        params = {
            "stopLoss":   {"type": "stop",  "price": plan.sl},
            "takeProfit": {"type": "limit", "price": plan.tp1},
        }
        order = await self.ex.create_order(
            symbol=plan.symbol,
            type="market",
            side=plan.side.value,
            amount=plan.lots,
            params=params,
        )
        return str(order.get("id") or order.get("orderId") or "")

    async def modify_sl(self, symbol: str, order_id: str, new_sl: float) -> None:
        try:
            await self.ex.edit_order(order_id, symbol,
                                     params={"stopLoss": new_sl})
        except Exception as e:
            log.warning("modify_sl failed %s: %s", order_id, e)

    async def close_partial(self, symbol: str, side: OrderSide,
                            lots: float) -> None:
        if lots < 0.01:
            return
        opposite = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        await self.ex.create_order(symbol, "market", opposite.value, lots)


class PositionManager:
    """Monitors open positions and progresses the TP ladder."""

    def __init__(self, ex: ExchangeClient) -> None:
        self.ex = ex
        self._positions: Dict[int, OpenPosition] = {}
        self._stop = asyncio.Event()

    async def open(self, plan: TradePlan) -> Optional[OpenPosition]:
        try:
            order_id = await self.ex.place_bracket(plan)
        except Exception as e:
            log.exception("order placement failed: %s", e)
            return None

        trade_id = log_trade(plan, order_id)
        pos = OpenPosition(
            trade_id=trade_id, plan=plan,
            broker_order_id=order_id, remaining_lots=plan.lots,
        )
        self._positions[trade_id] = pos
        log.info("opened trade %d: %s %s %.2f @ %.5f",
                 trade_id, plan.symbol, plan.side.value, plan.lots, plan.entry)
        return pos

    async def force_partial_close(self, fraction: float) -> None:
        """Pre-news protective close of `fraction` of every open position;
        moves remaining SL to breakeven per BTMM news protocol."""
        for pos in list(self._positions.values()):
            lots = round(pos.remaining_lots * fraction, 2)
            if lots < 0.01:
                continue
            try:
                await self.ex.close_partial(pos.plan.symbol, pos.plan.side, lots)
                pos.remaining_lots = round(pos.remaining_lots - lots, 2)
                await self.ex.modify_sl(pos.plan.symbol,
                                         pos.broker_order_id, pos.plan.entry)
                log.info("pre-news: closed %.2f of %s, SL→BE",
                         lots, pos.plan.symbol)
            except Exception as e:
                log.warning("pre-news close failed: %s", e)

    async def _check(self, pos: OpenPosition) -> None:
        try:
            price = await self.ex.fetch_last_price(pos.plan.symbol)
        except Exception as e:
            log.debug("price fetch failed %s: %s", pos.plan.symbol, e)
            return
        if price <= 0:
            return

        long_side = pos.plan.side == OrderSide.BUY

        # TP1: close configured fraction, move SL → breakeven
        tp1_hit = (price >= pos.plan.tp1) if long_side else (price <= pos.plan.tp1)
        if tp1_hit and not pos.tp1_hit:
            lots = round(pos.plan.lots * RISK.tp1_close_fraction, 2)
            await self.ex.close_partial(pos.plan.symbol, pos.plan.side, lots)
            pos.remaining_lots = round(pos.remaining_lots - lots, 2)
            await self.ex.modify_sl(pos.plan.symbol, pos.broker_order_id,
                                     pos.plan.entry)
            pos.tp1_hit = True
            log.info("TP1 %s: closed %.2f, SL→BE", pos.plan.symbol, lots)

        # TP2: close configured fraction
        tp2_hit = (price >= pos.plan.tp2) if long_side else (price <= pos.plan.tp2)
        if tp2_hit and pos.tp1_hit and not pos.tp2_hit:
            lots = round(pos.plan.lots * RISK.tp2_close_fraction, 2)
            await self.ex.close_partial(pos.plan.symbol, pos.plan.side, lots)
            pos.remaining_lots = round(pos.remaining_lots - lots, 2)
            pos.tp2_hit = True
            log.info("TP2 %s: closed %.2f", pos.plan.symbol, lots)

        # TP3 = 1x Daily ADR: close remainder
        tp3_hit = (price >= pos.plan.tp3) if long_side else (price <= pos.plan.tp3)
        if tp3_hit and pos.tp2_hit and pos.remaining_lots >= 0.01:
            await self.ex.close_partial(pos.plan.symbol, pos.plan.side,
                                         pos.remaining_lots)
            update_trade_status(pos.trade_id, "CLOSED_TP3")
            self._positions.pop(pos.trade_id, None)
            log.info("TP3 %s: fully closed", pos.plan.symbol)

    async def monitor_loop(self, interval: float = 3.0) -> None:
        while not self._stop.is_set():
            for pos in list(self._positions.values()):
                try:
                    await self._check(pos)
                except Exception as e:
                    log.exception("monitor check failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    @property
    def open_count(self) -> int:
        return len(self._positions)
