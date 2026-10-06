"""Проверка initData Telegram Mini App (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).

Без этой проверки любой, кто знает адрес API, сможет загружать файлы и читать чужие отчёты.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    if not init_data or not bot_token:
        raise AuthError("нет initData или токена бота")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    received = pairs.pop("hash", None)
    if not received:
        raise AuthError("в initData нет hash")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("подпись initData неверна")
    auth_date = int(pairs.get("auth_date", "0"))
    if max_age and time.time() - auth_date > max_age:
        raise AuthError("initData устарели — откройте приложение заново")
    user = json.loads(pairs.get("user", "{}"))
    if "id" not in user:
        raise AuthError("в initData нет пользователя")
    return user


def sign_link(secret: str, payload: str, ttl: int = 600) -> str:
    """Короткоживущая подпись для ссылки на скачивание отчёта (downloadFile не умеет передавать заголовки)."""
    exp = int(time.time()) + ttl
    sig = hmac.new(secret.encode(), f"{payload}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def check_link(secret: str, payload: str, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    good = hmac.new(secret.encode(), f"{payload}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, sig)
