import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://ishnk.ru"
).strip()

GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24").strip()
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow").strip()

try:
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))
except ValueError:
    CHECK_INTERVAL = 1800

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CACHE_FILE = DATA_DIR / "schedule_cache.json"
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не указан. Добавь токен в файл .env"
    )


# ============================================================
# SUBSCRIBERS
# ============================================================

def load_subscribers() -> set[int]:
    if not SUBSCRIBERS_FILE.exists():
        return set()

    try:
        data = json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))

        if not isinstance(data, list):
            return set()

        return {int(x) for x in data}

    except Exception as e:
        logger.error("Ошибка чтения subscribers.json: %s", e)
        return set()


def save_subscribers(subscribers: set[int]) -> None:
    SUBSCRIBERS_FILE.write_text(
        json.dumps(
            sorted(subscribers),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


subscribers = load_subscribers()


# ============================================================
# CACHE
# ============================================================

def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}

    try:
        return json.loads(
            CACHE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_cache(data: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# PLAYWRIGHT
# ============================================================

class ScheduleBrowser:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None
        self.lock = asyncio.Lock()

    async def start(self):
        logger.info("Запускаю Playwright...")

        self.playwright = await async_playwright().start()

        # Используем Chromium, который устанавливается Dockerfile.
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        self.page = await self.browser.new_page(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
        )

        self.page.set_default_timeout(30000)

        logger.info("Playwright запущен")

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()

            if self.playwright:
                await self.playwright.stop()

        except Exception as e:
            logger.error("Ошибка закрытия Playwright: %s", e)

    async def get_html(self) -> str:
        async with self.lock:
            if not self.page:
                await self.start()

            try:
                logger.info("Открываю: %s", SCHEDULE_URL)

                await self.page.goto(
                    SCHEDULE_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                # Даём JS сайта немного времени отрисовать расписание.
                await self.page.wait_for_timeout(2500)

                html = await self.page.content()

                logger.info(
                    "Получена страница: %s символов",
                    len(html),
                )

                return html

            except PlaywrightTimeoutError:
                logger.error("Таймаут загрузки сайта")
                raise

            except Exception:
                logger.exception("Ошибка загрузки сайта")
                raise


schedule_browser = ScheduleBrowser()


# ============================================================
# PARSER
# ============================================================

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def find_date(text: str) -> Optional[str]:
    """
    Ищет дату вида:
    03.09.2026
    3.09.2026
    """

    match = re.search(
        r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b",
        text,
    )

    if not match:
        return None

    day, month, year = match.groups()

    return f"{int(day):02d}.{int(month):02d}.{year}"


def find_time(text: str) -> Optional[str]:
    """
    Ищет время:
    08:30
    8:30
    """

    match = re.search(
        r"\b(\d{1,2}):(\d{2})\b",
        text,
    )

    if not match:
        return None

    hour, minute = match.groups()

    return f"{int(hour):02d}:{minute}"


def clean_lesson_name(text: str) -> str:
    text = normalize_text(text)

    # Убираем номер пары в начале.
    text = re.sub(
        r"^\s*\d+\s*[.)-]?\s*",
        "",
        text,
    )

    # Убираем время.
    text = re.sub(
        r"\b\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}\b",
        "",
        text,
    )

    return normalize_text(text)


def parse_schedule(html: str) -> list[dict]:
    """
    Универсальный парсер.

    Сначала пытается найти карточки/строки расписания.
    Если структура сайта немного поменялась, берёт текст
    элементов страницы и пытается выделить пары.
    """

    soup = BeautifulSoup(html, "html.parser")

    result = []

    # --------------------------------------------------------
    # Вариант 1. Таблица
    # --------------------------------------------------------

    tables = soup.find_all("table")

    for table in tables:
        rows = table.find_all("tr")

        for row in rows:
            cells = [
                normalize_text(cell.get_text(" ", strip=True))
                for cell in row.find_all(["td", "th"])
            ]

            cells = [x for x in cells if x]

            if not cells:
                continue

            joined = " | ".join(cells)

            if not any(
                char.isdigit()
                for char in joined
            ):
                continue

            lesson_time = find_time(joined)

            if lesson_time:
                result.append(
                    {
                        "time": lesson_time,
                        "text": joined,
                    }
                )

    # --------------------------------------------------------
    # Вариант 2. Карточки
    # --------------------------------------------------------

    if not result:
        selectors = [
            ".card",
            ".card-body",
            ".schedule",
            ".lesson",
            ".pair",
            ".subject",
            "article",
        ]

        elements = []

        for selector in selectors:
            found = soup.select(selector)

            if found:
                elements.extend(found)

        seen = set()

        for element in elements:
            text = normalize_text(
                element.get_text(" ", strip=True)
            )

            if not text:
                continue

            if text in seen:
                continue

            seen.add(text)

            lesson_time = find_time(text)

            if lesson_time:
                result.append(
                    {
                        "time": lesson_time,
                        "text": text,
                    }
                )

    # --------------------------------------------------------
    # Удаляем дубликаты
    # --------------------------------------------------------

    unique = []
    seen = set()

    for item in result:
        key = (
            item.get("time", ""),
            item.get("text", ""),
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    # Сортировка по времени
    unique.sort(
        key=lambda x: x.get("time", "99:99")
    )

    return unique


def schedule_to_text(
    schedule: list[dict],
    date_text: Optional[str] = None,
) -> str:

    if not schedule:
        return (
            f"📚 <b>Расписание {GROUP_NAME}</b>\n\n"
            f"📅 {date_text or 'Дата не определена'}\n\n"
            "Расписание не найдено."
        )

    lines = [
        f"📚 <b>Расписание {GROUP_NAME}</b>",
        "",
    ]

    if date_text:
        lines.extend(
            [
                f"📅 <b>{date_text}</b>",
                "",
            ]
        )

    for index, lesson in enumerate(schedule, start=1):
        time = lesson.get("time", "—")
        text = lesson.get("text", "—")

        lines.append(
            f"<b>{index}. {time}</b> — {text}"
        )

    return "\n".join(lines)


# ============================================================
# IMAGE
# ============================================================

def get_font(size: int, bold: bool = False):
    candidates = []

    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def create_schedule_image(
    schedule: list[dict],
    date_text: Optional[str] = None,
) -> Path:

    width = 1200
    padding = 50

    title_font = get_font(42, bold=True)
    date_font = get_font(28, bold=True)
    lesson_font = get_font(27, bold=False)
    time_font = get_font(27, bold=True)

    card_height = 95
    gap = 18

    height = (
        padding
        + 70
        + 50
        + len(schedule) * (card_height + gap)
        + padding
    )

    if not schedule:
        height = 350

    image = Image.new(
        "RGB",
        (width, height),
        "white",
    )

    draw = ImageDraw.Draw(image)

    y = padding

    draw.text(
        (padding, y),
        f"Расписание {GROUP_NAME}",
        font=title_font,
        fill="black",
    )

    y += 70

    if date_text:
        draw.text(
            (padding, y),
            date_text,
            font=date_font,
            fill="black",
        )

    y += 55

    if not schedule:
        draw.text(
            (padding, y),
            "Расписание не найдено",
            font=lesson_font,
            fill="black",
        )

    else:
        for index, lesson in enumerate(schedule, start=1):
            time = lesson.get("time", "—")
            text = lesson.get("text", "—")

            x1 = padding
            x2 = width - padding
            y1 = y
            y2 = y + card_height

            draw.rounded_rectangle(
                (x1, y1, x2, y2),
                radius=20,
                outline="black",
                width=2,
            )

            draw.text(
                (x1 + 25, y1 + 27),
                f"{index}. {time}",
                font=time_font,
                fill="black",
            )

            text_x = x1 + 240

            # Ограничиваем длину строки.
            display_text = text

            while (
                draw.textbbox(
                    (0, 0),
                    display_text,
                    font=lesson_font,
                )[2]
                > x2 - text_x - 25
                and len(display_text) > 10
            ):
                display_text = display_text[:-4] + "..."

            draw.text(
                (text_x, y1 + 27),
                display_text,
                font=lesson_font,
                fill="black",
            )

            y += card_height + gap

    filename = (
        datetime.now().strftime(
            "schedule_%Y%m%d_%H%M%S.png"
        )
    )

    path = IMAGES_DIR / filename

    image.save(
        path,
        "PNG",
        optimize=True,
    )

    return path


# ============================================================
# CURRENT SCHEDULE
# ============================================================

async def get_schedule():
    html = await schedule_browser.get_html()

    schedule = parse_schedule(html)

    # Пытаемся определить дату.
    soup = BeautifulSoup(html, "html.parser")

    page_text = normalize_text(
        soup.get_text(" ", strip=True)
    )

    date_text = find_date(page_text)

    return schedule, date_text


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

dp = Dispatcher()


async def send_schedule(
    message: Message,
    image: bool = True,
):
    try:
        schedule, date_text = await get_schedule()

        if image:
            image_path = create_schedule_image(
                schedule,
                date_text,
            )

            await message.answer_photo(
                photo=FSInputFile(image_path),
                caption=(
                    f"📚 <b>{GROUP_NAME}</b>\n"
                    f"📅 {date_text or 'Дата не определена'}"
                ),
            )

        else:
            await message.answer(
                schedule_to_text(
                    schedule,
                    date_text,
                )
            )

    except Exception as e:
        logger.exception(
            "Ошибка получения расписания: %s",
            e,
        )

        await message.answer(
            "❌ Не удалось получить расписание.\n"
            "Попробуйте ещё раз через несколько минут."
        )


# ============================================================
# COMMANDS
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"📚 <b>Бот расписания {GROUP_NAME}</b>\n\n"
        "Доступные команды:\n"
        "/schedule — расписание картинкой\n"
        "/scheduletext — расписание текстом\n"
        "/subscribe — получать обновления\n"
        "/unsubscribe — отключить обновления\n"
        "/checknow — проверить расписание сейчас\n"
        "/date — показать текущую дату\n\n"
        "Бот автоматически проверяет расписание."
    )


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):
    await send_schedule(
        message,
        image=True,
    )


@dp.message(Command("scheduletext"))
async def cmd_schedule_text(message: Message):
    await send_schedule(
        message,
        image=False,
    )


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    chat_id = message.chat.id

    subscribers.add(chat_id)
    save_subscribers(subscribers)

    await message.answer(
        "✅ Вы подписались на обновления расписания."
    )


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    chat_id = message.chat.id

    subscribers.discard(chat_id)
    save_subscribers(subscribers)

    await message.answer(
        "🔕 Подписка отключена."
    )


@dp.message(Command("date"))
async def cmd_date(message: Message):
    now = datetime.now()

    await message.answer(
        f"📅 Сегодня: <b>{now.strftime('%d.%m.%Y')}</b>\n"
        f"🕐 Время сервера: <b>{now.strftime('%H:%M:%S')}</b>"
    )


@dp.message(Command("checknow"))
async def cmd_check_now(message: Message):
    await message.answer(
        "🔎 Проверяю расписание..."
    )

    await send_schedule(
        message,
        image=True,
    )


# ============================================================
# AUTOMATIC CHECK
# ============================================================

def schedule_signature(
    schedule: list[dict],
    date_text: Optional[str],
) -> str:

    payload = {
        "date": date_text,
        "schedule": schedule,
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )


async def broadcast_schedule(
    schedule: list[dict],
    date_text: Optional[str],
):
    if not subscribers:
        logger.info(
            "Нет подписчиков — рассылка не нужна."
        )
        return

    image_path = create_schedule_image(
        schedule,
        date_text,
    )

    caption = (
        f"📚 <b>Обновление расписания</b>\n"
        f"Группа: <b>{GROUP_NAME}</b>\n"
        f"📅 {date_text or 'Дата не определена'}"
    )

    failed = []

    for chat_id in list(subscribers):
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(image_path),
                caption=caption,
            )

            await asyncio.sleep(0.05)

        except Exception as e:
            logger.warning(
                "Не удалось отправить %s: %s",
                chat_id,
                e,
            )

            failed.append(chat_id)

    # Удаляем чаты, куда Telegram больше не позволяет писать.
    for chat_id in failed:
        subscribers.discard(chat_id)

    if failed:
        save_subscribers(subscribers)


async def automatic_checker():
    logger.info(
        "Автоматическая проверка запущена. Интервал: %s сек.",
        CHECK_INTERVAL,
    )

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)

            schedule, date_text = await get_schedule()

            signature = schedule_signature(
                schedule,
                date_text,
            )

            cache = load_cache()

            old_signature = cache.get("signature")

            if old_signature is None:
                logger.info(
                    "Создаю первоначальный кеш расписания."
                )

                save_cache(
                    {
                        "signature": signature,
                        "date": date_text,
                    }
                )

                continue

            if signature != old_signature:
                logger.info(
                    "Обнаружено изменение расписания."
                )

                save_cache(
                    {
                        "signature": signature,
                        "date": date_text,
                    }
                )

                await broadcast_schedule(
                    schedule,
                    date_text,
                )

            else:
                logger.info(
                    "Изменений расписания нет."
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Ошибка автоматической проверки."
            )

            # При ошибке не прекращаем работу бота.
            await asyncio.sleep(60)


# ============================================================
# MAIN
# ============================================================

async def main():
    logger.info("========================================")
    logger.info("Запуск бота")
    logger.info("Группа: %s", GROUP_NAME)
    logger.info("URL: %s", SCHEDULE_URL)
    logger.info("Подписчиков: %s", len(subscribers))
    logger.info("========================================")

    await schedule_browser.start()

    checker_task = asyncio.create_task(
        automatic_checker()
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:
        checker_task.cancel()

        try:
            await checker_task
        except asyncio.CancelledError:
            pass

        await schedule_browser.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
