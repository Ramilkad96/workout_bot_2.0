# -*- coding: utf-8 -*-
"""Структура программы тренировок и её отображение.

Программа:
{
    "name": "Моя программа",
    "days": [
        {
            "name": "Грудь + трицепс",   # комментарий пользователя к дню
            "number": 1,                  # номер дня в неделе
            "exercises": [
                {"name": "Жим штанги лёжа", "sets": 4}
            ]
        }
    ]
}

В плане хранится только количество подходов. Повторы и вес фиксируются
фактически во время тренировки.
"""

MAX_DAYS = 7
MAX_SETS = 20


def plural_sets(n: int) -> str:
    """1 подход / 2 подхода / 5 подходов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} подход"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} подхода"
    return f"{n} подходов"


def plural_days(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def new_day(number: int, name: str = "") -> dict:
    return {"number": number, "name": name or f"День {number}", "exercises": []}


def day_title(day: dict) -> str:
    number = day.get("number")
    name = day.get("name") or ""
    if number and name and name != f"День {number}":
        return f"День {number} — {name}"
    if number:
        return f"День {number}"
    return name or "День"


def format_day(day: dict, with_header: bool = True) -> str:
    lines = []
    if with_header:
        lines.append(f"▸ {day_title(day)}")
    if not day["exercises"]:
        lines.append("   (упражнений пока нет)")
    for i, ex in enumerate(day["exercises"], start=1):
        lines.append(f"   {i}. {ex['name']} — {plural_sets(ex['sets'])}")
    return "\n".join(lines)


def format_program(program: dict) -> str:
    lines = [f"\U0001F4CB {program.get('name', 'Моя программа')}", ""]
    for day in program["days"]:
        lines.append(format_day(day))
        lines.append("")
    return "\n".join(lines).strip()


def total_exercises(program: dict) -> int:
    return sum(len(day["exercises"]) for day in program["days"])
