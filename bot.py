import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont


# ============================================================
# НАСТРОЙКИ
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://ishnk.ru",
).strip()

GROUP_NAME = os.getenv(
    "GROUP_NAME",
    "ЭС7-24",
).strip()

CHECK_INTERVAL = int(
    os.getenv("CHECK_INTERVAL", "1800")
)

DATA_DIR = Path(
    os.getenv("DATA_DIR", "data")
)

DATA_DIR.mkdir(parents=True, exist_ok=True)

IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CACHE_FILE = DATA_DIR / "schedule_cache.json"


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# ПРОВЕРКА ТОКЕНА
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Проверь файл .env"
    )


# ============================================================
# ПОДПИСЧИКИ
# ============================================================

def load_subscribers() -> set[int]:
    if not SUBSCRIBERS_FILE.exists():
        return set()

    try:
        data = json.loads(
            SUBSCRIBERS_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, list):
            return set()

        return {int(x) for x in data}

    except Exception as e:
        logger.error(
            "Ошибка загрузки подписчиков: %s",
            e,
        )
        return set()


def save_subscribers(
    subscribers: set[int],
) -> None:

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
# КЭШ
# ============================================================

def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}

    try:
        return json.loads(
            CACHE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception as e:
        logger.error(
            "Ошибка загрузки кэша: %s",
            e,
        )
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
# HTTP-КЛИЕНТ
# ============================================================

class ScheduleClient:

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.lock = asyncio.Lock()

    async def start(self):

        if self.session is not None:
            return

        timeout = aiohttp.ClientTimeout(
            total=60,
            connect=20,
        )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }

        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        )

        logger.info("HTTP-клиент запущен")

    async def close(self):

        if self.session:
            await self.session.close()
            self.session = None

            logger.info(
                "HTTP-клиент закрыт"
            )

    async def get_html(self) -> str:

        async with self.lock:

            if self.session is None:
                await self.start()

            logger.info(
                "Запрашиваю сайт: %s",
                SCHEDULE_URL,
            )

            try:

                async with self.session.get(
                    SCHEDULE_URL,
                    allow_redirects=True,
                ) as response:

                    logger.info(
                        "HTTP статус: %s",
                        response.status,
                    )

                    response.raise_for_status()

                    html = await response.text(
                        errors="replace"
                    )

                    logger.info(
                        "Получено HTML: %d символов",
                        len(html),
                    )

                    return html

            except aiohttp.ClientError as e:

                logger.error(
                    "Ошибка HTTP-запроса: %s",
                    e,
                )

                raise

            except asyncio.TimeoutError:

                logger.error(
                    "Таймаут HTTP-запроса"
                )

                raise


schedule_client = ScheduleClient()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def normalize_text(text: str) -> str:

    text = text.replace(
        "\xa0",
        " ",
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def find_date(text: str) -> Optional[str]:

    patterns = [
        r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b",
        r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2})\b",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
        )

        if not match:
            continue

        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3))

        if year < 100:
            year += 2000

        if 1 <= day <= 31 and 1 <= month <= 12:

            return (
                f"{day:02d}."
                f"{month:02d}."
                f"{year:04d}"
            )

    return None


