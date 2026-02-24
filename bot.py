import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import ClassVar
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

DEFAULT_BOT_TOKEN = "7824304844:AAHkM1wxBxztZthu14MIee27mv6uXZP4a2Y"
DEFAULT_SCHEDULE_URL = "http://www.ishnk.ru/2025/site/schedule/group/508/2026-02-25"
DEFAULT_GROUP_NAME = "ЭС7-24"
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_CHECK_INTERVAL_SECONDS = 1800
DEFAULT_DATE_FORMAT = "%d.%m.%Y"
DEFAULT_IGNORE_HTTPS_ERRORS = True


@dataclass
class Settings:
    token: str
    schedule_url: str
    group_name: str
    timezone: str = "Europe/Moscow"
    check_interval_seconds: int = 1800
    date_format: str = "%d.%m.%Y"
    ignore_https_errors: bool = True

    REQUIRED_ENV_VARS: ClassVar[tuple[str, ...]] = ("BOT_TOKEN", "SCHEDULE_URL", "GROUP_NAME")

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", DEFAULT_BOT_TOKEN)
        schedule_url = os.getenv("SCHEDULE_URL", DEFAULT_SCHEDULE_URL)
        group_name = os.getenv("GROUP_NAME", DEFAULT_GROUP_NAME)
        timezone = os.getenv("TIMEZONE", DEFAULT_TIMEZONE)
        check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", str(DEFAULT_CHECK_INTERVAL_SECONDS)))
        date_format = os.getenv("DATE_FORMAT", DEFAULT_DATE_FORMAT)
        ignore_https_errors = os.getenv("IGNORE_HTTPS_ERRORS", str(DEFAULT_IGNORE_HTTPS_ERRORS)).lower() in {"1", "true", "yes", "on"}
        return cls(
            token=token,
            schedule_url=schedule_url,
            group_name=group_name,
            timezone=timezone,
            check_interval_seconds=check_interval_seconds,
            date_format=date_format,
            ignore_https_errors=ignore_https_errors,
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

    def target_date_iso(self) -> str:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        target = now + timedelta(days=1)
        return target.strftime("%Y-%m-%d")

    def build_schedule_url(self) -> str:
        base_url = self.settings.schedule_url.strip().rstrip("/")
        target_iso = self.target_date_iso()

        if "{date}" in base_url:
            return base_url.format(date=target_iso)

        parts = base_url.split("/")
        if parts and len(parts[-1]) == 10 and parts[-1][4] == "-" and parts[-1][7] == "-":
            parts[-1] = target_iso
            return "/".join(parts)

        return f"{base_url}/{target_iso}"

    @staticmethod
    def _clean_line(text: str) -> str:
        return " ".join(text.replace("\xa0", " ").split())

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        normalized = text.lower().replace(" ", " ")
        normalized = normalized.replace("–", "-").replace("—", "-")
        return " ".join(normalized.split())

    def _contains_group_name(self, text: str) -> bool:
        return self._normalize_for_match(self.settings.group_name) in self._normalize_for_match(text)

    def _extract_schedule_from_html(self, html: str, target_iso: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        header = soup.select_one("div.header3")
        header_text = self._clean_line(header.get_text(" ", strip=True)) if header else ""

        cards = soup.select("div.card.myCard")
        if not cards:
            fallback_text = self._clean_line(soup.get_text(" ", strip=True))
            if not fallback_text:
                raise RuntimeError("Страница расписания загружена, но текст пустой.")
            if not self._contains_group_name(fallback_text):
                snippet = fallback_text[:500]
                raise RuntimeError(
                    f"Страница на {target_iso} загружена, но группа '{self.settings.group_name}' не найдена. "
                    f"Фрагмент страницы: {snippet}"
                )
            return fallback_text

        lines: list[str] = []
        if header_text:
            lines.append(header_text)

        for card in cards:
            card_header = card.select_one(".card-header")
            if card_header:
                lines.append(self._clean_line(card_header.get_text(" ", strip=True)))

            sections = card.select(".card-body .d-flex.flex-column")
            if not sections:
                sections = [card]

            for section in sections:
                section_lines = [self._clean_line(t) for t in section.stripped_strings]
                for value in section_lines:
                    if value:
                        lines.append(value)

            lines.append("")

        result = "\n".join(line for line in lines if line is not None).strip()
        if not self._contains_group_name(result):
            snippet = result[:500]
            raise RuntimeError(
                f"Страница на {target_iso} загружена, но группа '{self.settings.group_name}' не найдена. "
                f"Фрагмент расписания: {snippet}"
            )
        return result

    async def fetch_schedule_text(self) -> str:
        target_date = self.target_date_string()
        target_iso = self.target_date_iso()
        target_url = self.build_schedule_url()
        logger.info(
            "Загружаю расписание для группы %s на %s (%s)",
            self.settings.group_name,
            target_date,
            target_url,
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(ignore_https_errors=self.settings.ignore_https_errors)
            page = await context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(1500)

            html = await page.content()
            await context.close()
            await browser.close()

        return self._extract_schedule_from_html(html, target_iso)

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
            error_text = str(exc)
            if "ERR_CERT_AUTHORITY_INVALID" in error_text:
                error_text = (
                    "Ошибка SSL сертификата сайта расписания. "
                    "Включите IGNORE_HTTPS_ERRORS=true (по умолчанию уже включено) "
                    "или проверьте сертификат сайта."
                )
            for chat_id in list(subscriber_store.subscribers):
                await bot.send_message(chat_id, f"❌ Ошибка проверки: {error_text[:3500]}")

        await asyncio.sleep(watcher.settings.check_interval_seconds)


async def main():
    env_path = Path(__file__).with_name(".env")
    load_dotenv(dotenv_path=env_path)
    logger.info("Запуск с готовыми настройками: %s | группа %s", os.getenv("SCHEDULE_URL", DEFAULT_SCHEDULE_URL), os.getenv("GROUP_NAME", DEFAULT_GROUP_NAME))
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
