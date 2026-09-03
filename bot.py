import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message


# =========================
# CONFIG
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://www.ishnk.ru/2025/site/schedule/group/508"
).rstrip("/")

GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")

TZ = ZoneInfo(TIMEZONE_NAME)

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR = DATA_DIR / "images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CACHE_FILE = DATA_DIR / "schedule_cache.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в .env")


# =========================
# FILE STORAGE
# =========================

def load_json(path: Path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Ошибка чтения %s", path)
        return default


def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    tmp.replace(path)


def load_subscribers():
    return set(
        str(x)
        for x in load_json(SUBSCRIBERS_FILE, [])
    )


def save_subscribers(subscribers):
    save_json(
        SUBSCRIBERS_FILE,
        sorted(subscribers)
    )


# =========================
# DATE / URL
# =========================

def today():
    return datetime.now(TZ).date()


def build_schedule_url(date_value):
    """
    Например:

    https://www.ishnk.ru/2025/site/schedule/group/508/2026-09-03
    """

    return f"{SCHEDULE_URL}/{date_value.isoformat()}"


def parse_user_date(value):
    value = value.strip()

    formats = [
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d-%m-%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    return None


# =========================
# HTTP
# =========================

async def fetch_html(date_value):
    url = build_schedule_url(date_value)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }

    timeout = aiohttp.ClientTimeout(total=30)

    logger.info("Получаю расписание: %s", url)

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        async with session.get(url) as response:
            response.raise_for_status()

            html = await response.text(
                encoding="utf-8",
                errors="ignore"
            )

            logger.info(
                "Получено %s байт, HTTP %s",
                len(html),
                response.status
            )

            return html


# =========================
# PARSER
# =========================

TIME_RE = re.compile(
    r"\b([01]?\d|2[0-3]):[0-5]\d\b"
)


def clean_text(text):
    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


def parse_schedule(html):
    soup = BeautifulSoup(html, "html.parser")

    rows = []

    # Сначала пробуем таблицы.
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["th", "td"])
            ]

            cells = [
                x for x in cells
                if x
            ]

            if cells:
                rows.append(cells)

    # Если таблиц нет — пробуем блоки.
    if not rows:
        for element in soup.find_all(
            ["div", "li", "article", "section"]
        ):
            text = clean_text(
                element.get_text(" ", strip=True)
            )

            if not text:
                continue

            if TIME_RE.search(text):
                rows.append([text])

    # Удаляем дубликаты.
    result = []
    seen = set()

    for row in rows:
        key = " | ".join(row)

        if key in seen:
            continue

        seen.add(key)
        result.append(row)

    return result


def schedule_to_text(rows, date_value):
    if not rows:
        return (
            f"📅 {date_value.strftime('%d.%m.%Y')}\n"
            f"🎓 Группа: {GROUP_NAME}\n\n"
            "Расписание не найдено."
        )

    lines = [
        f"📅 {date_value.strftime('%d.%m.%Y')}",
        f"🎓 Группа: {GROUP_NAME}",
        ""
    ]

    for row in rows:
        lines.append(" | ".join(row))

    return "\n".join(lines)


# =========================
# IMAGE
# =========================

def get_font(size):
    possible_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]

    for font_path in possible_fonts:
        if Path(font_path).exists():
            return ImageFont.truetype(
                font_path,
                size
            )

    return ImageFont.load_default()


def render_schedule(rows, date_value):
    title_font = get_font(32)
    normal_font = get_font(22)

    lines = [
        f"Расписание — {GROUP_NAME}",
        date_value.strftime("%d.%m.%Y"),
        ""
    ]

    if not rows:
        lines.append("Расписание не найдено.")
    else:
        for row in rows:
            lines.append(" | ".join(row))

    padding = 40
    line_height = 34

    width = 1400
    height = max(
        300,
        padding * 2 + len(lines) * line_height
    )

    image = Image.new(
        "RGB",
        (width, height),
        "white"
    )

    draw = ImageDraw.Draw(image)

    y = padding

    for index, line in enumerate(lines):

        font = (
            title_font
            if index == 0
            else normal_font
        )

        draw.text(
            (padding, y),
            line,
            fill="black",
            font=font
        )

        y += line_height

    filename = (
        IMAGE_DIR /
        f"schedule_{date_value.isoformat()}.png"
    )

    image.save(filename)

    return filename


# =========================
# GET SCHEDULE
# =========================

async def get_schedule(date_value):
    html = await fetch_html(date_value)

    rows = parse_schedule(html)

    text = schedule_to_text(
        rows,
        date_value
    )

    image = render_schedule(
        rows,
        date_value
    )

    return {
        "date": date_value,
        "rows": rows,
        "text": text,
        "image": image,
        "html": html,
    }


# =========================
# BOT
# =========================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Бот расписания группы {GROUP_NAME}\n\n"
        "/schedule — расписание на сегодня\n"
        "/scheduletext — расписание текстом\n"
        "/tomorrow — расписание на завтра\n"
        "/date 04.09.2026 — расписание на дату\n"
        "/subscribe — получать обновления\n"
        "/unsubscribe — отключить обновления\n"
        "/checknow — проверить расписание"
    )


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):
    try:
        data = await get_schedule(today())

        await message.answer_photo(
            FSInputFile(data["image"]),
            caption=(
                f"📅 {data['date'].strftime('%d.%m.%Y')}\n"
                f"🎓 {GROUP_NAME}"
            )
        )

    except Exception as e:
        logger.exception("Ошибка /schedule")

        await message.answer(
            f"❌ Не удалось получить расписание.\n"
            f"{e}"
        )