def find_time(text: str) -> Optional[str]:

    # 08:30
    match = re.search(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
        text,
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = match.group(2)

    return f"{hour:02d}:{minute}"


def extract_times(text: str) -> list[str]:

    matches = re.findall(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
        text,
    )

    result = []

    for hour, minute in matches:

        value = f"{int(hour):02d}:{minute}"

        if value not in result:
            result.append(value)

    return result


def clean_text(text: str) -> str:

    text = normalize_text(text)

    # Убираем лишние пробелы вокруг разделителей.
    text = re.sub(
        r"\s*[-–—]\s*",
        " — ",
        text,
    )

    return text


# ============================================================
# ПАРСЕР РАСПИСАНИЯ
# ============================================================

def parse_schedule(
    html: str,
) -> tuple[list[dict], Optional[str]]:

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # --------------------------------------------------------
    # Дата
    # --------------------------------------------------------

    full_text = normalize_text(
        soup.get_text(
            " ",
            strip=True,
        )
    )

    date_text = find_date(full_text)

    result: list[dict] = []

    # --------------------------------------------------------
    # 1. Пытаемся разобрать таблицы
    # --------------------------------------------------------

    for table in soup.find_all("table"):

        rows = table.find_all("tr")

        for row in rows:

            cells = []

            for cell in row.find_all(
                ["td", "th"]
            ):

                value = normalize_text(
                    cell.get_text(
                        " ",
                        strip=True,
                    )
                )

                if value:
                    cells.append(value)

            if not cells:
                continue

            row_text = " | ".join(cells)

            times = extract_times(
                row_text
            )

            if not times:
                continue

            result.append(
                {
                    "time": times[0],
                    "text": row_text,
                }
            )

    # --------------------------------------------------------
    # 2. Карточки / блоки
    # --------------------------------------------------------

    if not result:

        selectors = [
            ".card",
            ".card-body",
            ".lesson",
            ".pair",
            ".schedule",
            ".schedule-item",
            ".lesson-item",
            ".item",
            "article",
        ]

        candidates = []

        for selector in selectors:

            candidates.extend(
                soup.select(selector)
            )

        seen = set()

        for element in candidates:

            text = normalize_text(
                element.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            if text in seen:
                continue

            seen.add(text)

            times = extract_times(text)

            if not times:
                continue

            result.append(
                {
                    "time": times[0],
                    "text": text,
                }
            )

    # --------------------------------------------------------
    # 3. Если ничего не нашли — ищем элементы с временем
    # --------------------------------------------------------

    if not result:

        all_elements = soup.find_all(
            ["div", "li", "p", "span", "td"]
        )

        seen = set()

        for element in all_elements:

            text = normalize_text(
                element.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            if len(text) > 500:
                continue

            times = extract_times(text)

            if not times:
                continue

            time = times[0]

            key = (
                time,
                text,
            )

            if key in seen:
                continue

            seen.add(key)

            result.append(
                {
                    "time": time,
                    "text": text,
                }
            )

    # --------------------------------------------------------
    # Удаляем дубли
    # --------------------------------------------------------

    unique = []

    seen = set()

    for item in result:

        item["text"] = clean_text(
            item["text"]
        )

        key = (
            item["time"],
            item["text"],
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(item)

    # --------------------------------------------------------
    # Сортировка
    # --------------------------------------------------------

    unique.sort(
        key=lambda item: item.get(
            "time",
            "99:99",
        )
    )

    # --------------------------------------------------------
    # Если нашли слишком много мусора
    # --------------------------------------------------------

    if len(unique) > 30:

        logger.warning(
            "Парсер нашёл слишком много "
            "элементов: %d. Ограничиваю до 30.",
            len(unique),
        )

        unique = unique[:30]

    logger.info(
        "Распарсено занятий: %d",
        len(unique),
    )

    if date_text:
        logger.info(
            "Найдена дата: %s",
            date_text,
        )

    return unique, date_text


# ============================================================
# ПОЛУЧЕНИЕ РАСПИСАНИЯ
# ============================================================

async def get_schedule():

    html = await schedule_client.get_html()

    # Полезно для диагностики:
    # если сайт отдаёт страницу Cloudflare,
    # авторизацию или ошибку — увидим это в логах.
    if len(html) < 500:

        logger.warning(
            "HTML подозрительно маленький: %d символов",
            len(html),
        )

    schedule, date_text = parse_schedule(
        html
    )

    return schedule, date_text


# ============================================================
# ТЕКСТОВОЕ ПРЕДСТАВЛЕНИЕ
# ============================================================

def schedule_to_text(
    schedule: list[dict],
    date_text: Optional[str],
) -> str:

    lines = [
        f"📚 <b>Расписание {GROUP_NAME}</b>",
        "",
    ]

    if date_text:

        lines.append(
            f"📅 <b>{date_text}</b>"
        )

        lines.append("")

    if not schedule:

        lines.append(
            "❌ Расписание не найдено."
        )

        return "\n".join(lines)

    for index, lesson in enumerate(
        schedule,
        start=1,
    ):

        time = lesson.get(
            "time",
            "—",
        )

        text = lesson.get(
            "text",
            "—",
        )

        # Telegram HTML
        text = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        lines.append(
            f"<b>{index}. {time}</b> — {text}"
        )

    return "\n".join(lines)


# ============================================================
# ШРИФТЫ
# ============================================================

def get_font(
    size: int,
    bold: bool = False,
):

    if bold:

        paths = [
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/"
            "LiberationSans-Bold.ttf",
        ]

    else:

        paths = [
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/"
            "LiberationSans-Regular.ttf",
        ]

    for path in paths:

        if Path(path).exists():

            return ImageFont.truetype(
                path,
                size,
            )

    return ImageFont.load_default()


# ============================================================
# КАРТИНКА
# ============================================================

def create_schedule_image(
    schedule: list[dict],
    date_text: Optional[str],
) -> Path:

    width = 1200
    padding = 50

    title_font = get_font(
        42,
        bold=True,
    )

    date_font = get_font(
        28,
        bold=True,
    )

    lesson_font = get_font(
        25,
        bold=False,
    )

    time_font = get_font(
        25,
        bold=True,
    )

    card_height = 100
    gap = 18

    if schedule:

        height = (
            padding
            + 75
            + 55
            + len(schedule)
            * (card_height + gap)
            + padding
        )

    else:

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

    y += 75

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

        for index, lesson in enumerate(
            schedule,
            start=1,
        ):

            time = lesson.get(
                "time",
                "—",
            )

            text = lesson.get(
                "text",
                "—",
            )

            x1 = padding
            x2 = width - padding

            y1 = y
            y2 = y + card_height

            draw.rounded_rectangle(
                (
                    x1,
                    y1,
                    x2,
                    y2,
                ),
                radius=18,
                outline="black",
                width=2,
            )

            draw.text(
                (
                    x1 + 25,
                    y1 + 32,
                ),
                f"{index}. {time}",
                font=time_font,
                fill="black",
            )

            text_x = x1 + 250

            max_width = (
                x2
                - text_x
                - 25
            )

            display_text = text

            while len(display_text) > 10:

                bbox = draw.textbbox(
                    (0, 0),
                    display_text,
                    font=lesson_font,
                )

                text_width = (
                    bbox[2] - bbox[0]
                )

                if text_width <= max_width:
                    break

                display_text = (
                    display_text[:-4]
                    + "..."
                )

            draw.text(
                (
                    text_x,
                    y1 + 32,
                ),
                display_text,
                font=lesson_font,
                fill="black",
            )

            y += (
                card_height
                + gap
            )

    filename = (
        "schedule_"
        + datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )
        + ".png"
    )

    path = IMAGES_DIR / filename

    image.save(
        path,
        "PNG",
        optimize=True,
    )

    return path


# ============================================================
# ХЭШ РАСПИСАНИЯ
# ============================================================

def schedule_signature(
    schedule: list[dict],
    date_text: Optional[str],
) -> str:

    data = {
        "date": date_text,
        "schedule": schedule,
    }

    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# ============================================================
# ОТПРАВКА РАСПИСАНИЯ
# ============================================================

async def send_schedule(
    message: Message,
    image: bool = True,
):

    try:

        await message.answer(
            "🔎 Получаю расписание..."
        )

        schedule, date_text = (
            await get_schedule()
        )

        if image:

            image_path = (
                create_schedule_image(
                    schedule,
                    date_text,
                )
            )

            caption = (
                f"📚 <b>{GROUP_NAME}</b>"
            )

            if date_text:

                caption += (
                    f"\n📅 <b>{date_text}</b>"
                )

            await message.answer_photo(
                photo=FSInputFile(
                    image_path
                ),
                caption=caption,
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
            "Ошибка получения расписания"
        )

        await message.answer(
            "❌ Не удалось получить "
            "расписание.\n\n"
            "Сайт временно недоступен "
            "или изменил структуру."
        )


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

dp = Dispatcher()


# ============================================================
# /start
# ============================================================

@dp.message(CommandStart())
async def cmd_start(
    message: Message,
):

    await message.answer(
        f"📚 <b>Бот расписания {GROUP_NAME}</b>\n\n"
        "Команды:\n\n"
        "/schedule — расписание картинкой\n"
        "/scheduletext — расписание текстом\n"
        "/subscribe — подписаться на обновления\n"
        "/unsubscribe — отписаться\n"
        "/checknow — проверить расписание\n"
        "/date — текущая дата\n"
    )


# ============================================================
# /schedule
# ============================================================

@dp.message(Command("schedule"))
async def cmd_schedule(
    message: Message,
):

    await send_schedule(
        message,
        image=True,
    )


# ============================================================
# /scheduletext
# ============================================================

@dp.message(Command("scheduletext"))
async def cmd_schedule_text(
    message: Message,
):

    await send_schedule(
        message,
        image=False,
    )


# ============================================================
# /subscribe
# ============================================================

@dp.message(Command("subscribe"))
async def cmd_subscribe(
    message: Message,
):

    chat_id = message.chat.id

    if chat_id in subscribers:

        await message.answer(
            "ℹ️ Вы уже подписаны "
            "на обновления."
        )

        return

    subscribers.add(chat_id)

    save_subscribers(
        subscribers
    )

    await message.answer(
        "✅ Вы подписались "
        "на обновления расписания."
    )


# ============================================================
# /unsubscribe
# ============================================================

@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(
    message: Message,
):

    chat_id = message.chat.id

    if chat_id not in subscribers:

        await message.answer(
            "ℹ️ Вы не были подписаны."
        )

        return

    subscribers.discard(chat_id)

    save_subscribers(
        subscribers
    )

    await message.answer(
        "🔕 Вы отписались "
        "от обновлений."
    )


# ============================================================
# /date
# ============================================================

@dp.message(Command("date"))
async def cmd_date(
    message: Message,
):

    now = datetime.now()

    await message.answer(
        "📅 Сегодня: "
        f"<b>{now.strftime('%d.%m.%Y')}</b>\n"
        "🕐 Время: "
        f"<b>{now.strftime('%H:%M:%S')}</b>"
    )


# ============================================================
# /checknow
# ============================================================

@dp.message(Command("checknow"))
async def cmd_checknow(
    message: Message,
):

    await send_schedule(
        message,
        image=True,
    )


# ============================================================
# АВТОМАТИЧЕСКАЯ ПРОВЕРКА
# ============================================================

async def automatic_checker():

    logger.info(
        "Автопроверка запущена."
    )

    logger.info(
        "Интервал: %d секунд",
        CHECK_INTERVAL,
    )

    while True:

        try:

            await asyncio.sleep(
                CHECK_INTERVAL
            )

            logger.info(
                "Проверяю расписание..."
            )

            schedule, date_text = (
                await get_schedule()
            )

            signature = (
                schedule_signature(
                    schedule,
                    date_text,
                )
            )

            cache = load_cache()

            old_signature = (
                cache.get(
                    "signature"
                )
            )

            # Первый запуск.
            if old_signature is None:

                logger.info(
                    "Создаю первоначальный кеш."
                )

                save_cache(
                    {
                        "signature": signature,
                        "date": date_text,
                    }
                )

                continue

            # Расписание изменилось.
            if signature != old_signature:

                logger.info(
                    "‼️ Расписание изменилось!"
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
                    "Изменений нет."
                )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Ошибка автоматической проверки."
            )

            # Не даём боту умереть из-за
            # временной ошибки сайта.
            await asyncio.sleep(60)


# ============================================================
# РАССЫЛКА
# ============================================================

async def broadcast_schedule(
    schedule: list[dict],
    date_text: Optional[str],
):

    if not subscribers:

        logger.info(
            "Нет подписчиков."
        )

        return

    image_path = (
        create_schedule_image(
            schedule,
            date_text,
        )
    )

    caption = (
        "📢 <b>Расписание обновилось!</b>\n"
        f"📚 Группа: <b>{GROUP_NAME}</b>"
    )

    if date_text:

        caption += (
            f"\n📅 <b>{date_text}</b>"
        )

    failed = []

    logger.info(
        "Отправляю обновление %d подписчикам.",
        len(subscribers),
    )

    for chat_id in list(subscribers):

        try:

            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(
                    image_path
                ),
                caption=caption,
            )

            await asyncio.sleep(
                0.05
            )

        except Exception as e:

            logger.warning(
                "Ошибка отправки %s: %s",
                chat_id,
                e,
            )

            failed.append(
                chat_id
            )

    # Удаляем недействительные чаты.
    for chat_id in failed:

        subscribers.discard(
            chat_id
        )

    if failed:

        save_subscribers(
            subscribers
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "========================================"
    )

    logger.info(
        "Запуск бота"
    )

    logger.info(
        "Группа: %s",
        GROUP_NAME,
    )

    logger.info(
        "URL: %s",
        SCHEDULE_URL,
    )

    logger.info(
        "Подписчиков: %d",
        len(subscribers),
    )

    logger.info(
        "========================================"
    )

    await schedule_client.start()

    checker_task = asyncio.create_task(
        automatic_checker()
    )

    try:

        await dp.start_polling(
            bot,
            allowed_updates=(
                dp.resolve_used_update_types()
            ),
        )

    finally:

        checker_task.cancel()

        try:

            await checker_task

        except asyncio.CancelledError:

            pass

        await schedule_client.close()

        await bot.session.close()

        logger.info(
            "Бот остановлен."
        )


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Остановка..."
        )
