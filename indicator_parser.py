"""FastAPI webhook receiver for TradingView / Pine Script alerts.

TradingView posts a JSON payload; we authenticate with an HMAC-SHA256
signature (header `X-Signature`) and enqueue a normalised `IndicatorSignal`
for the coordinator to consume.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from config import WEBHOOK
from db import init_db, log_signal
from models import IndicatorSignal, Level, SignalType

log = logging.getLogger(__name__)


_ALIASES = {
    "PFH": SignalType.PFH,
    "PFH Confirmed": SignalType.PFH,
    "PFL": SignalType.PFL,
    "PFL Confirmed": SignalType.PFL,
    "SUPER CYCLE PFH": SignalType.SUPER_PFH,
    "SUPER CYCLE PFL": SignalType.SUPER_PFL,
    "200 EMA Reset": SignalType.EMA_RESET_L2,
    "200 EMA Reset L2": SignalType.EMA_RESET_L2,
    "200 EMA Reset (L2 Continuation)": SignalType.EMA_RESET_L2,
    "200 EMA Reset L3": SignalType.EMA_RESET_L3,
    "200 EMA Reset (L3 Exhaustion)": SignalType.EMA_RESET_L3,
    "PRIME LONG": SignalType.PRIME_LONG,
    "PRIME SHORT": SignalType.PRIME_SHORT,
}


def _parse_signal_type(raw: str) -> SignalType:
    key = (raw or "").strip()
    if key in _ALIASES:
        return _ALIASES[key]
    lower = key.lower()
    for k, v in _ALIASES.items():
        if k.lower() == lower:
            return v
    raise KeyError(key)


class SignalPayload(BaseModel):
    symbol: str
    timeframe: str = "1H"
    signal: str = Field(..., description="Raw indicator signal type string")
    price: float
    level: Optional[str] = "NEUTRAL"
    daily_adr_pips: float = 0.0
    anchor_wick: Optional[float] = None
    fib_618: Optional[float] = None
    fib_786: Optional[float] = None
    ema_200: Optional[float] = None
    timestamp: Optional[str] = None

    def to_signal(self) -> IndicatorSignal:
        sig_type = _parse_signal_type(self.signal)
        if self.timestamp:
            try:
                ts = datetime.fromisoformat(
                    self.timestamp.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        level_str = (self.level or "NEUTRAL").upper()
        try:
            level = Level(level_str)
        except ValueError:
            level = Level.NEUTRAL

        return IndicatorSignal(
            symbol=self.symbol.upper(),
            timeframe=self.timeframe,
            signal_type=sig_type,
            trigger_price=self.price,
            active_level=level,
            daily_adr_pips=self.daily_adr_pips,
            timestamp=ts,
            anchor_wick=self.anchor_wick,
            fib_618=self.fib_618,
            fib_786=self.fib_786,
            ema_200=self.ema_200,
        )


def _verify_hmac(body: bytes, header_sig: Optional[str]) -> bool:
    """Reject if a secret is configured and the signature doesn't match."""
    if not WEBHOOK.shared_secret:
        return True
    if not header_sig:
        return False
    expected = hmac.new(
        WEBHOOK.shared_secret.encode(), body, sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_sig)


def create_app(queue: "asyncio.Queue[IndicatorSignal]") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        yield

    app = FastAPI(title="MMM BTMM Signal Receiver", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "queue_depth": queue.qsize()}

    @app.post("/webhook/signal")
    async def receive(request: Request,
                      x_signature: Optional[str] = Header(default=None)) -> dict:
        raw = await request.body()
        if not _verify_hmac(raw, x_signature):
            raise HTTPException(status_code=401, detail="bad signature")

        try:
            body = json.loads(raw.decode() or "{}")
            payload = SignalPayload(**body)
            sig = payload.to_signal()
        except (json.JSONDecodeError, ValidationError, KeyError, ValueError) as e:
            log.warning("bad payload: %s", e)
            raise HTTPException(status_code=400, detail=f"bad payload: {e}")

        log_signal(sig, raw.decode(errors="replace"))
        await queue.put(sig)
        return {"queued": True, "symbol": sig.symbol,
                "signal": sig.signal_type.value}

    return app
