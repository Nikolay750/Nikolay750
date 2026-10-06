"""Бот — вход в Mini App и уведомления. Вся работа с файлами — в Mini App (обходит лимит Bot API 20 МБ).

Запуск:  python -m bot.bot
"""
import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
URL = os.environ["WEBAPP_URL"]
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Проверить пакет", web_app=WebAppInfo(url=URL))]])
    await m.answer("Загрузите смету и акты КС-2 или договор с ВОР и ЛСР — найду расхождения в объёмах, "
                   "ценах, коэффициентах и материалах. Файлы до 500 МБ, сканы пока не распознаются.", reply_markup=kb)


@dp.message()
async def files_in_chat(m: Message):
    if m.document:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть загрузку", web_app=WebAppInfo(url=URL))]])
        await m.answer("Файлы загружайте через приложение: в чат бота Telegram пропускает только до 20 МБ.", reply_markup=kb)


async def main():
    bot = Bot(TOKEN)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Проверка", web_app=WebAppInfo(url=URL)))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
