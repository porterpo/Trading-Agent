"""Coordinator — runs the webhook API, signal consumer, news scheduler,
and position monitor as concurrent asyncio tasks.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Set

import uvicorn

from btmm_engine import BTMMEngine
from config import RISK, WEBHOOK
from db import init_db
from execution import ExchangeClient, PositionManager
from indicator_parser import create_app
from models import IndicatorSignal
from notifier import Telegram
from risk_manager import (
    NewsCalendar, adr_class, build_plan, in_kill_zone,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent")


async def signal_consumer(queue: "asyncio.Queue[IndicatorSignal]",
                          engine: BTMMEngine,
                          news: NewsCalendar,
                          pm: PositionManager,
                          ex: ExchangeClient,
                          tg: Telegram) -> None:
    while True:
        sig = await queue.get()
        try:
            await _handle_signal(sig, engine, news, pm, ex, tg)
        except Exception as e:
            log.exception("signal handling failed: %s", e)
        finally:
            queue.task_done()


async def _handle_signal(sig: IndicatorSignal,
                          engine: BTMMEngine,
                          news: NewsCalendar,
                          pm: PositionManager,
                          ex: ExchangeClient,
                          tg: Telegram) -> None:
    kz = in_kill_zone()
    if not kz:
        log.info("skip %s %s — outside kill zone",
                 sig.symbol, sig.signal_type.value)
        return

    ev = news.within_window(RISK.news_freeze_minutes)
    if ev:
        log.info("skip %s — news freeze around '%s' @ %s",
                 sig.symbol, ev.title, ev.ts.isoformat())
        await tg.send(f"⏸ Skipped {sig.symbol} — news freeze near *{ev.title}*")
        return

    if pm.open_count >= RISK.max_concurrent_positions:
        log.info("skip %s — max concurrent positions reached", sig.symbol)
        return

    state = await engine.apply(sig)
    reason = engine.is_actionable(sig, state)
    if reason:
        log.info("skip %s — %s", sig.symbol, reason)
        await tg.send(
            f"⏭ {sig.symbol} {sig.signal_type.value} rejected: {reason}")
        return

    equity = await ex.fetch_equity_usd()
    if equity <= 0:
        log.warning("equity unavailable; aborting %s", sig.symbol)
        return

    plan = build_plan(sig, state.level, state.bias, equity)
    if plan is None:
        log.info("no actionable plan for %s (%s/%s)",
                 sig.symbol, state.level.value, state.bias.value)
        return

    log.info("PLAN %s %s %.2f @ %.5f  SL %.5f  TP1 %.5f  TP2 %.5f  TP3 %.5f",
             plan.symbol, plan.side.value, plan.lots, plan.entry,
             plan.sl, plan.tp1, plan.tp2, plan.tp3)

    pos = await pm.open(plan)
    if pos:
        await tg.send(
            f"✅ *{plan.symbol}* {plan.side.value.upper()} {plan.lots} lots\n"
            f"Entry `{plan.entry:.5f}`  SL `{plan.sl:.5f}`\n"
            f"TP1 `{plan.tp1:.5f}`  TP2 `{plan.tp2:.5f}`  TP3 `{plan.tp3:.5f}`\n"
            f"{state.level.value}/{state.bias.value} · "
            f"{adr_class(sig.daily_adr_pips)} · risk {plan.risk_pct*100:.2f}%"
        )


async def news_scheduler(news: NewsCalendar,
                          pm: PositionManager,
                          tg: Telegram,
                          refresh_seconds: int = 300) -> None:
    """Hourly refresh + pre-news protective close within the freeze window."""
    hedged: Set[str] = set()
    while True:
        await news.refresh()
        for ev in news.upcoming_within(RISK.news_freeze_minutes + 5):
            key = f"{ev.title}@{ev.ts.isoformat()}"
            if key in hedged:
                continue
            log.warning("pre-news hedge: %s @ %s", ev.title, ev.ts.isoformat())
            await pm.force_partial_close(RISK.pre_news_partial_close_pct)
            await tg.send(
                f"🔔 Pre-news hedge: closed "
                f"{int(RISK.pre_news_partial_close_pct*100)}% "
                f"before *{ev.title}* ({ev.ts.isoformat()})"
            )
            hedged.add(key)
        await asyncio.sleep(refresh_seconds)


async def run() -> None:
    init_db()

    queue: "asyncio.Queue[IndicatorSignal]" = asyncio.Queue()
    engine = BTMMEngine()
    news = NewsCalendar()
    ex = ExchangeClient()
    pm = PositionManager(ex)
    tg = Telegram()

    app = create_app(queue)
    server = uvicorn.Server(uvicorn.Config(
        app, host=WEBHOOK.host, port=WEBHOOK.port,
        log_level="info", lifespan="on",
    ))

    tasks = [
        asyncio.create_task(server.serve(), name="webhook"),
        asyncio.create_task(
            signal_consumer(queue, engine, news, pm, ex, tg),
            name="consumer",
        ),
        asyncio.create_task(news_scheduler(news, pm, tg), name="news"),
        asyncio.create_task(pm.monitor_loop(), name="positions"),
    ]

    log.info("agent live on %s:%d", WEBHOOK.host, WEBHOOK.port)
    await tg.send("🚀 BTMM agent started")

    try:
        await asyncio.gather(*tasks)
    finally:
        pm.stop()
        for t in tasks:
            t.cancel()
        await ex.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("shutting down")
