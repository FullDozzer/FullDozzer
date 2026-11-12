#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разовая доставка анонса «бот теперь для всего колледжа».

Правила доставки определены в bot.py (announcement_*): получатели — снимок
существующих подписчиков на момент запуска, каждому чату — ровно одно
сообщение, повторный запуск ничего не дублирует.

Команды (по умолчанию — status, ничего не отправляет):

    python scripts/send_announcement.py
        Отчёт: сколько в снимке, сколько отправлено, сколько сбоев.

    python scripts/send_announcement.py --freeze-only
        Только зафиксировать снимок получателей. Полезно сделать это ДО
        запуска бота, чтобы «существующие» определялись этим моментом.

    python scripts/send_announcement.py --send
        Доставить анонс тем, кому ещё не отправлено. Требует
        ANNOUNCEMENT_BROADCAST=1 и BOT_TOKEN. Запускается один раз —
        повторные запуски безопасны.

    python scripts/send_announcement.py --retry-failed
        Вернуть в очередь только явные отказы Telegram ('failed') и
        доставить. Статусы 'sent'/'delivering' не трогаются никогда.

    python scripts/send_announcement.py --preview
        Отрендерить карточку и напечатать подпись, без отправки.
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

import bot  # noqa: E402


def print_status() -> None:
    snapshot_at = bot.announcement_snapshot_at()
    print(f"Снимок получателей: {snapshot_at or 'ещё не сделан'}")
    print(f"Размер снимка: {bot.announcement_snapshot_size()}")
    report = bot.announcement_report()
    for status in ("queued", "delivering", "sent", "failed"):
        print(f"  {status:11} {report.get(status, 0)}")
    print(f"  {'всего':11} {report.get('total', 0)}")
    print(f"Флаг ANNOUNCEMENT_BROADCAST: "
          f"{'включён' if bot.announcement_enabled() else 'выключен'}")


def preview(username: str) -> None:
    path = bot.render_announcement_image(username)
    caption = bot.announce_caption(username)
    print(f"Карточка: {path}")
    print(f"Подпись: {len(caption)} символов (лимит 1024)")
    print("-" * 60)
    print(caption)
    print("-" * 60)


async def deliver(retry_failed: bool) -> int:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        print("BOT_TOKEN не задан — отправка невозможна.")
        return 2
    if not bot.announcement_enabled():
        print("ANNOUNCEMENT_BROADCAST не включён — отправка отменена.\n"
              "Поставьте ANNOUNCEMENT_BROADCAST=1, если это осознанное решение.")
        return 2

    api = Bot(token=token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        if retry_failed:
            released = bot.release_failed_announcements()
            print(f"Возвращено в очередь из 'failed': {released}")
        result = await bot.run_announcement(api, bot.touch_announcement(api))
    finally:
        await api.session.close()
    print(f"Отправлено сейчас: {result['delivered']}, "
          f"ошибок: {result['errors']}")
    print_status()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", default=os.getenv("BOT_USERNAME", ""),
                        help="username бота для подписи и подвала карточки")
    parser.add_argument("--preview", action="store_true",
                        help="только карточка и подпись")
    parser.add_argument("--freeze-only", action="store_true",
                        help="только зафиксировать снимок получателей")
    parser.add_argument("--send", action="store_true",
                        help="доставить анонс (только один раз)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="переотправить только явные отказы Telegram")
    args = parser.parse_args()

    bot.init_db()
    if args.preview:
        preview(args.username or bot.BOT_NAME)
        return 0
    if args.freeze_only:
        print(f"Получателей в снимке: {bot.freeze_announcement_recipients()}")
        print_status()
        return 0
    if args.send or args.retry_failed:
        return asyncio.run(deliver(args.retry_failed))
    print_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
