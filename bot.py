import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from aiogram.exceptions import TelegramForbiddenError


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://www.ishnk.ru/2025/site/schedule/group/508"
).rstrip("/")

GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24").strip()

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

SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CACHE_FILE = DATA_DIR / "schedule_cache.json"
IMAGE_DIR = DATA_DIR / "images"

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("schedule_bot")


# ============================================================
# AIROGRAM
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не указан. Добавь его в .env"
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# RUSSIAN DATE
# ============================================================

WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}

MONTHS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def format_date_ru(value: date) -> str:
    return (
        f"{WEEKDAYS[value.weekday()]}, "
        f"{value.day} {MONTHS[value.month]} {value.year} года"
    )


def format_date_short(value: date) -> str:
    return (
        f"{value.day} {MONTHS[value.month]} "
        f"{value.year}"
    )


# ============================================================
# FILE STORAGE
# ============================================================

def load_json(path: Path, default):
    try:
        if not path.exists():
            return default

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        logger.exception(
            "Не удалось прочитать %s",
            path
        )
        return default


def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    tmp.replace(path)


def load_subscribers():
    data = load_json(
        SUBSCRIBERS_FILE,
        []
    )

    if not isinstance(data, list):
        return set()

    return {
        int(x)
        for x in data
        if str(x).isdigit()
    }


def save_subscribers(subscribers):
    save_json(
        SUBSCRIBERS_FILE,
        sorted(subscribers)
    )


SUBSCRIBERS = load_subscribers()


# ============================================================
# CACHE
# ============================================================

def load_cache():
    data = load_json(
        CACHE_FILE,
        {}
    )

    if not isinstance(data, dict):
        return {}

    return data


CACHE = load_cache()


# ============================================================
# HELPERS
# ============================================================

def clean_text(value: str) -> str:
    if not value:
        return ""

    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")
    value = value.replace("\ufeff", "")

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def build_url(target_date: date) -> str:
    return (
        f"{BASE_URL}/"
        f"{target_date.strftime('%Y-%m-%d')}"
    )


# ============================================================
# HTTP
# ============================================================

async def fetch_html(target_date: date) -> str:
    url = build_url(target_date)

    logger.info(
        "Загрузка расписания: %s",
        url
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 "
            "Chrome/120.0 Mobile Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Connection": "keep-alive",
    }

    timeout = aiohttp.ClientTimeout(
        total=30
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        async with session.get(
            url,
            ssl=False,
            allow_redirects=True
        ) as response:

            logger.info(
                "HTTP %s",
                response.status
            )

            response.raise_for_status()

            raw = await response.read()

            logger.info(
                "Получено HTML: %s байт",
                len(raw)
            )

            # На странице явно указано:
            # <meta charset="utf-8">
            try:
                html = raw.decode(
                    "utf-8",
                    errors="strict"
                )
            except UnicodeDecodeError:
                logger.warning(
                    "UTF-8 не подошёл, пробуем Windows-1251"
                )

                html = raw.decode(
                    "cp1251",
                    errors="replace"
                )

            return html


# ============================================================
# PARSER
# ============================================================

ROMAN_PAIRS = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
}


def parse_time(header) -> tuple[str, str] | None:
    """
    Реальная структура сайта:

    <span class="h4">
        08<sup>30</sup> - 09<sup>50</sup>
    </span>
    """

    time_node = header.select_one(
        "span.h4"
    )

    if not time_node:
        return None

    hours = []
    minutes = []

    # Собираем текст отдельно, включая SUP.
    for node in time_node.children:

        if getattr(node, "name", None) == "sup":
            minutes.append(
                clean_text(
                    node.get_text(
                        "",
                        strip=True
                    )
                )
            )
        else:
            text = clean_text(
                node.get_text(
                    "",
                    strip=True
                )
                if hasattr(node, "get_text")
                else str(node)
            )

            if text:
                hours.append(text)

    raw = clean_text(
        time_node.get_text(
            " ",
            strip=True
        )
    )

    # Основной вариант — напрямую из DOM.
    match = re.search(
        r"(\d{1,2})\s*(\d{2})\s*-\s*"
        r"(\d{1,2})\s*(\d{2})",
        raw
    )

    if match:
        h1, m1, h2, m2 = match.groups()

        return (
            f"{int(h1):02d}:{m1}",
            f"{int(h2):02d}:{m2}"
        )

    # Запасной вариант.
    digits = re.findall(
        r"\d+",
        raw
    )

    if len(digits) >= 4:

        h1 = digits[0]
        m1 = digits[1]
        h2 = digits[2]
        m2 = digits[3]

        return (
            f"{int(h1):02d}:{int(m1):02d}",
            f"{int(h2):02d}:{int(m2):02d}"
        )

    return None


