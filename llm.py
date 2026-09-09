# -*- coding: utf-8 -*-
"""Разбор программы тренировок через Claude API — запасной вариант.

Основной разбор эвристический (freeform.py) и бесплатный. Сюда попадают
только тексты, с которыми эвристики не справились: свободные формулировки
вроде «в понедельник грудь, жму штангу четыре раза по десять».

Ключ берётся из переменной окружения ANTHROPIC_API_KEY. Если ключа нет,
модуль просто выключен — бот работает как раньше. Любая ошибка (нет сети,
кончились кредиты, неверный ответ) не ломает бота: вызывающий код
возвращается к результату эвристик.
"""
import json
import logging
import re

import requests

from program import MAX_DAYS, MAX_SETS

log = logging.getLogger("trainer_bot.llm")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

MAX_INPUT_CHARS = 6000       # длинные простыни не отправляем
MAX_EXERCISES_PER_DAY = 30
DEFAULT_SETS = 3

SYSTEM_PROMPT = """Ты разбираешь программы тренировок, записанные человеком в свободной форме, в структурированный вид.

Правила:
- Определи тренировочные дни и упражнения в каждом дне.
- Для каждого упражнения определи количество ПОДХОДОВ (не повторений).
  «4х10» или «4 по 10» значит 4 подхода. «3 сета» значит 3 подхода.
  Перечисление повторений «12,12,10» значит 3 подхода.
  Если количество подходов определить нельзя, ставь 3.
- Названия упражнений оставляй такими, как их написал человек, только убирай
  из них вес, повторения и нумерацию списка.
- Название дня бери из текста («Понедельник», «Грудь + трицепс», «Фулбади»).
  Если названия нет, оставь пустую строку.
- Не придумывай упражнения, которых нет в тексте.
- Если в тексте нет ничего похожего на программу тренировок, верни {"days": []}.

Отвечай ТОЛЬКО JSON без пояснений, в формате:
{"days": [{"name": "Понедельник — грудь", "exercises": [{"name": "Жим лёжа", "sets": 4}]}]}"""


class LLMUnavailable(Exception):
    """LLM не смог разобрать текст — вызывающий код работает без него."""


def is_enabled(api_key: str | None) -> bool:
    return bool(api_key)


def _extract_json(text: str) -> dict:
    """Модель просили ответить чистым JSON, но подстрахуемся."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise LLMUnavailable("В ответе модели нет JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise LLMUnavailable(f"Не разобрал JSON от модели: {e}") from e


def _normalize(payload: dict) -> dict:
    """Приводим ответ модели к нашей структуре и не доверяем ему на слово."""
    raw_days = payload.get("days")
    if not isinstance(raw_days, list):
        raise LLMUnavailable("В ответе модели нет списка дней")

    days = []
    for raw_day in raw_days[:MAX_DAYS]:
        if not isinstance(raw_day, dict):
            continue
        exercises = []
        for raw_exercise in (raw_day.get("exercises") or [])[:MAX_EXERCISES_PER_DAY]:
            if not isinstance(raw_exercise, dict):
                continue
            name = str(raw_exercise.get("name") or "").strip()[:100]
            if not name:
                continue
            try:
                sets = int(raw_exercise.get("sets") or DEFAULT_SETS)
            except (TypeError, ValueError):
                sets = DEFAULT_SETS
            exercises.append({"name": name, "sets": max(1, min(sets, MAX_SETS))})
        if not exercises:
            continue
        days.append({
            "number": len(days) + 1,
            "name": str(raw_day.get("name") or "").strip()[:60],
            "exercises": exercises,
        })

    if not days:
        raise LLMUnavailable("Модель не нашла в тексте программу")

    for number, day in enumerate(days, start=1):
        day["number"] = number
        if not day["name"]:
            day["name"] = f"День {number}"

    return {"name": "Моя программа", "days": days}


def parse_with_llm(text: str, api_key: str, model: str, timeout: int = 20) -> dict:
    """Разбирает текст через Claude API. Бросает LLMUnavailable при любой беде."""
    if not is_enabled(api_key):
        raise LLMUnavailable("ANTHROPIC_API_KEY не задан")
    if not (text or "").strip():
        raise LLMUnavailable("Пустой текст")

    payload = {
        "model": model,
        "max_tokens": 2000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": text[:MAX_INPUT_CHARS]}],
    }
    try:
        response = requests.post(
            API_URL,
            json=payload,
            timeout=timeout,
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
            },
        )
    except requests.RequestException as e:
        raise LLMUnavailable(f"Сеть недоступна: {e}") from e

    if response.status_code != 200:
        # тело может содержать причину (нет кредитов, неверный ключ, лимит),
        # но сам ключ в логи не попадает
        raise LLMUnavailable(f"Claude API ответил {response.status_code}: {response.text[:200]}")

    try:
        blocks = response.json().get("content") or []
        answer = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    except (ValueError, AttributeError) as e:
        raise LLMUnavailable(f"Непонятный ответ API: {e}") from e

    return _normalize(_extract_json(answer))
