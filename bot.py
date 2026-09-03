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


# ============================================================
# НАСТРОЙКИ
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://www.ishnk.ru/2025/site/schedule/group/508"
).rstrip("/")

GROUP_NAME = os.getenv(
    "GROUP_NAME",
    "ЭС7-24"
)

CHECK_INTERVAL = int(
    os.getenv("CHECK_INTERVAL", "1800")
)

DATA_DIR = Path(
    os.getenv("DATA_DIR", "data")
)

TIMEZONE_NAME = os.getenv(
    "TIMEZONE",
    "Europe/Moscow"
)

TZ = ZoneInfo(TIMEZONE_NAME)


# ============================================================
# ПАПКИ
# ============================================================

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

IMAGE_DIR = DATA_DIR / "images"

IMAGE_DIR.mkdir(
    parents=True,
    exist_ok=True
)

SUBSCRIBERS_FILE = (
    DATA_DIR / "subscribers.json"
)

CACHE_FILE = (
    DATA_DIR / "schedule_cache.json"
)


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# ПРОВЕРКА ТОКЕНА
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не указан в .env"
    )


# ============================================================
# JSON
# ============================================================

def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open(
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception:
        logger.exception(
            "Ошибка чтения %s",
            path
        )

        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")

    with tmp.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    tmp.replace(path)


# ============================================================
# ПОДПИСЧИКИ
# ============================================================

def load_subscribers():
    data = load_json(
        SUBSCRIBERS_FILE,
        []
    )

    return set(
        str(x)
        for x in data
    )


def save_subscribers(subscribers):
    save_json(
        SUBSCRIBERS_FILE,
        sorted(subscribers)
    )


# ============================================================
# ДАТА
# ============================================================

def local_today():
    return datetime.now(TZ).date()


def build_schedule_url(date_value):
    """
    Формирует:

    https://www.ishnk.ru/2025/site/schedule/group/508/2026-09-03
    """

    return (
        f"{SCHEDULE_URL}/"
        f"{date_value.isoformat()}"
    )


def parse_user_date(value):
    value = value.strip()

    formats = [
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                value,
                fmt
            ).date()

        except ValueError:
            continue

    return None


# ============================================================
# ОЧИСТКА ТЕКСТА
# ============================================================

def clean_text(text):
    if not text:
        return ""

    text = text.replace(
        "\xa0",
        " "
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def normalize_text(text):
    """
    Убирает лишние пробелы и разделители.
    """

    text = clean_text(text)

    text = re.sub(
        r"\s*\|\s*",
        " | ",
        text
    )

    return text.strip()


# ============================================================
# HTTP
# ============================================================

async def fetch_html(date_value):
    url = build_schedule_url(
        date_value
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),

        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),

        "Accept-Language": (
            "ru-RU,ru;q=0.9,en;q=0.8"
        ),

        "Connection": "keep-alive",
    }

    timeout = aiohttp.ClientTimeout(
        total=30
    )

    logger.info(
        "Запрашиваю: %s",
        url
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        # ВАЖНО:
        # ssl=False нужен потому, что у сайта
        # некорректный/самоподписанный сертификат.
        async with session.get(
            url,
            ssl=False,
            allow_redirects=True
        ) as response:

            response.raise_for_status()

            raw = await response.read()

            logger.info(
                "HTTP %s, получено %s байт",
                response.status,
                len(raw)
            )

            if not raw:
                raise RuntimeError(
                    "Сайт вернул пустую страницу"
                )

            return raw


# ============================================================
# ПАРСИНГ
# ============================================================

TIME_RE = re.compile(
    r"\b(?:[01]?\d|2[0-3])"
    r":"
    r"[0-5]\d"
)

PAIR_RE = re.compile(
    r"\b(?:I|II|III|IV|V|VI|VII|VIII|IX|X)"
    r"\s*пара\b",
    re.IGNORECASE
)


def remove_duplicate_texts(items):
    result = []
    seen = set()

    for item in items:
        item = normalize_text(item)

        if not item:
            continue

        # Удаляем совсем короткий мусор.
        if len(item) < 3:
            continue

        key = item.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(item)

    return result


def parse_schedule(raw):
    """
    ВАЖНО:

    Мы передаём BeautifulSoup СЫРЫЕ БАЙТЫ,
    а не декодируем их вручную как UTF-8.

    BeautifulSoup сам определяет кодировку страницы.
    Это исправляет ситуацию с Windows-1251 / CP1251.
    """

    soup = BeautifulSoup(
        raw,
        "html.parser"
    )

    logger.info(
        "Определённая кодировка HTML: %s",
        soup.original_encoding
    )

    # --------------------------------------------------------
    # ВАРИАНТ 1. Таблица
    # --------------------------------------------------------

    table_rows = []

    for table in soup.find_all("table"):

        for tr in table.find_all("tr"):

            cells = []

            for cell in tr.find_all(
                ["th", "td"],
                recursive=False
            ):

                text = normalize_text(
                    cell.get_text(
                        " ",
                        strip=True
                    )
                )

                if text:
                    cells.append(text)

            if cells:
                table_rows.append(
                    " | ".join(cells)
                )

    table_rows = remove_duplicate_texts(
        table_rows
    )

    if table_rows:
        logger.info(
            "Найдено строк таблицы: %s",
            len(table_rows)
        )

        return table_rows


    # --------------------------------------------------------
    # ВАРИАНТ 2. Ищем блоки с временем
    # --------------------------------------------------------

    candidates = []

    for element in soup.find_all(
        ["article", "section", "li", "div"]
    ):

        text = normalize_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        # Нас интересуют блоки, где есть время.
        if not TIME_RE.search(text):
            continue

        # Ограничиваем слишком огромные контейнеры.
        # Например, весь body нам не нужен.
        if len(text) > 1500:
            continue

        candidates.append(
            (element, text)
        )


    # --------------------------------------------------------
    # Удаляем вложенные дубликаты
    # --------------------------------------------------------

    selected = []

    for element, text in candidates:

        is_inside_larger = False

        for other_element, other_text in candidates:

            if element is other_element:
                continue

            if len(other_text) <= len(text):
                continue

            if text not in other_text:
                continue

            # Если наш текст находится внутри
            # другого блока, обычно берём более крупный блок.
            is_inside_larger = True
            break

        if not is_inside_larger:
            selected.append(text)


    selected = remove_duplicate_texts(
        selected
    )


    if selected:
        logger.info(
            "Найдено блоков расписания: %s",
            len(selected)
        )

        return selected


    # --------------------------------------------------------
    # ВАРИАНТ 3. Резервный режим:
    # берём текст страницы построчно
    # --------------------------------------------------------

    lines = []

    for line in soup.stripped_strings:

        line = normalize_text(
            line
        )

        if not line:
            continue

        if TIME_RE.search(line):
            lines.append(line)


    lines = remove_duplicate_texts(
        lines
    )

    logger.info(
        "Резервный парсинг: %s строк",
        len(lines)
    )

    return lines


# ============================================================
# ПРЕОБРАЗОВАНИЕ РАСПИСАНИЯ В ТЕКСТ
# ============================================================

def schedule_to_text(
    rows,
    date_value
):
    header = (
        f"📅 {date_value.strftime('%d.%m.%Y')}\n"
        f"🎓 Группа: {GROUP_NAME}\n"
    )

    if not rows:
        return (
            header +
            "\n❌ Расписание не найдено."
        )

    lines = [
        header
    ]

    for index, row in enumerate(rows, 1):

        row = normalize_text(
            row
        )

        if not row:
            continue

        lines.append(
            f"{row}"
        )

    return "\n".join(lines)


# ============================================================
# ШРИФТЫ
# ============================================================

def get_font(size, bold=False):

    if bold:
        fonts = [
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans-Bold.ttf",

            "/usr/share/fonts/truetype/liberation2/"
            "LiberationSans-Bold.ttf",
        ]

    else:
        fonts = [
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans.ttf",

            "/usr/share/fonts/truetype/liberation2/"
            "LiberationSans-Regular.ttf",
        ]

    for font_path in fonts:

        path = Path(font_path)

        if path.exists():
            return ImageFont.truetype(
                str(path),
                size
            )

    return ImageFont.load_default()


# ============================================================
# ПЕРЕНОС ДЛИННЫХ СТРОК
# ============================================================

def wrap_text(
    draw,
    text,
    font,
    max_width
):
    words = text.split()

    if not words:
        return [""]

    lines = []
    current = ""

    for word in words:

        test = (
            word
            if not current
            else current + " " + word
        )

        bbox = draw.textbbox(
            (0, 0),
            test,
            font=font
        )

        width = (
            bbox[2] - bbox[0]
        )

        if width <= max_width:
            current = test

        else:

            if current:
                lines.append(
                    current
                )

            current = word

    if current:
        lines.append(
            current
        )

    return lines


# ============================================================
# СОЗДАНИЕ КАРТИНКИ
# ============================================================

def render_schedule(
    rows,
    date_value
):
    width = 1400

    title_font = get_font(
        42,
        bold=True
    )

    date_font = get_font(
        30,
        bold=True
    )

    text_font = get_font(
        25
    )

    max_text_width = (
        width - 100
    )

    prepared_lines = []

    # Заголовок
    prepared_lines.append(
        ("title", f"Расписание — {GROUP_NAME}")
    )

    prepared_lines.append(
        (
            "date",
            date_value.strftime(
                "%d.%m.%Y"
            )
        )
    )

    prepared_lines.append(
        ("empty", "")
    )

    if not rows:

        prepared_lines.append(
            (
                "text",
                "Расписание не найдено."
            )
        )

    else:

        for row in rows:

            wrapped = wrap_text(
                None if False else ImageDraw.Draw(
                    Image.new(
                        "RGB",
                        (1, 1)
                    )
                ),
                normalize_text(row),
                text_font,
                max_text_width
            )

            for line in wrapped:
                prepared_lines.append(
                    ("text", line)
                )

            prepared_lines.append(
                ("empty", "")
            )


    # --------------------------------------------------------
    # Высота
    # --------------------------------------------------------

    line_heights = {
        "title": 58,
        "date": 45,
        "text": 40,
        "empty": 18,
    }

    padding = 50

    height = padding * 2

    for kind, _ in prepared_lines:
        height += line_heights[kind]


    height = max(
        height,
        350
    )


    # --------------------------------------------------------
    # Картинка
    # --------------------------------------------------------

    image = Image.new(
        "RGB",
        (width, height),
        "white"
    )

    draw = ImageDraw.Draw(
        image
    )

    y = padding

    for kind, text in prepared_lines:

        if kind == "title":
            font = title_font

        elif kind == "date":
            font = date_font

        else:
            font = text_font


        if text:
            draw.text(
                (padding, y),
                text,
                fill="black",
                font=font
            )


        y += line_heights[kind]


    filename = (
        IMAGE_DIR /
        f"schedule_{date_value.isoformat()}.png"
    )

    image.save(
        filename,
        "PNG"
    )

    logger.info(
        "Создана картинка: %s",
        filename
    )

    return filename


# ============================================================
# ПОЛУЧЕНИЕ РАСПИСАНИЯ
# ============================================================

async def get_schedule(date_value):

    raw = await fetch_html(
        date_value
    )

    rows = parse_schedule(
        raw
    )

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
        "raw": raw,
    }


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()


