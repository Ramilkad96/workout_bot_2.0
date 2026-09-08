# -*- coding: utf-8 -*-
"""
Офлайн-тесты: сеть и Telegram не нужны. Фейковый API умеет отправлять,
редактировать и удалять сообщения, поэтому тесты проверяют не только логику,
но и то, что чат не засоряется лишними сообщениями.

Запуск: python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catalog
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
                "chat": {"id": self.user_id},
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


class MenuTests(unittest.TestCase):
    def test_commands_cover_main_actions(self):
        names = [c for c, _ in COMMANDS]
        for expected in ("train", "program", "edit", "newprogram", "help", "cancel"):
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

    def test_move_exercise_up_and_down(self):
        self.make_program(days=1, exercises_per_day=2)
        first, second = [
            ex["name"] for ex in self.storage.get_program(self.user_id)["days"][0]["exercises"]
        ]
        self.send("/edit")
        self.click("e:day:0")
        self.click("e:ex:1")
        self.click("e:up")
        names = [ex["name"] for ex in self.storage.get_program(self.user_id)["days"][0]["exercises"]]
        self.assertEqual(names, [second, first])
        self.click("e:up")
        self.assertEqual(self.api.last_note(), "Дальше двигать некуда")

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
        self.assertIn("прошлый подход — 80", screen)

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
        self.assertEqual(self.api.buttons(), ["t:again", "t:program", "t:edit"])

        history = self.storage.get_history(self.user_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "idle")

    def test_next_step_buttons_work(self):
        self.start_workout(exercises_per_day=1)
        self.click("t:finish")
        self.click("t:again")
        self.assertIn("Выберите день", self.api.screen())

    def test_train_without_program(self):
        self.send("/train")
        self.assertIn("/newprogram", self.api.screen())

    def test_stale_workout_callback(self):
        self.click("t:ex:0")
        self.assertEqual(self.api.last_note(), "Тренировка уже завершена. Начать новую — /train")


def _screen_or_prev(self, needle):
    """Ищет текст среди видимых сообщений (сводка/итог приходят отдельно)."""
    for message in reversed(self.visible()):
        if needle in message["text"]:
            return message["text"]
    return self.screen()


FakeAPI.screen_or_prev = _screen_or_prev


if __name__ == "__main__":
    unittest.main()
