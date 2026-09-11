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
import changelog
import editor
import freeform
import llm
import wizard
import workout
from config import (ANTHROPIC_API_KEY, BOT_TOKEN, DB_PATH, LLM_MODEL,
                    LLM_TIMEOUT, TZ_OFFSET_HOURS)
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
    ("import", "Загрузить программу текстом"),
    ("help", "Как пользоваться ботом"),
    ("whatsnew", "Что нового в боте"),
    ("cancel", "Отменить текущее действие"),
]

WELCOME_TEXT = (
    "👋 Это дневник тренировок.\n\n"
    "Он нужен, чтобы тренироваться по своей программе и вести записи прямо "
    "в чате — вместо блокнота, заметок в телефоне или таблицы.\n\n"
    "Как это работает:\n"
    "1️⃣ Заводите программу — по шагам или пришлите её текстом, как она "
    "у вас записана.\n"
    "2️⃣ На тренировке открываете нужный день, выбираете упражнение "
    "в любом порядке и записываете каждый подход: вес, потом повторения.\n"
    "3️⃣ В конце получаете сводку: что сделали и с какими весами. "
    "Все тренировки сохраняются.\n\n"
    "Кому подойдёт:\n"
    "• тренируетесь по программе — своей или от тренера;\n"
    "• надоело держать веса и подходы в голове;\n"
    "• нужен простой дневник без регистрации и лишних экранов.\n\n"
    "Чего бот не делает: не составляет программу за вас и не даёт "
    "тренерских советов — он работает по вашей программе.\n\n"
    "С чего начать:"
)

HELP_TEXT = (
    "Я помогу тренироваться по твоей собственной программе.\n\n"
    "📝 /newprogram — составить программу по шагам:\n"
    "   • сколько тренировочных дней в неделю;\n"
    "   • название каждого дня (группа мышц, «Фулбади», день недели "
    "или просто номер);\n"
    "   • упражнения — из каталога или своим названием;\n"
    "   • сколько подходов в каждом упражнении.\n\n"
    "📄 /import — прислать готовую программу одним сообщением, в свободной "
    "форме: бот сам разберёт её на дни и упражнения и покажет результат "
    "на подтверждение.\n"
    "📋 /program — посмотреть программу.\n"
    "✏️ /edit — изменить программу: дни, упражнения, подходы.\n"
    "🏋️ /train — тренировка: выбираешь упражнение в любом порядке и на "
    "каждый подход вводишь вес, затем повторения.\n"
    "📊 В конце — сводка по тренировке.\n\n"
    "❌ /cancel — отменить текущее действие."
)


