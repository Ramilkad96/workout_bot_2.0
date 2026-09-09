# -*- coding: utf-8 -*-
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()

# На Railway обычная файловая система эфемерна (сбрасывается при редеплое).
# Если подключите Railway Volume, укажите его точку монтирования через
# переменную окружения DB_PATH (например: /data/trainer_bot.db) — тогда
# данные переживут редеплой.
# Пустое или незаданное значение => файл рядом со скриптом (пустая строка
# в sqlite3 означает временную базу, которая стирается — этого не хотим).
DB_PATH = (os.environ.get("DB_PATH") or "").strip() or "trainer_bot.db"

# Часовой пояс для дат в отчётах (Railway работает в UTC).
# Москва = 3. Меняется переменной окружения TZ_OFFSET_HOURS.
try:
    TZ_OFFSET_HOURS = int((os.environ.get("TZ_OFFSET_HOURS") or "3").strip())
except ValueError:
    TZ_OFFSET_HOURS = 3

# Разбор программы через Claude API — необязательный запасной вариант.
# Без ключа бот работает только на эвристиках (freeform.py).
# Ключ берётся в Claude Console, оплачивается отдельно от подписки Claude.
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
LLM_MODEL = (os.environ.get("LLM_MODEL") or "").strip() or "claude-haiku-4-5-20251001"
try:
    LLM_TIMEOUT = int((os.environ.get("LLM_TIMEOUT") or "20").strip())
except ValueError:
    LLM_TIMEOUT = 20
