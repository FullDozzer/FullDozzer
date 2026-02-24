import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import ClassVar
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import wrap
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
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

def _clean_text_line(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _shorten_teacher(name: str) -> str:
    words = [w for w in _clean_text_line(name).split() if w]
    if len(words) < 2:
        return " ".join(words)
    initials = " ".join(f"{w[0]}." for w in words[1:] if w)
    return f"{words[0]} {initials}".strip()


def _extract_pair_header(card_header) -> tuple[str, str]:
    title_tag = card_header.select_one("span.h3")
    pair_title = f"{_clean_text_line(title_tag.get_text(strip=True))} пара" if title_tag else "Пара"

    time_tag = card_header.select_one("span.h4")
    pair_time = ""
    if time_tag:
        raw_time = _clean_text_line(time_tag.get_text(" ", strip=True))
        nums = re.findall(r"\d{1,2}", raw_time)
        if len(nums) >= 4:
            pair_time = f"{int(nums[0]):02d}:{int(nums[1]):02d}–{int(nums[2]):02d}:{int(nums[3]):02d}"
        else:
            pair_time = raw_time.replace("-", "–")
    return pair_title, pair_time


def _extract_subject(block) -> str:
    small = block.select_one("div.h5 small")
    if small:
        return _clean_text_line(small.get_text(" ", strip=True))
    fallback = block.select_one("div.h5 b") or block.select_one("div.h5")
    return _clean_text_line(fallback.get_text(" ", strip=True)) if fallback else ""


def _extract_room(block) -> str:
    for row in block.select(".d-flex.justify-content-between"):
        text = _clean_text_line(row.get_text(" ", strip=True)).lower()
        if "ауд" in text:
            for span in row.select("span"):
                span_text = _clean_text_line(span.get_text(" ", strip=True)).lower()
                if "ауд" not in span_text:
                    continue
                tag = span.select_one("span.h5")
                if tag:
                    return _clean_text_line(tag.get_text(" ", strip=True))
    return ""


def _extract_teacher(block) -> str:
    for row in block.select(".d-flex.justify-content-between"):
        text = _clean_text_line(row.get_text(" ", strip=True)).lower()
        if "преп" in text:
            for span in row.select("span"):
                span_text = _clean_text_line(span.get_text(" ", strip=True)).lower()
                if "преп" not in span_text:
                    continue
                tag = span.select_one("span.h5")
                if tag:
                    return _shorten_teacher(tag.get_text(" ", strip=True))
    return ""


def parse_schedule(html: str) -> str:
    data = parse_schedule_data(html)
    out: list[str] = [data["title"], f"Группа: {data['group']}", "", "━━━━━━━━━━━━━━", ""]

    for pair in data["pairs"]:
        out.append(f"{pair['number']} пара")
        if pair["time"]:
            out.append(f"🕐 {pair['time']}")

        entries = pair["entries"]
        if len(entries) > 1:
            for idx, entry in enumerate(entries):
                branch = "└─" if idx == len(entries) - 1 else "├─"
                out.append("")
                out.append(f"{branch} Подгруппа {entry.get('subgroup', idx + 1)}")
                if entry.get("subject"):
                    out.append(f"📖 {entry['subject']}")
                if entry.get("room"):
                    out.append(f"🏫 {entry['room']}")
                if entry.get("teacher"):
                    out.append(f"👩‍🏫 {entry['teacher']}")
        else:
            entry = entries[0]
            if entry.get("subject"):
                out.append(f"📖 {entry['subject']}")
            if entry.get("room"):
                out.append(f"🏫 {entry['room']}")
            if entry.get("teacher"):
                out.append(f"👩‍🏫 {entry['teacher']}")

        out.append("")
        out.append("━━━━━━━━━━━━━━")
        out.append("")

    return "\n".join(out).strip()


def parse_schedule_data(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    header = soup.select_one("div.header3")
    header_text = _clean_text_line(header.get_text(" ", strip=True)) if header else "Расписание"

    group = DEFAULT_GROUP_NAME
    if "группы" in header_text.lower() and "на " in header_text.lower():
        try:
            group = _clean_text_line(header_text.split("группы", 1)[1].split("на", 1)[0])
        except Exception:
            pass

    day_date = "завтра"
    if " на " in header_text.lower():
        try:
            day_date = _clean_text_line(header_text.lower().split("на", 1)[1]).replace("года", "").strip(" ,")
        except Exception:
            pass

    cards = soup.select("div.card.myCard")
    if not cards:
        raw_text = _clean_text_line(soup.get_text(" ", strip=True))
        if "404 not found" in raw_text.lower():
            raise RuntimeError("Расписание не найдено (404). Возможно, на эту дату ещё не опубликовано.")
        if not raw_text:
            raise RuntimeError("Страница расписания загружена, но карточки пар не найдены.")
        return {"title": "📚 Расписание", "group": group, "pairs": []}

    pairs: list[dict] = []

    for card in cards:
        card_header = card.select_one("div.card-header")
        if not card_header:
            continue

        pair_title, pair_time = _extract_pair_header(card_header)
        pair_number = pair_title.replace(" пара", "").strip()

        subgroups = [
            ("1", card.select_one("div.subGroup1")),
            ("2", card.select_one("div.subGroup2")),
        ]
        present_subgroups = [(n, b) for n, b in subgroups if b is not None]

        entries: list[dict] = []

        if present_subgroups:
            for num, block in present_subgroups:
                entries.append(
                    {
                        "subgroup": num,
                        "subject": _extract_subject(block),
                        "room": _extract_room(block),
                        "teacher": _extract_teacher(block),
                    }
                )
        else:
            body = card.select_one("div.card-body") or card
            entries.append(
                {
                    "subject": _extract_subject(body),
                    "room": _extract_room(body),
                    "teacher": _extract_teacher(body),
                }
            )

        pairs.append({"number": pair_number, "time": pair_time, "entries": entries})

    return {
        "title": f"📚 Расписание на {day_date}",
        "group": group,
        "pairs": pairs,
    }


def _load_font(size: int, bold: bool = False):
    env_font = os.getenv("SCHEDULE_FONT_PATH")
    candidates = [env_font] if env_font else []

    if bold:
        candidates.extend(
            [
                "DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/tahomabd.ttf",
                "C:/Windows/Fonts/segoeuib.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/tahoma.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
            ]
        )

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue

    raise RuntimeError(
        "Не найден шрифт с поддержкой кириллицы для рендера изображения. "
        "Укажите путь в SCHEDULE_FONT_PATH (например, C:/Windows/Fonts/arial.ttf)."
    )


def render_schedule_image(schedule_data: dict, output_path: str) -> str:
    bg_color = "#0f172a"
    card_color = "#1e293b"
    accent = "#38bdf8"
    text_main = "#f1f5f9"
    text_muted = "#94a3b8"

    width = 1000
    padding = 50
    card_radius = 25
    line_spacing = 10
    block_spacing = 18

    title_font = _load_font(46, bold=True)
    sub_font = _load_font(28)
    header_font = _load_font(32, bold=True)
    body_font = _load_font(28)

    temp_img = Image.new("RGB", (width, 3000), bg_color)
    draw = ImageDraw.Draw(temp_img)

    def wrapped_lines(text: str, line_width: int = 34) -> list[str]:
        return wrap(text or "", width=line_width) or ["-"]

    def line_height(font) -> int:
        return draw.textbbox((0, 0), "Ag", font=font)[3] + line_spacing

    y = padding
    y += draw.textbbox((0, 0), schedule_data["title"], font=title_font)[3] + 14
    y += draw.textbbox((0, 0), f"Группа {schedule_data['group']}", font=sub_font)[3] + 28

    for pair in schedule_data["pairs"]:
        card_height = 36 + line_height(header_font)
        for entry in pair["entries"]:
            card_height += len(wrapped_lines(entry.get("subject", ""))) * line_height(body_font)
            card_height += (line_height(body_font) * 2) + block_spacing
            if entry.get("subgroup"):
                card_height += line_height(body_font)
        card_height += 24
        y += card_height + 24

    height = y + padding
    img = Image.new("RGB", (width, max(height, 700)), bg_color)
    draw = ImageDraw.Draw(img)

    y = padding
    draw.text((padding, y), schedule_data["title"], font=title_font, fill=text_main)
    y += draw.textbbox((0, 0), schedule_data["title"], font=title_font)[3] + 10
    draw.text((padding, y), f"Группа {schedule_data['group']}", font=sub_font, fill=text_muted)
    y += draw.textbbox((0, 0), f"Группа {schedule_data['group']}", font=sub_font)[3] + 30

    for pair in schedule_data["pairs"]:
        card_top = y
        card_left = padding
        card_right = width - padding

        content_y = card_top + 26
        header_text = f"{pair['number']} пара   {pair['time']}"
        draw.text((card_left + 30, content_y), header_text, font=header_font, fill=accent)
        content_y += line_height(header_font) + 8

        for idx, entry in enumerate(pair["entries"]):
            if entry.get("subgroup"):
                branch = "-"
                draw.text(
                    (card_left + 40, content_y),
                    f"{branch} Подгруппа {entry['subgroup']}",
                    font=body_font,
                    fill=accent,
                )
                content_y += line_height(body_font)

            for line in wrapped_lines(entry.get("subject", "")):
                draw.text((card_left + 40, content_y), line, font=body_font, fill=text_main)
                content_y += line_height(body_font)

            draw.text((card_left + 40, content_y), f"Ауд.: {entry.get('room') or '-'}", font=body_font, fill=text_muted)
            content_y += line_height(body_font)
            draw.text((card_left + 40, content_y), f"Преп.: {entry.get('teacher') or '-'}", font=body_font, fill=text_muted)
            content_y += line_height(body_font) + 8

        card_bottom = content_y + 20
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            [card_left, card_top, card_right, card_bottom],
            radius=card_radius,
            fill=card_color,
        )
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        content_y = card_top + 26
        draw.text((card_left + 30, content_y), header_text, font=header_font, fill=accent)
        content_y += line_height(header_font) + 8

        for idx, entry in enumerate(pair["entries"]):
            if entry.get("subgroup"):
                branch = "-"
                draw.text(
                    (card_left + 40, content_y),
                    f"{branch} Подгруппа {entry['subgroup']}",
                    font=body_font,
                    fill=accent,
                )
                content_y += line_height(body_font)

            for line in wrapped_lines(entry.get("subject", "")):
                draw.text((card_left + 40, content_y), line, font=body_font, fill=text_main)
                content_y += line_height(body_font)

            draw.text((card_left + 40, content_y), f"Ауд.: {entry.get('room') or '-'}", font=body_font, fill=text_muted)
            content_y += line_height(body_font)
            draw.text((card_left + 40, content_y), f"Преп.: {entry.get('teacher') or '-'}", font=body_font, fill=text_muted)
            content_y += line_height(body_font) + 8

        y = card_bottom + 24

    img.save(output_path)
    return output_path


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

    @staticmethod
    def normalize_schedule_url(url: str) -> str:
        cleaned = url.strip()
        if "ishnk.ru/students/schedule" in cleaned:
            return DEFAULT_SCHEDULE_URL
        return cleaned

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", DEFAULT_BOT_TOKEN)
        schedule_url = cls.normalize_schedule_url(os.getenv("SCHEDULE_URL", DEFAULT_SCHEDULE_URL))
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

    async def fetch_schedule_data(self) -> tuple[str, dict]:
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
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(1500)

            html = await page.content()
            await context.close()
            await browser.close()

        if response and response.status == 404:
            raise RuntimeError(
                f"Расписание на {target_iso} не найдено (404) по ссылке: {target_url}. "
                "Скорее всего, расписание на эту дату ещё не опубликовано."
            )

        return parse_schedule(html), parse_schedule_data(html)

    async def check_for_updates(self) -> tuple[str, str, dict]:
        text, schedule_data = await self.fetch_schedule_data()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self.last_hash is None:
            self.last_hash = digest
            return "initial", text, schedule_data

        if digest != self.last_hash:
            self.last_hash = digest
            return "updated", text, schedule_data

        return "unchanged", text, schedule_data


def build_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📝 Получить расписание текстом")]],
        resize_keyboard=True,
    )