def parse_schedule(html: str) -> list[dict]:
    """
    Парсер специально под реальную HTML-структуру ishnk.ru.

    Берём только:

        div.card.myCard

    у которых есть:

        div.card-header
        span.h3
        span.h4

    Поэтому таблица основного расписания ниже
    не попадёт в результаты.
    """

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    result = []

    cards = soup.select(
        "div.card.myCard"
    )

    logger.info(
        "Найдено потенциальных карточек: %s",
        len(cards)
    )

    for card in cards:

        header = card.select_one(
            ".card-header"
        )

        body = card.select_one(
            ".card-body"
        )

        if not header or not body:
            continue

        pair_node = header.select_one(
            "span.h3"
        )

        time_node = header.select_one(
            "span.h4"
        )

        if not pair_node or not time_node:
            continue

        pair_raw = clean_text(
            pair_node.get_text(
                " ",
                strip=True
            )
        )

        if pair_raw not in ROMAN_PAIRS:
            continue

        times = parse_time(header)

        if not times:
            continue

        time_start, time_end = times

        # ----------------------------------------------------
        # КАБИНЕТ
        # ----------------------------------------------------

        room = ""

        for span in body.select("span"):

            text = clean_text(
                span.get_text(
                    " ",
                    strip=True
                )
            )

            if re.match(
                r"^ауд\.",
                text,
                re.IGNORECASE
            ):
                room = re.sub(
                    r"^ауд\.\s*",
                    "",
                    text,
                    flags=re.IGNORECASE
                ).strip()

                break

        # ----------------------------------------------------
        # ПРЕПОДАВАТЕЛЬ
        # ----------------------------------------------------

        teacher = ""

        staff = body.select_one(
            ".Staff"
        )

        if staff:
            teacher = clean_text(
                staff.get_text(
                    " ",
                    strip=True
                )
            )

            if not teacher:
                teacher = clean_text(
                    staff.get("title", "")
                )

        # ----------------------------------------------------
        # ПРЕДМЕТ
        # ----------------------------------------------------

        subject = ""

        # На мобильной версии сайта:
        #
        # <div class="d-md-none text-center text-truncate">
        #     Экспл Н/Г мест
        # </div>

        mobile_subject = body.select_one(
            ".d-md-none.text-center.text-truncate"
        )

        if mobile_subject:
            subject = clean_text(
                mobile_subject.get_text(
                    " ",
                    strip=True
                )
            )

        # Запасной вариант.
        if not subject:

            desktop_subject = body.select_one(
                ".d-none.d-md-block b"
            )

            if desktop_subject:
                subject = clean_text(
                    desktop_subject.get_text(
                        " ",
                        strip=True
                    )
                )

        # Ещё один fallback.
        if not subject:

            subject_block = body.select_one(
                ".px-3.py-1.h5"
            )

            if subject_block:
                subject = clean_text(
                    subject_block.get_text(
                        " ",
                        strip=True
                    )
                )

        # ----------------------------------------------------
        # ИГНОРИРУЕМ ПУСТЫЕ КАРТОЧКИ
        # ----------------------------------------------------

        if not subject and not teacher and not room:
            continue

        item = {
            "pair": pair_raw,
            "pair_number": ROMAN_PAIRS[pair_raw],
            "start": time_start,
            "end": time_end,
            "room": room,
            "teacher": teacher,
            "subject": subject,
        }

        result.append(item)

        logger.info(
            "Пара %s | %s-%s | %s | %s | %s",
            pair_raw,
            time_start,
            time_end,
            subject,
            room,
            teacher
        )

    result.sort(
        key=lambda x: x["pair_number"]
    )

    logger.info(
        "Итог: %s занятий",
        len(result)
    )

    return result


