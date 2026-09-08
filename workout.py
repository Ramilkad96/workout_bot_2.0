# -*- coding: utf-8 -*-
"""Логика тренировки: запись подходов (сначала вес, потом повторения),
свободный выбор упражнения и сводка по завершению.

Состояние тренировки (лежит в сессии пользователя):
{
    "day_name": "День 1 — Грудь",
    "workout_id": 12,
    "started_at": "2026-09-08T10:00:00+00:00",
    "exercises": [{"name": "Жим штанги лёжа", "sets": 4}],   # план
    "log": [{"sets": [[80.0, 8]], "status": "pending"}],      # факт, по индексам плана
    "current": 0,            # индекс упражнения, которое записываем (или None)
    "step": "choose",        # choose | weight | reps
    "pending_weight": None,  # вес, для которого ждём повторения
    "msg_id": None           # id сообщения-экрана, которое редактируем
}
"""
import re
from datetime import datetime, timezone

from program import plural_sets
from telegram_api import inline_keyboard

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"

STEP_CHOOSE = "choose"
STEP_WEIGHT = "weight"
STEP_REPS = "reps"

MAX_WEIGHT = 1000
MAX_REPS = 1000

BODYWEIGHT_WORDS = {"-", "0", "свой", "своё", "св", "б/в", "бв", "без веса", "нет"}

_NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")


class InputError(Exception):
    pass


def parse_weight(text: str) -> float:
    """«80», «80.5», «80,5» -> число. «-», «0», «свой» -> 0 (собственный вес)."""
    value = text.strip().lower()
    if value in BODYWEIGHT_WORDS:
        return 0.0
    value = value.replace("кг", "").replace("kg", "").strip()
    if not _NUMBER_RE.match(value):
        raise InputError(
            "Не понял вес. Введите число, например: 80 или 62.5\n"
            "Если упражнение с собственным весом — отправьте «-»"
        )
    weight = float(value.replace(",", "."))
    if not 0 <= weight <= MAX_WEIGHT:
        raise InputError(f"Вес должен быть от 0 до {MAX_WEIGHT} кг.")
    return weight


def parse_reps(text: str) -> int:
    value = text.strip()
    if not value.isdigit():
        raise InputError("Не понял повторения. Введите целое число, например: 8")
    reps = int(value)
    if not 1 <= reps <= MAX_REPS:
        raise InputError(f"Повторений должно быть от 1 до {MAX_REPS}.")
    return reps


def new_session_state(day_name: str, workout_id: int, exercises: list) -> dict:
    return {
        "day_name": day_name,
        "workout_id": workout_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "exercises": [dict(ex) for ex in exercises],
        "log": [{"sets": [], "status": STATUS_PENDING} for _ in exercises],
        "current": None,
        "step": STEP_CHOOSE,
        "pending_weight": None,
        "msg_id": None,
    }


# ---------------- операции ----------------

def select_exercise(state: dict, index: int):
    state["current"] = index
    state["step"] = STEP_WEIGHT
    state["pending_weight"] = None
    if state["log"][index]["status"] == STATUS_SKIPPED:
        state["log"][index]["status"] = STATUS_PENDING


def set_pending_weight(state: dict, weight: float):
    state["pending_weight"] = weight
    state["step"] = STEP_REPS


def record_set(state: dict, reps: int):
    index = state["current"]
    state["log"][index]["sets"].append([state["pending_weight"], reps])
    state["pending_weight"] = None
    state["step"] = STEP_WEIGHT


def finish_exercise(state: dict):
    index = state["current"]
    entry = state["log"][index]
    entry["status"] = STATUS_DONE if entry["sets"] else STATUS_PENDING
    state["current"] = None
    state["step"] = STEP_CHOOSE
    state["pending_weight"] = None


def skip_exercise(state: dict, index: int | None = None):
    index = state["current"] if index is None else index
    entry = state["log"][index]
    entry["status"] = STATUS_SKIPPED if not entry["sets"] else STATUS_DONE
    state["current"] = None
    state["step"] = STEP_CHOOSE
    state["pending_weight"] = None


def back_to_list(state: dict):
    if state["current"] is not None:
        entry = state["log"][state["current"]]
        if entry["sets"]:
            entry["status"] = STATUS_DONE
    state["current"] = None
    state["step"] = STEP_CHOOSE
    state["pending_weight"] = None


def current_exercise(state: dict) -> dict | None:
    index = state.get("current")
    if index is None or not (0 <= index < len(state["exercises"])):
        return None
    return state["exercises"][index]


def sets_done(state: dict, index: int | None = None) -> int:
    index = state["current"] if index is None else index
    return len(state["log"][index]["sets"])


