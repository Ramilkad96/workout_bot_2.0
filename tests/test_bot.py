# -*- coding: utf-8 -*-
"""
Офлайн-тесты: сеть и Telegram не нужны. Прогоняют весь пошаговый мастер
создания программы и тренировку целиком через фейковый Telegram API.
Запуск: python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import TrainerBot
from program import plural_sets, plural_days, format_program
from storage import Storage
import catalog


class FakeAPI:
    def __init__(self):
        self.sent = []            # (chat_id, text, reply_markup)
        self.callback_notes = []  # (query_id, text)

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_query_id, text=None):
        self.callback_notes.append((callback_query_id, text))

    def last_text(self):
        return self.sent[-1][1]

    def last_markup(self):
        return self.sent[-1][2]

    def last_note(self):
        return self.callback_notes[-1][1] if self.callback_notes else None

    def buttons_data(self):
        """Плоский список callback_data последней клавиатуры."""
        markup = self.last_markup()
        if not markup:
            return []
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def make_message(user_id, text, chat_id=None):
    return {
        "message": {
            "chat": {"id": chat_id or user_id},
            "from": {"id": user_id, "username": "tester"},
            "text": text,
        }
    }


def make_callback(user_id, data, chat_id=None, query_id="cbq"):
    return {
        "callback_query": {
            "id": query_id,
            "from": {"id": user_id, "username": "tester"},
            "message": {"chat": {"id": chat_id or user_id}},
            "data": data,
        }
    }


class BaseBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)
        self.api = FakeAPI()
        self.bot = TrainerBot(self.api, self.storage)
        self.user_id = 42

    def tearDown(self):
        os.unlink(self.tmp.name)

    def send(self, text):
        self.bot.handle_update(make_message(self.user_id, text))

    def click(self, data):
        self.bot.handle_update(make_callback(self.user_id, data))

    def build_simple_program(self, days=1):
        """Быстрый прогон мастера: N дней, по одному упражнению из каталога."""
        self.send("/newprogram")
        self.click(f"w:dc:{days}")
        for _ in range(days):
            self.click("w:nocom")
            self.click("w:grp:0")
            self.click("w:ex:0:0")
            self.click("w:sets:4")
            self.click("w:dayend")


class PluralTests(unittest.TestCase):
    def test_sets_plural(self):
        self.assertEqual(plural_sets(1), "1 подход")
        self.assertEqual(plural_sets(3), "3 подхода")
        self.assertEqual(plural_sets(5), "5 подходов")
        self.assertEqual(plural_sets(11), "11 подходов")
        self.assertEqual(plural_sets(21), "21 подход")

    def test_days_plural(self):
        self.assertEqual(plural_days(1), "1 день")
        self.assertEqual(plural_days(2), "2 дня")
        self.assertEqual(plural_days(5), "5 дней")


class CatalogTests(unittest.TestCase):
    def test_catalog_not_empty_and_indexable(self):
        self.assertGreater(len(catalog.group_names()), 3)
        for gi, name in enumerate(catalog.group_names()):
            self.assertTrue(catalog.exercises_of(gi))
            self.assertTrue(catalog.is_valid(gi, 0))
        self.assertFalse(catalog.is_valid(999))
        self.assertFalse(catalog.is_valid(0, 999))

    def test_callback_data_fits_telegram_limit(self):
        """callback_data не должен превышать 64 байта."""
        for gi in range(len(catalog.group_names())):
            for ei in range(len(catalog.exercises_of(gi))):
                data = f"w:ex:{gi}:{ei}"
                self.assertLessEqual(len(data.encode("utf-8")), 64)


class WizardTests(BaseBotTest):
    def test_full_wizard_two_days(self):
        self.send("/newprogram")
        self.assertIn("Сколько тренировочных дней", self.api.last_text())

        # 2 дня в неделю
        self.click("w:dc:2")
        self.assertIn("День 1 из 2", self.api.last_text())

        # комментарий к первому дню
        self.send("Грудь + трицепс")
        self.assertIn("Выберите группу мышц", self.api.last_text())

        # упражнение из каталога: группа "Грудь" -> первое упражнение
        self.click("w:grp:0")
        self.assertIn(catalog.group_name(0), self.api.last_text())
        self.click("w:ex:0:0")
        self.assertIn("Сколько подходов", self.api.last_text())
        self.click("w:sets:4")
        menu = self.api.last_text()
        self.assertIn(catalog.exercise_name(0, 0), menu)
        self.assertIn("4 подхода", menu)

        # второе упражнение — своим названием, своё количество подходов
        self.click("w:more")
        self.click("w:own")
        self.assertIn("Напишите название", self.api.last_text())
        self.send("Жим одной рукой на тренажёре Смита")
        self.click("w:setsx")
        self.send("7")
        self.assertIn("7 подходов", self.api.last_text())

        # день 1 готов -> переходим ко дню 2
        self.click("w:dayend")
        self.assertIn("День 2 из 2", self.api.last_text())

        # у второго дня комментарий пропускаем
        self.click("w:nocom")
        self.click("w:grp:1")
        self.click("w:ex:1:0")
        self.click("w:sets:3")
        # последний день -> кнопка завершения
        self.assertIn("w:dayend", self.api.buttons_data())
        self.click("w:dayend")

        self.assertIn("Программа сохранена", self.api.last_text())

        program = self.storage.get_program(self.user_id)
        self.assertIsNotNone(program)
        self.assertEqual(len(program["days"]), 2)

        day1, day2 = program["days"]
        self.assertEqual(day1["name"], "Грудь + трицепс")
        self.assertEqual(day1["number"], 1)
        self.assertEqual(len(day1["exercises"]), 2)
        self.assertEqual(day1["exercises"][0]["name"], catalog.exercise_name(0, 0))
        self.assertEqual(day1["exercises"][0]["sets"], 4)
        self.assertEqual(day1["exercises"][1]["name"], "Жим одной рукой на тренажёре Смита")
        self.assertEqual(day1["exercises"][1]["sets"], 7)

        # у пропущенного комментария — дефолтное имя
        self.assertEqual(day2["name"], "День 2")
        self.assertEqual(day2["exercises"][0]["sets"], 3)

        # сессия очищена
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "idle")

    def test_cannot_finish_day_without_exercises(self):
        self.send("/newprogram")
        self.click("w:dc:1")
        self.click("w:nocom")
        # кнопки "День готов" на экране выбора группы нет, пока нет упражнений
        self.assertNotIn("w:dayend", self.api.buttons_data())
        # но даже прямой вызов не должен завершать день
        self.click("w:dayend")
        self.assertEqual(self.api.last_note(), "Добавьте хотя бы одно упражнение")
        self.assertIsNone(self.storage.get_program(self.user_id))

    def test_undo_removes_last_exercise(self):
        self.send("/newprogram")
        self.click("w:dc:1")
        self.click("w:nocom")
        self.click("w:grp:0")
        self.click("w:ex:0:0")
        self.click("w:sets:4")
        self.click("w:more")
        self.click("w:grp:0")
        self.click("w:ex:0:1")
        self.click("w:sets:3")
        self.assertIn(catalog.exercise_name(0, 1), self.api.last_text())

        self.click("w:undo")
        menu = self.api.last_text()
        self.assertIn(catalog.exercise_name(0, 0), menu)
        self.assertNotIn(catalog.exercise_name(0, 1), menu)

        # ещё раз убрать последнее -> список пуст, дальше удалять нечего
        self.click("w:undo")
        self.click("w:undo")
        self.assertEqual(self.api.last_note(), "Удалять нечего")

    def test_custom_sets_validation(self):
        self.send("/newprogram")
        self.click("w:dc:1")
        self.click("w:nocom")
        self.click("w:grp:0")
        self.click("w:ex:0:0")
        self.click("w:setsx")
        self.send("много")
        self.assertIn("Нужно число", self.api.last_text())
        self.send("999")
        self.assertIn("Нужно число", self.api.last_text())
        self.send("5")
        self.assertIn("5 подходов", self.api.last_text())

    def test_back_to_groups(self):
        self.send("/newprogram")
        self.click("w:dc:1")
        self.click("w:nocom")
        self.click("w:grp:2")
        self.click("w:groups")
        self.assertIn("Выберите группу мышц", self.api.last_text())

    def test_cancel_drops_wizard(self):
        self.send("/newprogram")
        self.click("w:dc:3")
        self.send("/cancel")
        self.assertIn("отменено", self.api.last_text())
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "idle")

    def test_stale_wizard_callback_is_handled(self):
        self.click("w:sets:4")
        self.assertIn("Мастер уже закрыт", self.api.last_note())

    def test_new_program_replaces_old_only_after_finish(self):
        self.build_simple_program(days=1)
        first = self.storage.get_program(self.user_id)

        self.send("/newprogram")
        self.assertIn("будет заменена", self.api.sent[-2][1])
        self.click("w:dc:1")
        # мастер не завершён — старая программа на месте
        self.assertEqual(self.storage.get_program(self.user_id), first)


class WorkoutTests(BaseBotTest):
    def test_train_without_program(self):
        self.send("/train")
        self.assertIn("/newprogram", self.api.last_text())

    def test_full_workout_and_summary(self):
        self.send("/newprogram")
        self.click("w:dc:1")
        self.send("Грудь")
        self.click("w:grp:0")
        self.click("w:ex:0:0")
        self.click("w:sets:4")
        self.click("w:more")
        self.click("w:own")
        self.send("Отжимания")
        self.click("w:sets:3")
        self.click("w:dayend")
        self.assertIn("Программа сохранена", self.api.last_text())

        self.send("/train")
        self.assertIn("Выберите день", self.api.last_text())
        self.assertEqual(self.api.buttons_data(), ["train_day:0"])

        self.click("train_day:0")
        prompt = self.api.last_text()
        self.assertIn(catalog.exercise_name(0, 0), prompt)
        self.assertIn("План: 4 подхода", prompt)

        self.send("80x8, 80x8, 75x6, 75x6")
        self.assertIn("Отжимания", self.api.last_text())

        self.send("15, 12, 10")
        summary = self.api.last_text()
        self.assertIn("завершена", summary)
        self.assertIn("Общий тоннаж", summary)
        # тоннаж: 80*8 + 80*8 + 75*6 + 75*6 = 2180, отжимания без веса = 0
        self.assertIn("2180", summary)

        history = self.storage.get_history(self.user_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(self.storage.get_session(self.user_id)["state"], "idle")

    def test_skip_exercise_marked_in_summary(self):
        self.build_simple_program(days=1)
        self.send("/train")
        self.click("train_day:0")
        self.send("/skip")
        summary = self.api.last_text()
        self.assertIn("пропущено", summary)

    def test_invalid_set_input_reprompts(self):
        self.build_simple_program(days=1)
        self.send("/train")
        self.click("train_day:0")
        self.send("абырвалг")
        self.assertIn("Не понял", self.api.last_text())
        session = self.storage.get_session(self.user_id)
        self.assertEqual(session["state"], "logging")
        self.assertEqual(session["data"]["exercise_index"], 0)

    def test_program_command_shows_saved_program(self):
        self.build_simple_program(days=2)
        self.send("/program")
        text = self.api.last_text()
        self.assertIn("День 1", text)
        self.assertIn("День 2", text)
        self.assertIn(catalog.exercise_name(0, 0), text)


if __name__ == "__main__":
    unittest.main()