# ============================================================
# SCHEDULE LOADING + TOMORROW FALLBACK
# ============================================================

async def get_schedule(target_date: date):
    html = await fetch_html(
        target_date
    )

    schedule = parse_schedule(
        html
    )

    return schedule


async def get_schedule_with_fallback(
    target_date: date
):
    """
    Если запрашивается будущий день и там нет
    расписания — возвращаем расписание сегодня.
    """

    schedule = await get_schedule(
        target_date
    )

    if schedule:
        return (
            schedule,
            target_date,
            False
        )

    today = datetime.now(
        TZ
    ).date()

    if target_date > today:

        logger.info(
            "На %s расписания нет. "
            "Пробуем сегодняшний день %s.",
            target_date,
            today
        )

        today_schedule = await get_schedule(
            today
        )

        if today_schedule:
            return (
                today_schedule,
                today,
                True
            )

    return (
        [],
        target_date,
        False
    )


# ============================================================
# SIGNATURE
# ============================================================

def schedule_signature(
    schedule: list[dict]
) -> str:

    normalized = []

    for item in schedule:

        normalized.append({
            "pair": item["pair"],
            "start": item["start"],
            "end": item["end"],
            "room": item["room"],
            "teacher": item["teacher"],
            "subject": item["subject"],
        })

    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


# ============================================================
# FONTS
# ============================================================

def find_font(
    bold: bool = False
):
    """
    Ищем DejaVu в нескольких стандартных местах.
    """

    if bold:

        names = [
            "DejaVuSans-Bold.ttf",
            "DejaVuSans-BoldOblique.ttf",
        ]

    else:

        names = [
            "DejaVuSans.ttf",
            "DejaVuSans-Book.ttf",
        ]

    paths = []

    for name in names:

        paths.extend([
            f"/usr/share/fonts/truetype/dejavu/{name}",
            f"/usr/share/fonts/dejavu/{name}",
            f"/usr/local/share/fonts/{name}",
        ])

    for path in paths:

        if Path(path).exists():
            return path

    # Последняя попытка через fontconfig.
    try:

        result = subprocess.run(
            [
                "fc-match",
                "-f",
                "%{file}",
                "DejaVu Sans"
                + (
                    ":style=Bold"
                    if bold
                    else ""
                )
            ],
            capture_output=True,
            text=True,
            timeout=3
        )

        path = result.stdout.strip()

        if path and Path(path).exists():
            return path

    except Exception:
        pass

    raise RuntimeError(
        "Шрифт DejaVu Sans не найден. "
        "Нужно пересобрать Docker image."
    )


def get_font(size: int, bold=False):

    return ImageFont.truetype(
        find_font(bold),
        size
    )


# ============================================================
# IMAGE HELPERS
# ============================================================

BG = "#F5F7FB"
WHITE = "#FFFFFF"
DARK = "#111827"
TEXT = "#253044"
MUTED = "#7B8494"
ACCENT = "#5865F2"
ACCENT_DARK = "#4552D9"
LIGHT_ACCENT = "#EEF0FF"
BORDER = "#E5E8EF"
GREEN = "#16A085"


def rounded_box(
    draw,
    xy,
    radius,
    fill,
    outline=None,
    width=1
):
    draw.rounded_rectangle(
        xy,
        radius=radius,
        fill=fill,
        outline=outline,
        width=width
    )


