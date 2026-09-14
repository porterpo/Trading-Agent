"""Trade journal CLI — grouped stats over closed trades.

Usage:
    python journal.py                    # all closed trades
    python journal.py --symbol EURUSD    # filter to one symbol
    python journal.py --since 2026-01-01 # trades opened on or after date
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from typing import Callable, Dict, List, Optional

from config import DB_PATH


def _rows(symbol: Optional[str] = None,
          since: Optional[str] = None) -> List[sqlite3.Row]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM trades WHERE status != 'OPEN'"
        args: list = []
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        if since:
            sql += " AND ts_open >= ?"
            args.append(since)
        sql += " ORDER BY ts_open"
        return list(con.execute(sql, args))
    finally:
        con.close()


def _r_multiple(row: sqlite3.Row) -> Optional[float]:
    equity = row["equity_at_open"]
    risk_pct = row["risk_pct"]
    pnl = row["pnl_usd"]
    if not equity or not risk_pct or pnl is None:
        return None
    risk_usd = equity * risk_pct
    return pnl / risk_usd if risk_usd else None


def _group_stats(rows: List[sqlite3.Row],
                 key_fn: Callable[[sqlite3.Row], Optional[str]]) -> List[Dict]:
    groups: Dict[str, list] = defaultdict(list)
    for r in rows:
        k = key_fn(r) or "(unset)"
        groups[k].append(r)

    out = []
    for k, rs in sorted(groups.items()):
        n = len(rs)
        pnls = [(r["pnl_usd"] or 0.0) for r in rs]
        wins = sum(1 for p in pnls if p > 0)
        rs_multiples = [x for x in (_r_multiple(r) for r in rs) if x is not None]
        avg_r = sum(rs_multiples) / len(rs_multiples) if rs_multiples else None
        out.append({
            "key": k,
            "n": n,
            "win_pct": (wins / n) * 100 if n else 0.0,
            "pnl_usd": sum(pnls),
            "avg_r": avg_r,
        })
    out.sort(key=lambda s: s["pnl_usd"], reverse=True)
    return out


def _print_table(title: str, stats: List[Dict]) -> None:
    print(f"\n== {title} ==")
    print(f"{'group':<22} {'n':>4} {'win%':>6} {'pnl$':>10} {'avgR':>8}")
    print("-" * 54)
    for s in stats:
        r = f"{s['avg_r']:+.2f}" if s["avg_r"] is not None else "--"
        print(f"{s['key']:<22} {s['n']:>4} {s['win_pct']:>5.1f}% "
              f"{s['pnl_usd']:>+10.2f} {r:>8}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", help="Filter to one symbol")
    ap.add_argument("--since", help="ISO date, e.g. 2026-01-01")
    args = ap.parse_args()

    rows = _rows(symbol=args.symbol, since=args.since)
    if not rows:
        print("no closed trades yet")
        return

    total_pnl = sum((r["pnl_usd"] or 0.0) for r in rows)
    wins = sum(1 for r in rows if (r["pnl_usd"] or 0.0) > 0)
    win_pct = (wins / len(rows)) * 100

    all_r = [x for x in (_r_multiple(r) for r in rows) if x is not None]
    avg_r_str = f"{sum(all_r)/len(all_r):+.2f}R" if all_r else "n/a (missing equity_at_open)"

    print(f"\n{len(rows)} closed trades  |  win rate {win_pct:.1f}%  "
          f"|  net ${total_pnl:+.2f}  |  avg {avg_r_str}")

    _print_table("by signal_type", _group_stats(rows, lambda r: r["signal_type"]))
    _print_table("by level",        _group_stats(rows, lambda r: r["level"]))
    _print_table("by kill_zone",    _group_stats(rows, lambda r: r["kill_zone"]))
    _print_table("by adr_class",    _group_stats(rows, lambda r: r["adr_class"]))
    _print_table("by symbol",       _group_stats(rows, lambda r: r["symbol"]))
    _print_table("by side",         _group_stats(rows, lambda r: r["side"]))
    _print_table("by exit_reason",  _group_stats(rows, lambda r: r["exit_reason"]))


if __name__ == "__main__":
    main()
