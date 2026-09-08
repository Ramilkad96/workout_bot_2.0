# -*- coding: utf-8 -*-
"""
Telegram-бот «Тренер» — пользователь пошагово составляет свою программу
тренировок, тренируется по ней, логирует подходы и получает сводку по
завершению тренировки.

Запуск:
    1. Получить токен у @BotFather в Telegram.
    2. Положить его в .env (см. .env.example) или в переменную окружения BOT_TOKEN.
    3. pip install -r requirements.txt
    4. python bot.py
"""
import logging
import time

import wizard
from config import BOT_TOKEN, DB_PATH
from program import MAX_SETS, format_program, plural_sets, day_title
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
import catalog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trainer_bot")

HELP_TEXT = (
    "Я помогу тренироваться по твоей собственной программе.\n\n"
    "📝 /newprogram — составить программу тренировок. Бот проведёт по шагам:\n"
    "   • сколько тренировочных дней в неделю;\n"
    "   • комментарий к каждому дню (какая группа мышц);\n"
    "   • упражнения — из каталога или своим названием;\n"
    "   • сколько подходов в каждом упражнении.\n\n"
    "📋 /program — посмотреть сохранённую программу.\n"
    "🏋️ /train — начать тренировку: выбрать день и записывать фактические "
    "подходы (например: 80x8, 80x8, 75x6).\n"
    "📊 В конце тренировки бот пришлёт сводку: тоннаж, подходы, повторения, время.\n\n"
    "❌ /cancel — отменить текущее действие."
)


