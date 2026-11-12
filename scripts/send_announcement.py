#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тестовая отправка карточки анонса «бот теперь для всего колледжа».

Запуск:
    python scripts/send_announcement.py --dry-run
    python scripts/send_announcement.py --chat 123456789
    python scripts/send_announcement.py --username mellowagent --chat @channel

--dry-run (по умолчанию) только рендерит картинку и печатает подпись —
ничего и никуда не отправляется. Реальная отправка требует в .env
BOT_TOKEN, а --chat указывает, кому именно уходит тест (себе, в канал
или в тестовую группу). Массовой рассылки в этом скрипте нет специально:
сначала проверяем карточку на одном адресате.

Каталог samples/ обновлять не нужно: файл рендерится в DATA_DIR/images,
а копия для репозитория делается скриптом make_sample_announcement.py.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.types import FSInputFile  # noqa: E402

import bot  # noqa: E402


async def send(token: str, chat_id: str, username: str) -> bool:
    api = Bot(token=token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        path = bot.render_announcement_image(username)
        await api.send_photo(chat_id, FSInputFile(str(path)),
                             caption=bot.announce_caption(username))
        print(f"Отправлено в {chat_id}: {path}")
        return True
    finally:
        await api.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", default="",
                        help="куда отправить тест: id, @channel или @username")
    parser.add_argument("--username", default=os.getenv("BOT_USERNAME", "mellowagent"),
                        help="username бота для подписи и подвала карточки")
    parser.add_argument("--dry-run", action="store_true",
                        help="только собрать карточку и подпись")
    args = parser.parse_args()

    bot.init_db()
    path = bot.render_announcement_image(args.username)
    caption = bot.announce_caption(args.username)
    print(f"Карточка: {path}")
    print(f"Подпись: {len(caption)} символов")
    print("-" * 60)
    print(caption)
    print("-" * 60)

    if args.dry_run or not args.chat:
        if not args.dry_run:
            print("--chat не указан: ограничиваюсь dry-run.")
        return 0

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        print("BOT_TOKEN не задан — отправка невозможна.")
        return 2
    try:
        asyncio.run(send(token, args.chat, args.username))
    except Exception as error:                       # noqa: BLE001
        print(f"Не удалось отправить: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
