import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
SUBSCRIBERS_FILE = Path("subscribers.json")


@dataclass
class Settings:
    token: str
    schedule_url: str
    group_name: str
    timezone: str = "Europe/Moscow"
    check_interval_seconds: int = 1800
    date_format: str = "%d.%m.%Y"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ["BOT_TOKEN"]
        schedule_url = os.environ["SCHEDULE_URL"]
        group_name = os.environ["GROUP_NAME"]
        timezone = os.getenv("TIMEZONE", "Europe/Moscow")
        check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "1800"))
        date_format = os.getenv("DATE_FORMAT", "%d.%m.%Y")
        return cls(
            token=token,
            schedule_url=schedule_url,
            group_name=group_name,
            timezone=timezone,
            check_interval_seconds=check_interval_seconds,
            date_format=date_format,
        )


class SubscriberStore:
    def __init__(self, path: Path):
        self.path = path
        self.subscribers: set[int] = set()
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.subscribers = {int(chat_id) for chat_id in data}
        except Exception:
            logger.exception("Не удалось загрузить список подписчиков")

    def _save(self):
        self.path.write_text(
            json.dumps(sorted(self.subscribers), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, chat_id: int):
        if chat_id not in self.subscribers:
            self.subscribers.add(chat_id)
            self._save()

    def remove(self, chat_id: int):
        if chat_id in self.subscribers:
            self.subscribers.remove(chat_id)
            self._save()


class ScheduleWatcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_hash: str | None = None

    def target_date_string(self) -> str:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        target = now + timedelta(days=1)
        return target.strftime(self.settings.date_format)

    async def fetch_schedule_text(self) -> str:
        target_date = self.target_date_string()
        logger.info("Загружаю расписание для группы %s на %s", self.settings.group_name, target_date)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(self.settings.schedule_url, wait_until="domcontentloaded", timeout=90_000)

            group_option = page.get_by_role("option", name=self.settings.group_name)
            if await group_option.count() == 0:
                group_item = page.get_by_text(self.settings.group_name, exact=False)
                if await group_item.count() == 0:
                    await browser.close()
                    raise RuntimeError(
                        f"Не нашёл группу '{self.settings.group_name}'. Проверь GROUP_NAME и страницу."
                    )
                await group_item.first.click()
            else:
                await group_option.first.click()

            date_input = page.locator("input[type='date'], input[name*='date'], input[id*='date']")
            if await date_input.count() > 0:
                await date_input.first.fill(
                    (datetime.now(ZoneInfo(self.settings.timezone)) + timedelta(days=1)).strftime("%Y-%m-%d")
                )
                await date_input.first.press("Enter")
            else:
                date_field_by_text = page.get_by_placeholder("дата")
                if await date_field_by_text.count() > 0:
                    await date_field_by_text.first.fill(target_date)
                    await date_field_by_text.first.press("Enter")

            await page.wait_for_timeout(2500)
            html = await page.content()
            await browser.close()

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            clean = " ".join(soup.get_text(" ", strip=True).split())
            if not clean:
                raise RuntimeError("Страница загружена, но текст расписания не найден.")
            return clean

        table_text = "\n".join(row.get_text(" | ", strip=True) for row in table.find_all("tr"))
        if not table_text.strip():
            raise RuntimeError("Таблица расписания пустая.")
        return table_text

    async def check_for_updates(self) -> tuple[str, str]:
        text = await self.fetch_schedule_text()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self.last_hash is None:
            self.last_hash = digest
            return "initial", text

        if digest != self.last_hash:
            self.last_hash = digest
            return "updated", text

        return "unchanged", text


async def send_schedule(bot: Bot, chat_id: int, title: str, schedule_text: str):
    header = f"{title}\n\n"
    chunk_size = TELEGRAM_TEXT_LIMIT - len(header)
    chunks = [schedule_text[i : i + chunk_size] for i in range(0, len(schedule_text), chunk_size)] or ["(пусто)"]

    for index, chunk in enumerate(chunks, start=1):
        suffix = f"\n\nЧасть {index}/{len(chunks)}" if len(chunks) > 1 else ""
        await bot.send_message(chat_id, f"{header}{chunk}{suffix}")


async def broadcast_schedule(bot: Bot, subscribers: set[int], title: str, schedule_text: str):
    for chat_id in list(subscribers):
        try:
            await send_schedule(bot, chat_id, title, schedule_text)
        except Exception:
            logger.exception("Не удалось отправить расписание в чат %s", chat_id)


async def monitor_loop(bot: Bot, watcher: ScheduleWatcher, subscriber_store: SubscriberStore):
    while True:
        try:
            status, schedule_text = await watcher.check_for_updates()
            target_date = watcher.target_date_string()

            if not subscriber_store.subscribers:
                logger.info("Нет подписчиков для рассылки")
            elif status == "initial":
                await broadcast_schedule(
                    bot,
                    subscriber_store.subscribers,
                    f"📌 Текущее расписание на {target_date}",
                    schedule_text,
                )
                logger.info("Отправлено текущее расписание подписчикам")
            elif status == "updated":
                await broadcast_schedule(
                    bot,
                    subscriber_store.subscribers,
                    f"🆕 Новое расписание на {target_date}",
                    schedule_text,
                )
                logger.info("Отправлено новое расписание подписчикам")
            else:
                logger.info("Изменений не обнаружено")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка проверки расписания")
            for chat_id in list(subscriber_store.subscribers):
                await bot.send_message(chat_id, f"❌ Ошибка проверки: {exc}")

        await asyncio.sleep(watcher.settings.check_interval_seconds)


async def main():
    load_dotenv()
    settings = Settings.from_env()
    bot = Bot(token=settings.token)
    dp = Dispatcher()
    watcher = ScheduleWatcher(settings)
    subscriber_store = SubscriberStore(SUBSCRIBERS_FILE)

    @dp.message(Command("start"))
    async def start_handler(message: Message):
        subscriber_store.add(message.chat.id)
        await message.answer(
            "Привет! Я мониторю расписание колледжа.\n"
            f"Группа: {settings.group_name}\n"
            f"Проверка каждые {settings.check_interval_seconds // 60} мин.\n"
            "Вы подписаны на уведомления.\n"
            "Команды: /checknow, /subscribe, /unsubscribe"
        )

    @dp.message(Command("subscribe"))
    async def subscribe_handler(message: Message):
        subscriber_store.add(message.chat.id)
        await message.answer("✅ Вы подписаны на уведомления о новом расписании.")

    @dp.message(Command("unsubscribe"))
    async def unsubscribe_handler(message: Message):
        subscriber_store.remove(message.chat.id)
        await message.answer("🛑 Вы отписались от уведомлений.")

    @dp.message(Command("checknow"))
    async def checknow_handler(message: Message):
        try:
            status, schedule_text = await watcher.check_for_updates()
            target_date = watcher.target_date_string()

            if status == "updated":
                await send_schedule(bot, message.chat.id, f"🆕 Новое расписание на {target_date}", schedule_text)
            elif status == "initial":
                await send_schedule(bot, message.chat.id, f"📌 Текущее расписание на {target_date}", schedule_text)
            else:
                await message.answer("Изменений не обнаружено.")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ Ошибка проверки: {exc}")

    asyncio.create_task(monitor_loop(bot, watcher, subscriber_store))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