@dp.message(Command("scheduletext"))
async def cmd_scheduletext(message: Message):
    try:
        data = await get_schedule(today())

        text = data["text"]

        if len(text) > 4000:
            text = text[:3900] + "\n\n..."

        await message.answer(text)

    except Exception as e:
        logger.exception("Ошибка /scheduletext")

        await message.answer(
            f"❌ Не удалось получить расписание.\n"
            f"{e}"
        )


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message):
    date_value = today() + timedelta(days=1)

    try:
        data = await get_schedule(date_value)

        await message.answer_photo(
            FSInputFile(data["image"]),
            caption=(
                f"📅 {date_value.strftime('%d.%m.%Y')}\n"
                f"🎓 {GROUP_NAME}"
            )
        )

    except Exception as e:
        logger.exception("Ошибка /tomorrow")

        await message.answer(
            f"❌ Не удалось получить расписание.\n"
            f"{e}"
        )


@dp.message(Command("date"))
async def cmd_date(message: Message):
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "/date 04.09.2026\n\n"
            "Также можно:\n"
            "/date 2026-09-04"
        )
        return

    date_value = parse_user_date(parts[1])

    if date_value is None:
        await message.answer(
            "❌ Неверная дата.\n"
            "Пример: /date 04.09.2026"
        )
        return

    try:
        data = await get_schedule(date_value)

        await message.answer_photo(
            FSInputFile(data["image"]),
            caption=(
                f"📅 {date_value.strftime('%d.%m.%Y')}\n"
                f"🎓 {GROUP_NAME}"
            )
        )

    except Exception as e:
        logger.exception("Ошибка /date")

        await message.answer(
            f"❌ Не удалось получить расписание.\n"
            f"{e}"
        )


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    subscribers = load_subscribers()

    subscribers.add(str(message.chat.id))

    save_subscribers(subscribers)

    await message.answer(
        "✅ Подписка включена.\n"
        "Я буду проверять изменения расписания."
    )


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    subscribers = load_subscribers()

    subscribers.discard(str(message.chat.id))

    save_subscribers(subscribers)

    await message.answer(
        "✅ Подписка отключена."
    )


@dp.message(Command("checknow"))
async def cmd_checknow(message: Message):
    try:
        await check_schedule(
            force=True
        )

        await message.answer(
            "✅ Проверка выполнена."
        )

    except Exception as e:
        logger.exception("Ошибка /checknow")

        await message.answer(
            f"❌ Ошибка проверки:\n{e}"
        )


# =========================
# AUTO CHECK
# =========================

def make_signature(data):
    content = data["text"]

    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


async def check_schedule(force=False):
    """
    Проверяем расписание на завтра.

    Если оно изменилось — отправляем подписчикам.
    """

    date_value = today() + timedelta(days=1)

    data = await get_schedule(date_value)

    signature = make_signature(data)

    cache = load_json(
        CACHE_FILE,
        {}
    )

    cache_key = date_value.isoformat()

    old_signature = cache.get(cache_key)

    if not force and old_signature == signature:
        logger.info(
            "Изменений нет: %s",
            date_value
        )
        return

    cache[cache_key] = signature

    # Не раздуваем cache бесконечно.
    if len(cache) > 30:
        keys = sorted(cache.keys())

        for key in keys[:-30]:
            del cache[key]

    save_json(
        CACHE_FILE,
        cache
    )

    subscribers = load_subscribers()

    if not subscribers:
        logger.info(
            "Подписчиков нет."
        )
        return

    caption = (
        "🔔 Обновление расписания!\n\n"
        f"📅 {date_value.strftime('%d.%m.%Y')}\n"
        f"🎓 {GROUP_NAME}"
    )

    for chat_id in subscribers:
        try:
            await bot.send_photo(
                chat_id=int(chat_id),
                photo=FSInputFile(
                    data["image"]
                ),
                caption=caption
            )

            await asyncio.sleep(0.05)

        except Exception:
            logger.exception(
                "Не удалось отправить %s",
                chat_id
            )


async def scheduler_loop():
    await asyncio.sleep(10)

    while True:
        try:
            await check_schedule()

        except Exception:
            logger.exception(
                "Ошибка автоматической проверки"
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# =========================
# MAIN
# =========================

async def main():
    logger.info(
        "Бот запускается..."
    )

    logger.info(
        "Группа: %s",
        GROUP_NAME
    )

    logger.info(
        "URL: %s/<YYYY-MM-DD>",
        SCHEDULE_URL
    )

    logger.info(
        "Часовой пояс: %s",
        TIMEZONE_NAME
    )

    logger.info(
        "Интервал проверки: %s сек.",
        CHECK_INTERVAL
    )

    asyncio.create_task(
        scheduler_loop()
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
