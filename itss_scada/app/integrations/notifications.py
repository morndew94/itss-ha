"""Mobile push notification dispatcher (HA notify & HA telegram_bot using chat_id only)."""

from __future__ import annotations

from typing import Any


class PushNotifier:
    """Sends alerts via HA notify service and/or HA telegram_bot.send_message (fail-safe)."""

    def __init__(
        self,
        *,
        ha_client: Any = None,
        notify_service: str = "",
        telegram_chat_id: str = "",
    ) -> None:
        self.ha_client = ha_client
        self.notify_service = notify_service.strip()
        self.telegram_chat_id = str(telegram_chat_id or "").strip()

    async def send(self, title: str, message: str) -> dict[str, str]:
        """Deliver notification to configured HA channels without raising."""
        results: dict[str, str] = {}
        ha_ready = bool(self.ha_client and getattr(self.ha_client, "is_configured", False))

        # 1. Home Assistant mobile app / notify service (e.g., "mobile_app_phone")
        if self.notify_service and ha_ready:
            svc = self.notify_service.replace("notify.", "")
            try:
                await self.ha_client.call_service("notify", svc, {"title": title, "message": message})
                results["ha_notify"] = "sent"
            except Exception as err:  # noqa: BLE001
                results["ha_notify"] = f"error:{err}"

        # 2. Home Assistant built-in telegram_bot integration (requires chat_id only)
        if self.telegram_chat_id and ha_ready:
            target: int | str = (
                int(self.telegram_chat_id)
                if self.telegram_chat_id.lstrip("-").isdigit()
                else self.telegram_chat_id
            )
            try:
                await self.ha_client.call_service(
                    "telegram_bot",
                    "send_message",
                    {"target": target, "title": f"[{title}]", "message": message},
                )
                results["telegram"] = "sent"
            except Exception as err:  # noqa: BLE001
                results["telegram"] = f"error:{err}"

        if not results:
            results["status"] = "no_channel_configured"
        return results
