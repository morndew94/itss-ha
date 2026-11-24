"""Mobile push notification dispatcher (HA notify service/entity & HA telegram_bot)."""

from __future__ import annotations

from typing import Any


class PushNotifier:
    """Sends alerts via HA notify service/entity and/or HA telegram_bot.send_message."""

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

    async def send(self, title: str, message: str) -> dict[str, Any]:
        """Deliver notification to configured HA channels and return detailed diagnostics."""
        results: dict[str, Any] = {}
        ha_ready = bool(self.ha_client and getattr(self.ha_client, "is_configured", False))
        if not ha_ready:
            return {"status": "ha_not_connected"}

        available_notify_svcs: list[str] = []
        if hasattr(self.ha_client, "list_services"):
            svcs = await self.ha_client.list_services(("notify", "telegram_bot"))
            available_notify_svcs = svcs.get("notify", [])

        # 1. Home Assistant notify (supports both legacy notify.<svc> and modern notify.<entity>)
        if self.notify_service:
            svc_name = self.notify_service.replace("notify.", "")
            entity_id = f"notify.{svc_name}"
            # If notify.<svc_name> is a registered service, call it directly; else try notify.send_message entity
            use_entity = bool(available_notify_svcs) and (svc_name not in available_notify_svcs) and ("send_message" in available_notify_svcs)
            try:
                if use_entity:
                    await self.ha_client.call_service(
                        "notify",
                        "send_message",
                        {"entity_id": entity_id, "title": title, "message": message},
                    )
                    results["ha_notify"] = f"sent_via_entity:{entity_id}"
                else:
                    await self.ha_client.call_service(
                        "notify",
                        svc_name,
                        {"title": title, "message": message},
                    )
                    results["ha_notify"] = f"sent_via_service:notify.{svc_name}"
            except Exception as err:  # noqa: BLE001
                results["ha_notify"] = f"error:{err}"
                if available_notify_svcs:
                    results["availableNotifyServices"] = available_notify_svcs

        # 2. Home Assistant built-in telegram_bot.send_message
        #    v2.7.0: deprecated 된 `target` 대신 `chat_id` 사용, 구형 HA 는 `target` 으로 1회 재시도
        if self.telegram_chat_id:
            target: int | str = (
                int(self.telegram_chat_id)
                if self.telegram_chat_id.lstrip("-").isdigit()
                else self.telegram_chat_id
            )
            text = f"[{title}]\n{message}"
            try:
                await self.ha_client.call_service(
                    "telegram_bot",
                    "send_message",
                    {"chat_id": target, "message": text},
                )
                results["telegram"] = f"sent_to:{target}"
            except Exception as err:  # noqa: BLE001
                try:
                    await self.ha_client.call_service(
                        "telegram_bot",
                        "send_message",
                        {"target": target, "message": text},
                    )
                    results["telegram"] = f"sent_to:{target}(legacy_target)"
                except Exception:  # noqa: BLE001
                    results["telegram"] = f"error:{err}"

        if not results:
            results["status"] = "no_channel_configured"
            if available_notify_svcs:
                results["availableNotifyServices"] = available_notify_svcs
        return results
