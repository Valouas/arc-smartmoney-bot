from __future__ import annotations
import requests

class TelegramAlerts:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text: str):
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True
            }, timeout=7).raise_for_status()
        except Exception:
            pass