class TrainerBot:
    def __init__(self, api: TelegramAPI, storage: Storage):
        self.api = api
        self.storage = storage

    # ---------- точка входа ----------
    def handle_update(self, update: dict):
        try:
            if "message" in update:
                self._handle_message(update["message"])
            elif "callback_query" in update:
                self._handle_callback(update["callback_query"])
        except Exception:
            log.exception("Ошибка при обработке update: %s", update)

    def _send_screen(self, chat_id: int, screen: tuple):
        text, keyboard = screen
        self.api.send_message(chat_id, text, reply_markup=keyboard)

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
            self._start_wizard(chat_id, user_id)
            return

        if text.startswith("/program"):
            program = self.storage.get_program(user_id)
            if not program:
                self.api.send_message(
                    chat_id, "Программа ещё не создана. Составить — /newprogram"
                )
            else:
                self.api.send_message(chat_id, format_program(program))
            return

        if text.startswith("/train"):
            self._start_train(chat_id, user_id)
            return

        state_name = session["state"]
        if state_name == "wizard":
            self._handle_wizard_text(chat_id, user_id, session["data"], text)
            return
        if state_name == "logging":
            self._handle_set_input(chat_id, user_id, session["data"], text)
            return

        self.api.send_message(chat_id, "Не понял команду. Список команд — /help")

    # ---------- мастер создания программы ----------
    def _start_wizard(self, chat_id: int, user_id: int):
        state = wizard.new_state()
        self.storage.set_session(user_id, "wizard", state)
        if self.storage.get_program(user_id):
            self.api.send_message(
                chat_id,
                "Составляем новую программу. Текущая программа будет заменена "
                "только после того, как мастер дойдёт до конца.",
            )
        self._send_screen(chat_id, wizard.screen_days_count())

    def _handle_wizard_text(self, chat_id: int, user_id: int, state: dict, text: str):
        step = state["step"]

        if step == wizard.STEP_DAY_COMMENT:
            wizard.set_day_comment(state, text)
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_pick_group(state))
            return

        if step == wizard.STEP_CUSTOM_EXERCISE:
            name = text.strip()
            if not name or name.startswith("/"):
                self.api.send_message(chat_id, "Название не распознал, напишите ещё раз.")
                return
            state["pending_exercise"] = name[:100]
            state["step"] = wizard.STEP_SETS
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_sets(state["pending_exercise"]))
            return

        if step == wizard.STEP_CUSTOM_SETS:
            try:
                sets = int(text.strip())
            except ValueError:
                self.api.send_message(chat_id, f"Нужно число от 1 до {MAX_SETS}.")
                return
            if not 1 <= sets <= MAX_SETS:
                self.api.send_message(chat_id, f"Нужно число от 1 до {MAX_SETS}.")
                return
            wizard.add_exercise(state, state["pending_exercise"], sets)
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_day_menu(state))
            return

        if step == wizard.STEP_DAYS_COUNT:
            self.api.send_message(chat_id, "Выберите количество дней кнопкой выше.")
            return

        self.api.send_message(chat_id, "Продолжим — нажмите кнопку в сообщении выше.")

    def _handle_wizard_callback(self, chat_id: int, user_id: int, state: dict, data: str) -> str | None:
        """Возвращает текст всплывающего ответа на callback (или None)."""
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "dc":
            count = int(parts[2])
            wizard.set_days_count(state, count)
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_after_days_count(state))
            self._send_screen(chat_id, wizard.screen_day_comment(state))
            return None

        if action == "nocom":
            wizard.set_day_comment(state, "")
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_pick_group(state))
            return None

        if action == "grp":
            group_index = int(parts[2])
            if not catalog.is_valid(group_index):
                return "Группа не найдена"
            state["group_index"] = group_index
            state["step"] = wizard.STEP_PICK_EXERCISE
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_pick_exercise(group_index))
            return None

        if action == "groups":
            state["step"] = wizard.STEP_PICK_GROUP
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_pick_group(state))
            return None

        if action == "ex":
            group_index, exercise_index = int(parts[2]), int(parts[3])
            if not catalog.is_valid(group_index, exercise_index):
                return "Упражнение не найдено"
            state["pending_exercise"] = catalog.exercise_name(group_index, exercise_index)
            state["step"] = wizard.STEP_SETS
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_sets(state["pending_exercise"]))
            return None

        if action == "own":
            state["step"] = wizard.STEP_CUSTOM_EXERCISE
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_custom_exercise())
            return None

        if action == "sets":
            sets = int(parts[2])
            if not state.get("pending_exercise"):
                return "Сначала выберите упражнение"
            wizard.add_exercise(state, state["pending_exercise"], sets)
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_day_menu(state))
            return None

        if action == "setsx":
            state["step"] = wizard.STEP_CUSTOM_SETS
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_custom_sets())
            return None

        if action == "more":
            state["step"] = wizard.STEP_PICK_GROUP
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_pick_group(state))
            return None

        if action == "undo":
            removed = wizard.remove_last_exercise(state)
            self.storage.set_session(user_id, "wizard", state)
            self._send_screen(chat_id, wizard.screen_day_menu(state))
            return None if removed else "Удалять нечего"

        if action == "dayend":
            day = wizard.current_day(state)
            if not day["exercises"]:
                return "Добавьте хотя бы одно упражнение"
            if wizard.finish_day(state):
                self.storage.set_session(user_id, "wizard", state)
                self._send_screen(chat_id, wizard.screen_day_comment(state))
            else:
                self._finish_wizard(chat_id, user_id, state)
            return None

        return None

    def _finish_wizard(self, chat_id: int, user_id: int, state: dict):
        program = wizard.build_program(state)
        self.storage.save_program(user_id, program, "")
        self.storage.clear_session(user_id)
        self.api.send_message(
            chat_id,
            "Программа сохранена! 🎉\n\n"
            + format_program(program)
            + "\n\nНачать тренировку — /train",
        )

    # ---------- тренировка ----------
    def _start_train(self, chat_id: int, user_id: int):
        program = self.storage.get_program(user_id)
        if not program:
            self.api.send_message(chat_id, "Сначала составьте программу — /newprogram")
            return
        buttons = [
            [(day_title(day), f"train_day:{i}")] for i, day in enumerate(program["days"])
        ]
        self.storage.set_session(user_id, "choosing_day", {})
        self.api.send_message(
            chat_id, "Выберите день тренировки:", reply_markup=inline_keyboard(buttons)
        )

    def _handle_set_input(self, chat_id: int, user_id: int, state: dict, text: str):
        if current_exercise(state) is None:
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
        self.api.send_message(
            chat_id,
            f"Упражнение {idx}/{total}: {ex['name']}\n"
            f"План: {plural_sets(ex.get('sets', 0))}\n\n"
            f"Запишите фактические подходы через запятую (вес x повторы), "
            f"например: 80x8, 80x8, 75x6\n"
            f"Если без веса — просто повторы: 8, 8, 6\n"
            f"Пропустить упражнение — /skip",
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

        session = self.storage.get_session(user_id)

        if data.startswith("w:"):
            if session["state"] != "wizard":
                self.api.answer_callback_query(
                    query_id, "Мастер уже закрыт. Начните заново: /newprogram"
                )
                return
            note = self._handle_wizard_callback(chat_id, user_id, session["data"], data)
            self.api.answer_callback_query(query_id, note)
            return

        if data.startswith("train_day:"):
            day_index = int(data.split(":", 1)[1])
            program = self.storage.get_program(user_id)
            if not program or day_index >= len(program["days"]):
                self.api.answer_callback_query(
                    query_id, "Программа изменилась, начните заново: /train"
                )
                return
            day = program["days"][day_index]
            workout_id = self.storage.start_workout(user_id, day_title(day))
            state = new_session_state(day_title(day), workout_id, day["exercises"])
            self.storage.set_session(user_id, "logging", state)
            self.api.answer_callback_query(query_id)
            self.api.send_message(chat_id, f"Начинаем: {day_title(day)} 💪")
            self._prompt_current_exercise(chat_id, state)
            return

        self.api.answer_callback_query(query_id)


def run_polling():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задан. Укажите его в .env (см. .env.example) "
            "или переменной окружения."
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
