# -*- coding: utf-8 -*-
"""Разбор программы тренировок из текста в свободной форме.

Пользователь присылает программу так, как она у него записана — в заметках,
в переписке с тренером, в тетради. Строгого шаблона нет, поэтому разбираем
эвристиками и обязательно показываем результат на подтверждение: то, что
бот понял неправильно, правится в редакторе.

Что понимаем как заголовок дня:
    «День 1», «День 2: Грудь», «Тренировка 3», «Понедельник — ноги»,
    «Грудь и трицепс:», а также короткую строку-название перед упражнениями.

Что понимаем как упражнение (число подходов ищем в любом виде):
    «Жим лёжа 4х10», «Жим лёжа 4x10x80кг», «Приседания 5*5»,
    «Подтягивания 4 подхода», «Тяга 3 по 12», «Жим 3 сета»,
    «Разводка 12,12,10» (три подхода), «Планка» (без чисел — по умолчанию).

В программе хранится только количество подходов, поэтому повторения и вес
из текста используются лишь как подсказка о числе подходов.
"""
import re

from program import (MAX_DAYS, MAX_SETS, format_program, plural_days,
                     plural_exercises, total_exercises)
from telegram_api import inline_keyboard

DEFAULT_SETS = 3
MAX_EXERCISES_PER_DAY = 30

WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
    "пн", "вт", "ср", "чт", "пт", "сб", "вс",
)

# «День 1», «Тренировка 2», «Day 3», «Занятие 1»
DAY_WORD_RE = re.compile(
    r"^(?:день|дн|day|тренировка|тренировочный\s+день|workout|занятие)\s*"
    r"[№#]?\s*(\d+|[A-Za-zА-Яа-я])?\s*[:.\)\-—–]*\s*(.*)$",
    re.IGNORECASE,
)

# ведущая нумерация: «1.», «2)», «3 -», «-», «•», «*», «—»
LEADING_NUMBER_RE = re.compile(r"^\s*(?:\d{1,2}\s*[.)\]:]|\d{1,2}\s+[-–—]|[-–—•*·]+)\s*")

# «4х10», «4x10», «5*5», «4×10» — первое число это подходы
SETS_BY_CROSS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[xх×*]\s*(\d{1,3})(?!\d)", re.IGNORECASE)
# «4 подхода», «3 сета», «4 sets», «3 подх.»
SETS_BY_WORD_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:подход\w*|подх\.?|сет\w*|sets?|серии|серий|сери\w*)",
    re.IGNORECASE,
)
# «3 по 12», «4 по 8 раз»
SETS_BY_PO_RE = re.compile(r"(?<!\d)(\d{1,2})\s*по\s*\d{1,3}", re.IGNORECASE)
# «12,12,10» или «12/12/10» — количество групп это количество подходов
REPS_LIST_RE = re.compile(r"(?<!\d)\d{1,3}(?:\s*[,/]\s*\d{1,3}){1,9}(?!\d)")

# вес: «80кг», «(80 кг)», «@80»
WEIGHT_RE = re.compile(
    r"(?:@\s*\d+(?:[.,]\d+)?|[(]?\s*\d+(?:[.,]\d+)?\s*(?:кг|kg)\s*[)]?)",
    re.IGNORECASE,
)
# хвост с повторениями, который остаётся после вырезания подходов:
# «по 8 повторений», «по 10 раз», «10 повторов»
REPS_TAIL_RE = re.compile(
    r"(?:по\s*)?\d{1,3}\s*(?:повтор\w*|раз\w*|reps?)|по\s*\d{1,3}\b",
    re.IGNORECASE,
)

# слова, по которым короткая строка без чисел опознаётся как название дня,
# чтобы «Планка» осталась упражнением, а «Ноги» стали заголовком
GROUP_WORDS_RE = re.compile(
    r"(груд|спин|ног(?:и|а)\b|плеч|рук(?:и|а)\b|бицепс|трицепс|пресс|кардио|"
    r"фулбади|фул\s*бади|full\s*body|верх\b|низ\b|ягодиц|дельт|"
    r"push|pull|legs|upper|lower)",
    re.IGNORECASE,
)

NOISE_LINE_RE = re.compile(
    r"^(?:программа|программа\s+тренировок|моя\s+программа|тренировки|план|"
    r"неделя\s*\d*|week\s*\d*)\s*[:.]?\s*$",
    re.IGNORECASE,
)


class FreeformError(Exception):
    pass


def _clean(line: str) -> str:
    return line.strip().strip("*_#").strip()


def _looks_like_exercise(line: str) -> bool:
    """Строка похожа на упражнение, если в ней есть буквы и она не заголовок."""
    body = LEADING_NUMBER_RE.sub("", _clean(line))
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]{3,}", body))