def draw_shadow(
    image,
    box,
    radius=28
):
    """
    Простой мягкий shadow без сторонних библиотек.
    """

    shadow = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(
        shadow
    )

    x1, y1, x2, y2 = box

    draw.rounded_rectangle(
        (
            x1 + 4,
            y1 + 8,
            x2 + 4,
            y2 + 8
        ),
        radius=radius,
        fill=(15, 23, 42, 25)
    )

    shadow = shadow.filter(
        __import__("PIL").ImageFilter.GaussianBlur(10)
    )

    image.alpha_composite(
        shadow
    )


def fit_text(
    draw,
    text,
    font,
    max_width
):
    """
    Обрезает длинный текст с …
    """

    text = clean_text(text)

    if not text:
        return ""

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font
    )

    if bbox[2] - bbox[0] <= max_width:
        return text

    suffix = "…"

    while text:

        candidate = text + suffix

        bbox = draw.textbbox(
            (0, 0),
            candidate,
            font=font
        )

        if bbox[2] - bbox[0] <= max_width:
            return candidate

        text = text[:-1]

    return suffix


# ============================================================
# RENDER
# ============================================================

def render_schedule(
    schedule: list[dict],
    target_date: date,
    fallback=False
) -> Path:

    width = 1200

    header_height = 265
    card_height = 185
    gap = 22
    bottom = 100

    height = (
        header_height
        + len(schedule) * card_height
        + max(0, len(schedule) - 1) * gap
        + bottom
    )

    image = Image.new(
        "RGBA",
        (width, height),
        BG
    )

    draw = ImageDraw.Draw(
        image
    )

    # --------------------------------------------------------
    # FONTS
    # --------------------------------------------------------

    font_group = get_font(
        27,
        bold=True
    )

    font_title = get_font(
        55,
        bold=True
    )

    font_date = get_font(
        27,
        bold=False
    )

    font_count = get_font(
        24,
        bold=True
    )

    font_pair = get_font(
        22,
        bold=True
    )

    font_time = get_font(
        31,
        bold=True
    )

    font_subject = get_font(
        31,
        bold=True
    )

    font_label = get_font(
        19,
        bold=False
    )

    font_value = get_font(
        23,
        bold=True
    )

    font_footer = get_font(
        19,
        bold=False
    )

    # --------------------------------------------------------
    # TOP HEADER
    # --------------------------------------------------------

    rounded_box(
        draw,
        (35, 30, width - 35, header_height - 15),
        32,
        fill=WHITE,
        outline=BORDER,
        width=2
    )

    # Accent line.
    rounded_box(
        draw,
        (35, 30, 49, header_height - 15),
        8,
        fill=ACCENT
    )

    draw.text(
        (75, 58),
        GROUP_NAME,
        font=font_group,
        fill=ACCENT
    )

    draw.text(
        (75, 96),
        "РАСПИСАНИЕ",
        font=font_title,
        fill=DARK
    )

    date_text = format_date_ru(
        target_date
    )

    draw.text(
        (78, 169),
        date_text,
        font=font_date,
        fill=MUTED
    )

    count_text = (
        f"{len(schedule)} "
        + (
            "занятие"
            if len(schedule) == 1
            else "занятий"
        )
    )

    count_bbox = draw.textbbox(
        (0, 0),
        count_text,
        font=font_count
    )

    count_width = (
        count_bbox[2]
        - count_bbox[0]
    )

    rounded_box(
        draw,
        (
            width - 75 - count_width - 34,
            65,
            width - 75,
            112
        ),
        24,
        fill=LIGHT_ACCENT
    )

    draw.text(
        (
            width - 75 - count_width - 17,
            76
        ),
        count_text,
        font=font_count,
        fill=ACCENT
    )

    if fallback:

        fallback_text = (
            "Завтра расписания нет · "
            "показываем сегодня"
        )

        draw.text(
            (78, 210),
            fallback_text,
            font=font_label,
            fill=GREEN
        )

    # --------------------------------------------------------
    # LESSON CARDS
    # --------------------------------------------------------

    y = header_height + 20

    for index, item in enumerate(
        schedule
    ):

        x1 = 35
        x2 = width - 35
        y1 = y
        y2 = y + card_height

        draw_shadow(
            image,
            (x1, y1, x2, y2),
            radius=27
        )

        rounded_box(
            draw,
            (x1, y1, x2, y2),
            27,
            fill=WHITE,
            outline=BORDER,
            width=2
        )

        # ----------------------------------------------------
        # PAIR NUMBER
        # ----------------------------------------------------

        badge_x1 = x1 + 25
        badge_y1 = y1 + 25
        badge_x2 = badge_x1 + 75
        badge_y2 = badge_y1 + 75

        rounded_box(
            draw,
            (
                badge_x1,
                badge_y1,
                badge_x2,
                badge_y2
            ),
            22,
            fill=ACCENT
        )

        pair = item["pair"]

        bbox = draw.textbbox(
            (0, 0),
            pair,
            font=font_pair
        )

        pair_w = bbox[2] - bbox[0]
        pair_h = bbox[3] - bbox[1]

        draw.text(
            (
                badge_x1
                + (75 - pair_w) / 2,
                badge_y1
                + (75 - pair_h) / 2
                - 2
            ),
            pair,
            font=font_pair,
            fill=WHITE
        )

        # ----------------------------------------------------
        # TIME
        # ----------------------------------------------------

        time_text = (
            f"{item['start']} — {item['end']}"
        )

        draw.text(
            (
                x1 + 125,
                y1 + 27
            ),
            time_text,
            font=font_time,
            fill=DARK
        )

        # ----------------------------------------------------
        # SUBJECT
        # ----------------------------------------------------

        subject = fit_text(
            draw,
            item["subject"] or "Занятие",
            font_subject,
            650
        )

        draw.text(
            (
                x1 + 125,
                y1 + 77
            ),
            subject,
            font=font_subject,
            fill=DARK
        )

        # ----------------------------------------------------
        # BOTTOM INFO
        # ----------------------------------------------------

        info_y = y1 + 133

        # Room
        room = item["room"] or "Кабинет не указан"

        draw.text(
            (
                x1 + 125,
                info_y
            ),
            "КАБИНЕТ",
            font=font_label,
            fill=MUTED
        )

        draw.text(
            (
                x1 + 125,
                info_y + 24
            ),
            fit_text(
                draw,
                room,
                font_value,
                220
            ),
            font=font_value,
            fill=TEXT
        )

        # Teacher
        teacher = (
            item["teacher"]
            or "Преподаватель не указан"
        )

        draw.text(
            (
                x1 + 400,
                info_y
            ),
            "ПРЕПОДАВАТЕЛЬ",
            font=font_label,
            fill=MUTED
        )

        draw.text(
            (
                x1 + 400,
                info_y + 24
            ),
            fit_text(
                draw,
                teacher,
                font_value,
                680
            ),
            font=font_value,
            fill=TEXT
        )

        y += card_height + gap

    # --------------------------------------------------------
    # FOOTER
    # --------------------------------------------------------

    footer_y = height - 58

    draw.text(
        (
            45,
            footer_y
        ),
        "ИНК · автоматическое расписание",
        font=font_footer,
        fill=MUTED
    )

    output = (
        IMAGE_DIR
        / f"{target_date.isoformat()}.png"
    )

    # RGB — Telegram нормально принимает PNG.
    image.convert(
        "RGB"
    ).save(
        output,
        "PNG",
        optimize=True
    )

    logger.info(
        "Изображение создано: %s",
        output
    )

    return output


