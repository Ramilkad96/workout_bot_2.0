# -*- coding: utf-8 -*-
"""
Telegram-бот «Тренер» — пользователь пошагово составляет свою программу
тренировок, тренируется по ней (вес и повторения по каждому подходу) и
получает сводку по завершению тренировки.

Запуск:
    1. Получить токен у @BotFather в Telegram.
    2. Положить его в .env (см. .env.example) или в переменную окружения BOT_TOKEN.
    3. pip install -r requirements.txt
    4. python bot.py
"""
import logging
import time

import catalog
import editor
import wizard
import workout
from config import BOT_TOKEN, DB_PATH
from program import MAX_SETS, day_title, format_program
from storage import Storage
from telegram_api import TelegramAPI, inline_keyboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trainer_bot")

COMMANDS = [
    ("train", "Начать тренировку"),
    ("program", "Моя программа"),
    ("edit", "Редактировать программу"),
    ("newprogram", "Составить программу заново"),
    ("help", "Как пользоваться ботом"),
    ("cancel", "Отменить текущее действие"),
]

HELP_TEXT = (
    "Я помогу тренироваться по твоей собственной программе.\n\n"
    "📝 /newprogram — составить программу по шагам:\n"
    "   • сколько тренировочных дней в неделю;\n"
    "   • название каждого дня (группа мышц, «Фулбади», день недели "
    "или просто номер);\n"
    "   • упражнения — из каталога или своим названием;\n"
    "   • сколько подходов в каждом упражнении.\n\n"
    "📋 /program — посмотреть программу.\n"
    "✏️ /edit — изменить программу: дни, упражнения, подходы.\n"
    "🏋️ /train — тренировка: выбираешь упражнение в любом порядке и на "
    "каждый подход вводишь вес, затем повторения.\n"
    "📊 В конце — сводка по тренировке.\n\n"
    "❌ /cancel — отменить текущее действие."
)


