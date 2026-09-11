# -*- coding: utf-8 -*-
"""
Офлайн-тесты: сеть и Telegram не нужны. Фейковый API умеет отправлять,
редактировать и удалять сообщения, поэтому тесты проверяют не только логику,
но и то, что чат не засоряется лишними сообщениями.

Запуск: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catalog
import changelog
import freeform
import llm
from bot import COMMANDS, TrainerBot
from program import format_program, plural_days, plural_sets
from storage import Storage


class FakeAPI:
    """Имитирует чат: хранит сообщения, правит и удаляет их по id."""

    def __init__(self):
        self.messages = {}       # message_id -> dict
        self.order = []
        self.next_id = 1
        self.callback_notes = []
        self.delete_calls = []
        self.commands = None
        self.menu_button_set = False

    # --- API ---
    def send_message(self, chat_id, text, reply_markup=None):
        message_id = self.next_id
        self.next_id += 1
        self.messages[message_id] = {
            "chat_id": chat_id, "text": text, "markup": reply_markup, "deleted": False,
        }
        self.order.append(message_id)
        return {"message_id": message_id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        message = self.messages.get(message_id)
        if not message or message["deleted"]:
            return False
        message["text"] = text
        message["markup"] = reply_markup
        return True

    def delete_message(self, chat_id, message_id):
        self.delete_calls.append(message_id)
        message = self.messages.get(message_id)
        if not message or message["deleted"]:
            return False
        message["deleted"] = True
        return True

    def answer_callback_query(self, callback_query_id, text=None):
        self.callback_notes.append((callback_query_id, text))

    def set_my_commands(self, commands):
        self.commands = commands

    def set_chat_menu_button(self):
        self.menu_button_set = True

    # --- помощники для тестов ---
    def visible(self):
        return [self.messages[i] for i in self.order if not self.messages[i]["deleted"]]

    def screen(self):
        """Текст последнего видимого сообщения."""
        visible = self.visible()
        return visible[-1]["text"] if visible else ""

    def screen_markup(self):
        visible = self.visible()
        return visible[-1]["markup"] if visible else None

    def buttons(self):
        markup = self.screen_markup()
        if not markup:
            return []
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]

    def button_labels(self):
        markup = self.screen_markup()
        if not markup:
            return []
        return [b["text"] for row in markup["inline_keyboard"] for b in row]

    def all_text(self):
        return "\n".join(m["text"] for m in self.visible())

    def last_note(self):
        return self.callback_notes[-1][1] if self.callback_notes else None


class BaseBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)
        self.api = FakeAPI()
        self.bot = TrainerBot(self.api, self.storage)
        self.user_id = 42
        self.user_message_id = 900

    def tearDown(self):
        os.unlink(self.tmp.name)

    def send(self, text):
        """Сообщение от пользователя (со своим message_id, как в Telegram)."""
        self.user_message_id += 1
        self.bot.handle_update({
            "message": {
                "message_id": self.user_message_id,
                "chat": {"id": self.user_id, "type": "private"},
                "from": {"id": self.user_id, "username": "tester"},
                "text": text,
            }
        })
        return self.user_message_id

    def click(self, data):
        """Нажатие кнопки на текущем экране."""
        visible = self.api.visible()
        message_id = None
        for mid in reversed(self.api.order):
            if not self.api.messages[mid]["deleted"] and self.api.messages[mid]["markup"]:
                message_id = mid
                break
        self.bot.handle_update({
            "callback_query": {
                "id": "cbq",
                "from": {"id": self.user_id, "username": "tester"},
                "message": {"message_id": message_id, "chat": {"id": self.user_id}},
                "data": data,
            }
        })

    # --- быстрые сценарии ---
    def make_program(self, days=1, exercises_per_day=2):
        self.send("/newprogram")
        self.click(f"w:dc:{days}")
        for _ in range(days):
            self.click("w:nocom")
            for e in range(exercises_per_day):
                self.click("w:grp:0")
                self.click(f"w:ex:0:{e}")
                self.click("w:sets:3")
                if e + 1 < exercises_per_day:
                    self.click("w:more")
            self.click("w:dayend")


class CrossModuleTests(unittest.TestCase):
    """Ловит рассинхронизацию файлов: один модуль зовёт функцию, которой
    в другом уже (или ещё) нет. Именно так однажды молча сломалась кнопка
    «Завершить тренировку» — bot.py обновили, а workout.py нет."""

    def test_every_module_call_exists(self):
        import ast
        import importlib

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        missing = []

        for filename in sorted(os.listdir(root)):
            if not filename.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(root, filename), encoding="utf-8").read())

            # какие модули этот файл импортирует именно как модули
            imported = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.asname or alias.name
                        try:
                            imported[name] = importlib.import_module(alias.name)
                        except ImportError:
                            pass

            # имена, переопределённые внутри файла, проверять нельзя:
            # это уже не модуль, а локальная переменная
            shadowed = {
                target.id
                for node in ast.walk(tree)
                if isinstance(node, (ast.Assign, ast.For))
                for target in ast.walk(node.targets[0] if isinstance(node, ast.Assign) else node.target)
                if isinstance(target, ast.Name)
            }
            shadowed |= {
                arg.arg
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for arg in node.args.args
            }

            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                    continue
                name = node.value.id
                if name not in imported or name in shadowed:
                    continue
                if not hasattr(imported[name], node.attr):
                    missing.append(f"{filename}:{node.lineno} → {name}.{node.attr}")

        self.assertEqual(
            missing, [], "Вызовы несуществующих функций:\n" + "\n".join(missing)
        )


class MenuTests(unittest.TestCase):
    def test_commands_cover_main_actions(self):
        names = [c for c, _ in COMMANDS]
        for expected in ("train", "program", "edit", "newprogram", "import", "help", "cancel"):
            self.assertIn(expected, names)
        for _, description in COMMANDS:
            self.assertTrue(description and description[0].isupper())


class PluralTests(unittest.TestCase):
    def test_plurals(self):
        self.assertEqual(plural_sets(1), "1 подход")
        self.assertEqual(plural_sets(3), "3 подхода")
        self.assertEqual(plural_sets(5), "5 подходов")
        self.assertEqual(plural_days(2), "2 дня")


class CatalogTests(unittest.TestCase):
    def test_callback_data_fits_telegram_limit(self):
        for gi in range(len(catalog.group_names())):
            for ei in range(len(catalog.exercises_of(gi))):
                for prefix in ("w:ex", "e:exsel"):
                    self.assertLessEqual(len(f"{prefix}:{gi}:{ei}".encode()), 64)


class WizardTests(BaseBotTest):
    def test_wizard_edits_one_screen_instead_of_spamming(self):
        """Весь мастер живёт в одном сообщении, ответы пользователя удаляются."""
        self.send("/newprogram")
        screen_count_before = len(self.api.visible())
        self.click("w:dc:1")
        user_msg = self.send("Фулбади")
        self.click("w:grp:0")
        self.click("w:ex:0:0")
        self.click("w:sets:4")

        # ни одного нового сообщения от бота за все эти шаги
        self.assertEqual(len(self.api.visible()), screen_count_before)
        # сообщение пользователя удалено
        self.assertIn(user_msg, self.api.delete_calls)
        self.assertIn("4 подхода", self.api.screen())

    def test_full_wizard_saves_program(self):
        self.send("/newprogram")
        self.click("w:dc:2")
        self.send("Грудь + трицепс")
        self.click("w:grp:0")
        self.click("w:ex:0:0")
        self.click("w:sets:4")
        self.click("w:more")
        self.click("w:own")
        self.send("Кроссовер лёжа")
        self.click("w:setsx")
        self.send("7")
        self.click("w:dayend")
        self.click("w:nocom")
        self.click("w:grp:1")
        self.click("w:ex:1:0")
        self.click("w:sets:3")
        self.click("w:dayend")

        self.assertIn("Программа сохранена", self.api.screen_or_prev("Программа сохранена"))
        program = self.storage.get_program(self.user_id)
        self.assertEqual(len(program["days"]), 2)
        day1 = program["days"][0]
        self.assertEqual(day1["name"], "Грудь + трицепс")
        self.assertEqual(day1["exercises"][1], {"name": "Кроссовер лёжа", "sets": 7})
        self.assertEqual(program["days"][1]["name"], "День 2")

    def test_custom_exercise_can_be_saved_to_my_list(self):
        self.send("/newprogram")
        self.click("w:dc:1")
        self.click("w:nocom")
        self.click("w:own")
        self.send("Тяга Пендлея")
        self.assertIn("w:savetoggle", self.api.buttons())
        self.assertIn("☆ Сохранить в мои упражнения: нет", self.api.button_labels())

        self.click("w:savetoggle")
        self.assertIn("⭐ Сохранить в мои упражнения: да", self.api.button_labels())
        self.click("w:sets:4")
        self.assertEqual(self.storage.list_user_exercises(self.user_id), ["Тяга Пендлея"])

        # дальше упражнение доступно кнопкой, без набора текста
        self.click("w:more")
        self.assertIn("w:mine", self.api.buttons())
        self.click("w:mine")
        self.click("w:mex:0")
        self.click("w:sets:3")
        self.click("w:dayend")
        exercises = self.storage.get_program(self.user_id)["days"][0]["exercises"]
        self.assertEqual([e["name"] for e in exercises], ["Тяга Пендлея", "Тяга Пендлея"])

    def test_custom_exercise_not_saved_by_default(self):
        self.send("/newprogram")
        self.click("w:dc:1")
        self.click("w:nocom")
        self.click("w:own")
        self.send("Разовое упражнение")
        self.click("w:sets:3")
        self.assertEqual(self.storage.list_user_exercises(self.user_id), [])

    def test_day_name_examples_cover_fullbody(self):
        self.send("/newprogram")
        self.click("w:dc:3")
        for example in ("Грудь + трицепс", "Фулбади", "Понедельник", "День 1"):
            self.assertIn(example, self.api.screen())

    def test_day_cannot_be_empty(self):
        self.send("/newprogram")
        self.click("w:dc:1")
        self.click("w:nocom")
        self.click("w:dayend")
        self.assertEqual(self.api.last_note(), "Добавьте хотя бы одно упражнение")


class EditorTests(BaseBotTest):
    def test_rename_day(self):
        self.make_program(days=1)
        self.send("/edit")
        self.click("e:day:0")
        self.click("e:rename")
        self.send("Понедельник")
        self.assertIn("Понедельник", self.api.screen())
        self.assertEqual(self.storage.get_program(self.user_id)["days"][0]["name"], "Понедельник")

    def test_add_and_delete_exercise(self):
        self.make_program(days=1, exercises_per_day=1)
        self.send("/edit")
        self.click("e:day:0")
        self.click("e:addex")
        self.click("e:grp:2")
        self.click("e:exsel:2:0")
        self.click("e:sets:5")
        exercises = self.storage.get_program(self.user_id)["days"][0]["exercises"]
        self.assertEqual(len(exercises), 2)
        self.assertEqual(exercises[1], {"name": catalog.exercise_name(2, 0), "sets": 5})

        self.click("e:ex:1")
        self.click("e:delex")
        self.assertEqual(len(self.storage.get_program(self.user_id)["days"][0]["exercises"]), 1)

    def test_change_sets_of_existing_exercise(self):
        self.make_program(days=1, exercises_per_day=1)
        self.send("/edit")
        self.click("e:day:0")
        self.click("e:ex:0")
        self.click("e:setsedit")
        self.click("e:sets:8")
        self.assertEqual(
            self.storage.get_program(self.user_id)["days"][0]["exercises"][0]["sets"], 8
        )
        self.assertIn("8 подходов", self.api.screen())

    def test_custom_sets_in_editor(self):
        self.make_program(days=1, exercises_per_day=1)
        self.send("/edit")
        self.click("e:day:0")
        self.click("e:ex:0")
        self.click("e:setsedit")
        self.click("e:setsx")
        self.send("ерунда")
        self.assertIn("Нужно число", self.api.screen())
        self.assertIn("количество подходов числом", self.api.screen())
        self.send("6")
        self.assertEqual(
            self.storage.get_program(self.user_id)["days"][0]["exercises"][0]["sets"], 6
        )

    def test_day_save_button_returns_to_days(self):
        """«К дням» больше нет — день закрывается кнопкой «Сохранить день»."""
        self.make_program(days=2, exercises_per_day=1)
        self.send("/edit")
        self.click("e:day:0")
        self.assertIn("e:daysave", self.api.buttons())
        self.assertNotIn("e:days", self.api.buttons())

        self.click("e:rename")
        self.send("Понедельник")
        self.click("e:daysave")
        self.assertEqual(self.api.last_note(), "День сохранён")
        self.assertIn("Выберите день", self.api.screen())
        self.assertEqual(
            self.storage.get_program(self.user_id)["days"][0]["name"], "Понедельник"
        )

    def test_my_exercises_management(self):
        self.make_program(days=1, exercises_per_day=1)
        self.send("/edit")
        self.assertIn("e:mylist", self.api.buttons())
        self.click("e:mylist")
        self.assertIn("Список пуст", self.api.screen())

        self.click("e:myadd")
        self.send("Кроссовер лёжа на полу")
        self.assertIn("🗑 Кроссовер лёжа на полу", self.api.button_labels())
        self.assertEqual(self.storage.list_user_exercises(self.user_id), ["Кроссовер лёжа на полу"])

        # дубликат не добавляется
        self.click("e:myadd")
        self.send("Кроссовер лёжа на полу")
        self.assertIn("уже есть в списке", self.api.screen())
        self.assertEqual(len(self.storage.list_user_exercises(self.user_id)), 1)

        # удаление
        self.click("e:mydel:0")
        self.assertEqual(self.storage.list_user_exercises(self.user_id), [])

    def test_my_exercise_can_be_picked_in_editor(self):
        self.storage.ensure_user(self.user_id, "tester")
        self.storage.add_user_exercise(self.user_id, "Тяга Пендлея")
        self.make_program(days=1, exercises_per_day=1)
        self.send("/edit")
        self.click("e:day:0")
        self.click("e:addex")
        self.assertIn("e:mine", self.api.buttons())
        self.click("e:mine")
        self.click("e:mex:0")
        self.click("e:sets:4")
        exercises = self.storage.get_program(self.user_id)["days"][0]["exercises"]
        self.assertEqual(exercises[-1], {"name": "Тяга Пендлея", "sets": 4})

    def test_add_and_delete_day_renumbers(self):
        self.make_program(days=2, exercises_per_day=1)
        self.send("/edit")
        self.click("e:adday")
        program = self.storage.get_program(self.user_id)
        self.assertEqual(len(program["days"]), 3)
        self.assertEqual([d["number"] for d in program["days"]], [1, 2, 3])

        self.click("e:days")
        self.click("e:day:0")
        self.click("e:delday")
        program = self.storage.get_program(self.user_id)
        self.assertEqual([d["number"] for d in program["days"]], [1, 2])

    def test_cannot_delete_last_day(self):
        self.make_program(days=1, exercises_per_day=1)
        self.send("/edit")
        self.click("e:day:0")
        self.assertNotIn("e:delday", self.api.buttons())

    def test_editor_finishes_and_clears_session(self):
        self.make_program(days=1, exercises_per_day=1)
        self.send("/edit")
        self.click("e:done")
        self.assertIn("Программа сохранена", self.api.all_text())
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "idle")

    def test_edit_without_program(self):
        self.send("/edit")
        self.assertIn("/newprogram", self.api.screen())


class WorkoutTests(BaseBotTest):
    def start_workout(self, exercises_per_day=2):
        self.make_program(days=1, exercises_per_day=exercises_per_day)
        self.send("/train")
        self.click("t:day:0")

    def test_exercises_can_be_done_in_any_order(self):
        self.start_workout(exercises_per_day=2)
        self.assertIn("Выберите упражнение", self.api.screen())
        # начинаем со второго упражнения
        self.click("t:ex:1")
        self.assertIn(catalog.exercise_name(0, 1), self.api.screen())
        self.assertIn("Подход 1 из 3", self.api.screen())

    def test_weight_then_reps_per_set(self):
        self.start_workout(exercises_per_day=1)
        self.click("t:ex:0")
        self.assertIn("Введите вес", self.api.screen())

        self.send("80")
        screen = self.api.screen()
        self.assertIn("80 кг", screen)
        self.assertIn("Сколько повторений", screen)

        self.send("8")
        screen = self.api.screen()
        self.assertIn("Подход 2 из 3", screen)
        self.assertIn("80 кг x 8", screen)
        # строки про «-» больше нет, вместо неё кнопки
        self.assertNotIn("отправьте «-»", screen)
        self.assertIn("t:wq:80", self.api.buttons())
        self.assertIn("t:wq:0", self.api.buttons())

        # второй подход с другим весом
        self.send("82.5")
        self.send("6")
        self.assertIn("82.5 кг x 6", self.api.screen())

        # третий подход завершает план -> возврат к списку упражнений
        self.send("85")
        self.send("5")
        self.assertIn("Выберите упражнение", self.api.screen())
        self.assertIn("3/3", self.api.screen())

    def test_extra_set_beyond_plan(self):
        """План по подходам выполнен — можно вернуться и добавить ещё один."""
        self.start_workout(exercises_per_day=1)
        self.click("t:ex:0")
        for weight, reps in (("50", "10"), ("50", "10"), ("50", "8")):
            self.send(weight)
            self.send(reps)
        self.assertIn("3/3", self.api.screen())

        self.click("t:ex:0")
        self.assertIn("сверх плана", self.api.screen())
        self.send("45")
        self.send("12")
        self.click("t:back")
        self.assertIn("4/3", self.api.screen())

    def test_bodyweight_input(self):
        self.start_workout(exercises_per_day=1)
        self.click("t:ex:0")
        self.send("-")
        self.assertIn("свой вес", self.api.screen())
        self.send("15")
        self.assertIn("15", self.api.screen())

    def test_invalid_weight_and_reps(self):
        self.start_workout(exercises_per_day=1)
        self.click("t:ex:0")
        screens_before = len(self.api.visible())
        self.send("тяжело")
        self.assertIn("Не понял вес", self.api.screen())
        self.assertIn("Введите вес", self.api.screen())
        self.send("80")
        self.send("много")
        self.assertIn("Не понял повторения", self.api.screen())
        # ошибки не плодят новых сообщений в чате
        self.assertEqual(len(self.api.visible()), screens_before)
        self.send("10")
        self.assertIn("80 кг x 10", self.api.screen())

    def test_skip_and_return_to_list(self):
        self.start_workout(exercises_per_day=2)
        self.click("t:ex:0")
        self.click("t:skip")
        screen = self.api.screen()
        self.assertIn("пропущено", screen)
        self.assertIn("Выберите упражнение", screen)

    def test_partial_exercise_can_be_finished_early(self):
        self.start_workout(exercises_per_day=1)
        self.click("t:ex:0")
        self.send("50")
        self.send("12")
        self.click("t:done")
        self.assertIn("Выберите упражнение", self.api.screen())
        self.assertIn("1/3", self.api.screen())

    def test_summary_has_no_totals_and_offers_next_step(self):
        self.start_workout(exercises_per_day=2)
        self.click("t:ex:0")
        self.send("60")
        self.send("10")
        self.click("t:done")
        self.click("t:ex:1")
        self.click("t:skip")
        self.click("t:finish")

        text = self.api.all_text()
        self.assertIn("завершена", text)
        self.assertIn("60 кг x 10", text)
        self.assertIn("пропущено", text)
        self.assertIn("Время тренировки", text)
        # то, что просили убрать
        self.assertNotIn("тоннаж", text.lower())
        self.assertNotIn("Подходов всего", text)
        self.assertNotIn("повторений всего", text.lower())

        # после сводки — выбор следующего шага
        self.assertIn("Что дальше?", self.api.screen())
        self.assertEqual(self.api.buttons(), ["t:editlast", "t:again", "t:program"])
        self.assertEqual(
            self.api.button_labels(),
            [
                "✏️ Редактировать прошлую тренировку",
                "🏋️ Начать следующую тренировку",
                "📋 Посмотреть программу",
            ],
        )

        history = self.storage.get_history(self.user_id)
        self.assertEqual(len(history), 1)
        # тренировка остаётся в сессии, чтобы её можно было поправить
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "after_workout")

    def test_next_step_buttons_work(self):
        self.start_workout(exercises_per_day=1)
        self.click("t:finish")
        self.click("t:again")
        self.assertIn("Выберите день", self.api.screen())

    def test_summary_contains_date(self):
        from datetime import datetime, timedelta, timezone
        from config import TZ_OFFSET_HOURS
        from program import MONTHS

        self.start_workout(exercises_per_day=1)
        self.click("t:ex:0")
        self.send("60")
        self.send("10")
        self.click("t:finish")

        now = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)
        expected = f"{now.day} {MONTHS[now.month - 1]} {now.year}"
        self.assertIn(expected, self.api.all_text())

    def test_edit_last_workout_after_summary(self):
        self.start_workout(exercises_per_day=2)
        self.click("t:ex:0")
        self.send("60")
        self.send("10")
        self.click("t:done")
        self.click("t:finish")

        # возвращаемся в ту же тренировку и дописываем второе упражнение
        self.click("t:editlast")
        self.assertIn("Выберите упражнение", self.api.screen())
        self.assertIn("60 кг x 10", self.api.screen())
        self.click("t:ex:1")
        self.send("30")
        self.send("12")
        self.click("t:done")
        self.click("t:finish")

        # тренировка одна и та же, а не новая запись в истории
        history = self.storage.get_history(self.user_id)
        self.assertEqual(len(history), 1)
        self.assertIn("30 кг x 12", self.api.all_text())

    def test_quick_weight_button(self):
        self.start_workout(exercises_per_day=1)
        self.click("t:ex:0")
        self.click("t:wq:0")           # «Без веса»
        self.assertIn("свой вес", self.api.screen())
        self.send("12")
        self.click("t:ex:0")
        self.click("t:wq:60")          # быстрый выбор веса кнопкой
        self.assertIn("60 кг", self.api.screen())
        self.send("8")
        self.assertIn("60 кг x 8", self.api.screen())

    def test_edit_button_only_on_program_view(self):
        self.start_workout(exercises_per_day=1)
        self.click("t:finish")
        self.assertNotIn("t:edit", self.api.buttons())   # экран «Что дальше?»
        self.click("t:program")
        self.assertIn("t:edit", self.api.buttons())      # а вот вместе с программой — да

    def test_train_without_program(self):
        self.send("/train")
        self.assertIn("/newprogram", self.api.screen())

    def test_stale_workout_callback_updates_the_screen(self):
        """После перезапуска бота сессия теряется. Кнопка не должна выглядеть
        сломанной: экран обновляется, а не просто мигает подсказка."""
        self.make_program(days=1, exercises_per_day=1)
        self.send("/train")
        self.click("t:day:0")
        self.storage.clear_session(self.user_id)     # как будто бот перезапустился

        self.click("t:finish")
        self.assertEqual(self.api.last_note(), "Тренировка больше не активна")
        self.assertIn("больше не активна", self.api.screen())
        self.assertIn("/train", self.api.screen())

    def test_weight_screen_has_no_guessed_options(self):
        """Оставили только повтор прошлого веса — угадывать соседние веса лишнее."""
        self.make_program(days=1, exercises_per_day=1)
        self.send("/train")
        self.click("t:day:0")
        self.click("t:ex:0")
        self.send("60")
        self.send("10")
        buttons = self.api.buttons()
        self.assertIn("t:wq:60", buttons)
        self.assertIn("t:wq:0", buttons)
        self.assertEqual([b for b in buttons if b.startswith("t:wq:")], ["t:wq:60", "t:wq:0"])

    def test_broken_handler_is_reported_not_silent(self):
        """Любой сбой должен быть виден: молчащая кнопка выглядит как поломка."""
        def explode(*args, **kwargs):
            raise RuntimeError("что-то сломалось внутри")

        self.make_program(days=1, exercises_per_day=1)
        self.send("/train")
        self.click("t:day:0")
        self.bot._finish_workout = explode

        self.click("t:finish")
        self.assertEqual(self.api.last_note(), "Не получилось, попробуйте ещё раз")
        self.assertIn("Что-то пошло не так", self.api.screen())


def _screen_or_prev(self, needle):
    """Ищет текст среди видимых сообщений (сводка/итог приходят отдельно)."""
    for message in reversed(self.visible()):
        if needle in message["text"]:
            return message["text"]
    return self.screen()


FakeAPI.screen_or_prev = _screen_or_prev


PROGRAM_TEXT = """Понедельник - грудь и трицепс
жим лежа 4х10
жим гантелей на наклонной 3х12
французский жим 12,12,10
планка