def _tidy_header(text: str) -> str:
    """«Понедельник - грудь» и «Среда: спина» приводим к одному виду."""
    text = text.strip(" :.-—–")
    return re.sub(r"^\s*([^:\-–—]+?)\s*[:\-–—]+\s*(.+)$", r"\1 — \2", text)


def _day_header(line: str) -> str | None:
    """Возвращает название дня, если строка похожа на заголовок, иначе None."""
    text = _clean(line)
    if not text:
        return None

    low = text.lower()

    # «Понедельник», «Пн — ноги», «Вторник: спина»
    for weekday in WEEKDAYS:
        if low == weekday or re.match(rf"^{weekday}\b[\s:.\-—–,]+", low):
            return _tidy_header(text)

    # «День 1», «Тренировка 2: грудь»
    match = DAY_WORD_RE.match(text)
    if match and (match.group(1) or match.group(2)):
        number, rest = match.group(1), (match.group(2) or "").strip(" :.-—–")
        if rest:
            return _tidy_header(rest)
        return f"День {number}" if number else None

    return None


def _extract_sets(line: str) -> tuple[int | None, str]:
    """Находит количество подходов и возвращает (подходы, строка без этой части)."""
    for pattern in (SETS_BY_CROSS_RE, SETS_BY_WORD_RE, SETS_BY_PO_RE):
        match = pattern.search(line)
        if match:
            sets = int(match.group(1))
            return sets, line[: match.start()] + " " + line[match.end():]

    match = REPS_LIST_RE.search(line)
    if match:
        groups = [g for g in re.split(r"[,/]", match.group(0)) if g.strip()]
        return len(groups), line[: match.start()] + " " + line[match.end():]

    return None, line


def _clean_name(raw: str) -> str:
    name = WEIGHT_RE.sub(" ", raw)
    name = REPS_TAIL_RE.sub(" ", name)
    name = LEADING_NUMBER_RE.sub("", name)
    name = re.sub(r"\(\s*\)", " ", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" :;,.-–—()[]")
    name = re.sub(r"\s+(?:по|на|с|x|х|×|\*)$", "", name, flags=re.IGNORECASE)
    return name.strip(" :;,.-–—()[]")[:100]


def _parse_exercise(line: str) -> dict | None:
    sets, rest = _extract_sets(line)
    name = _clean_name(rest)
    if not re.search(r"[A-Za-zА-Яа-яЁё]{2,}", name):
        return None
    return {
        "name": name,
        "sets": max(1, min(sets, MAX_SETS)) if sets else DEFAULT_SETS,
        "guessed_sets": sets is None,
    }


def parse_freeform(text: str) -> tuple[dict, list]:
    """Разбирает текст в программу. Возвращает (программа, предупреждения)."""
    program, warnings, _ = analyze(text)
    return program, warnings


