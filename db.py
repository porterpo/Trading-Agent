"""SQLite persistence for signals, trades, and BTMM state.

Kept intentionally small — analytics live in SQL views/queries downstream
rather than in Python here.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, List, Optional

from config import DB_PATH
from models import IndicatorSignal, TradePlan


_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT,
    signal_type TEXT NOT NULL,
    level TEXT,
    trigger_price REAL,
    daily_adr_pips REAL,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open TEXT NOT NULL,
    ts_close TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL,
    fill_price REAL,
    sl REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    lots REAL,
    risk_pct REAL,
    equity_at_open REAL,
    level TEXT,
    signal_type TEXT,
    kill_zone TEXT,
    adr_class TEXT,
    rationale TEXT,
    status TEXT DEFAULT 'OPEN',
    exit_reason TEXT,
    pnl_pips REAL,
    pnl_usd REAL,
    broker_order_id TEXT
);

CREATE TABLE IF NOT EXISTS partial_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL REFERENCES trades(id),
    ts TEXT NOT NULL,
    fill_type TEXT NOT NULL,
    lots REAL NOT NULL,
    price REAL,
    pnl_pips REAL,
    pnl_usd REAL
);

CREATE TABLE IF NOT EXISTS btmm_state (
    symbol TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    bias TEXT NOT NULL,
    anchor_wick REAL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_ts     ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status  ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol  ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_signal  ON trades(signal_type);
CREATE INDEX IF NOT EXISTS idx_partials_trade ON partial_fills(trade_id);
"""

# Columns added post-v1. SQLite lacks ADD COLUMN IF NOT EXISTS so we ALTER
# inside a try/except — existing DBs migrate silently on next boot.
_MIGRATIONS = [
    ("trades", "fill_price", "REAL"),
    ("trades", "equity_at_open", "REAL"),
    ("trades", "kill_zone", "TEXT"),
    ("trades", "adr_class", "TEXT"),
    ("trades", "exit_reason", "TEXT"),
]


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    with _lock:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _connect() as c:
        c.executescript(SCHEMA)
        for table, col, coltype in _MIGRATIONS:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists — migration is a no-op


def log_signal(sig: IndicatorSignal, payload_json: str) -> int:
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO signals (ts, symbol, timeframe, signal_type, level,
                                    trigger_price, daily_adr_pips, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sig.timestamp.isoformat(), sig.symbol, sig.timeframe,
                sig.signal_type.value, sig.active_level.value,
                sig.trigger_price, sig.daily_adr_pips, payload_json,
            ),
        )
        return int(cur.lastrowid)


def log_trade(plan: TradePlan, broker_order_id: Optional[str],
              *, kill_zone: Optional[str] = None,
              adr_class: Optional[str] = None,
              equity_at_open: Optional[float] = None,
              fill_price: Optional[float] = None) -> int:
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO trades
                    (ts_open, symbol, side, entry, fill_price, sl, tp1, tp2, tp3,
                     lots, risk_pct, equity_at_open, level, signal_type,
                     kill_zone, adr_class, rationale, broker_order_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now(), plan.symbol, plan.side.value, plan.entry, fill_price,
                plan.sl, plan.tp1, plan.tp2, plan.tp3, plan.lots, plan.risk_pct,
                equity_at_open, plan.level.value, plan.signal_type.value,
                kill_zone, adr_class, plan.rationale, broker_order_id,
            ),
        )
        return int(cur.lastrowid)


def log_partial_fill(trade_id: int, fill_type: str, lots: float,
                     price: Optional[float] = None,
                     pnl_pips: Optional[float] = None,
                     pnl_usd: Optional[float] = None) -> None:
    with _connect() as c:
        c.execute(
            """INSERT INTO partial_fills
                    (trade_id, ts, fill_type, lots, price, pnl_pips, pnl_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, _now(), fill_type, lots, price, pnl_pips, pnl_usd),
        )


def update_trade_status(trade_id: int, status: str,
                        pnl_pips: Optional[float] = None,
                        pnl_usd: Optional[float] = None,
                        exit_reason: Optional[str] = None) -> None:
    with _connect() as c:
        c.execute(
            """UPDATE trades
                  SET status=?, pnl_pips=?, pnl_usd=?, exit_reason=?, ts_close=?
                WHERE id=?""",
            (status, pnl_pips, pnl_usd, exit_reason, _now(), trade_id),
        )


def open_trades() -> List[dict]:
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE status='OPEN'")]


def upsert_state(symbol: str, level: str, bias: str,
                 anchor_wick: Optional[float]) -> None:
    with _connect() as c:
        c.execute(
            """INSERT INTO btmm_state (symbol, level, bias, anchor_wick, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                    level=excluded.level,
                    bias=excluded.bias,
                    anchor_wick=excluded.anchor_wick,
                    updated_at=excluded.updated_at""",
            (symbol, level, bias, anchor_wick, _now()),
        )


def get_state(symbol: str) -> Optional[dict]:
    with _connect() as c:
        r = c.execute(
            "SELECT * FROM btmm_state WHERE symbol=?", (symbol,),
        ).fetchone()
        return dict(r) if r else None
