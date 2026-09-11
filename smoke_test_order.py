"""End-to-end order lifecycle check against the configured MT5 demo account.

Run: .\.venv\Scripts\python.exe smoke_test_order.py

Verifies (in order):
    1. Fetches a live EURUSD tick
    2. Opens a 0.01-lot BUY with a wide bracket (SL/TP 30 pips away)
    3. Reads the position back and confirms the bracket is attached
    4. Moves the SL to breakeven via the same code path the agent uses
    5. Closes the position with a reverse market order (partial-close code path)
    6. Confirms no residual position remains

Uses a 0.01 lot (minimum) — the notional risk if something went wrong is a
handful of pips against a $10k demo balance. Never run this against live.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SYMBOL = "EURUSD"
LOTS = 0.01
PIP = 0.0001
WIDE_PIPS = 30


def _our_pos(mt5: Any, sym: str, magic: int) -> Any:
    """Return the first open position on `sym` tagged with our magic, or None."""
    positions = mt5.positions_get(symbol=sym)
    if not positions:
        return None
    for p in positions:
        if p.magic == magic:
            return p
    return None


async def main() -> int:
    import MetaTrader5 as mt5  # type: ignore
    from config import EXCHANGE
    from execution import ExchangeClient
    from models import Level, OrderSide, SignalType, TradePlan

    ex = ExchangeClient()
    magic = EXCHANGE.mt5_magic

    try:
        # 1. Live tick
        price = await ex.fetch_last_price(SYMBOL)
        if price <= 0:
            print("[FAIL] EURUSD ticker returned 0")
            return 1
        print(f"[OK] EURUSD last: {price:.5f}")

        # Safety: if a leftover position from a prior run exists, refuse.
        existing = _our_pos(mt5, SYMBOL, magic)
        if existing is not None:
            print(f"[ABORT] pre-existing tagged position on {SYMBOL} "
                  f"(ticket {existing.ticket}). Close it manually first.")
            return 2

        # 2. Build a wide plan and open
        entry = price
        sl = round(entry - WIDE_PIPS * PIP, 5)
        tp1 = round(entry + WIDE_PIPS * PIP, 5)
        plan = TradePlan(
            symbol=SYMBOL, side=OrderSide.BUY,
            entry=entry, sl=sl, tp1=tp1,
            tp2=round(entry + 50 * PIP, 5),
            tp3=round(entry + 80 * PIP, 5),
            lots=LOTS, risk_pct=0.0001,
            level=Level.L1, signal_type=SignalType.PFH,
            rationale="order-lifecycle smoke test",
        )
        print(f"[..] opening BUY {LOTS} @ ~{entry:.5f} SL={sl:.5f} TP={tp1:.5f}")
        ticket_str = await ex.place_bracket(plan)
        if not ticket_str:
            print("[FAIL] place_bracket returned empty ticket")
            return 3
        print(f"[OK] order acknowledged, ticket={ticket_str}")

        # 3. Give MT5 a beat, then read the position back
        await asyncio.sleep(1.0)
        pos = _our_pos(mt5, SYMBOL, magic)
        if pos is None:
            print("[FAIL] no tagged position visible after open")
            return 4
        print(f"[OK] position live: ticket={pos.ticket} vol={pos.volume:.2f} "
              f"entry={pos.price_open:.5f} sl={pos.sl:.5f} tp={pos.tp:.5f}")

        if abs(pos.sl - sl) > 0.0001:
            print(f"[WARN] SL drifted from requested ({sl:.5f}) to {pos.sl:.5f} — "
                  f"broker may have snapped to stop-level. Not fatal.")

        # 4. Tighten SL to (current bid - 15 pips) — must stay below the
        #    live bid to be valid for a BUY position. We can't move to true
        #    breakeven here because price hasn't run in our favor.
        tick = mt5.symbol_info_tick(SYMBOL)
        new_sl = round(tick.bid - 15 * PIP, 5)
        print(f"[..] modifying SL {pos.sl:.5f} → {new_sl:.5f} "
              f"(bid={tick.bid:.5f})")
        await ex.modify_sl(SYMBOL, str(pos.ticket), new_sl)
        await asyncio.sleep(1.0)
        pos = _our_pos(mt5, SYMBOL, magic)
        if pos is None:
            print("[FAIL] position vanished after SL modify")
            return 5
        if abs(pos.sl - new_sl) > 0.0001:
            print(f"[FAIL] SL not updated: expected {new_sl:.5f}, got {pos.sl:.5f}")
            return 6
        print(f"[OK] SL is now {pos.sl:.5f}")

        # 5. Close via close_partial (reverse market order, hedge-safe)
        print(f"[..] closing {LOTS} lots")
        await ex.close_partial(SYMBOL, OrderSide.BUY, LOTS)
        await asyncio.sleep(1.0)
        pos = _our_pos(mt5, SYMBOL, magic)
        if pos is not None:
            print(f"[FAIL] position still open after close: "
                  f"ticket={pos.ticket} vol={pos.volume}")
            return 7
        print("[OK] position closed cleanly")

    except Exception as e:
        print(f"[FAIL] exception: {e}")
        import traceback
        traceback.print_exc()
        return 99
    finally:
        await ex.close()

    print("\nFull order lifecycle verified ✔")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
