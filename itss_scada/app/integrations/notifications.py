"""Mobile push notification dispatcher (Home Assistant notify & Telegram Bot API)."""

from __future__ import annotations

from typing import Any, Callable
import httpx


class PushNotifier:
    """Sends alerts via HA notify service and/or Telegram Bot API (fail-safe)."""

    def __init__(
        self,
        *,
        ha_client: Any = None,
        notify_service: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        telegram_post: Callable[[str, dict], Any] | None = None,
    ) -> None:
        self.ha_client = ha_client
        self.notify_service = notify_service.strip()
        self.telegram_bot_token = telegram_bot_token.strip()
        self.telegram_chat_id = telegram_chat_id.strip()
        self._telegram_post = telegram_post

    async def send(self, title: str, message: str) -> dict[str, str]:
        """Deliver notification to all configured channels without raising."""
        results: dict[str, str] = {}

        # 1. Home Assistant mobile app / notify service (e.g., "mobile_app_phone")
        if self.notify_service and self.ha_client and getattr(self.ha_client, "is_configured", False):
            svc = self.notify_service.replace("notify.", "")
            try:
                await self.ha_client.call_service("notify", svc, {"title": title, "message": message})
                results["ha_notify"] = "sent"
            except Exception as err:  # noqa: BLE001
                results["ha_notify"] = f"error:{err}"

        # 2. Telegram Bot API
        if self.telegram_bot_token and self.telegram_chat_id:
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            payload = {"chat_id": self.telegram_chat_id, "text": f"[{title}]\n{message}"}
            try:
                if self._telegram_post:
                    self._telegram_post(url, payload)
                else:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(url, json=payload)
                        resp.raise_for_status()
                results["telegram"] = "sent"
            except Exception as err:  # noqa: BLE001
                results["telegram"] = f"error:{err}"

        if not results:
            results["status"] = "no_channel_configured"
        return results
