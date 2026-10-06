import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")             # https://ваш-домен/  — его же указать в BotFather
DATA_DIR = os.environ.get("DATA_DIR", "./data")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))  # на пакет; сканы ИД бывают по 100 МБ
INITDATA_MAX_AGE = int(os.environ.get("INITDATA_MAX_AGE", "86400"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))  # пакеты клиентов не храним вечно
WORKERS = int(os.environ.get("WORKERS", "2"))
DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"          # только локально: пускает без Telegram
