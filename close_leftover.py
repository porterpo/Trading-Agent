"""Close any lingering EURUSD position tagged with this agent's magic.

Useful after a crashed smoke test.

Run: .\.venv\Scripts\python.exe close_leftover.py
"""
from __future__ import annotations

import asyncio
import sys

SYMBOL = "EURUSD"


async def main() -> int:
    import MetaTrader5 as mt5  # type: ignore
    from config import EXCHANGE
    from execution import ExchangeClient
    from models import OrderSide

    ex = ExchangeClient()
    try:
        # Force init so we can query positions
        await ex.fetch_equity_usd()

        positions = mt5.positions_get(symbol=SYMBOL) or []
        ours = [p for p in positions if p.magic == EXCHANGE.mt5_magic]
        if not ours:
            print(f"nothing to close on {SYMBOL}")
            return 0

        for p in ours:
            side = OrderSide.BUY if p.type == mt5.POSITION_TYPE_BUY else OrderSide.SELL
            print(f"closing ticket={p.ticket} side={side.value} vol={p.volume:.2f}")
            await ex.close_partial(SYMBOL, side, p.volume)

        print("done")
    finally:
        await ex.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