class TrainerBot:
    def __init__(self, api: TelegramAPI, storage: Storage, llm_parse=None):
        self.api = api
        self.storage = storage
        # llm_parse(text) -> программа; None значит «работаем без AI».
        # Вынесено параметром, чтобы тесты подставляли свою заглушку.
        self.llm_parse = llm_parse

    # ---------- обновления бота ----------
    def _announce_updates(self, chat_id: int, user_id: int, session: dict):
        """Один раз после обновления показываем, что изменилось. Только когда
        человек ничего не делает — посреди тренировки это мешало бы."""
        if session["state"] != "idle":
            return
        seen = self.storage.get_seen_version(user_id)
        if seen == changelog.VERSION:
            return
        releases = changelog.releases_since(seen)
        self.storage.set_seen_version(user_id, changelog.VERSION)
        if releases:
            self.api.send_message(chat_id, changelog.format_releases(releases))

    # ---------- служебное ----------
    def handle_update(self, update: dict):
        try:
            if "message" in update:
                self._handle_message(update["message"])
            elif "callback_query" in update:
                self._handle_callback(update["callback_query"])
        except Exception:
            log.exception("Ошибка при обработке update: %s", update)
            self._report_failure(update)

    def _report_failure(self, update: dict):
        """Если обработка упала, пользователь не должен остаться перед
        «зависшей» кнопкой: гасим её и честно говорим, что сломалось."""
        callback = update.get("callback_query") or {}
        message = update.get("message") or callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        try:
            if callback.get("id"):
                self.api.answer_callback_query(callback["id"], "Не получилось, попробуйте ещё раз")
            if chat_id:
                self.api.send_message(
                    chat_id,
                    "⚠️ Что-то пошло не так при обработке этого действия. "
                    "Попробуйте ещё раз, а если повторится — /cancel и начните заново.",
                )
        except Exception:
            log.exception("Не удалось сообщить пользователю об ошибке")

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
        user = message.get("from") or {}
        user_id = user.get("id")
        text = (message.get("text") or "").strip()

        if not user_id:
            return
        if message["chat"].get("type", "private") != "private":
            # в группе у каждого своя сессия, но экран один на всех — путаница
            self.api.send_message(
                chat_id, "Я работаю только в личных сообщениях: напишите мне в личку."
            )
            return

        is_new_user = self.storage.ensure_user(user_id, user.get("username"))
        session = self.storage.get_session(user_id)
        if is_new_user:
            # новичку прошлые обновления не нужны — он и так видит свежую версию
            self.storage.set_seen_version(user_id, changelog.VERSION)
        else:
            self._announce_updates(chat_id, user_id, session)

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
        if state_name == "import":
            self._import_text(chat_id, user_id, session["data"], text, offer=False)
            return

        # человек просто вставил программу в чат, ничего не нажимая
        if freeform.looks_like_program(text):
            self._import_text(chat_id, user_id, {"msg_id": None}, text, offer=True)
            return

        self.api.send_message(chat_id, "Не понял. Список команд — /help")

    def _handle_command(self, chat_id: int, user_id: int, text: str, session: dict):
        command = text.split()[0].split("@")[0]

        if command == "/start":
            self.storage.clear_session(user_id)
            self.api.send_message(
                chat_id,
                WELCOME_TEXT,
                reply_markup=inline_keyboard([
                    [("📝 Составить программу по шагам", "s:new")],
                    [("📄 Прислать программу текстом", "s:import")],
                    [("❓ Все команды", "s:help")],
                ]),
            )
            return
        if command == "/whatsnew":
            self.storage.set_seen_version(user_id, changelog.VERSION)
            self.api.send_message(
                chat_id,
                changelog.format_releases(
                    changelog.RELEASES[:3], "🆕 Что нового в боте"
                ),
            )
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
        if command == "/import":
            state = {"msg_id": None, "step": "await_text"}
            self._render(chat_id, state, freeform.screen_ask_text())
            self.storage.set_session(user_id, "import", state)
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

    def _my_exercises(self, user_id: int) -> list:
        return self.storage.list_user_exercises(user_id)

    def _wizard_text(self, chat_id: int, user_id: int, state: dict, text: str, message: dict):
        step = state["step"]
        self._cleanup_user_message(chat_id, message)

        if step == wizard.STEP_PASTE:
            # текст программы: разбираем и уходим в подтверждение
            import_state = {"msg_id": state.get("msg_id")}
            self._import_text(chat_id, user_id, import_state, text, offer=False)
            return
        if step == wizard.STEP_DAY_COMMENT:
            wizard.set_day_comment(state, text)
            self._render(chat_id, state, wizard.screen_pick_group(state, self._my_exercises(user_id)))
        elif step == wizard.STEP_CUSTOM_EXERCISE:
            name = text.strip()[:100]
            if not name:
                return
            state["pending_exercise"] = name
            state["is_custom"] = not self.storage.has_user_exercise(user_id, name)
            state["save_to_list"] = False
            state["step"] = wizard.STEP_SETS
            self._render(chat_id, state, self._wizard_sets_screen(state))
        elif step == wizard.STEP_CUSTOM_SETS:
            sets = self._parse_sets(text)
            if sets is None:
                self._render_error(
                    chat_id, state, wizard.screen_custom_sets(),
                    f"Нужно число от 1 до {MAX_SETS}.",
                )
                self.storage.set_session(user_id, "wizard", state)
                return
            self._wizard_add_exercise(chat_id, user_id, state, sets)
        else:
            return
        self.storage.set_session(user_id, "wizard", state)

    def _wizard_sets_screen(self, state: dict) -> tuple:
        return wizard.screen_sets(
            state["pending_exercise"],
            is_custom=state.get("is_custom", False),
            save_to_list=state.get("save_to_list", False),
        )

    def _wizard_add_exercise(self, chat_id: int, user_id: int, state: dict, sets: int):
        name = state["pending_exercise"]
        if state.get("is_custom") and state.get("save_to_list"):
            self.storage.add_user_exercise(user_id, name)
        wizard.add_exercise(state, name, sets)
        self._render(chat_id, state, wizard.screen_day_menu(state))

    def _wizard_callback(self, chat_id: int, user_id: int, state: dict, data: str) -> str | None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "dc":
            wizard.set_days_count(state, int(parts[2]))
            self._render(chat_id, state, wizard.screen_day_comment(state))
        elif action == "paste":
            state["step"] = wizard.STEP_PASTE
            self._render(chat_id, state, wizard.screen_paste())
        elif action == "steps":
            state["step"] = wizard.STEP_DAYS_COUNT
            self._render(chat_id, state, wizard.screen_days_count())
        elif action == "nocom":
            wizard.set_day_comment(state, "")
            self._render(chat_id, state, wizard.screen_pick_group(state, self._my_exercises(user_id)))
        elif action == "grp":
            group_index = int(parts[2])
            if not catalog.is_valid(group_index):
                return "Группа не найдена"
            state["group_index"] = group_index
            state["step"] = wizard.STEP_PICK_EXERCISE
            self._render(chat_id, state, wizard.screen_pick_exercise(group_index))
        elif action == "groups":
            state["step"] = wizard.STEP_PICK_GROUP
            self._render(chat_id, state, wizard.screen_pick_group(state, self._my_exercises(user_id)))
        elif action == "mine":
            my_exercises = self._my_exercises(user_id)
            if not my_exercises:
                return "Список пуст"
            state["step"] = wizard.STEP_MY_LIST
            self._render(chat_id, state, wizard.screen_my_exercises(my_exercises))
        elif action == "mex":
            my_exercises = self._my_exercises(user_id)
            index = int(parts[2])
            if not 0 <= index < len(my_exercises):
                return "Упражнение не найдено"
            state["pending_exercise"] = my_exercises[index]
            state["is_custom"] = False
            state["step"] = wizard.STEP_SETS
            self._render(chat_id, state, self._wizard_sets_screen(state))
        elif action == "savetoggle":
            state["save_to_list"] = not state.get("save_to_list", False)
            self._render(chat_id, state, self._wizard_sets_screen(state))
        elif action == "ex":
            group_index, exercise_index = int(parts[2]), int(parts[3])
            if not catalog.is_valid(group_index, exercise_index):
                return "Упражнение не найдено"
            state["pending_exercise"] = catalog.exercise_name(group_index, exercise_index)
            state["is_custom"] = False
            state["step"] = wizard.STEP_SETS
            self._render(chat_id, state, self._wizard_sets_screen(state))
        elif action == "own":
            state["step"] = wizard.STEP_CUSTOM_EXERCISE
            self._render(chat_id, state, wizard.screen_custom_exercise())
        elif action == "sets":
            if not state.get("pending_exercise"):
                return "Сначала выберите упражнение"
            self._wizard_add_exercise(chat_id, user_id, state, int(parts[2]))
        elif action == "setsx":
            state["step"] = wizard.STEP_CUSTOM_SETS
            self._render(chat_id, state, wizard.screen_custom_sets())
        elif action == "more":
            state["step"] = wizard.STEP_PICK_GROUP
            self._render(chat_id, state, wizard.screen_pick_group(state, self._my_exercises(user_id)))
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

    # ---------- загрузка программы текстом ----------
    def _import_text(self, chat_id: int, user_id: int, state: dict, text: str, offer: bool):
        """Сначала бесплатные эвристики; если они не справились — Claude API
        (когда он настроен). На самопредложении разбора AI не зовём, чтобы не
        тратить запросы на случайные сообщения."""
        program, warnings, stats = None, [], {}
        try:
            program, warnings, stats = freeform.analyze(text)
        except freeform.FreeformError as e:
            heuristic_error = str(e)
        else:
            heuristic_error = None

        via_llm = False
        if self.llm_parse and not offer and (program is None or freeform.looks_weak(stats)):
            try:
                program = self.llm_parse(text)
                warnings, via_llm = [], True
            except Exception as e:  # noqa: BLE001 — падать из-за AI нельзя
                log.info("Разбор через LLM не удался: %s", e)

        if program is None:
            if offer:
                return
            self._render_error(
                chat_id, state, freeform.screen_ask_text(),
                heuristic_error or "Не получилось разобрать текст.",
            )
            self.storage.set_session(user_id, "import", state)
            return

        state["program"] = program
        state["warnings"] = warnings
        screen = (
            freeform.screen_offer(program, warnings, via_llm) if offer
            else freeform.screen_confirm(program, warnings, via_llm)
        )
        self._render(chat_id, state, screen)
        self.storage.set_session(user_id, "import", state)

    def _import_callback(self, chat_id: int, user_id: int, state: dict, data: str) -> str | None:
        action = data.split(":")[1] if ":" in data else ""
        program = state.get("program")

        if action == "cancel":
            self._drop_screen(chat_id, state)
            self.storage.clear_session(user_id)
            self.api.send_message(chat_id, "Загрузка отменена.")
            return None

        if action == "retry":
            state.pop("program", None)
            state.pop("warnings", None)
            self._render(chat_id, state, freeform.screen_ask_text())
            self.storage.set_session(user_id, "import", state)
            return None

        if action in ("save", "edit"):
            if not program:
                return "Программа не найдена, пришлите текст заново"
            self.storage.save_program(user_id, program, "")
            if action == "edit":
                self.storage.clear_session(user_id)
                self._start_editor(chat_id, user_id)
                return None
            self._drop_screen(chat_id, state)
            self.storage.clear_session(user_id)
            self.api.send_message(
                chat_id,
                "Программа сохранена! 🎉\n\n" + format_program(program),
                reply_markup=inline_keyboard(
                    [[("🏋️ Начать тренировку", "t:again")], [("✏️ Редактировать", "t:edit")]]
                ),
            )
            return None

        return None

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
            state["is_custom"] = not self.storage.has_user_exercise(user_id, name)
            state["save_to_list"] = False
            state["view"] = editor.VIEW_SETS
            self._render(chat_id, state, self._editor_sets_screen(state))
        elif view == editor.VIEW_MY_ADD:
            name = text.strip()[:100]
            if not name:
                return
            added = self.storage.add_user_exercise(user_id, name)
            state["view"] = editor.VIEW_MY_LIST
            screen = editor.screen_my_list(self._my_exercises(user_id))
            if added:
                self._render(chat_id, state, screen)
            else:
                self._render_error(chat_id, state, screen, "Такое упражнение уже есть в списке.")
        elif view == editor.VIEW_CUSTOM_SETS:
            sets = self._parse_sets(text)
            if sets is None:
                self._render_error(
                    chat_id, state, editor.screen_custom_sets(),
                    f"Нужно число от 1 до {MAX_SETS}.",
                )  # экран ввода остаётся тем же, ошибка сверху
                self._editor_save(user_id, state)
                return
            self._editor_apply_sets(chat_id, user_id, state, sets)
        else:
            return
        self._editor_save(user_id, state)

    def _editor_sets_screen(self, state: dict) -> tuple:
        title = state.get("pending_exercise")
        if not title:
            day = editor.current_day(state)
            title = day["exercises"][state["exercise"]]["name"]
        return editor.screen_sets(
            title,
            is_custom=state.get("is_custom", False),
            save_to_list=state.get("save_to_list", False),
        )

    def _editor_apply_sets(self, chat_id: int, user_id: int, state: dict, sets: int):
        """Подходы задаются и при добавлении упражнения, и при его правке."""
        if state.get("pending_exercise"):
            name = state["pending_exercise"]
            if state.get("is_custom") and state.get("save_to_list"):
                self.storage.add_user_exercise(user_id, name)
            editor.add_exercise(state, name, sets)
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
        elif action == "daysave":
            state["view"] = editor.VIEW_DAYS
            state["day"] = None
            self.storage.save_program(user_id, state["program"], "")
            self._render(chat_id, state, editor.screen_days(state))
            self._editor_save(user_id, state)
            return "День сохранён"
        elif action == "addex":
            state["pending_exercise"] = ""
            state["view"] = editor.VIEW_PICK_GROUP
            self._render(chat_id, state, editor.screen_pick_group(state, self._my_exercises(user_id)))
        elif action == "grp":
            group_index = int(parts[2])
            if not catalog.is_valid(group_index):
                return "Группа не найдена"
            state["group_index"] = group_index
            state["view"] = editor.VIEW_PICK_EXERCISE
            self._render(chat_id, state, editor.screen_pick_exercise(group_index))
        elif action == "groups":
            state["view"] = editor.VIEW_PICK_GROUP
            self._render(chat_id, state, editor.screen_pick_group(state, self._my_exercises(user_id)))
        elif action == "mine":
            my_exercises = self._my_exercises(user_id)
            if not my_exercises:
                return "Список пуст"
            state["view"] = editor.VIEW_PICK_MINE
            self._render(chat_id, state, editor.screen_my_pick(my_exercises))
        elif action == "mex":
            my_exercises = self._my_exercises(user_id)
            index = int(parts[2])
            if not 0 <= index < len(my_exercises):
                return "Упражнение не найдено"
            state["pending_exercise"] = my_exercises[index]
            state["is_custom"] = False
            state["view"] = editor.VIEW_SETS
            self._render(chat_id, state, self._editor_sets_screen(state))
        elif action == "savetoggle":
            state["save_to_list"] = not state.get("save_to_list", False)
            self._render(chat_id, state, self._editor_sets_screen(state))
        elif action == "mylist":
            state["view"] = editor.VIEW_MY_LIST
            self._render(chat_id, state, editor.screen_my_list(self._my_exercises(user_id)))
        elif action == "myadd":
            state["view"] = editor.VIEW_MY_ADD
            self._render(chat_id, state, editor.screen_my_add())
        elif action == "mydel":
            my_exercises = self._my_exercises(user_id)
            index = int(parts[2])
            if not 0 <= index < len(my_exercises):
                return "Упражнение не найдено"
            self.storage.delete_user_exercise(user_id, my_exercises[index])
            self._render(chat_id, state, editor.screen_my_list(self._my_exercises(user_id)))
        elif action == "exsel":
            group_index, exercise_index = int(parts[2]), int(parts[3])
            if not catalog.is_valid(group_index, exercise_index):
                return "Упражнение не найдено"
            state["pending_exercise"] = catalog.exercise_name(group_index, exercise_index)
            state["is_custom"] = False
            state["view"] = editor.VIEW_SETS
            self._render(chat_id, state, self._editor_sets_screen(state))
        elif action == "own":
            state["view"] = editor.VIEW_CUSTOM_EXERCISE
            self._render(chat_id, state, editor.screen_custom_exercise())
        elif action == "setsedit":
            state["pending_exercise"] = ""
            state["is_custom"] = False
            state["view"] = editor.VIEW_SETS
            self._render(chat_id, state, self._editor_sets_screen(state))
        elif action == "sets":
            self._editor_apply_sets(chat_id, user_id, state, int(parts[2]))
        elif action == "setsx":
            state["view"] = editor.VIEW_CUSTOM_SETS
            self._render(chat_id, state, editor.screen_custom_sets())
        elif action == "delex":
            editor.delete_exercise(state)
            self._render(chat_id, state, editor.screen_day(state))
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
        elif action == "wq":
            try:
                weight = float(parts[2])
            except (IndexError, ValueError):
                return "Не понял вес"
            workout.set_pending_weight(state, weight)
            self._render(chat_id, state, workout.screen_reps(state))
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
        workout.close_open_exercise(state)
        summary = workout.build_summary(state, TZ_OFFSET_HOURS)
        self.storage.finish_workout(state["workout_id"], state["log"])
        self._drop_screen(chat_id, state)
        self.api.send_message(chat_id, summary)
        # тренировку держим в сессии — её можно открыть и поправить
        after = {"msg_id": None, "workout": state}
        self._render(chat_id, after, workout.screen_after_summary())
        self.storage.set_session(user_id, "after_workout", after)

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
        answered = False
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
        elif data.startswith("i:"):
            if session["state"] != "import":
                note = "Экран устарел. Загрузить программу — /import"
            else:
                note = self._import_callback(chat_id, user_id, state, data)
        elif data.startswith("s:"):
            action = data.split(":")[1] if ":" in data else ""
            if action == "new":
                self.storage.clear_session(user_id)
                self._start_wizard(chat_id, user_id)
            elif action == "import":
                import_state = {"msg_id": None, "step": "await_text"}
                self._render(chat_id, import_state, freeform.screen_ask_text())
                self.storage.set_session(user_id, "import", import_state)
            elif action == "help":
                self.api.send_message(chat_id, HELP_TEXT)
        elif data.startswith("t:"):
            note = self._train_callback(chat_id, user_id, session, state, data, message_id)

        if not answered:
            self.api.answer_callback_query(query_id, note)

    def _train_callback(self, chat_id, user_id, session, state, data, message_id) -> str | None:
        action = data.split(":")[1] if ":" in data else ""

        # кнопки, доступные вне тренировки
        if action == "again":
            self.storage.clear_session(user_id)
            self._start_train(chat_id, user_id)
            return None
        if action == "editlast":
            last = state.get("workout") if session["state"] == "after_workout" else None
            if not last:
                return "Прошлая тренировка больше недоступна"
            last["msg_id"] = None
            self._render(chat_id, last, workout.screen_choose(last))
            self.storage.set_session(user_id, "logging", last)
            return None
        if action == "program":
            program = self.storage.get_program(user_id)
            if not program:
                return "Программа ещё не создана"
            # кнопка «Редактировать» показывается только вместе с самой программой
            self.api.send_message(
                chat_id,
                format_program(program),
                reply_markup=inline_keyboard([[("✏️ Редактировать", "t:edit")]]),
            )
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
            # Сессия потерялась (например, бот перезапустился без Volume) —
            # обновляем экран, чтобы не выглядело как сломанная кнопка.
            self._render(
                chat_id, state,
                ("Эта тренировка больше не активна — бот перезапускался или "
                 "она уже завершена.\n\nНачать новую — /train", None),
            )
            return "Тренировка больше не активна"
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

    llm_parse = None
    if llm.is_enabled(ANTHROPIC_API_KEY):
        def llm_parse(text: str) -> dict:
            return llm.parse_with_llm(text, ANTHROPIC_API_KEY, LLM_MODEL, LLM_TIMEOUT)
        log.info("Разбор через Claude API включён, модель %s", LLM_MODEL)
    else:
        log.info("ANTHROPIC_API_KEY не задан — работаем только на эвристиках")

    trainer = TrainerBot(api, storage, llm_parse)

    me = api.get_me()
    log.info("Бот запущен: @%s, версия %s", me.get("username"), changelog.VERSION)
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
