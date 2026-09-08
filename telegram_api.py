# -*- coding: utf-8 -*-
"""Тонкая обёртка над Telegram Bot API (long polling), без сторонних
фреймворков — используется только `requests`, чтобы не зависеть от пакетов,
которые могут быть недоступны для установки."""
import logging

import requests

API_ROOT = "https://api.telegram.org"

log = logging.getLogger("trainer_bot.api")


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

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None
    ):
        """Меняет текст и кнопки уже отправленного сообщения.

        Возвращает False, если отредактировать не удалось (сообщение слишком
        старое, удалено пользователем и т.п.) — вызывающий код тогда шлёт новое.
        Повтор того же текста Telegram считает ошибкой «message is not
        modified» — её глушим отдельно, это не проблема.
        """
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        params["reply_markup"] = reply_markup if reply_markup is not None else {"inline_keyboard": []}
        try:
            self._call("editMessageText", **params)
            return True
        except Exception as e:
            if "message is not modified" in str(e):
                return True
            log.info("Не удалось отредактировать сообщение %s: %s", message_id, e)
            return False

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        """Удаляет сообщение. Ошибки не считаются фатальными: Telegram не даёт
        удалять сообщения старше 48 часов и часть чужих сообщений."""
        try:
            self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except Exception as e:
            log.info("Не удалось удалить сообщение %s: %s", message_id, e)
            return False

    def answer_callback_query(self, callback_query_id: str, text: str | None = None):
        params = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        return self._call("answerCallbackQuery", **params)

    def set_my_commands(self, commands: list):
        """commands: список (команда, описание) — показывается в меню Telegram."""
        return self._call(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in commands],
        )

    def set_chat_menu_button(self):
        """Кнопка «Меню» в чате открывает список команд."""
        return self._call("setChatMenuButton", menu_button={"type": "commands"})

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