Четверг: спина
подтягивания 4 подхода
тяга верхнего блока 4х12
тяга гантели 3 по 12 (24 кг)"""


class FreeformParserTests(unittest.TestCase):
    """Разбор программы из текста в свободной форме — без бота, чистая логика."""

    def parse(self, text):
        program, warnings = freeform.parse_freeform(text)
        return program, warnings

    def names(self, program, day=0):
        return [e["name"] for e in program["days"][day]["exercises"]]

    def sets(self, program, day=0):
        return [e["sets"] for e in program["days"][day]["exercises"]]

    def test_weekday_headers_and_cross_notation(self):
        program, _ = self.parse(PROGRAM_TEXT)
        self.assertEqual(len(program["days"]), 2)
        self.assertEqual(program["days"][0]["name"], "Понедельник — грудь и трицепс")
        self.assertEqual(program["days"][1]["name"], "Четверг — спина")
        self.assertEqual(self.sets(program, 0), [4, 3, 3, 3])   # 12,12,10 -> 3 подхода
        self.assertEqual(self.sets(program, 1), [4, 4, 3])      # «4 подхода», «3 по 12»

    def test_numbered_list_and_weights_are_stripped(self):
        program, _ = self.parse(
            "День 1\n"
            "1. Приседания со штангой 5х5\n"
            "2) Жим ногами 4x12\n"
            "3 - Выпады 3х10 (20 кг)\n"
            "Румынская тяга @80 4х10"
        )
        self.assertEqual(
            self.names(program),
            ["Приседания со штангой", "Жим ногами", "Выпады", "Румынская тяга"],
        )
        self.assertEqual(self.sets(program), [5, 4, 3, 4])

    def test_sets_written_as_words(self):
        program, _ = self.parse(
            "Тренировка 1\n"
            "Жим штанги лёжа — 4 подхода по 8 повторений\n"
            "Тяга 3 сета\n"
            "Подтягивания 5 подходов"
        )
        self.assertEqual(self.names(program), ["Жим штанги лёжа", "Тяга", "Подтягивания"])
        self.assertEqual(self.sets(program), [4, 3, 5])

    def test_exercise_without_numbers_gets_default_and_warning(self):
        program, warnings = self.parse("День 1\nЖим лежа 4х10\nПланка\nПресс 3х20")
        self.assertIn("Планка", self.names(program))
        self.assertEqual(self.sets(program), [4, freeform.DEFAULT_SETS, 3])
        self.assertTrue(any("Планка" in w for w in warnings))

    def test_program_without_day_headers_becomes_one_day(self):
        program, _ = self.parse("жим лежа 4х10\nтяга штанги 4х10\nприседания 4х10")
        self.assertEqual(len(program["days"]), 1)
        self.assertEqual(program["days"][0]["name"], "День 1")

    def test_english_program(self):
        program, _ = self.parse("Day 1 - Push\nBench press 4x8\nOverhead press 3x10")
        self.assertEqual(program["days"][0]["name"], "Push")
        self.assertEqual(self.sets(program), [4, 3])

    def test_noise_lines_ignored(self):
        program, _ = self.parse("Программа тренировок\nНеделя 1\nДень 1\nЖим 4х10\nТяга 4х10")
        self.assertEqual(len(program["days"]), 1)
        self.assertEqual(len(program["days"][0]["exercises"]), 2)

    def test_garbage_is_rejected(self):
        with self.assertRaises(freeform.FreeformError):
            self.parse("привет как дела")
        with self.assertRaises(freeform.FreeformError):
            self.parse("")

    def test_too_many_days_are_trimmed(self):
        text = "\n\n".join(f"День {i}\nЖим {i}х10" for i in range(1, 10))
        program, warnings = self.parse(text)
        self.assertEqual(len(program["days"]), 7)
        self.assertTrue(any("Дней больше" in w for w in warnings))

    def test_looks_like_program_is_conservative(self):
        self.assertTrue(freeform.looks_like_program(PROGRAM_TEXT))
        self.assertFalse(freeform.looks_like_program("привет"))
        self.assertFalse(freeform.looks_like_program("спасибо\nвсё понятно\nдо встречи"))
        self.assertFalse(freeform.looks_like_program("Жим лежа 4х10"))


class ImportFlowTests(BaseBotTest):
    def test_pasted_program_is_offered_and_saved(self):
        """Человек просто вставил программу в чат, ничего не нажимая."""
        self.send(PROGRAM_TEXT)
        screen = self.api.screen()
        self.assertIn("Похоже на программу тренировок", screen)
        self.assertIn("жим лежа", screen)
        self.assertIn("Итого: 2 дня, 7 упражнений", screen)
        self.assertIsNone(self.storage.get_program(self.user_id))  # пока не сохранено

        self.click("i:save")
        program = self.storage.get_program(self.user_id)
        self.assertEqual(len(program["days"]), 2)
        self.assertEqual(program["days"][0]["exercises"][0]["sets"], 4)
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "idle")

    def test_import_command(self):
        self.send("/import")
        self.assertIn("Пришлите программу одним сообщением", self.api.screen())
        self.send(PROGRAM_TEXT)
        self.assertIn("Вот что я понял", self.api.screen())
        self.click("i:save")
        self.assertIn("Программа сохранена", self.api.screen())

    def test_import_from_wizard_button(self):
        self.send("/newprogram")
        self.assertIn("w:paste", self.api.buttons())
        self.click("w:paste")
        self.assertIn("Пришлите программу", self.api.screen())
        self.send(PROGRAM_TEXT)
        self.click("i:save")
        self.assertEqual(len(self.storage.get_program(self.user_id)["days"]), 2)

    def test_import_can_go_back_to_step_by_step(self):
        self.send("/newprogram")
        self.click("w:paste")
        self.click("w:steps")
        self.assertIn("Сколько тренировочных дней", self.api.screen())

    def test_unparseable_text_shows_error_and_keeps_asking(self):
        self.send("/import")
        self.send("привет, как дела")
        screen = self.api.screen()
        self.assertIn("Не нашёл в тексте ни одного упражнения", screen)
        self.assertIsNone(self.storage.get_program(self.user_id))
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "import")
        # можно сразу прислать правильный текст
        self.send(PROGRAM_TEXT)
        self.click("i:save")
        self.assertIsNotNone(self.storage.get_program(self.user_id))

    def test_import_save_and_edit_opens_editor(self):
        self.send("/import")
        self.send(PROGRAM_TEXT)
        self.click("i:edit")
        self.assertIn("Выберите день", self.api.screen())
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "editor")
        self.assertIsNotNone(self.storage.get_program(self.user_id))

    def test_import_cancel_keeps_old_program(self):
        self.make_program(days=1, exercises_per_day=1)
        before = self.storage.get_program(self.user_id)
        self.send("/import")
        self.send(PROGRAM_TEXT)
        self.click("i:cancel")
        self.assertEqual(self.storage.get_program(self.user_id), before)
        self.assertIn("отменена", self.api.screen())

    def test_imported_program_is_trainable(self):
        self.send(PROGRAM_TEXT)
        self.click("i:save")
        self.send("/train")
        # названия дней сохранились и видны на кнопках выбора дня
        self.assertEqual(
            self.api.button_labels(),
            ["Понедельник — грудь и трицепс", "Четверг — спина"],
        )
        self.click("t:day:0")
        self.click("t:ex:0")
        self.send("60")
        self.send("10")
        self.click("t:finish")
        self.assertIn("60 кг x 10", self.api.all_text())

    def test_ordinary_message_is_not_treated_as_program(self):
        self.send("спасибо, всё понятно")
        self.assertIn("Список команд", self.api.screen())
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "idle")


class LLMParserTests(unittest.TestCase):
    """Разбор ответа модели: доверять ему на слово нельзя."""

    def test_plain_json(self):
        answer = '{"days": [{"name": "Понедельник", "exercises": [{"name": "Жим", "sets": 4}]}]}'
        program = llm._normalize(llm._extract_json(answer))
        self.assertEqual(program["days"][0]["name"], "Понедельник")
        self.assertEqual(program["days"][0]["exercises"], [{"name": "Жим", "sets": 4}])

    def test_json_in_code_fence_and_with_chatter(self):
        answer = 'Вот результат:\n```json\n{"days": [{"name": "", "exercises": ' \
                 '[{"name": "Тяга", "sets": 3}]}]}\n```'
        program = llm._normalize(llm._extract_json(answer))
        self.assertEqual(program["days"][0]["name"], "День 1")
        self.assertEqual(program["days"][0]["exercises"][0]["name"], "Тяга")

    def test_invalid_values_are_clamped_and_cleaned(self):
        answer = ('{"days": [{"name": "  Очень длинное название дня  ", "exercises": ['
                  '{"name": "Жим", "sets": 999},'
                  '{"name": "Тяга", "sets": "не число"},'
                  '{"name": "   ", "sets": 3},'
                  '{"sets": 3},'
                  '"мусор"]}]}')
        program = llm._normalize(llm._extract_json(answer))
        exercises = program["days"][0]["exercises"]
        self.assertEqual([e["name"] for e in exercises], ["Жим", "Тяга"])
        self.assertEqual(exercises[0]["sets"], 20)                    # обрезано до максимума
        self.assertEqual(exercises[1]["sets"], llm.DEFAULT_SETS)      # не число -> по умолчанию

    def test_empty_or_broken_answers_raise(self):
        for answer in ('{"days": []}', '{"days": "не список"}', "совсем не json", ""):
            with self.assertRaises(llm.LLMUnavailable):
                llm._normalize(llm._extract_json(answer))

    def test_too_many_days_are_cut(self):
        days = ",".join(
            f'{{"name": "Д{i}", "exercises": [{{"name": "Жим", "sets": 3}}]}}' for i in range(12)
        )
        program = llm._normalize(llm._extract_json('{"days": [' + days + ']}'))
        self.assertEqual(len(program["days"]), 7)
        self.assertEqual([d["number"] for d in program["days"]], [1, 2, 3, 4, 5, 6, 7])

    def test_disabled_without_key(self):
        self.assertFalse(llm.is_enabled(""))
        self.assertFalse(llm.is_enabled(None))
        self.assertTrue(llm.is_enabled("sk-ant-..."))
        with self.assertRaises(llm.LLMUnavailable):
            llm.parse_with_llm("текст", api_key="", model="m")


class LLMFallbackTests(BaseBotTest):
    """AI подключается только тогда, когда эвристики не справились."""

    FREE_TEXT = (
        "в понедельник тренирую грудь\n"
        "жму штангу четыре раза по десять\n"
        "потом развожу гантели три подхода"
    )

    def setUp(self):
        super().setUp()
        self.llm_calls = []

        def fake_llm(text):
            self.llm_calls.append(text)
            return {
                "name": "Моя программа",
                "days": [{
                    "number": 1,
                    "name": "Понедельник — грудь",
                    "exercises": [
                        {"name": "Жим штанги лёжа", "sets": 4},
                        {"name": "Разводка гантелей", "sets": 3},
                    ],
                }],
            }

        self.bot.llm_parse = fake_llm

    def test_llm_used_when_heuristics_fail(self):
        self.send("/import")
        self.send(self.FREE_TEXT)
        self.assertEqual(len(self.llm_calls), 1)
        screen = self.api.screen()
        self.assertIn("Жим штанги лёжа", screen)
        self.assertIn("разобрал с помощью AI", screen)

        self.click("i:save")
        program = self.storage.get_program(self.user_id)
        self.assertEqual(program["days"][0]["name"], "Понедельник — грудь")

    def test_llm_not_called_when_heuristics_are_confident(self):
        self.send("/import")
        self.send(PROGRAM_TEXT)
        self.assertEqual(self.llm_calls, [])
        self.assertNotIn("с помощью AI", self.api.screen())

    def test_llm_not_called_on_spontaneous_paste(self):
        """Случайные сообщения не должны тратить платные запросы."""
        self.send(PROGRAM_TEXT)
        self.assertEqual(self.llm_calls, [])

    def test_llm_failure_falls_back_to_heuristics_error(self):
        def broken_llm(text):
            raise llm.LLMUnavailable("кончились кредиты")

        self.bot.llm_parse = broken_llm
        self.send("/import")
        self.send(self.FREE_TEXT)
        self.assertIn("Не нашёл в тексте ни одного упражнения", self.api.screen())
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "import")

    def test_any_llm_exception_is_survived(self):
        def exploding_llm(text):
            raise RuntimeError("что угодно")

        self.bot.llm_parse = exploding_llm
        self.send("/import")
        self.send(PROGRAM_TEXT)          # эвристики уверенные — AI и не понадобится
        self.click("i:save")
        self.assertIsNotNone(self.storage.get_program(self.user_id))

    def test_bot_works_without_llm_at_all(self):
        self.bot.llm_parse = None
        self.send("/import")
        self.send(PROGRAM_TEXT)
        self.click("i:save")
        self.assertEqual(len(self.storage.get_program(self.user_id)["days"]), 2)


class WelcomeTests(BaseBotTest):
    def test_start_explains_what_the_bot_is(self):
        self.send("/start")
        screen = self.api.screen()
        for fragment in ("дневник тренировок", "Как это работает", "Кому подойдёт",
                         "Чего бот не делает"):
            self.assertIn(fragment, screen)
        self.assertEqual(self.api.buttons(), ["s:new", "s:import", "s:help"])

    def test_start_buttons_lead_where_promised(self):
        self.send("/start")
        self.click("s:new")
        self.assertIn("Сколько тренировочных дней", self.api.screen())

        self.send("/start")
        self.click("s:import")
        self.assertIn("Пришлите программу одним сообщением", self.api.screen())

        self.send("/start")
        self.click("s:help")
        self.assertIn("/train", self.api.screen())


class ChangelogTests(BaseBotTest):
    def test_new_user_sees_no_changelog(self):
        self.send("/start")
        self.assertNotIn("Что нового", self.api.all_text())
        self.assertEqual(
            self.storage.get_seen_version(self.user_id), changelog.VERSION
        )

    def test_existing_user_sees_updates_once(self):
        self.send("/start")                                   # пользователь заведён
        self.storage.set_seen_version(self.user_id, "1.2.0")  # как будто бот обновился

        self.send("/program")
        text = self.api.all_text()
        self.assertIn("Что нового", text)
        self.assertIn("1.4.0", text)
        self.assertIn("1.3.0", text)
        self.assertNotIn("1.2.0", text)   # это он уже видел
        self.assertEqual(self.storage.get_seen_version(self.user_id), changelog.VERSION)

        # второй раз показывать не нужно
        before = len(self.api.visible())
        self.send("/program")
        new_texts = [m["text"] for m in self.api.visible()[before:]]
        self.assertFalse(any("Что нового" in t for t in new_texts))

    def test_update_is_not_shown_in_the_middle_of_a_workout(self):
        self.make_program(days=1, exercises_per_day=1)
        self.send("/train")
        self.click("t:day:0")
        self.storage.set_seen_version(self.user_id, "1.0.0")   # бот обновился на ходу

        self.click("t:ex:0")
        self.send("60")
        self.assertNotIn("Что нового", self.api.all_text())
        # версия не «съедена»: как только тренировка закончится, обновление покажется
        self.assertEqual(self.storage.get_seen_version(self.user_id), "1.0.0")

        self.send("10")
        self.click("t:finish")
        self.storage.clear_session(self.user_id)
        self.send("/program")
        self.assertIn("Что нового", self.api.all_text())

    def test_user_from_before_the_feature_sees_only_the_latest(self):
        """У старых пользователей версия не записана — всю историю не вываливаем."""
        self.storage.ensure_user(self.user_id, "tester")
        self.assertIsNone(self.storage.get_seen_version(self.user_id))
        self.send("/program")
        text = self.api.all_text()
        self.assertIn(changelog.VERSION, text)
        self.assertNotIn("1.1.0", text)

    def test_whatsnew_command(self):
        self.send("/start")
        self.send("/whatsnew")
        self.assertIn("Что нового в боте", self.api.screen())
        self.assertIn(changelog.VERSION, self.api.screen())

    def test_versions_are_ordered_and_valid(self):
        versions = [changelog._as_tuple(r["version"]) for r in changelog.RELEASES]
        self.assertEqual(versions, sorted(versions, reverse=True))
        self.assertEqual(changelog.RELEASES[0]["version"], changelog.VERSION)
        for release in changelog.RELEASES:
            self.assertTrue(release["title"] and release["changes"])

    def test_releases_since_comparison(self):
        self.assertEqual(changelog.releases_since(changelog.VERSION), [])
        # с предпоследней версии видно ровно одно обновление — последнее
        previous = changelog.RELEASES[1]["version"]
        self.assertEqual(changelog.releases_since(previous), changelog.RELEASES[:1])
        self.assertEqual(len(changelog.releases_since("0.0.1")), len(changelog.RELEASES))
        self.assertEqual(changelog.releases_since("что-то странное"),
                         changelog.RELEASES)   # битую версию считаем самой старой


class MultiUserTests(unittest.TestCase):
    """Бот общий для всех, поэтому проверяем, что данные пользователей
    не пересекаются даже при вперемешку идущих действиях."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)
        self.api = FakeAPI()
        self.bot = TrainerBot(self.api, self.storage)
        self.msg_id = 0

    def tearDown(self):
        os.unlink(self.tmp.name)

    def send(self, user_id, text, chat_type="private"):
        self.msg_id += 1
        self.bot.handle_update({
            "message": {
                "message_id": 5000 + self.msg_id,
                "chat": {"id": user_id, "type": chat_type},
                "from": {"id": user_id, "username": f"u{user_id}"},
                "text": text,
            }
        })

    def click(self, user_id, data):
        message_id = None
        for mid in reversed(self.api.order):
            message = self.api.messages[mid]
            if not message["deleted"] and message["markup"] and message["chat_id"] == user_id:
                message_id = mid
                break
        self.bot.handle_update({
            "callback_query": {
                "id": "cbq",
                "from": {"id": user_id, "username": f"u{user_id}"},
                "message": {"message_id": message_id, "chat": {"id": user_id}},
                "data": data,
            }
        })

    def chat_texts(self, user_id):
        return [m["text"] for m in self.api.visible() if m["chat_id"] == user_id]

    def test_two_users_build_programs_simultaneously(self):
        anna, boris = 101, 202
        self.send(anna, "/newprogram")
        self.send(boris, "/newprogram")
        self.click(anna, "w:dc:1")
        self.click(boris, "w:dc:2")
        self.send(anna, "Грудь")
        self.send(boris, "Ноги")
        self.click(anna, "w:grp:0")
        self.click(boris, "w:grp:2")
        self.click(anna, "w:ex:0:0")
        self.click(boris, "w:ex:2:0")
        self.click(anna, "w:sets:4")
        self.click(boris, "w:sets:5")
        self.click(anna, "w:dayend")
        self.click(boris, "w:dayend")
        self.click(boris, "w:nocom")
        self.click(boris, "w:grp:1")
        self.click(boris, "w:ex:1:0")
        self.click(boris, "w:sets:6")
        self.click(boris, "w:dayend")

        anna_program = self.storage.get_program(anna)
        boris_program = self.storage.get_program(boris)
        self.assertEqual(len(anna_program["days"]), 1)
        self.assertEqual(len(boris_program["days"]), 2)
        self.assertEqual(anna_program["days"][0]["name"], "Грудь")
        self.assertEqual(boris_program["days"][0]["name"], "Ноги")
        self.assertEqual(anna_program["days"][0]["exercises"][0]["sets"], 4)
        self.assertEqual(boris_program["days"][0]["exercises"][0]["sets"], 5)

    def test_two_users_train_simultaneously(self):
        anna, boris = 101, 202
        for user_id, sets in ((anna, 3), (boris, 3)):
            self.send(user_id, "/newprogram")
            self.click(user_id, "w:dc:1")
            self.click(user_id, "w:nocom")
            self.click(user_id, "w:grp:0")
            self.click(user_id, "w:ex:0:0")
            self.click(user_id, f"w:sets:{sets}")
            self.click(user_id, "w:dayend")

        self.send(anna, "/train")
        self.send(boris, "/train")
        self.click(anna, "t:day:0")
        self.click(boris, "t:day:0")
        self.click(anna, "t:ex:0")
        self.click(boris, "t:ex:0")
        self.send(anna, "80")
        self.send(boris, "40")
        self.send(anna, "8")
        self.send(boris, "15")
        self.click(anna, "t:finish")
        self.click(boris, "t:finish")

        # у каждого своя история и свои веса, ничего не перетекло в чужой чат
        self.assertEqual(len(self.storage.get_history(anna)), 1)
        self.assertEqual(len(self.storage.get_history(boris)), 1)
        anna_chat = "\n".join(self.chat_texts(anna))
        boris_chat = "\n".join(self.chat_texts(boris))
        self.assertIn("80 кг x 8", anna_chat)
        self.assertNotIn("40 кг", anna_chat)
        self.assertIn("40 кг x 15", boris_chat)
        self.assertNotIn("80 кг", boris_chat)

    def test_personal_exercise_lists_are_isolated(self):
        anna, boris = 101, 202
        self.storage.ensure_user(anna, "anna")
        self.storage.ensure_user(boris, "boris")
        self.storage.add_user_exercise(anna, "Тяга Пендлея")
        self.assertEqual(self.storage.list_user_exercises(anna), ["Тяга Пендлея"])
        self.assertEqual(self.storage.list_user_exercises(boris), [])

        # у Бориса кнопки личного списка вообще нет
        self.send(boris, "/newprogram")
        self.click(boris, "w:dc:1")
        self.click(boris, "w:nocom")
        self.assertNotIn("w:mine", [
            b["callback_data"]
            for row in self.api.visible()[-1]["markup"]["inline_keyboard"]
            for b in row
        ])

    def test_finished_workout_status_is_saved_correctly(self):
        """Завершение прямо с экрана ввода веса не должно терять статус."""
        user_id = 303
        self.send(user_id, "/newprogram")
        self.click(user_id, "w:dc:1")
        self.click(user_id, "w:nocom")
        self.click(user_id, "w:grp:0")
        self.click(user_id, "w:ex:0:0")
        self.click(user_id, "w:sets:3")
        self.click(user_id, "w:dayend")

        self.send(user_id, "/train")
        self.click(user_id, "t:day:0")
        self.click(user_id, "t:ex:0")
        self.send(user_id, "70")
        self.send(user_id, "12")
        self.click(user_id, "t:finish")   # не нажимая «упражнение выполнено»

        saved = json.loads(self.storage.get_history(user_id)[0]["data"])
        self.assertEqual(saved[0]["status"], "done")
        self.assertEqual(saved[0]["sets"], [[70.0, 12]])

    def test_group_chat_is_refused(self):
        """В группе экран был бы один на всех — бот работает только в личке."""
        self.send(999, "/train", chat_type="supergroup")
        self.assertIn("только в личных сообщениях", self.api.screen())
        self.assertEqual(self.storage.get_session(999)["state"], "idle")


if __name__ == "__main__":
    unittest.main()
