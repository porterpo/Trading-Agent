"""MetaTrader 5 async broker adapter.

Wraps the synchronous `MetaTrader5` package in `asyncio.to_thread` calls so it
plugs into `ExchangeClient` with the same interface as the paper broker and
the ccxt async clients.

Works with any MT5 broker (HFM, EasyMarkets, IC Markets, Pepperstone, ...).
Broker is selected purely by the credentials + `MT5_SERVER` you supply.

Position model:
    * We tag every order this agent places with `mt5_magic` so we can find our
      own positions later.
    * `create_order` opens a new market position — unless there's already an
      existing position on the same symbol with our magic and the incoming
      side is opposite. In that case we treat it as a (partial) close and
      attach the position ticket so hedge-mode accounts reduce the original
      instead of opening a counter-position.
    * `edit_order` uses TRADE_ACTION_SLTP against the position ticket to move
      the stop-loss. Existing TP is preserved.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

try:
    import MetaTrader5 as mt5  # type: ignore
except ImportError:  # pragma: no cover — package is Windows-only
    mt5 = None  # type: ignore

from config import EXCHANGE

log = logging.getLogger(__name__)


_TIMEFRAME_MAP: Dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60 * 1, "4h": 60 * 4,
    "1d": 60 * 24, "1w": 60 * 24 * 7,
}


def _tf_const(timeframe: str) -> int:
    """Map string timeframe to the mt5.TIMEFRAME_* constant."""
    key = timeframe.lower()
    if key not in _TIMEFRAME_MAP:
        raise ValueError(f"unsupported MT5 timeframe: {timeframe}")
    minutes = _TIMEFRAME_MAP[key]
    # mt5.TIMEFRAME_* constants are the minute count for intraday and specific
    # named values for D1/W1/MN1 — resolve by name to stay portable.
    name_map = {
        1: "TIMEFRAME_M1", 5: "TIMEFRAME_M5", 15: "TIMEFRAME_M15",
        30: "TIMEFRAME_M30", 60: "TIMEFRAME_H1", 240: "TIMEFRAME_H4",
        60 * 24: "TIMEFRAME_D1", 60 * 24 * 7: "TIMEFRAME_W1",
    }
    return getattr(mt5, name_map[minutes])


class MT5Broker:
    """Async facade over MetaTrader5. Interface parity with `_PaperBroker`."""

    def __init__(self) -> None:
        if mt5 is None:
            raise RuntimeError(
                "MetaTrader5 package not installed. Run "
                "`pip install MetaTrader5` on Windows."
            )
        self._suffix = EXCHANGE.mt5_symbol_suffix
        self._magic = EXCHANGE.mt5_magic
        self._initialized = False

    # ------------------------------------------------------------------ init
    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        kwargs: Dict[str, Any] = {}
        if EXCHANGE.mt5_terminal_path:
            kwargs["path"] = EXCHANGE.mt5_terminal_path
        if EXCHANGE.account_id:
            kwargs["login"] = int(EXCHANGE.account_id)
        if EXCHANGE.api_key:
            kwargs["password"] = EXCHANGE.api_key
        if EXCHANGE.mt5_server:
            kwargs["server"] = EXCHANGE.mt5_server

        ok = await asyncio.to_thread(mt5.initialize, **kwargs)
        if not ok:
            err = mt5.last_error()
            raise RuntimeError(f"mt5.initialize failed: {err}")
        self._initialized = True
        info = await asyncio.to_thread(mt5.account_info)
        if info is None:
            raise RuntimeError(f"mt5.account_info failed: {mt5.last_error()}")
        log.info("MT5 connected: login=%s server=%s currency=%s balance=%.2f",
                 info.login, info.server, info.currency, info.balance)

    async def close(self) -> None:
        if self._initialized:
            await asyncio.to_thread(mt5.shutdown)
            self._initialized = False

    def set_sandbox_mode(self, on: bool) -> None:
        # MT5 sandboxing = use a demo account. No runtime toggle.
        pass

    # ---------------------------------------------------------------- helpers
    def _sym(self, symbol: str) -> str:
        """Apply broker-specific symbol suffix (e.g. EURUSD → EURUSD.a)."""
        if self._suffix and not symbol.endswith(self._suffix):
            return f"{symbol}{self._suffix}"
        return symbol

    async def _ensure_symbol(self, symbol: str) -> None:
        info = await asyncio.to_thread(mt5.symbol_info, symbol)
        if info is None:
            raise RuntimeError(f"symbol not found: {symbol}")
        if not info.visible:
            ok = await asyncio.to_thread(mt5.symbol_select, symbol, True)
            if not ok:
                raise RuntimeError(f"symbol_select failed: {symbol}")

    def _filling_mode(self, symbol_info: Any) -> int:
        """Pick a filling mode the broker accepts for this symbol."""
        fm = getattr(symbol_info, "filling_mode", 0)
        # bitmask: 1 = FOK, 2 = IOC. Prefer IOC (partial fills allowed) then FOK.
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    async def _find_our_position(self, symbol: str) -> Optional[Any]:
        """Return the first open position on `symbol` tagged with our magic."""
        positions = await asyncio.to_thread(mt5.positions_get, symbol=symbol)
        if not positions:
            return None
        for p in positions:
            if p.magic == self._magic:
                return p
        return None

    # -------------------------------------------------------- market data
    async def fetch_balance(self) -> dict:
        await self._ensure_init()
        info = await asyncio.to_thread(mt5.account_info)
        if info is None:
            return {"total": {}}
        return {
            "total": {info.currency: float(info.balance)},
            "info": {"balance": float(info.balance),
                     "equity": float(info.equity)},
        }

    async def fetch_ticker(self, symbol: str) -> dict:
        await self._ensure_init()
        sym = self._sym(symbol)
        await self._ensure_symbol(sym)
        tick = await asyncio.to_thread(mt5.symbol_info_tick, sym)
        if tick is None:
            return {"last": 0.0}
        # `.last` is only populated on venues that publish it. Fall back to mid.
        last = float(tick.last) if tick.last else (tick.bid + tick.ask) / 2
        return {"last": last, "bid": float(tick.bid), "ask": float(tick.ask)}

    async def fetch_ohlcv(self, symbol: str, timeframe: str,
                          limit: int = 20) -> list:
        await self._ensure_init()
        sym = self._sym(symbol)
        await self._ensure_symbol(sym)
        tf = _tf_const(timeframe)
        rates = await asyncio.to_thread(mt5.copy_rates_from_pos, sym, tf, 0, limit)
        if rates is None:
            return []
        return [
            [int(r["time"]) * 1000, float(r["open"]), float(r["high"]),
             float(r["low"]), float(r["close"]), float(r["tick_volume"])]
            for r in rates
        ]

    # ------------------------------------------------------------- trading
    async def create_order(self, symbol: str, type: str, side: str,
                           amount: float, price: Optional[float] = None,
                           params: Optional[dict] = None) -> dict:
        await self._ensure_init()
        params = params or {}
        sym = self._sym(symbol)
        await self._ensure_symbol(sym)
        sym_info = await asyncio.to_thread(mt5.symbol_info, sym)
        if sym_info is None:
            raise RuntimeError(f"symbol_info failed: {sym}")

        side_l = side.lower()
        is_buy = side_l == "buy"

        # Detect partial-close: opposite-side order on an existing tagged position.
        existing = await self._find_our_position(sym)
        closing = False
        if existing is not None:
            existing_is_buy = existing.type == mt5.POSITION_TYPE_BUY
            if is_buy != existing_is_buy:
                closing = True

        tick = await asyncio.to_thread(mt5.symbol_info_tick, sym)
        if tick is None:
            raise RuntimeError(f"no tick for {sym}")

        if closing:
            entry_price = tick.bid if existing.type == mt5.POSITION_TYPE_BUY else tick.ask
        else:
            entry_price = tick.ask if is_buy else tick.bid

        req: Dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(amount),
            "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price": float(entry_price),
            "deviation": 20,
            "magic": self._magic,
            "comment": "trading-agent",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(sym_info),
        }
        if closing:
            req["position"] = existing.ticket
        else:
            sl = self._extract_price(params.get("stopLoss"))
            tp = self._extract_price(params.get("takeProfit"))
            if sl:
                req["sl"] = float(sl)
            if tp:
                req["tp"] = float(tp)

        log.debug("mt5.order_send: %s", req)
        result = await asyncio.to_thread(mt5.order_send, req)
        if result is None:
            raise RuntimeError(f"order_send returned None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(
                f"order_send retcode={result.retcode} comment={result.comment}"
            )
        # For a market open, result.order is the position ticket.
        # For a closing deal, we return the deal ticket for logging parity.
        oid = result.order if not closing else result.deal
        return {
            "id": str(oid),
            "symbol": sym,
            "side": side_l,
            "amount": float(amount),
            "price": float(entry_price),
            "params": params,
        }

    async def edit_order(self, order_id: str, symbol: str,
                         params: Optional[dict] = None) -> dict:
        """Modify SL (and optionally TP) on the position with ticket=order_id."""
        await self._ensure_init()
        params = params or {}
        sym = self._sym(symbol)

        new_sl = params.get("stopLoss")
        new_tp = params.get("takeProfit")
        if new_sl is None and new_tp is None:
            return {"id": order_id, "params": params}

        try:
            ticket = int(order_id)
        except (TypeError, ValueError):
            log.warning("edit_order: non-numeric ticket %r — skipped", order_id)
            return {"id": order_id, "params": params}

        # Preserve the untouched leg by reading it back from the live position.
        positions = await asyncio.to_thread(mt5.positions_get, ticket=ticket)
        if not positions:
            log.warning("edit_order: no position for ticket %s", ticket)
            return {"id": order_id, "params": params}
        pos = positions[0]

        sl_val = float(new_sl) if new_sl is not None else float(pos.sl or 0)
        tp_val = float(new_tp) if new_tp is not None else float(pos.tp or 0)

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": sym,
            "sl": sl_val,
            "tp": tp_val,
            "magic": self._magic,
        }
        result = await asyncio.to_thread(mt5.order_send, req)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = getattr(result, "retcode", None)
            comment = getattr(result, "comment", mt5.last_error())
            log.warning("edit_order failed ticket=%s retcode=%s comment=%s",
                        ticket, code, comment)
        return {"id": order_id, "params": params}

    # ---------------------------------------------------------------- misc
    @staticmethod
    def _extract_price(spec: Any) -> Optional[float]:
        """Accept either a raw number or the ccxt-style {'price': X} dict."""
        if spec is None:
            return None
        if isinstance(spec, dict):
            v = spec.get("price")
            return float(v) if v is not None else None
        return float(spec)