# ============================================================
# TELEGRAM TEXT
# ============================================================

def schedule_caption(
    schedule,
    target_date,
    fallback=False
):

    lines = [
        f"📚 <b>{GROUP_NAME}</b>",
        f"📅 {format_date_ru(target_date)}",
        "",
    ]

    for item in schedule:

        line = (
            f"<b>{item['pair']} пара</b>  "
            f"{item['start']}–{item['end']}\n"
            f"   {item['subject'] or 'Занятие'}"
        )

        if item["room"]:
            line += (
                f"\n   🏫 {item['room']}"
            )

        if item["teacher"]:
            line += (
                f"\n   👤 {item['teacher']}"
            )

        lines.append(line)
        lines.append("")

    if fallback:

        lines.append(
            "ℹ️ На завтра расписание не опубликовано. "
            "Показываю сегодняшнее."
        )

    return "\n".join(lines)


# ============================================================
# SEND SCHEDULE
# ============================================================

async def send_schedule(
    message: Message,
    target_date: date
):

    try:

        schedule, actual_date, fallback = (
            await get_schedule_with_fallback(
                target_date
            )
        )

        if not schedule:

            await message.answer(
                "Расписание на этот день "
                "не найдено."
            )

            return

        image_path = render_schedule(
            schedule=schedule,
            target_date=actual_date,
            fallback=fallback
        )

        caption = schedule_caption(
            schedule,
            actual_date,
            fallback
        )

        await message.answer_photo(
            photo=FSInputFile(
                image_path
            ),
            caption=caption
        )

    except Exception:

        logger.exception(
            "Ошибка отправки расписания"
        )

        await message.answer(
            "Не удалось обработать расписание.\n"
            "Ошибка записана в лог."
        )