async def send_text_schedule(bot: Bot, chat_id: int, title: str, schedule_text: str):
    header = f"{title}\n\n"
    chunk_size = TELEGRAM_TEXT_LIMIT - len(header)
    chunks = [schedule_text[i : i + chunk_size] for i in range(0, len(schedule_text), chunk_size)] or ["(пусто)"]

    for index, chunk in enumerate(chunks, start=1):
        suffix = f"\n\nЧасть {index}/{len(chunks)}" if len(chunks) > 1 else ""
        await bot.send_message(chat_id, f"{header}{chunk}{suffix}")


async def send_schedule(bot: Bot, chat_id: int, title: str, schedule_text: str, schedule_data: dict):
    image_file = tempfile.NamedTemporaryFile(prefix="schedule_", suffix=".png", delete=False)
    image_path = image_file.name
    image_file.close()

    try:
        render_schedule_image(schedule_data, image_path)
        await bot.send_photo(chat_id, FSInputFile(image_path), caption=title)
        return
    except Exception:
        logger.exception("Не удалось отправить картинку расписания, отправляю текст")
    finally:
        Path(image_path).unlink(missing_ok=True)

    await send_text_schedule(bot, chat_id, title, schedule_text)


async def broadcast_schedule(bot: Bot, subscribers: set[int], title: str, schedule_text: str, schedule_data: dict):
    for chat_id in list(subscribers):
        try:
            await send_schedule(bot, chat_id, title, schedule_text, schedule_data)
        except Exception:
            logger.exception("Не удалось отправить расписание в чат %s", chat_id)