# ============================================================
# /start
# ============================================================

@dp.message(Command("start"))
async def cmd_start(
    message: Message
):

    await message.answer(
        f"📚 Расписание группы {GROUP_NAME}\n\n"

        "Команды:\n"
        "📅 /schedule — сегодня\n"
        "📝 /scheduletext — сегодня текстом\n"
        "➡️ /tomorrow — завтра\n"
        "📆 /date 04.09.2026 — нужная дата\n\n"

        "🔔 /subscribe — получать обновления\n"
        "🔕 /unsubscribe — отключить обновления\n"
        "🔄 /checknow — проверить сейчас"
    )


# ============================================================
# /schedule
# ============================================================

@dp.message(Command("schedule"))
async def cmd_schedule(
    message: Message
):

    date_value = local_today()

    try:

        data = await get_schedule(
            date_value
        )

        await message.answer_photo(
            photo=FSInputFile(
                data["image"]
            ),

            caption=(
                f"📅 "
                f"{date_value.strftime('%d.%m.%Y')}\n"
                f"🎓 {GROUP_NAME}"
            )
        )

    except Exception as e:

        logger.exception(
            "Ошибка /schedule"
        )

        await message.answer(
            "❌ Не удалось получить расписание.\n\n"
            f"{e}"
        )


# ============================================================
# /scheduletext
# ============================================================