def analyze(text: str) -> tuple[dict, list, dict]:
    """То же, что parse_freeform, но ещё возвращает статистику разбора —
    по ней видно, стоит ли отдать текст на разбор языковой модели.

    Бросает FreeformError, если ничего похожего на упражнения не нашлось.
    """
    raw_lines = [line for line in (text or "").splitlines()]
    days = []
    warnings = []
    pending_header = None
    guessed_names = []

    def ensure_day(name: str | None = None):
        # имя оставляем пустым, чтобы в конце проставить «День N» по порядку
        if not days or name is not None:
            days.append({"number": len(days) + 1, "name": name or "", "exercises": []})
        return days[-1]

    lines = [(i, _clean(line)) for i, line in enumerate(raw_lines)]
    for position, (index, line) in enumerate(lines):
        if not line or NOISE_LINE_RE.match(line):
            continue

        header = _day_header(line)
        if header is None and not re.search(r"\d", line) and len(line.split()) <= 4:
            # Короткая строка без чисел — либо название дня, либо упражнение
            # без указанных подходов («Планка»). Заголовком считаем только
            # строку с двоеточием или с названием группы мышц, и только если
            # следом реально идёт упражнение.
            following = next((nxt for _, nxt in lines[position + 1:] if nxt), "")
            looks_like_title = line.endswith(":") or GROUP_WORDS_RE.search(line)
            if (
                looks_like_title
                and following
                and _looks_like_exercise(following)
                and _day_header(following) is None
            ):
                header = _tidy_header(line)

        if header is not None:
            pending_header = header
            continue

        if not _looks_like_exercise(line):
            continue

        exercise = _parse_exercise(line)
        if not exercise:
            continue

        day = ensure_day(pending_header if pending_header is not None else None)
        pending_header = None

        if len(day["exercises"]) >= MAX_EXERCISES_PER_DAY:
            warnings.append(f"В дне «{day['name']}» слишком много упражнений, лишние пропущены.")
            continue

        if exercise.pop("guessed_sets"):
            guessed_names.append(exercise["name"])
        day["exercises"].append(exercise)

    days = [day for day in days if day["exercises"]]
    # Одна строка без единого указания подходов — это не программа, а обычное
    # сообщение: лучше честно не понять, чем сохранить мусор.
    recognized = sum(len(day["exercises"]) for day in days)
    explicit_sets = recognized - len(guessed_names)
    if days and recognized < 3 and explicit_sets == 0:
        days = []

    if not days:
        raise FreeformError(
            "Не нашёл в тексте ни одного упражнения.\n\n"
            "Пришлите программу примерно так:\n\n"
            "День 1 — грудь\n"
            "Жим штанги лёжа 4х10\n"
            "Разводка гантелей 3х12\n\n"
            "День 2 — спина\n"
            "Подтягивания 4 подхода\n"
            "Тяга блока 3х12"
        )

    if len(days) > MAX_DAYS:
        warnings.append(f"Дней больше {MAX_DAYS}, лишние не сохранены.")
        days = days[:MAX_DAYS]

    for number, day in enumerate(days, start=1):
        day["number"] = number
        if not day["name"]:
            day["name"] = f"День {number}"

    if guessed_names:
        shown = ", ".join(guessed_names[:3])
        more = f" и ещё {len(guessed_names) - 3}" if len(guessed_names) > 3 else ""
        warnings.append(
            f"Не понял количество подходов у: {shown}{more} — поставил "
            f"{DEFAULT_SETS}. Поправьте в редакторе, если нужно."
        )

    meaningful_lines = [
        line for _, line in lines
        if line and not NOISE_LINE_RE.match(line) and re.search(r"[A-Za-zА-Яа-яЁё]{3,}", line)
    ]
    recognized = sum(len(day["exercises"]) for day in days)
    stats = {
        "lines": len(meaningful_lines),
        "recognized": recognized,
        "guessed": len(guessed_names),
        "explicit": recognized - len(guessed_names),
        "days": len(days),
    }
    return {"name": "Моя программа", "days": days}, warnings, stats


def looks_weak(stats: dict) -> bool:
    """Разбор считаем слабым, если упражнений мало, у большинства не нашлось
    подходов или значительная часть строк осталась непонятой. В этих случаях
    имеет смысл попробовать разобрать текст языковой моделью."""
    recognized = stats.get("recognized", 0)
    if recognized < 2:
        return True
    if stats.get("guessed", 0) * 2 > recognized:
        return True
    lines = stats.get("lines", 0)
    return bool(lines) and recognized * 2 < lines


def looks_like_program(text: str) -> bool:
    """Похоже ли присланное сообщение на программу тренировок.

    Нужно, чтобы предлагать разбор, когда человек просто вставил программу
    в чат, ничего не нажимая. Намеренно строго: лучше не предложить,
    чем лезть с разбором к обычному сообщению.
    """
    lines = [_clean(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 3:
        return False
    with_sets = sum(1 for line in lines if _extract_sets(line)[0] is not None)
    return with_sets >= 2


def screen_confirm(program: dict, warnings: list, via_llm: bool = False) -> tuple:
    """Показываем, что бот понял, до сохранения — разбор всё-таки на глазок."""
    lines = ["Вот что я понял:", "", format_program(program), ""]
    if via_llm:
        lines.append("🤖 Текст оказался непростым, разобрал с помощью AI — "
                     "проверьте внимательнее.")
        lines.append("")
    for warning in warnings:
        lines.append(f"⚠️ {warning}")
    if warnings:
        lines.append("")
    lines.append(
        f"Итого: {plural_days(len(program['days']))}, "
        f"{plural_exercises(total_exercises(program))}."
    )
    lines.append("Сохранить эту программу?")
    buttons = [
        [("✅ Сохранить", "i:save")],
        [("✏️ Сохранить и поправить", "i:edit")],
        [("🔁 Прислать другой текст", "i:retry")],
        [("❌ Отмена", "i:cancel")],
    ]
    return "\n".join(lines), inline_keyboard(buttons)


def screen_ask_text() -> tuple:
    return (
        "Пришлите программу одним сообщением — как она у вас записана.",
        inline_keyboard([[("❌ Отмена", "i:cancel")]]),
    )


def screen_offer(program: dict, warnings: list, via_llm: bool = False) -> tuple:
    """Пользователь просто вставил текст в чат, ничего не нажимая."""
    text, keyboard = screen_confirm(program, warnings, via_llm)
    return "Похоже на программу тренировок.\n\n" + text, keyboard
