# -*- coding: utf-8 -*-
"""Логика логирования подходов и формирования сводки по тренировке."""
import re
from datetime import datetime, timezone

SET_GROUP_RE = re.compile(r"^\s*(?:(?P<weight>\d+(?:[.,]\d+)?)\s*[xXхХ×]\s*)?(?P<reps>\d+)\s*$")


class SetParseError(Exception):
    pass


def parse_sets_input(text: str) -> list:
    """
    Разбирает ввод фактических подходов, например:
        "80x8, 80x8, 75x6"   -> [(80,8), (80,8), (75,6)]
        "8, 8, 6"            -> [(0,8), (0,8), (0,6)]  (без веса — свой вес)
        "пропуск" / "skip"   -> []  (обрабатывается вызывающим кодом отдельно)
    """
    groups = [g.strip() for g in text.split(",") if g.strip()]
    if not groups:
        raise SetParseError(
            "Не удалось распознать подходы. Введите через запятую, например: 80x8, 80x8, 75x6"
        )
    result = []
    for g in groups:
        m = SET_GROUP_RE.match(g)
        if not m:
            raise SetParseError(
                f"Не понял «{g}». Формат подхода: вес x повторы (например 80x8) "
                f"или просто повторы (например 8), через запятую."
            )
        weight_raw = m.group("weight")
        weight = float(weight_raw.replace(",", ".")) if weight_raw else 0.0
        reps = int(m.group("reps"))
        result.append((weight, reps))
    return result


def new_session_state(day_name: str, workout_id: int, exercises: list) -> dict:
    return {
        "day_name": day_name,
        "workout_id": workout_id,
        "exercise_index": 0,
        "exercises": exercises,  # план: [{"name","sets","reps","weight"}]
        "log": [],  # факт: [{"name": str, "sets": [(weight,reps), ...]}]
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def current_exercise(state: dict):
    idx = state["exercise_index"]
    if idx >= len(state["exercises"]):
        return None
    return state["exercises"][idx]


def record_sets(state: dict, sets: list):
    ex = current_exercise(state)
    state["log"].append({"name": ex["name"], "planned": ex, "sets": sets})
    state["exercise_index"] += 1


def skip_exercise(state: dict):
    ex = current_exercise(state)
    state["log"].append({"name": ex["name"], "planned": ex, "sets": []})
    state["exercise_index"] += 1


def is_finished(state: dict) -> bool:
    return state["exercise_index"] >= len(state["exercises"])


def build_summary(state: dict) -> str:
    started = datetime.fromisoformat(state["started_at"])
    finished = datetime.now(timezone.utc)
    duration = finished - started
    minutes = int(duration.total_seconds() // 60)

    total_volume = 0.0
    total_sets = 0
    total_reps = 0
    lines = [f"\U0001F3C1 Тренировка «{state['day_name']}» завершена!", ""]

    for entry in state["log"]:
        sets = entry["sets"]
        if not sets:
            lines.append(f"⏭ {entry['name']} — пропущено")
            continue
        volume = sum(w * r for w, r in sets)
        total_volume += volume
        total_sets += len(sets)
        total_reps += sum(r for _, r in sets)
        sets_str = ", ".join(
            f"{r}" if w == 0 else f"{w:g}x{r}" for w, r in sets
        )
        lines.append(f"✅ {entry['name']}: {sets_str}  (тоннаж {volume:g} кг)")

    lines.append("")
    lines.append(f"⏱ Время тренировки: {minutes} мин")
    lines.append(f"🔢 Подходов всего: {total_sets}, повторений всего: {total_reps}")
    lines.append(f"🏋️ Общий тоннаж: {total_volume:g} кг")
    return "\n".join(lines)
