# -*- coding: utf-8 -*-
"""Пошаговый мастер создания программы тренировок.

Логика вынесена отдельно от bot.py: здесь только состояние и «экраны»
(текст + клавиатура), без обращений к сети. Благодаря этому весь мастер
покрывается офлайн-тестами.

Порядок шагов:
    1. Сколько тренировочных дней в неделю   -> STEP_DAYS_COUNT
    2. Комментарий к дню (группа мышц)       -> STEP_DAY_COMMENT
    3. Выбор упражнения (каталог или своё)   -> STEP_PICK_GROUP / PICK_EXERCISE / CUSTOM_EXERCISE
    4. Количество подходов                   -> STEP_SETS / CUSTOM_SETS
    5. Ещё упражнение или следующий день     -> STEP_DAY_MENU
"""
import catalog
from program import MAX_DAYS, MAX_SETS, new_day, day_title, format_day, plural_days
from telegram_api import inline_keyboard

STEP_DAYS_COUNT = "days_count"
STEP_DAY_COMMENT = "day_comment"
STEP_PICK_GROUP = "pick_group"
STEP_PICK_EXERCISE = "pick_exercise"
STEP_CUSTOM_EXERCISE = "custom_exercise"
STEP_SETS = "sets"
STEP_CUSTOM_SETS = "custom_sets"
STEP_DAY_MENU = "day_menu"

CANCEL_HINT = "\n\nОтменить создание программы — /cancel"


# --------------------------- состояние ---------------------------

def new_state() -> dict:
    return {
        "step": STEP_DAYS_COUNT,
        "days_count": 0,
        "current": 0,          # индекс редактируемого дня
        "days": [],
        "pending_exercise": "",
        "group_index": None,
    }


def current_day(state: dict) -> dict | None:
    if 0 <= state["current"] < len(state["days"]):
        return state["days"][state["current"]]
    return None


def set_days_count(state: dict, count: int):
    state["days_count"] = count
    state["days"] = [new_day(i + 1) for i in range(count)]
    state["current"] = 0
    state["step"] = STEP_DAY_COMMENT


def set_day_comment(state: dict, comment: str):
    day = current_day(state)
    day["name"] = comment.strip() or day["name"]
    state["step"] = STEP_PICK_GROUP


def add_exercise(state: dict, name: str, sets: int):
    current_day(state)["exercises"].append({"name": name.strip(), "sets": int(sets)})
    state["pending_exercise"] = ""
    state["group_index"] = None
    state["step"] = STEP_DAY_MENU


def remove_last_exercise(state: dict) -> bool:
    day = current_day(state)
    if day["exercises"]:
        day["exercises"].pop()
        return True
    return False


def finish_day(state: dict) -> bool:
    """Переходит к следующему дню. Возвращает False, если дней больше нет."""
    state["current"] += 1
    if state["current"] >= state["days_count"]:
        return False
    state["step"] = STEP_DAY_COMMENT
    return True


def build_program(state: dict) -> dict:
    return {"name": "Моя программа", "days": state["days"]}


# --------------------------- экраны ---------------------------

def screen_days_count() -> tuple:
    buttons = [
        [(str(n), f"w:dc:{n}") for n in range(1, 5)],
        [(str(n), f"w:dc:{n}") for n in range(5, MAX_DAYS + 1)],
    ]
    text = (
        "Создаём программу тренировок.\n\n"
        "Шаг 1. Сколько тренировочных дней в неделю?" + CANCEL_HINT
    )
    return text, inline_keyboard(buttons)


def screen_day_comment(state: dict) -> tuple:
    day = current_day(state)
    text = (
        f"День {day['number']} из {state['days_count']}.\n\n"
        f"Напишите комментарий к дню — какая группа мышц "
        f"(например: «Грудь + трицепс»)." + CANCEL_HINT
    )
    return text, inline_keyboard([[("Пропустить", "w:nocom")]])


def screen_pick_group(state: dict) -> tuple:
    day = current_day(state)
    number = len(day["exercises"]) + 1
    names = catalog.group_names()
    buttons = []
    for i in range(0, len(names), 2):
        row = [(names[i], f"w:grp:{i}")]
        if i + 1 < len(names):
            row.append((names[i + 1], f"w:grp:{i + 1}"))
        buttons.append(row)
    buttons.append([("✍️ Вписать своё", "w:own")])
    if day["exercises"]:
        buttons.append([("✅ День готов", "w:dayend")])
    text = (
        f"{day_title(day)}. Упражнение {number}.\n\n"
        f"Выберите группу мышц или впишите своё упражнение:"
    )
    return text, inline_keyboard(buttons)


def screen_pick_exercise(group_index: int) -> tuple:
    names = catalog.exercises_of(group_index)
    buttons = [[(name, f"w:ex:{group_index}:{i}")] for i, name in enumerate(names)]
    buttons.append([("⬅️ К группам", "w:groups"), ("✍️ Своё", "w:own")])
    text = f"{catalog.group_name(group_index)} — выберите упражнение:"
    return text, inline_keyboard(buttons)


def screen_custom_exercise() -> tuple:
    return "Напишите название упражнения:" + CANCEL_HINT, None


def screen_sets(exercise_name: str) -> tuple:
    buttons = [
        [(str(n), f"w:sets:{n}") for n in range(1, 5)],
        [(str(n), f"w:sets:{n}") for n in range(5, 9)],
        [("Другое количество", "w:setsx")],
    ]
    text = f"{exercise_name}\n\nСколько подходов?"
    return text, inline_keyboard(buttons)


def screen_custom_sets() -> tuple:
    return f"Напишите количество подходов числом (1–{MAX_SETS}):", None


def screen_day_menu(state: dict) -> tuple:
    day = current_day(state)
    is_last_day = state["current"] + 1 >= state["days_count"]
    next_label = "✅ Завершить программу" if is_last_day else "➡️ Следующий день"
    buttons = [
        [("➕ Ещё упражнение", "w:more")],
        [(next_label, "w:dayend")],
    ]
    if day["exercises"]:
        buttons.insert(1, [("🗑 Удалить последнее", "w:undo")])
    text = f"{format_day(day)}\n\nЧто дальше?"
    return text, inline_keyboard(buttons)


def screen_after_days_count(state: dict) -> tuple:
    return (
        f"Отлично, {plural_days(state['days_count'])} в неделю. "
        f"Теперь заполним каждый день."
    ), None