# ============================================================
# COMMANDS
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):

    text = (
        f"📚 <b>ИНК | {GROUP_NAME}</b>\n\n"
        "Бот расписания колледжа.\n\n"
        "📅 /schedule — сегодня\n"
        "➡️ /tomorrow — завтра\n"
        "🔔 /subscribe — подписаться\n"
        "🔕 /unsubscribe — отписаться\n"
        "🔄 /checknow — проверить изменения\n\n"
        "Если на завтра расписание ещё не опубликовано, "
        "бот покажет расписание на сегодня."
    )

    await message.answer(
        text
    )


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):

    today = datetime.now(
        TZ
    ).date()

    await send_schedule(
        message,
        today
    )


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message):

    today = datetime.now(
        TZ
    ).date()

    tomorrow = today + timedelta(
        days=1
    )

    await send_schedule(
        message,
        tomorrow
    )


@dp.message(Command("checknow"))
async def cmd_checknow(message: Message):

    await message.answer(
        "🔄 Проверяю расписание..."
    )

    tomorrow = (
        datetime.now(TZ).date()
        + timedelta(days=1)
    )

    await send_schedule(
        message,
        tomorrow
    )


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):

    user_id = message.chat.id

    if user_id in SUBSCRIBERS:

        await message.answer(
            "🔔 Вы уже подписаны."
        )

        return

    SUBSCRIBERS.add(
        user_id
    )

    save_subscribers(
        SUBSCRIBERS
    )

    await message.answer(
        "🔔 <b>Подписка включена.</b>\n\n"
        "Я буду автоматически проверять расписание "
        "и сообщать об изменениях."
    )


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):

    user_id = message.chat.id

    if user_id not in SUBSCRIBERS:

        await message.answer(
            "Вы не были подписаны."
        )

        return

    SUBSCRIBERS.discard(
        user_id
    )

    save_subscribers(
        SUBSCRIBERS
    )

    await message.answer(
        "🔕 Подписка отключена."
    )


# ============================================================
# AUTOMATIC CHECK
# ============================================================

