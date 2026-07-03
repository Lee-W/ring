"""通用 webhook 後端——🔴 等你時把 JSON payload POST 到你指定的 URL。

config 給 URL 即可啟用::

    notify_webhook_url = "https://example.com/my-endpoint"

payload 欄位視為穩定介面（只加不改）：``source`` / ``event`` / ``session_id`` /
``provider`` / ``project`` / ``location`` / ``last_action`` / ``title`` / ``message``。
接 Slack incoming webhook、自家 bot、IFTTT 之類都從這裡進。純 stdlib urllib，
timeout 短（3s）、失敗安靜吞掉，不擋 hook 主流程。
"""

from __future__ import annotations

import json
import urllib.request

from ring.config import get_config
from ring.notify.base import display_name, notify_message, notify_title
from ring.registry import Session


class WebhookNotifier:
    name = "webhook"

    def available(self) -> bool:
        return bool(get_config().notify_webhook_url)

    def supports_click(self) -> bool:
        return False

    def send(self, sessions: list[Session]) -> None:
        """逐 session 各 POST 一則 JSON。"""
        url = get_config().notify_webhook_url
        if not url:
            return
        for s in sessions:
            payload = {
                "source": "ring",
                "event": "waiting",
                "session_id": s.session_id,
                "provider": s.provider,
                "project": s.project,
                "location": s.location,
                "last_action": s.last_action,
                "label": display_name(s),
                "waiting_detail": s.waiting_detail,
                "title": notify_title(s),
                "message": notify_message(s),
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=3).close()
            except Exception:
                pass


notifier = WebhookNotifier()