@dp.message(Command("scheduletext"))
async def cmd_scheduletext(
    message: Message
):

    date_value = local_today()

    try:

        data = await get_schedule(
            date_value
        )

        text = data["text"]

        # Telegram ограничивает длину сообщения.
        if len(text) > 4000:
            text = (
                text[:3900] +
                "\n\n..."
            )

        await message.answer(
            text
        )

    except Exception as e:

        logger.exception(
            "Ошибка /scheduletext"
        )

        await message.answer(
            "❌ Не удалось получить расписание.\n\n"
            f"{e}"
        )


# ============================================================
# /tomorrow
# ============================================================

@dp.message(Command("tomorrow"))
async def cmd_tomorrow(
    message: Message
):

    date_value = (
        local_today() +
        timedelta(days=1)
    )

    try:

        data = await get_schedule(
            date_value
        )

        await message.answer_photo(
            photo=FSInputFile(
                data["image"]
            ),

            caption=(
                f"📅 "
                f"{date_value.strftime('%d.%m.%Y')}\n"
                f"🎓 {GROUP_NAME}"
            )
        )

    except Exception as e:

        logger.exception(
            "Ошибка /tomorrow"
        )

        await message.answer(
            "❌ Не удалось получить расписание.\n\n"
            f"{e}"
        )


# ============================================================
# /date
# ============================================================

