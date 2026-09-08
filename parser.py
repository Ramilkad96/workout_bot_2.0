# -*- coding: utf-8 -*-
"""
Парсер программы тренировок из текста по шаблону.

Формат шаблона:

    Название: Моя программа

    День 1: Грудь/трицепс
    Жим штанги лёжа 4x8
    Разводка гантелей 3x12x14кг
    Отжимания на брусьях 3x10

    День 2: Спина/бицепс
    Подтягивания 4x8
    Тяга штанги в наклоне 3x10x40кг

Правила:
- Строка "Название: ..." (необязательна) задаёт имя программы.
- Строка вида "День <N>[: <имя>]" (или "Day <N>") начинает новый день.
  Имя дня необязательно — если не указано, используется "День N".
- Строка упражнения: "<Название> <подходы>x<повторы>[x<вес>[кг]]"
  Разделитель x/х/× — латинская x, кириллическая х или ×, регистр не важен.
  Вес — необязателен, если не указан, будет 0 (например, для своего веса).
- Пустые строки и строки-комментарии (начинающиеся с #) игнорируются.
"""
import re

DAY_RE = re.compile(r"^(?:день|day)\s*(\d+)?\s*[:\-]?\s*(.*)$", re.IGNORECASE)
NAME_RE = re.compile(r"^(?:название|программа|name)\s*[:\-]\s*(.+)$", re.IGNORECASE)
EXERCISE_RE = re.compile(
    r"^(?P<name>.+?)\s+"
    r"(?P<sets>\d+)\s*[xXхХ×]\s*(?P<reps>\d+)"
    r"(?:\s*[xXхХ×]\s*(?P<weight>\d+(?:[.,]\d+)?)\s*(?:кг|kg)?)?\s*$"
)


class ProgramParseError(Exception):
    def __init__(self, message, line_number=None, line_text=None):
        self.line_number = line_number
        self.line_text = line_text
        if line_number is not None:
            message = f"Строка {line_number}: {message} («{line_text}»)"
        super().__init__(message)


def parse_program(text: str) -> dict:
    """
    Разбирает текст программы тренировок в структуру:
    {
        "name": str,
        "days": [
            {
                "name": str,
                "exercises": [
                    {"name": str, "sets": int, "reps": int, "weight": float}
                ]
            }
        ]
    }
    Бросает ProgramParseError с понятным сообщением при ошибке.
    """
    program_name = "Моя программа"
    days = []
    current_day = None
    auto_day_index = 0

    lines = text.splitlines()
    for i, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        name_match = NAME_RE.match(line)
        if name_match:
            program_name = name_match.group(1).strip()
            continue

        day_match = DAY_RE.match(line)
        if day_match:
            auto_day_index += 1
            day_num = day_match.group(1) or str(auto_day_index)
            day_name = day_match.group(2).strip()
            if not day_name:
                day_name = f"День {day_num}"
            current_day = {"name": day_name, "exercises": []}
            days.append(current_day)
            continue

        ex_match = EXERCISE_RE.match(line)
        if ex_match:
            if current_day is None:
                raise ProgramParseError(
                    "упражнение указано до объявления дня (нужна строка вида 'День 1: ...')",
                    i,
                    raw_line.strip(),
                )
            weight_raw = ex_match.group("weight")
            weight = float(weight_raw.replace(",", ".")) if weight_raw else 0.0
            current_day["exercises"].append(
                {
                    "name": ex_match.group("name").strip(),
                    "sets": int(ex_match.group("sets")),
                    "reps": int(ex_match.group("reps")),
                    "weight": weight,
                }
            )
            continue

        raise ProgramParseError(
            "не удалось распознать строку. Ожидался формат "
            "'Название упражнения 4x8' или 'День 1: Название дня'",
            i,
            raw_line.strip(),
        )

    if not days:
        raise ProgramParseError("в программе не найдено ни одного дня тренировок")

    for day in days:
        if not day["exercises"]:
            raise ProgramParseError(f"в дне «{day['name']}» не найдено ни одного упражнения")

    return {"name": program_name, "days": days}


def format_program(program: dict) -> str:
    """Человекочитаемое представление разобранной программы (для /program)."""
    lines = [f"\U0001F4CB {program['name']}", ""]
    for day in program["days"]:
        lines.append(f"▸ {day['name']}")
        for ex in day["exercises"]:
            weight_part = f" x {ex['weight']:g} кг" if ex["weight"] else ""
            lines.append(f"   • {ex['name']} — {ex['sets']}x{ex['reps']}{weight_part}")
        lines.append("")
    return "\n".join(lines).strip()
