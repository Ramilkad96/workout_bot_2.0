# -*- coding: utf-8 -*-
"""Редактор сохранённой программы: переименование дней, добавление и удаление
дней и упражнений, изменение количества подходов и порядка упражнений.

Как и мастер, живёт в сессии пользователя и рисует «экраны» (текст + кнопки).
Все изменения применяются к рабочей копии программы и сохраняются сразу —
поэтому выйти из редактора можно в любой момент, ничего не потеряется.
"""
import catalog
from program import MAX_DAYS, MAX_SETS, day_title, format_day, format_program, new_day, plural_sets
from telegram_api import inline_keyboard

VIEW_DAYS = "days"
VIEW_DAY = "day"
VIEW_EXERCISE = "exercise"
VIEW_RENAME_DAY = "rename_day"
VIEW_PICK_GROUP = "pick_group"
VIEW_PICK_EXERCISE = "pick_exercise"
VIEW_CUSTOM_EXERCISE = "custom_exercise"
VIEW_SETS = "sets"
VIEW_CUSTOM_SETS = "custom_sets"


def new_state(program: dict) -> dict:
    return {
        "program": program,
        "view": VIEW_DAYS,
        "day": None,
        "exercise": None,
        "pending_exercise": "",
        "group_index": None,
        "msg_id": None,
    }


def current_day(state: dict) -> dict | None:
    index = state.get("day")
    days = state["program"]["days"]
    if index is None or not (0 <= index < len(days)):
        return None
    return days[index]


def renumber(program: dict):
    for i, day in enumerate(program["days"], start=1):
        old_default = f"День {day.get('number', i)}"
        if day.get("name") in ("", old_default):
            day["name"] = f"День {i}"
        day["number"] = i


# ---------------- изменения ----------------

def add_day(state: dict) -> bool:
    days = state["program"]["days"]
    if len(days) >= MAX_DAYS:
        return False
    days.append(new_day(len(days) + 1))
    renumber(state["program"])
    state["day"] = len(days) - 1
    state["view"] = VIEW_DAY
    return True


def delete_day(state: dict) -> bool:
    days = state["program"]["days"]
    if len(days) <= 1:
        return False
    days.pop(state["day"])
    renumber(state["program"])
    state["day"] = None
    state["view"] = VIEW_DAYS
    return True


def rename_day(state: dict, name: str):
    day = current_day(state)
    day["name"] = name.strip()[:60] or day["name"]
    state["view"] = VIEW_DAY


def add_exercise(state: dict, name: str, sets: int):
    current_day(state)["exercises"].append({"name": name.strip()[:100], "sets": int(sets)})
    state["pending_exercise"] = ""
    state["group_index"] = None
    state["view"] = VIEW_DAY


def delete_exercise(state: dict):
    current_day(state)["exercises"].pop(state["exercise"])
    state["exercise"] = None
    state["view"] = VIEW_DAY


def change_sets(state: dict, sets: int):
    current_day(state)["exercises"][state["exercise"]]["sets"] = int(sets)
    state["view"] = VIEW_EXERCISE


def move_exercise(state: dict, delta: int) -> bool:
    exercises = current_day(state)["exercises"]
    i = state["exercise"]
    j = i + delta
    if not (0 <= j < len(exercises)):
        return False
    exercises[i], exercises[j] = exercises[j], exercises[i]
    state["exercise"] = j
    return True


# ---------------- экраны ----------------

def screen_days(state: dict) -> tuple:
    program = state["program"]
    buttons = [
        [(day_title(day), f"e:day:{i}")] for i, day in enumerate(program["days"])
    ]
    if len(program["days"]) < MAX_DAYS:
        buttons.append([("➕ Добавить день", "e:adday")])
    buttons.append([("✅ Готово", "e:done")])
    text = format_program(program) + "\n\nВыберите день, который хотите изменить:"
    return text, inline_keyboard(buttons)


def screen_day(state: dict) -> tuple:
    day = current_day(state)
    buttons = [[("✏️ Переименовать день", "e:rename")]]
    for i, ex in enumerate(day["exercises"]):
        buttons.append([(f"{i + 1}. {ex['name']}", f"e:ex:{i}")])
    buttons.append([("➕ Добавить упражнение", "e:addex")])
    row = [("⬅️ К дням", "e:days")]
    if len(state["program"]["days"]) > 1:
        row.append(("🗑 Удалить день", "e:delday"))
    buttons.append(row)
    text = format_day(day) + "\n\nВыберите упражнение или действие:"
    return text, inline_keyboard(buttons)


def screen_exercise(state: dict) -> tuple:
    day = current_day(state)
    index = state["exercise"]
    ex = day["exercises"][index]
    buttons = [
        [("🔢 Изменить подходы", "e:setsedit")],
        [("🔼 Выше", "e:up"), ("🔽 Ниже", "e:down")],
        [("🗑 Удалить упражнение", "e:delex")],
        [("⬅️ К дню", "e:day")],
    ]
    text = (
        f"{day_title(day)}\n"
        f"Упражнение {index + 1} из {len(day['exercises'])}\n\n"
        f"{ex['name']} — {plural_sets(ex['sets'])}"
    )
    return text, inline_keyboard(buttons)


def screen_rename_day(state: dict) -> tuple:
    day = current_day(state)
    text = (
        f"{day_title(day)}\n\n"
        f"Напишите новое название дня — группа мышц, «Фулбади», "
        f"день недели или просто номер."
    )
    return text, inline_keyboard([[("⬅️ Отмена", "e:day")]])


def screen_pick_group(state: dict) -> tuple:
    names = catalog.group_names()
    buttons = []
    for i in range(0, len(names), 2):
        row = [(names[i], f"e:grp:{i}")]
        if i + 1 < len(names):
            row.append((names[i + 1], f"e:grp:{i + 1}"))
        buttons.append(row)
    buttons.append([("✍️ Вписать своё", "e:own")])
    buttons.append([("⬅️ Отмена", "e:day")])
    text = f"{day_title(current_day(state))}\n\nВыберите группу мышц:"
    return text, inline_keyboard(buttons)


def screen_pick_exercise(group_index: int) -> tuple:
    names = catalog.exercises_of(group_index)
    buttons = [[(name, f"e:exsel:{group_index}:{i}")] for i, name in enumerate(names)]
    buttons.append([("⬅️ К группам", "e:groups"), ("✍️ Своё", "e:own")])
    return f"{catalog.group_name(group_index)} — выберите упражнение:", inline_keyboard(buttons)


def screen_custom_exercise() -> tuple:
    return "Напишите название упражнения:", inline_keyboard([[("⬅️ Отмена", "e:day")]])


def screen_sets(title: str) -> tuple:
    buttons = [
        [(str(n), f"e:sets:{n}") for n in range(1, 5)],
        [(str(n), f"e:sets:{n}") for n in range(5, 9)],
        [("Другое количество", "e:setsx")],
    ]
    return f"{title}\n\nСколько подходов?", inline_keyboard(buttons)


def screen_custom_sets() -> tuple:
    return (
        f"Напишите количество подходов числом (1–{MAX_SETS}):",
        inline_keyboard([[("⬅️ Отмена", "e:day")]]),
    )
