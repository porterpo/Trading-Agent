"""One-shot connectivity check against the configured MT5 terminal.

Run: .\.venv\Scripts\python.exe smoke_test_mt5.py

Verifies (in order):
    1. MT5 terminal reachable + login accepted
    2. Account info readable (balance, currency, server)
    3. EURUSD ticker streaming
    4. EURUSD 1h OHLCV pullable
    5. Clean shutdown

No orders are placed. Safe to run against a live account.
"""
from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def main() -> int:
    from config import EXCHANGE
    from execution import ExchangeClient

    print(f"→ EXCHANGE_ID={EXCHANGE.exchange_id}")
    print(f"→ MT5_SERVER={EXCHANGE.mt5_server}")
    print(f"→ MT5 login={EXCHANGE.account_id}")
    print(f"→ Symbol suffix={EXCHANGE.mt5_symbol_suffix or '(none)'}\n")

    try:
        ex = ExchangeClient()
    except Exception as e:
        print(f"[FAIL] ExchangeClient init: {e}")
        return 1

    try:
        equity = await ex.fetch_equity_usd()
        print(f"[OK] account equity: {equity:.2f}")

        price = await ex.fetch_last_price("EURUSD")
        if price <= 0:
            print("[FAIL] EURUSD ticker returned 0 — check symbol suffix / Market Watch")
            return 2
        print(f"[OK] EURUSD last: {price:.5f}")

        bars = await ex.fetch_ohlcv("EURUSD", timeframe="1h", limit=5)
        if not bars:
            print("[FAIL] EURUSD OHLCV returned no bars")
            return 3
        print(f"[OK] EURUSD 1h bars: got {len(bars)}, most recent close={bars[-1][4]:.5f}")

    except Exception as e:
        print(f"[FAIL] runtime: {e}")
        return 4
    finally:
        await ex.close()

    print("\nAll checks passed ✔")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