async def check_schedule_changes():

    while True:

        try:

            now = datetime.now(
                TZ
            )

            today = now.date()

            tomorrow = (
                today
                + timedelta(days=1)
            )

            logger.info(
                "Автоматическая проверка: %s",
                now.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )

            # ------------------------------------------------
            # ПРОВЕРЯЕМ СЕГОДНЯ
            # ------------------------------------------------

            today_schedule = await get_schedule(
                today
            )

            if today_schedule:

                today_signature = schedule_signature(
                    today_schedule
                )

                cache_key = (
                    f"today:{today.isoformat()}"
                )

                previous = CACHE.get(
                    cache_key
                )

                if previous is None:

                    CACHE[cache_key] = (
                        today_signature
                    )

                    save_json(
                        CACHE_FILE,
                        CACHE
                    )

                    logger.info(
                        "Создан базовый снимок "
                        "сегодняшнего расписания."
                    )

                elif previous != today_signature:

                    logger.info(
                        "Обнаружено изменение "
                        "сегодняшнего расписания."
                    )

                    CACHE[cache_key] = (
                        today_signature
                    )

                    save_json(
                        CACHE_FILE,
                        CACHE
                    )

                    await broadcast_schedule(
                        today_schedule,
                        today,
                        "сегодняшнее"
                    )

            # ------------------------------------------------
            # ПРОВЕРЯЕМ ЗАВТРА
            # ------------------------------------------------

            tomorrow_schedule = await get_schedule(
                tomorrow
            )

            if tomorrow_schedule:

                tomorrow_signature = schedule_signature(
                    tomorrow_schedule
                )

                cache_key = (
                    f"tomorrow:{tomorrow.isoformat()}"
                )

                previous = CACHE.get(
                    cache_key
                )

                if previous is None:

                    CACHE[cache_key] = (
                        tomorrow_signature
                    )

                    save_json(
                        CACHE_FILE,
                        CACHE
                    )

                    logger.info(
                        "Создан базовый снимок "
                        "завтрашнего расписания."
                    )

                elif previous != tomorrow_signature:

                    logger.info(
                        "Обнаружено изменение "
                        "завтрашнего расписания."
                    )

                    CACHE[cache_key] = (
                        tomorrow_signature
                    )

                    save_json(
                        CACHE_FILE,
                        CACHE
                    )

                    await broadcast_schedule(
                        tomorrow_schedule,
                        tomorrow,
                        "завтрашнее"
                    )

            else:

                logger.info(
                    "На завтра расписания пока нет. "
                    "При ручном запросе будет показан сегодняшний день."
                )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Ошибка автоматической проверки"
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# BROADCAST
# ============================================================

async def broadcast_schedule(
    schedule,
    target_date,
    description
):

    if not SUBSCRIBERS:

        logger.info(
            "Нет подписчиков."
        )

        return

    image_path = render_schedule(
        schedule,
        target_date,
        fallback=False
    )

    caption = (
        f"🔄 <b>Расписание изменилось</b>\n\n"
        f"📚 {GROUP_NAME}\n"
        f"📅 {format_date_ru(target_date)}\n\n"
        f"Обновлено автоматически."
    )

    dead_users = set()

    for user_id in list(
        SUBSCRIBERS
    ):

        try:

            await bot.send_photo(
                chat_id=user_id,
                photo=FSInputFile(
                    image_path
                ),
                caption=caption
            )

            logger.info(
                "Уведомление отправлено: %s",
                user_id
            )

            # Небольшая пауза.
            await asyncio.sleep(
                0.05
            )

        except TelegramForbiddenError:

            logger.info(
                "Пользователь заблокировал бота: %s",
                user_id
            )

            dead_users.add(
                user_id
            )

        except Exception:

            logger.exception(
                "Ошибка отправки пользователю %s",
                user_id
            )

    if dead_users:

        SUBSCRIBERS.difference_update(
            dead_users
        )

        save_subscribers(
            SUBSCRIBERS
        )


# ============================================================
# STARTUP
# ============================================================

async def main():

    logger.info(
        "=" * 60
    )

    logger.info(
        "Schedule bot started"
    )

    logger.info(
        "Group: %s",
        GROUP_NAME
    )

    logger.info(
        "URL: %s",
        BASE_URL
    )

    logger.info(
        "Check interval: %s sec",
        CHECK_INTERVAL
    )

    logger.info(
        "Subscribers: %s",
        len(SUBSCRIBERS)
    )

    logger.info(
        "=" * 60
    )

    checker_task = asyncio.create_task(
        check_schedule_changes()
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        checker_task.cancel()

        try:
            await checker_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()


if __name__ == "__main__":

    try:
        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped"
        )
