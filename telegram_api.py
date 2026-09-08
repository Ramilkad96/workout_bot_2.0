# -*- coding: utf-8 -*-
"""Тонкая обёртка над Telegram Bot API (long polling), без сторонних
фреймворков — используется только `requests`, чтобы не зависеть от пакетов,
которые могут быть недоступны для установки."""
import requests

API_ROOT = "https://api.telegram.org"


class TelegramAPI:
    def __init__(self, token: str, timeout: int = 35):
        self.base = f"{API_ROOT}/bot{token}"
        self.timeout = timeout

    def _call(self, method: str, **params):
        resp = requests.post(f"{self.base}/{method}", json=params, timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error on {method}: {payload}")
        return payload["result"]

    def get_updates(self, offset: int | None = None, timeout: int = 30):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", **params)

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None):
        params = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self._call("sendMessage", **params)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None):
        params = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        return self._call("answerCallbackQuery", **params)

    def get_me(self):
        return self._call("getMe")


def inline_keyboard(buttons: list) -> dict:
    """buttons: список рядов, каждый ряд — список (текст, callback_data)."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in buttons
        ]
    }