@dp.message(Command("date"))
async def cmd_date(
    message: Message
):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        await message.answer(
            "Использование:\n\n"
            "/date 04.09.2026\n\n"
            "или:\n"
            "/date 2026-09-04"
        )

        return


    date_value = parse_user_date(
        parts[1]
    )

    if date_value is None:

        await message.answer(
            "❌ Неверный формат даты.\n\n"
            "Пример:\n"
            "/date 04.09.2026"
        )

        return


    try:

        data = await get_schedule(
            date_value
        )

        await message.answer_photo(
            photo=FSInputFile(
                data["image"]
            ),

            caption=(
                f"📅 "
                f"{date_value.strftime('%d.%m.%Y')}\n"
                f"🎓 {GROUP_NAME}"
            )
        )

    except Exception as e:

        logger.exception(
            "Ошибка /date"
        )

        await message.answer(
            "❌ Не удалось получить расписание.\n\n"
            f"{e}"
        )


# ============================================================
# /subscribe
# ============================================================

@dp.message(Command("subscribe"))
async def cmd_subscribe(
    message: Message
):

    subscribers = load_subscribers()

    subscribers.add(
        str(message.chat.id)
    )

    save_subscribers(
        subscribers
    )

    await message.answer(
        "🔔 Подписка включена.\n\n"
        "Бот будет автоматически проверять "
        "расписание на завтра."
    )


# ============================================================
# /unsubscribe
# ============================================================

@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(
    message: Message
):

    subscribers = load_subscribers()

    subscribers.discard(
        str(message.chat.id)
    )

    save_subscribers(
        subscribers
    )

    await message.answer(
        "🔕 Подписка отключена."
    )


# ============================================================
# /checknow
# ============================================================

@dp.message(Command("checknow"))
async def cmd_checknow(
    message: Message
):

    try:

        changed = await check_schedule(
            force=True
        )

        if changed:
            await message.answer(
                "✅ Проверка выполнена.\n"
                "Обновление найдено."
            )

        else:
            await message.answer(
                "✅ Проверка выполнена.\n"
                "Изменений нет."
            )

    except Exception as e:

        logger.exception(
            "Ошибка /checknow"
        )

        await message.answer(
            "❌ Ошибка проверки:\n"
            f"{e}"
        )


# ============================================================
# ПОДПИСЧИКИ — ПРОВЕРКА РАСПИСАНИЯ
# ============================================================

def make_signature(data):
    return hashlib.sha256(
        data["text"].encode(
            "utf-8"
        )
    ).hexdigest()


async def check_schedule(
    force=False
):
    """
    Автоматически проверяем расписание
    на следующий день.
    """

    date_value = (
        local_today() +
        timedelta(days=1)
    )

    data = await get_schedule(
        date_value
    )

    signature = make_signature(
        data
    )

    cache = load_json(
        CACHE_FILE,
        {}
    )

    cache_key = (
        date_value.isoformat()
    )

    old_signature = cache.get(
        cache_key
    )

    changed = (
        old_signature != signature
    )

    # Если ничего не изменилось
    # и это не принудительная проверка.
    if not force and not changed:

        logger.info(
            "Изменений нет: %s",
            date_value
        )

        return False


    # Сохраняем новый хэш.
    cache[cache_key] = signature


    # Храним только последние 30 дат.
    if len(cache) > 30:

        keys = sorted(
            cache.keys()
        )

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

        return changed


    # При force=True не рассылаем сообщение всем.
    # Это предотвращает случайный спам через /checknow.
    if force:

        return changed


    caption = (
        "🔔 Обновление расписания!\n\n"
        f"📅 "
        f"{date_value.strftime('%d.%m.%Y')}\n"
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

            await asyncio.sleep(
                0.05
            )

        except Exception:

            logger.exception(
                "Ошибка отправки пользователю %s",
                chat_id
            )


    return True


# ============================================================
# АВТОМАТИЧЕСКИЙ ЦИКЛ
# ============================================================

async def scheduler_loop():

    # Небольшая задержка после запуска.
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


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "================================"
    )

    logger.info(
        "Бот запускается"
    )

    logger.info(
        "Группа: %s",
        GROUP_NAME
    )

    logger.info(
        "Базовый URL: %s",
        SCHEDULE_URL
    )

    logger.info(
        "Пример URL: %s",
        build_schedule_url(
            local_today()
        )
    )

    logger.info(
        "Часовой пояс: %s",
        TIMEZONE_NAME
    )

    logger.info(
        "Интервал: %s секунд",
        CHECK_INTERVAL
    )

    logger.info(
        "SSL verification: OFF для сайта расписания"
    )

    logger.info(
        "================================"
    )


    # Запускаем автоматическую проверку.
    asyncio.create_task(
        scheduler_loop()
    )


    # Запускаем Telegram.
    await dp.start_polling(
        bot
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:
        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Бот остановлен."
        )
