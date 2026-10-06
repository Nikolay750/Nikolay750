"""Конфигурация backend'а Mini App — через переменные окружения (см. .env.example)."""

import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OPERATOR_TG_ID = os.environ.get("OPERATOR_TG_ID", "")   # telegram id оператора — только ему открыт экран очереди
INITDATA_MAX_AGE = int(os.environ.get("INITDATA_MAX_AGE", "86400"))
DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"       # разрешает открывать без initData — только для локальной отладки


def link_signing_secret() -> str:
    """Секрет для подписи ссылок на скачивание отчёта (sign_link/check_link).

    Step 9: НЕ использовать строковый fallback "dev" в проде — это был бы
    предсказуемый секрет, подделываемый кем угодно. Падаем явно, если
    BOT_TOKEN не задан и это не осознанная локальная отладка (DEV_MODE)."""
    if BOT_TOKEN:
        return BOT_TOKEN
    if DEV_MODE:
        return "dev"
    raise RuntimeError(
        "BOT_TOKEN не задан: подписывать ссылки на отчёты нечем. "
        "Задайте BOT_TOKEN в окружении, или явно включите DEV_MODE=1 для локальной отладки."
    )
