"""Конфигурация backend'а Mini App — через переменные окружения (см. .env.example)."""

import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OPERATOR_TG_ID = os.environ.get("OPERATOR_TG_ID", "")   # telegram id оператора — только ему открыт экран очереди
INITDATA_MAX_AGE = int(os.environ.get("INITDATA_MAX_AGE", "86400"))
DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"       # разрешает открывать без initData — только для локальной отладки