class TrainerBot:
    def __init__(self, api: TelegramAPI, storage: Storage):
        self.api = api
        self.storage = storage

    # ---------- служебное ----------
    def handle_update(self, update: dict):
        try:
            if "message" in update:
                self._handle_message(update["message"])
            elif "callback_query" in update:
                self._handle_callback(update["callback_query"])
        except Exception:
            log.exception("Ошибка при обработке update: %s", update)

    def _render(self, chat_id: int, state: dict, screen: tuple):
        """Показывает экран: правит уже отправленное сообщение, если возможно,
        иначе шлёт новое. За счёт этого чат не засоряется клавиатурами."""
        text, markup = screen
        msg_id = state.get("msg_id")
        if msg_id and self.api.edit_message_text(chat_id, msg_id, text, markup):
            return
        result = self.api.send_message(chat_id, text, reply_markup=markup)
        state["msg_id"] = result.get("message_id")

    def _render_error(self, chat_id: int, state: dict, screen: tuple, error: str):
        """Ошибка ввода показывается прямо в экране, а не отдельным сообщением —
        иначе клавиатура уезжает вверх и чат засоряется."""
        text, markup = screen
        self._render(chat_id, state, (f"⚠️ {error}\n\n{text}", markup))

    def _drop_screen(self, chat_id: int, state: dict):
        """Убирает сообщение-экран (перед финальным сообщением)."""
        msg_id = state.get("msg_id")
        if msg_id:
            self.api.delete_message(chat_id, msg_id)
            state["msg_id"] = None

    def _cleanup_user_message(self, chat_id: int, message: dict):
        """Удаляет введённое пользователем значение — чат остаётся чистым."""
        message_id = message.get("message_id")
        if message_id:
            self.api.delete_message(chat_id, message_id)

    # ---------- сообщения ----------
    def _handle_message(self, message: dict):
        chat_id = message["chat"]["id"]
        user = message["from"]
        user_id = user["id"]
        text = (message.get("text") or "").strip()

        self.storage.ensure_user(user_id, user.get("username"))
        session = self.storage.get_session(user_id)

        if text.startswith("/"):
            self._handle_command(chat_id, user_id, text, session)
            return

        state_name = session["state"]
        if state_name == "wizard":
            self._wizard_text(chat_id, user_id, session["data"], text, message)
            return
        if state_name == "editor":
            self._editor_text(chat_id, user_id, session["data"], text, message)
            return
        if state_name == "logging":
            self._workout_text(chat_id, user_id, session["data"], text, message)
            return

        self.api.send_message(chat_id, "Не понял. Список команд — /help")

    def _handle_command(self, chat_id: int, user_id: int, text: str, session: dict):
        command = text.split()[0].split("@")[0]

        if command == "/start":
            self.storage.clear_session(user_id)
            self.api.send_message(chat_id, "Привет! " + HELP_TEXT)
            return
        if command == "/help":
            self.api.send_message(chat_id, HELP_TEXT)
            return
        if command == "/cancel":
            self._drop_screen(chat_id, session["data"])
            self.storage.clear_session(user_id)
            self.api.send_message(chat_id, "Действие отменено.")
            return
        if command == "/newprogram":
            self._start_wizard(chat_id, user_id)
            return
        if command == "/program":
            program = self.storage.get_program(user_id)
            if not program:
                self.api.send_message(chat_id, "Программа ещё не создана. Составить — /newprogram")
            else:
                self.api.send_message(
                    chat_id,
                    format_program(program),
                    reply_markup=inline_keyboard([[("✏️ Редактировать", "t:edit")]]),
                )
            return
        if command == "/edit":
            self._start_editor(chat_id, user_id)
            return
        if command == "/train":
            self._start_train(chat_id, user_id)
            return

        self.api.send_message(chat_id, "Такой команды нет. Список команд — /help")

    # ---------- мастер создания программы ----------
    def _start_wizard(self, chat_id: int, user_id: int):
        state = wizard.new_state()
        if self.storage.get_program(user_id):
            self.api.send_message(
                chat_id,
                "Составляем новую программу. Текущая программа будет заменена "
                "только после того, как мастер дойдёт до конца.\n"
                "Изменить существующую — /edit",
            )
        self._render(chat_id, state, wizard.screen_days_count())
        self.storage.set_session(user_id, "wizard", state)

    def _wizard_text(self, chat_id: int, user_id: int, state: dict, text: str, message: dict):
        step = state["step"]
        self._cleanup_user_message(chat_id, message)

        if step == wizard.STEP_DAY_COMMENT:
            wizard.set_day_comment(state, text)
            self._render(chat_id, state, wizard.screen_pick_group(state))
        elif step == wizard.STEP_CUSTOM_EXERCISE:
            name = text.strip()[:100]
            if not name:
                return
            state["pending_exercise"] = name
            state["step"] = wizard.STEP_SETS
            self._render(chat_id, state, wizard.screen_sets(name))
        elif step == wizard.STEP_CUSTOM_SETS:
            sets = self._parse_sets(text)
            if sets is None:
                self._render_error(
                    chat_id, state, wizard.screen_custom_sets(),
                    f"Нужно число от 1 до {MAX_SETS}.",
                )
                self.storage.set_session(user_id, "wizard", state)
                return
            wizard.add_exercise(state, state["pending_exercise"], sets)
            self._render(chat_id, state, wizard.screen_day_menu(state))
        else:
            return
        self.storage.set_session(user_id, "wizard", state)

    def _wizard_callback(self, chat_id: int, user_id: int, state: dict, data: str) -> str | None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "dc":
            wizard.set_days_count(state, int(parts[2]))
            self._render(chat_id, state, wizard.screen_day_comment(state))
        elif action == "nocom":
            wizard.set_day_comment(state, "")
            self._render(chat_id, state, wizard.screen_pick_group(state))
        elif action == "grp":
            group_index = int(parts[2])
            if not catalog.is_valid(group_index):
                return "Группа не найдена"
            state["group_index"] = group_index
            state["step"] = wizard.STEP_PICK_EXERCISE
            self._render(chat_id, state, wizard.screen_pick_exercise(group_index))
        elif action == "groups":
            state["step"] = wizard.STEP_PICK_GROUP
            self._render(chat_id, state, wizard.screen_pick_group(state))
        elif action == "ex":
            group_index, exercise_index = int(parts[2]), int(parts[3])
            if not catalog.is_valid(group_index, exercise_index):
                return "Упражнение не найдено"
            state["pending_exercise"] = catalog.exercise_name(group_index, exercise_index)
            state["step"] = wizard.STEP_SETS
            self._render(chat_id, state, wizard.screen_sets(state["pending_exercise"]))
        elif action == "own":
            state["step"] = wizard.STEP_CUSTOM_EXERCISE
            self._render(chat_id, state, wizard.screen_custom_exercise())
        elif action == "sets":
            if not state.get("pending_exercise"):
                return "Сначала выберите упражнение"
            wizard.add_exercise(state, state["pending_exercise"], int(parts[2]))
            self._render(chat_id, state, wizard.screen_day_menu(state))
        elif action == "setsx":
            state["step"] = wizard.STEP_CUSTOM_SETS
            self._render(chat_id, state, wizard.screen_custom_sets())
        elif action == "more":
            state["step"] = wizard.STEP_PICK_GROUP
            self._render(chat_id, state, wizard.screen_pick_group(state))
        elif action == "undo":
            removed = wizard.remove_last_exercise(state)
            self._render(chat_id, state, wizard.screen_day_menu(state))
            if not removed:
                self.storage.set_session(user_id, "wizard", state)
                return "Удалять нечего"
        elif action == "dayend":
            if not wizard.current_day(state)["exercises"]:
                return "Добавьте хотя бы одно упражнение"
            if wizard.finish_day(state):
                self._render(chat_id, state, wizard.screen_day_comment(state))
            else:
                self._finish_wizard(chat_id, user_id, state)
                return None
        else:
            return None

        self.storage.set_session(user_id, "wizard", state)
        return None

    def _finish_wizard(self, chat_id: int, user_id: int, state: dict):
        program = wizard.build_program(state)
        self.storage.save_program(user_id, program, "")
        self._drop_screen(chat_id, state)
        self.storage.clear_session(user_id)
        self.api.send_message(
            chat_id,
            "Программа сохранена! 🎉\n\n" + format_program(program),
            reply_markup=inline_keyboard(
                [[("🏋️ Начать тренировку", "t:again")], [("✏️ Редактировать", "t:edit")]]
            ),
        )

    # ---------- редактор программы ----------
    def _start_editor(self, chat_id: int, user_id: int):
        program = self.storage.get_program(user_id)
        if not program:
            self.api.send_message(chat_id, "Программа ещё не создана. Составить — /newprogram")
            return
        state = editor.new_state(program)
        self._render(chat_id, state, editor.screen_days(state))
        self.storage.set_session(user_id, "editor", state)

    def _editor_save(self, user_id: int, state: dict):
        self.storage.save_program(user_id, state["program"], "")
        self.storage.set_session(user_id, "editor", state)

    def _editor_text(self, chat_id: int, user_id: int, state: dict, text: str, message: dict):
        view = state["view"]
        self._cleanup_user_message(chat_id, message)

        if view == editor.VIEW_RENAME_DAY:
            editor.rename_day(state, text)
            self._render(chat_id, state, editor.screen_day(state))
        elif view == editor.VIEW_CUSTOM_EXERCISE:
            name = text.strip()[:100]
            if not name:
                return
            state["pending_exercise"] = name
            state["view"] = editor.VIEW_SETS
            self._render(chat_id, state, editor.screen_sets(name))
        elif view == editor.VIEW_CUSTOM_SETS:
            sets = self._parse_sets(text)
            if sets is None:
                self._render_error(
                    chat_id, state, editor.screen_custom_sets(),
                    f"Нужно число от 1 до {MAX_SETS}.",
                )
                self._editor_save(user_id, state)
                return
            self._editor_apply_sets(chat_id, state, sets)
        else:
            return
        self._editor_save(user_id, state)

    def _editor_apply_sets(self, chat_id: int, state: dict, sets: int):
        """Подходы задаются и при добавлении упражнения, и при его правке."""
        if state.get("pending_exercise"):
            editor.add_exercise(state, state["pending_exercise"], sets)
            self._render(chat_id, state, editor.screen_day(state))
        else:
            editor.change_sets(state, sets)
            self._render(chat_id, state, editor.screen_exercise(state))

    def _editor_callback(self, chat_id: int, user_id: int, state: dict, data: str) -> str | None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "days":
            state["view"] = editor.VIEW_DAYS
            state["day"] = None
            self._render(chat_id, state, editor.screen_days(state))
        elif action == "day":
            if len(parts) > 2:
                state["day"] = int(parts[2])
            state["view"] = editor.VIEW_DAY
            state["exercise"] = None
            state["pending_exercise"] = ""
            if editor.current_day(state) is None:
                state["view"] = editor.VIEW_DAYS
                self._render(chat_id, state, editor.screen_days(state))
                return "День не найден"
            self._render(chat_id, state, editor.screen_day(state))
        elif action == "adday":
            if not editor.add_day(state):
                return "Больше дней добавить нельзя"
            self._render(chat_id, state, editor.screen_day(state))
        elif action == "delday":
            if not editor.delete_day(state):
                return "Нельзя удалить последний день"
            self._render(chat_id, state, editor.screen_days(state))
        elif action == "rename":
            state["view"] = editor.VIEW_RENAME_DAY
            self._render(chat_id, state, editor.screen_rename_day(state))
        elif action == "ex":
            state["exercise"] = int(parts[2])
            state["view"] = editor.VIEW_EXERCISE
            self._render(chat_id, state, editor.screen_exercise(state))
        elif action == "addex":
            state["pending_exercise"] = ""
            state["view"] = editor.VIEW_PICK_GROUP
            self._render(chat_id, state, editor.screen_pick_group(state))
        elif action == "grp":
            group_index = int(parts[2])
            if not catalog.is_valid(group_index):
                return "Группа не найдена"
            state["group_index"] = group_index
            state["view"] = editor.VIEW_PICK_EXERCISE
            self._render(chat_id, state, editor.screen_pick_exercise(group_index))
        elif action == "groups":
            state["view"] = editor.VIEW_PICK_GROUP
            self._render(chat_id, state, editor.screen_pick_group(state))
        elif action == "exsel":
            group_index, exercise_index = int(parts[2]), int(parts[3])
            if not catalog.is_valid(group_index, exercise_index):
                return "Упражнение не найдено"
            state["pending_exercise"] = catalog.exercise_name(group_index, exercise_index)
            state["view"] = editor.VIEW_SETS
            self._render(chat_id, state, editor.screen_sets(state["pending_exercise"]))
        elif action == "own":
            state["view"] = editor.VIEW_CUSTOM_EXERCISE
            self._render(chat_id, state, editor.screen_custom_exercise())
        elif action == "setsedit":
            state["pending_exercise"] = ""
            day = editor.current_day(state)
            title = day["exercises"][state["exercise"]]["name"]
            state["view"] = editor.VIEW_SETS
            self._render(chat_id, state, editor.screen_sets(title))
        elif action == "sets":
            self._editor_apply_sets(chat_id, state, int(parts[2]))
        elif action == "setsx":
            state["view"] = editor.VIEW_CUSTOM_SETS
            self._render(chat_id, state, editor.screen_custom_sets())
        elif action == "delex":
            editor.delete_exercise(state)
            self._render(chat_id, state, editor.screen_day(state))
        elif action in ("up", "down"):
            if not editor.move_exercise(state, -1 if action == "up" else 1):
                return "Дальше двигать некуда"
            self._render(chat_id, state, editor.screen_exercise(state))
        elif action == "done":
            program = state["program"]
            self.storage.save_program(user_id, program, "")
            self._drop_screen(chat_id, state)
            self.storage.clear_session(user_id)
            self.api.send_message(
                chat_id,
                "Программа сохранена ✅\n\n" + format_program(program),
                reply_markup=inline_keyboard([[("🏋️ Начать тренировку", "t:again")]]),
            )
            return None
        else:
            return None

        self._editor_save(user_id, state)
        return None

    # ---------- тренировка ----------
    def _start_train(self, chat_id: int, user_id: int):
        program = self.storage.get_program(user_id)
        if not program:
            self.api.send_message(chat_id, "Сначала составьте программу — /newprogram")
            return
        buttons = [
            [(day_title(day), f"t:day:{i}")] for i, day in enumerate(program["days"])
        ]
        state = {"msg_id": None}
        self._render(chat_id, state, ("Выберите день тренировки:", inline_keyboard(buttons)))
        self.storage.set_session(user_id, "choosing_day", state)

    def _begin_workout(self, chat_id: int, user_id: int, day_index: int, msg_id: int | None):
        program = self.storage.get_program(user_id)
        if not program or day_index >= len(program["days"]):
            return "Программа изменилась, начните заново: /train"
        day = program["days"][day_index]
        if not day["exercises"]:
            return "В этом дне нет упражнений"
        workout_id = self.storage.start_workout(user_id, day_title(day))
        state = workout.new_session_state(day_title(day), workout_id, day["exercises"])
        state["msg_id"] = msg_id
        self._render(chat_id, state, workout.screen_choose(state))
        self.storage.set_session(user_id, "logging", state)
        return None

    def _workout_text(self, chat_id: int, user_id: int, state: dict, text: str, message: dict):
        self._cleanup_user_message(chat_id, message)
        step = state.get("step")

        if step == workout.STEP_WEIGHT:
            try:
                weight = workout.parse_weight(text)
            except workout.InputError as e:
                self._render_error(chat_id, state, workout.screen_weight(state), str(e))
                self.storage.set_session(user_id, "logging", state)
                return
            workout.set_pending_weight(state, weight)
            self._render(chat_id, state, workout.screen_reps(state))
        elif step == workout.STEP_REPS:
            try:
                reps = workout.parse_reps(text)
            except workout.InputError as e:
                self._render_error(chat_id, state, workout.screen_reps(state), str(e))
                self.storage.set_session(user_id, "logging", state)
                return
            workout.record_set(state, reps)
            ex = workout.current_exercise(state)
            if workout.sets_done(state) >= ex.get("sets", 0):
                # план по подходам выполнен — возвращаемся к выбору упражнения
                workout.finish_exercise(state)
                self._render(chat_id, state, workout.screen_choose(state))
            else:
                self._render(chat_id, state, workout.screen_weight(state))
        else:
            return
        self.storage.set_session(user_id, "logging", state)

    def _workout_callback(self, chat_id: int, user_id: int, state: dict, data: str) -> str | None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "ex":
            index = int(parts[2])
            if not 0 <= index < len(state["exercises"]):
                return "Упражнение не найдено"
            workout.select_exercise(state, index)
            self._render(chat_id, state, workout.screen_weight(state))
        elif action == "done":
            workout.finish_exercise(state)
            self._render(chat_id, state, workout.screen_choose(state))
        elif action == "skip":
            workout.skip_exercise(state)
            self._render(chat_id, state, workout.screen_choose(state))
        elif action == "back":
            workout.back_to_list(state)
            self._render(chat_id, state, workout.screen_choose(state))
        elif action == "reweight":
            state["pending_weight"] = None
            state["step"] = workout.STEP_WEIGHT
            self._render(chat_id, state, workout.screen_weight(state))
        elif action == "finish":
            self._finish_workout(chat_id, user_id, state)
            return None
        else:
            return None

        self.storage.set_session(user_id, "logging", state)
        return None

    def _finish_workout(self, chat_id: int, user_id: int, state: dict):
        summary = workout.build_summary(state)
        self.storage.finish_workout(state["workout_id"], state["log"])
        self._drop_screen(chat_id, state)
        self.storage.clear_session(user_id)
        self.api.send_message(chat_id, summary)
        next_state = {"msg_id": None}
        self._render(chat_id, next_state, workout.screen_after_summary())

    # ---------- inline-кнопки ----------
    def _handle_callback(self, callback: dict):
        query_id = callback["id"]
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"].get("message_id")
        user_id = callback["from"]["id"]
        data = callback.get("data", "")

        session = self.storage.get_session(user_id)
        state = session["data"]
        if message_id:
            state["msg_id"] = message_id

        note = None
        if data.startswith("w:"):
            if session["state"] != "wizard":
                note = "Мастер уже закрыт. Начните заново: /newprogram"
            else:
                note = self._wizard_callback(chat_id, user_id, state, data)
        elif data.startswith("e:"):
            if session["state"] != "editor":
                note = "Редактор уже закрыт. Открыть снова: /edit"
            else:
                note = self._editor_callback(chat_id, user_id, state, data)
        elif data.startswith("t:"):
            note = self._train_callback(chat_id, user_id, session, state, data, message_id)

        self.api.answer_callback_query(query_id, note)

    def _train_callback(self, chat_id, user_id, session, state, data, message_id) -> str | None:
        action = data.split(":")[1] if ":" in data else ""

        # кнопки, доступные вне тренировки
        if action == "again":
            self.storage.clear_session(user_id)
            self._start_train(chat_id, user_id)
            return None
        if action == "program":
            program = self.storage.get_program(user_id)
            if not program:
                return "Программа ещё не создана"
            self.api.send_message(chat_id, format_program(program))
            return None
        if action == "edit":
            self.storage.clear_session(user_id)
            self._start_editor(chat_id, user_id)
            return None
        if action == "day":
            if session["state"] != "choosing_day":
                return "Выбор дня устарел, начните заново: /train"
            return self._begin_workout(chat_id, user_id, int(data.split(":")[2]), message_id)

        if session["state"] != "logging":
            return "Тренировка уже завершена. Начать новую — /train"
        return self._workout_callback(chat_id, user_id, state, data)

    # ---------- утилиты ----------
    @staticmethod
    def _parse_sets(text: str) -> int | None:
        try:
            sets = int(text.strip())
        except ValueError:
            return None
        return sets if 1 <= sets <= MAX_SETS else None


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
    try:
        api.set_my_commands(COMMANDS)
        api.set_chat_menu_button()
    except Exception:
        log.exception("Не удалось установить меню команд")

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