def all_touched(state: dict) -> bool:
    return all(e["status"] != STATUS_PENDING or e["sets"] for e in state["log"])


def last_weight(state: dict, index: int | None = None) -> float | None:
    index = state["current"] if index is None else index
    sets = state["log"][index]["sets"]
    return sets[-1][0] if sets else None


# ---------------- отображение ----------------

def format_set(weight, reps) -> str:
    if not weight:
        return f"{reps}"
    return f"{weight:g} кг x {reps}"


def format_sets(sets: list) -> str:
    return ", ".join(format_set(w, r) for w, r in sets)


def status_mark(entry: dict) -> str:
    if entry["status"] == STATUS_DONE:
        return "✅"
    if entry["status"] == STATUS_SKIPPED:
        return "⏭"
    return "▫️"


def duration_minutes(state: dict) -> int:
    started = datetime.fromisoformat(state["started_at"])
    return int((datetime.now(timezone.utc) - started).total_seconds() // 60)


def build_summary(state: dict) -> str:
    lines = [f"🏁 Тренировка «{state['day_name']}» завершена!", ""]
    for ex, entry in zip(state["exercises"], state["log"]):
        if entry["sets"]:
            lines.append(f"✅ {ex['name']}")
            lines.append(f"     {format_sets(entry['sets'])}")
        elif entry["status"] == STATUS_SKIPPED:
            lines.append(f"⏭ {ex['name']} — пропущено")
        else:
            lines.append(f"▫️ {ex['name']} — не выполнено")
    lines.append("")
    lines.append(f"⏱ Время тренировки: {duration_minutes(state)} мин")
    return "\n".join(lines)


def exercise_line(ex: dict, entry: dict) -> str:
    """Строка упражнения для экрана выбора."""
    mark = status_mark(entry)
    done = len(entry["sets"])
    planned = ex.get("sets", 0)
    if done:
        return f"{mark} {ex['name']} — {done}/{planned}: {format_sets(entry['sets'])}"
    if entry["status"] == STATUS_SKIPPED:
        return f"{mark} {ex['name']} — пропущено"
    return f"{mark} {ex['name']} — план {plural_sets(planned)}"


# ---------------- экраны ----------------

def screen_choose(state: dict) -> tuple:
    """Список упражнений: можно выполнять в любом порядке."""
    lines = [f"🏋️ {state['day_name']}", ""]
    buttons = []
    for i, (ex, entry) in enumerate(zip(state["exercises"], state["log"])):
        lines.append(exercise_line(ex, entry))
        buttons.append([(f"{status_mark(entry)} {ex['name']}", f"t:ex:{i}")])
    lines.append("")
    lines.append("Выберите упражнение — порядок любой.")
    buttons.append([("🏁 Завершить тренировку", "t:finish")])
    return "\n".join(lines), inline_keyboard(buttons)


def screen_weight(state: dict) -> tuple:
    ex = current_exercise(state)
    entry = state["log"][state["current"]]
    done = len(entry["sets"])
    planned = ex.get("sets", 0)
    lines = [f"{ex['name']}"]
    if entry["sets"]:
        lines.append(f"Записано: {format_sets(entry['sets'])}")
    lines.append("")
    if done >= planned:
        # план выполнен, но добавить лишний подход никто не мешает
        lines.append(f"Подход {done + 1} (сверх плана). Введите вес в кг:")
    else:
        lines.append(f"Подход {done + 1} из {planned}. Введите вес в кг:")
    previous = last_weight(state)
    if previous is not None:
        hint = f"{previous:g}" if previous else "свой вес"
        lines.append(f"(прошлый подход — {hint})")
    lines.append("Свой вес — отправьте «-»")

    buttons = []
    if entry["sets"]:
        buttons.append([("✅ Упражнение выполнено", "t:done")])
    else:
        buttons.append([("⏭ Пропустить упражнение", "t:skip")])
    buttons.append([("⬅️ К списку упражнений", "t:back")])
    return "\n".join(lines), inline_keyboard(buttons)


def screen_reps(state: dict) -> tuple:
    ex = current_exercise(state)
    entry = state["log"][state["current"]]
    weight = state["pending_weight"]
    weight_text = f"{weight:g} кг" if weight else "свой вес"
    text = (
        f"{ex['name']}\n"
        f"Подход {len(entry['sets']) + 1}: {weight_text}\n\n"
        f"Сколько повторений?"
    )
    return text, inline_keyboard([[("⬅️ Исправить вес", "t:reweight")]])


def screen_after_summary() -> tuple:
    buttons = [
        [("🏋️ Ещё тренировка", "t:again")],
        [("📋 Моя программа", "t:program"), ("✏️ Редактировать", "t:edit")],
    ]
    return "Что дальше?", inline_keyboard(buttons)
