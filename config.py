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