async def monitor_loop(bot: Bot, watcher: ScheduleWatcher, subscriber_store: SubscriberStore):
    while True:
        try:
            status, schedule_text, schedule_data = await watcher.check_for_updates()
            target_date = watcher.target_date_string()

            if not subscriber_store.subscribers:
                logger.info("Нет подписчиков для рассылки")
            elif status == "initial":
                await broadcast_schedule(
                    bot,
                    subscriber_store.subscribers,
                    f"📌 Текущее расписание на {target_date}",
                    schedule_text,
                    schedule_data,
                )
                logger.info("Отправлено текущее расписание подписчикам")
            elif status == "updated":
                await broadcast_schedule(
                    bot,
                    subscriber_store.subscribers,
                    f"🆕 Новое расписание на {target_date}",
                    schedule_text,
                    schedule_data,
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
    if "ishnk.ru/students/schedule" in os.getenv("SCHEDULE_URL", ""):
        logger.warning("Обнаружен старый SCHEDULE_URL (/students/schedule). Использую прямую ссылку группы: %s", settings.schedule_url)
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
            "Команды: /schedule, /checknow, /scheduletext, /subscribe, /unsubscribe\n"
            "Или нажмите кнопку ниже, чтобы получить текстовый вариант.",
            reply_markup=build_main_keyboard(),
        )

    @dp.message(Command("subscribe"))
    async def subscribe_handler(message: Message):
        subscriber_store.add(message.chat.id)
        await message.answer("✅ Вы подписаны на уведомления о новом расписании.")

    @dp.message(Command("unsubscribe"))
    async def unsubscribe_handler(message: Message):
        subscriber_store.remove(message.chat.id)
        await message.answer("🛑 Вы отписались от уведомлений.")

    @dp.message(Command("schedule"))
    async def schedule_handler(message: Message):
        try:
            schedule_text, schedule_data = await watcher.fetch_schedule_data()
            await send_schedule(bot, message.chat.id, f"📅 Расписание на {watcher.target_date_string()}", schedule_text, schedule_data)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ Ошибка получения расписания: {exc}")

    @dp.message(Command("scheduletext"))
    async def schedule_text_handler(message: Message):
        try:
            schedule_text, _ = await watcher.fetch_schedule_data()
            await send_text_schedule(bot, message.chat.id, f"📝 Текстовое расписание на {watcher.target_date_string()}", schedule_text)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ Ошибка получения текстового расписания: {exc}")

    @dp.message(lambda message: (message.text or "").strip() == "📝 Получить расписание текстом")
    async def schedule_text_button_handler(message: Message):
        try:
            schedule_text, _ = await watcher.fetch_schedule_data()
            await send_text_schedule(bot, message.chat.id, f"📝 Текстовое расписание на {watcher.target_date_string()}", schedule_text)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ Ошибка получения текстового расписания: {exc}")

    @dp.message(Command("checknow"))
    async def checknow_handler(message: Message):
        try:
            status, schedule_text, schedule_data = await watcher.check_for_updates()
            target_date = watcher.target_date_string()

            if status == "updated":
                await send_schedule(bot, message.chat.id, f"🆕 Новое расписание на {target_date}", schedule_text, schedule_data)
            elif status == "initial":
                await send_schedule(bot, message.chat.id, f"📌 Текущее расписание на {target_date}", schedule_text, schedule_data)
            else:
                await message.answer("Изменений не обнаружено.")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ Ошибка проверки: {exc}")

    asyncio.create_task(monitor_loop(bot, watcher, subscriber_store))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
