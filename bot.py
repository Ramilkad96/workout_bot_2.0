# -*- coding: utf-8 -*-
"""
Telegram-бот "Тренер" — пользователь вписывает свою программу тренировок
текстом по шаблону, тренируется по ней, логирует подходы и получает сводку
по завершению тренировки.

Запуск:
    1. Получить токен у @BotFather в Telegram.
    2. Положить его в .env (см. .env.example) или в переменную окружения BOT_TOKEN.
    3. pip install -r requirements.txt
    4. python bot.py
"""
import logging
import time

from config import BOT_TOKEN, DB_PATH
from parser import parse_program, format_program, ProgramParseError
from storage import Storage
from telegram_api import TelegramAPI, inline_keyboard
from workout import (
    new_session_state,
    current_exercise,
    record_sets,
    skip_exercise,
    is_finished,
    build_summary,
    parse_sets_input,
    SetParseError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trainer_bot")

HELP_TEXT = (
    "Я помогу тренироваться по твоей собственной программе.\n\n"
    "1. Пришли программу тренировок командой /newprogram — текстом, по шаблону:\n\n"
    "Название: Моя программа\n"
    "День 1: Грудь/трицепс\n"
    "Жим штанги лёжа 4x8\n"
    "Разводка гантелей 3x12x14кг\n"
    "Отжимания на брусьях 3x10\n\n"
    "День 2: Спина/бицепс\n"
    "Подтягивания 4x8\n"
    "Тяга штанги в наклоне 3x10x40кг\n\n"
    "Формат строки упражнения: Название подходыXповторы[xвес[кг]].\n"
    "Вес необязателен (можно не указывать для упражнений с собственным весом).\n\n"
    "2. Посмотреть сохранённую программу — /program\n"
    "3. Начать тренировку — /train, выбрать день, и по очереди вводить "
    "фактически выполненные подходы (например: 80x8, 80x8, 75x6).\n"
    "4. В конце тренировки бот пришлёт сводку: тоннаж, подходы, повторения, время.\n\n"
    "Отменить текущее действие — /cancel."
)


class TrainerBot:
    def __init__(self, api: TelegramAPI, storage: Storage):
        self.api = api
        self.storage = storage

    # ---------- точка входа для одного update ----------
    def handle_update(self, update: dict):
        try:
            if "message" in update:
                self._handle_message(update["message"])
            elif "callback_query" in update:
                self._handle_callback(update["callback_query"])
        except Exception:
            log.exception("Ошибка при обработке update: %s", update)

    # ---------- сообщения ----------
    def _handle_message(self, message: dict):
        chat_id = message["chat"]["id"]
        user = message["from"]
        user_id = user["id"]
        text = (message.get("text") or "").strip()

        self.storage.ensure_user(user_id, user.get("username"))
        session = self.storage.get_session(user_id)

        if text.startswith("/start"):
            self.storage.clear_session(user_id)
            self.api.send_message(chat_id, "Привет! " + HELP_TEXT)
            return

        if text.startswith("/help"):
            self.api.send_message(chat_id, HELP_TEXT)
            return

        if text.startswith("/cancel"):
            self.storage.clear_session(user_id)
            self.api.send_message(chat_id, "Действие отменено.")
            return

        if text.startswith("/newprogram"):
            self.storage.set_session(user_id, "awaiting_program", {})
            self.api.send_message(
                chat_id,
                "Пришли программу тренировок одним сообщением по шаблону "
                "(смотри /help для примера).",
            )
            return

        if text.startswith("/program"):
            program = self.storage.get_program(user_id)
            if not program:
                self.api.send_message(chat_id, "Программа ещё не сохранена. Используй /newprogram.")
            else:
                self.api.send_message(chat_id, format_program(program))
            return

        if text.startswith("/train"):
            self._start_train(chat_id, user_id)
            return

        # --- обработка в зависимости от состояния сессии ---
        state = session["state"]
        if state == "awaiting_program":
            self._handle_program_text(chat_id, user_id, text)
            return

        if state == "logging":
            self._handle_set_input(chat_id, user_id, session["data"], text)
            return

        # нет активного состояния и это не команда
        self.api.send_message(
            chat_id, "Не понял команду. Список команд — /help."
        )

    def _handle_program_text(self, chat_id: int, user_id: int, text: str):
        try:
            program = parse_program(text)
        except ProgramParseError as e:
            self.api.send_message(
                chat_id, f"Ошибка разбора программы: {e}\n\nПопробуй ещё раз или /cancel."
            )
            return
        self.storage.save_program(user_id, program, text)
        self.storage.clear_session(user_id)
        self.api.send_message(
            chat_id,
            f"Программа «{program['name']}» сохранена!\n\n{format_program(program)}\n\n"
            f"Начать тренировку — /train",
        )

    def _start_train(self, chat_id: int, user_id: int):
        program = self.storage.get_program(user_id)
        if not program:
            self.api.send_message(chat_id, "Сначала пришли программу — /newprogram.")
            return
        buttons = [
            [(day["name"], f"train_day:{i}")] for i, day in enumerate(program["days"])
        ]
        self.storage.set_session(user_id, "choosing_day", {})
        self.api.send_message(
            chat_id, "Выбери день тренировки:", reply_markup=inline_keyboard(buttons)
        )

    def _handle_set_input(self, chat_id: int, user_id: int, session_data: dict, text: str):
        state = session_data
        ex = current_exercise(state)
        if ex is None:
            self._finish_workout(chat_id, user_id, state)
            return

        if text.lower() in ("/skip", "пропустить", "skip"):
            skip_exercise(state)
        else:
            try:
                sets = parse_sets_input(text)
            except SetParseError as e:
                self.api.send_message(chat_id, str(e))
                return
            record_sets(state, sets)

        if is_finished(state):
            self._finish_workout(chat_id, user_id, state)
        else:
            self.storage.set_session(user_id, "logging", state)
            self._prompt_current_exercise(chat_id, state)

    def _prompt_current_exercise(self, chat_id: int, state: dict):
        ex = current_exercise(state)
        idx = state["exercise_index"] + 1
        total = len(state["exercises"])
        weight_part = f" x {ex['weight']:g} кг" if ex["weight"] else ""
        self.api.send_message(
            chat_id,
            f"Упражнение {idx}/{total}: {ex['name']}\n"
            f"План: {ex['sets']}x{ex['reps']}{weight_part}\n\n"
            f"Введи фактические подходы через запятую (вес x повторы), "
            f"например: 80x8, 80x8, 75x6\n"
            f"Если без веса — просто повторы: 8, 8, 6\n"
            f"Или /skip, чтобы пропустить упражнение.",
        )

    def _finish_workout(self, chat_id: int, user_id: int, state: dict):
        summary = build_summary(state)
        self.storage.finish_workout(state["workout_id"], state["log"])
        self.storage.clear_session(user_id)
        self.api.send_message(chat_id, summary)

    # ---------- inline-кнопки ----------
    def _handle_callback(self, callback: dict):
        query_id = callback["id"]
        chat_id = callback["message"]["chat"]["id"]
        user_id = callback["from"]["id"]
        data = callback.get("data", "")

        if data.startswith("train_day:"):
            day_index = int(data.split(":", 1)[1])
            program = self.storage.get_program(user_id)
            if not program or day_index >= len(program["days"]):
                self.api.answer_callback_query(query_id, "Программа изменилась, начни заново: /train")
                return
            day = program["days"][day_index]
            workout_id = self.storage.start_workout(user_id, day["name"])
            state = new_session_state(day["name"], workout_id, day["exercises"])
            self.storage.set_session(user_id, "logging", state)
            self.api.answer_callback_query(query_id)
            self.api.send_message(chat_id, f"Начинаем тренировку «{day['name']}»! 💪")
            self._prompt_current_exercise(chat_id, state)
            return

        self.api.answer_callback_query(query_id)


def run_polling():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задан. Укажи его в .env (см. .env.example) или переменной окружения."
        )
    api = TelegramAPI(BOT_TOKEN)
    storage = Storage(DB_PATH)
    trainer = TrainerBot(api, storage)

    me = api.get_me()
    log.info("Бот запущен: @%s", me.get("username"))

    offset = None
    while True:
        try:
            updates = api.get_updates(offset=offset, timeout=30)
        except Exception:
            log.exception("Ошибка long polling, повтор через 5 секунд")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            trainer.handle_update(update)


if __name__ == "__main__":
    run_polling()
