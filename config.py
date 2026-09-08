# -*- coding: utf-8 -*-
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# На Railway обычная файловая система эфемерна (сбрасывается при редеплое).
# Если подключите Railway Volume, укажите его точку монтирования через
# переменную окружения DB_PATH (например: /data/trainer_bot.db) — тогда
# данные переживут редеплой.
DB_PATH = os.environ.get("DB_PATH", "trainer_bot.db")
