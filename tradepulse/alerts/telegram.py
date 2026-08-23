"""Telegram Bot API client for critical-event notifications -- port of
base44/shared/telegram.ts.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping

import httpx

logger = logging.getLogger(__name__)

Severity = Literal["warning", "critical"]


class TelegramAlerter:
    def __init__(self, bot_token: str | None, chat_id: str | None, timeout_seconds: int = 10) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout_seconds = timeout_seconds

    async def send(self, severity: Severity, message: str, details: Mapping[str, Any] | None = None) -> bool:
        """Alerting is optional infrastructure -- its absence or failure must
        never block or fail a trading cycle, so this always returns a bool
        rather than raising."""
        if not self._bot_token or not self._chat_id:
            logger.warning(
                "telegram_alert_skipped_no_credentials",
                extra={"event": "telegram_alert_skipped_no_credentials", "alert_message": message},
            )
            return False

        prefix = "\U0001f6a8 CRITICAL" if severity == "critical" else "⚠️ WARNING"
        text = f"<b>{prefix}</b>\n{message}"
        if details:
            text += "\n\n" + "\n".join(f"<b>{key}</b>: {value}" for key, value in details.items())

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(url, json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"})
            data = response.json()
        except Exception as exc:  # network/parse failure -- never propagate
            logger.error("telegram_alert_failed", extra={"event": "telegram_alert_failed", "error": str(exc)})
            return False

        if not data.get("ok"):
            logger.error(
                "telegram_alert_rejected",
                extra={"event": "telegram_alert_rejected", "error": data.get("description", "unknown Telegram API error")},
            )
            return False
        return True
