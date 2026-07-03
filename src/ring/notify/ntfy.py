"""ntfy 後端（https://ntfy.sh 或 self-hosted）——人不在座位時，🔴 等你直接推到手機。

config 給完整 topic URL 即可啟用::

    notify_ntfy_url = "https://ntfy.sh/my-ring-topic"

用 JSON publish（POST 到 server 根路徑、topic 放 body）而非 header 形式——
標題有中文時 HTTP header 只吃 latin-1，JSON 就沒這個問題。純 stdlib urllib，
timeout 短（3s）、失敗安靜吞掉，不擋 hook 主流程。
"""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from ring.config import get_config
from ring.notify.base import notify_message, notify_title
from ring.registry import Session


def _split_topic(url: str) -> tuple[str, str]:
    """把完整 topic URL 拆成 (server 根 URL, topic)；拆不出 topic 回 ("", "")。"""
    parts = urlsplit(url)
    topic = parts.path.strip("/").rsplit("/", 1)[-1]
    if not parts.scheme or not parts.netloc or not topic:
        return "", ""
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")), topic


class NtfyNotifier:
    name = "ntfy"

    def available(self) -> bool:
        return _split_topic(get_config().notify_ntfy_url) != ("", "")

    def supports_click(self) -> bool:
        return False

    def send(self, sessions: list[Session]) -> None:
        """逐 session 各 publish 一則 ntfy 訊息（priority=high、紅圈 tag）。"""
        base, topic = _split_topic(get_config().notify_ntfy_url)
        if not topic:
            return
        for s in sessions:
            payload = {
                "topic": topic,
                "title": notify_title(s),
                "message": notify_message(s),
                "priority": 4,
                "tags": ["red_circle"],
            }
            req = urllib.request.Request(
                base,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=3).close()
            except Exception:
                pass


notifier = NtfyNotifier()
