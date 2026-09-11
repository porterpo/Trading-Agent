"""Telegram notifier — best-effort delivery, never blocks trading."""
from __future__ import annotations

import logging

import httpx

from config import NOTIFY

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self,
                 token: str = NOTIFY.telegram_bot_token,
                 chat_id: str = NOTIFY.telegram_chat_id) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    async def send(self, text: str) -> None:
        if not self.enabled:
            log.debug("telegram disabled: %s", text)
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(url, json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                })
                if r.status_code >= 400:
                    log.warning("telegram %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("telegram send failed: %s", e)
