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
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24").strip()

SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://www.ishnk.ru/2025/site/schedule/group/508"
).rstrip("/")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(TIMEZONE_NAME)

SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CACHE_FILE = DATA_DIR / "schedule_cache.json"
IMAGE_DIR = DATA_DIR / "images"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("schedule_bot")


# ============================================================
# INIT DIRS
# ============================================================

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CHECK TOKEN
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не указан. Добавь его в .env"
    )


# ============================================================
# HTTP
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0 Mobile Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


# ============================================================
# DATE
# ============================================================

RUSSIAN_WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}

RUSSIAN_MONTHS = {
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


def today():
    return datetime.now(TZ).date()


def tomorrow():
    return today() + timedelta(days=1)


def format_date(value):
    return (
        f"{RUSSIAN_WEEKDAYS[value.weekday()]}, "
        f"{value.day} {RUSSIAN_MONTHS[value.month]} {value.year} года"
    )


def build_url(date_value):
    return f"{SCHEDULE_URL}/{date_value.isoformat()}"


# ============================================================
# JSON STORAGE
# ============================================================

def load_json(path, default):
    try:
        if not path.exists():
            return default

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        logger.exception("Ошибка чтения %s", path)
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    tmp.replace(path)


def load_subscribers():
    data = load_json(SUBSCRIBERS_FILE, [])

    if not isinstance(data, list):
        return []

    return [int(x) for x in data]


def save_subscribers(subscribers):
    save_json(
        SUBSCRIBERS_FILE,
        sorted(set(int(x) for x in subscribers))
    )


def load_cache():
    data = load_json(CACHE_FILE, {})

    if not isinstance(data, dict):
        return {}

    return data


def save_cache(cache):
    save_json(CACHE_FILE, cache)


# ============================================================
# HTML
# ============================================================

async def fetch_html(date_value):
    url = build_url(date_value)

    logger.info("Загрузка расписания: %s", url)

    timeout = aiohttp.ClientTimeout(
        total=30,
        connect=15,
        sock_read=20,
    )

    async with aiohttp.ClientSession(
        headers=HEADERS,
        timeout=timeout
    ) as session:

        # ВАЖНО:
        # у ishnk.ru самоподписанный сертификат.
        # ssl=False позволяет получить страницу.
        async with session.get(
            url,
            ssl=False,
            allow_redirects=True
        ) as response:

            response.raise_for_status()

            raw = await response.read()

            logger.info(
                "Получено HTML: %s байт",
                len(raw)
            )

            return raw


def decode_html(raw):
    """
    У сайта charset=utf-8, но функция оставлена
    максимально устойчивой к кривой кодировке.
    """

    # UTF-8
    try:
        text = raw.decode("utf-8")

        # Проверяем, что русский текст выглядит нормально
        if "Расписание" in text or "группы" in text:
            return text

    except UnicodeDecodeError:
        pass

    # Windows-1251
    try:
        text = raw.decode("cp1251")

        if "Расписание" in text or "группы" in text:
            return text

    except UnicodeDecodeError:
        pass

    # Последняя попытка
    return raw.decode(
        "utf-8",
        errors="replace"
    )


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):
    if not value:
        return ""

    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def roman_to_int(value):
    mapping = {
        "I": 1,
        "II": 2,
        "III": 3,
        "IV": 4,
        "V": 5,
        "VI": 6,
        "VII": 7,
        "VIII": 8,
        "IX": 9,
        "X": 10,
    }

    return mapping.get(value.upper())


# ============================================================
# PARSER
# ============================================================

def parse_schedule(html, date_value):
    """
    Парсер специально под структуру ishnk.ru.

    Основные карточки имеют:

        <div class="card myCard">
            <div class="card-header bg-menu ...">
                I пара
                08 30 - 09 50
            </div>

            <div class="card-body">
                ауд. УК107
                Дроздов А.П.
                Экспл Н/Г мест
            </div>
        </div>

    Таблица основного расписания ниже НЕ используется.
    """

    soup = BeautifulSoup(html, "html.parser")

    result = []

    # --------------------------------------------------------
    # Заголовок страницы
    # --------------------------------------------------------

    title_node = soup.select_one(".header3")

    page_title = ""

    if title_node:
        page_title = clean_text(
            title_node.get_text(" ", strip=True)
        )

    # --------------------------------------------------------
    # Ищем именно карточки с card-header
    # --------------------------------------------------------

    cards = soup.select(".card.myCard")

    logger.info(
        "Найдено myCard: %s",
        len(cards)
    )

    for card in cards:

        header = card.select_one(".card-header")

        if not header:
            continue

        header_text = clean_text(
            header.get_text(" ", strip=True)
        )

        # Нужны только карточки вида:
        # I пара 08:30 - 09:50
        pair_match = re.search(
            r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b\s*пара",
            header_text,
            re.IGNORECASE
        )

        time_match = re.search(
            r"(\d{1,2})\s*(?:[:.]|\s)\s*(\d{2})"
            r"\s*[-–—]\s*"
            r"(\d{1,2})\s*(?:[:.]|\s)\s*(\d{2})",
            header_text
        )

        if not pair_match or not time_match:
            continue

        pair_roman = pair_match.group(1).upper()

        pair_number = roman_to_int(pair_roman)

        if not pair_number:
            continue

        h1, m1, h2, m2 = time_match.groups()

        time_start = f"{int(h1):02d}:{int(m1):02d}"
        time_end = f"{int(h2):02d}:{int(m2):02d}"

        # ----------------------------------------------------
        # Body
        # ----------------------------------------------------

        body = card.select_one(".card-body")

        if not body:
            continue

        # ----------------------------------------------------
        # Кабинет
        # ----------------------------------------------------

        room = ""

        room_node = body.select_one(
            "span:not(.Staff)"
        )

        # Сначала ищем непосредственно "ауд."
        body_text = clean_text(
            body.get_text(" ", strip=True)
        )

        room_match = re.search(
            r"ауд\.\s*([A-Za-zА-Яа-я0-9№./-]+)",
            body_text,
            re.IGNORECASE
        )

        if room_match:
            room = room_match.group(1).strip()

        # Если regex не нашёл, ищем span.h5 рядом с "ауд."
        if not room:
            room_candidates = body.select(
                "span.h5"
            )

            for candidate in room_candidates:
                candidate_text = clean_text(
                    candidate.get_text(" ", strip=True)
                )

                if candidate_text:
                    room = candidate_text
                    break

        # ----------------------------------------------------
        # Преподаватель
        # ----------------------------------------------------

        teacher = ""

        staff = body.select_one(".Staff")

        if staff:
            teacher = clean_text(
                staff.get_text(" ", strip=True)
            )

        if not teacher:
            teacher_node = body.select_one(
                ".d-none.d-md-block"
            )

            if teacher_node:
                teacher = clean_text(
                    teacher_node.get_text(" ", strip=True)
                )

                teacher = re.sub(
                    r"^преп\.\s*",
                    "",
                    teacher,
                    flags=re.IGNORECASE
                )

        # ----------------------------------------------------
        # Предмет
        # ----------------------------------------------------

        subject = ""

        # На мобильной версии
        mobile_subject = body.select_one(
            ".d-md-none.text-center"
        )

        if mobile_subject:
            subject = clean_text(
                mobile_subject.get_text(" ", strip=True)
            )

        # На desktop версии
        if not subject:
            desktop_subject = body.select_one(
                ".d-none.d-md-block"
            )

            if desktop_subject:
                bold = desktop_subject.select_one("b")

                if bold:
                    subject = clean_text(
                        bold.get_text(" ", strip=True)
                    )
                else:
                    subject = clean_text(
                        desktop_subject.get_text(
                            " ",
                            strip=True
                        )
                    )

        # ----------------------------------------------------
        # Если что-то не нашли — дополнительный fallback
        # ----------------------------------------------------

        if not subject:
            candidates = body.select(
                ".px-3.py-1"
            )

            for candidate in candidates:
                text = clean_text(
                    candidate.get_text(
                        " ",
                        strip=True
                    )
                )

                if text:
                    subject = text
                    break

        # ----------------------------------------------------
        # Пропускаем мусор
        # ----------------------------------------------------

        if not subject and not teacher and not room:
            continue

        result.append({
            "pair": pair_number,
            "pair_roman": pair_roman,
            "start": time_start,
            "end": time_end,
            "room": room,
            "teacher": teacher,
            "subject": subject,
        })

    # --------------------------------------------------------
    # Убираем дубликаты
    # --------------------------------------------------------

    unique = []

    seen = set()

    for item in result:

        key = (
            item["pair"],
            item["start"],
            item["end"],
            item["room"],
            item["teacher"],
            item["subject"],
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    unique.sort(
        key=lambda x: (
            x["pair"],
            x["start"]
        )
    )

    logger.info(
        "Итог: %s занятий",
        len(unique)
    )

    for item in unique:
        logger.info(
            "Пара %s | %s-%s | %s | %s | %s",
            item["pair_roman"],
            item["start"],
            item["end"],
            item["room"],
            item["teacher"],
            item["subject"],
        )

    return {
        "date": date_value.isoformat(),
        "group": GROUP_NAME,
        "title": page_title,
        "lessons": unique,
    }


# ============================================================
# SCHEDULE FETCH
# ============================================================

async def get_schedule(date_value):
    raw = await fetch_html(date_value)

    html = decode_html(raw)

    return parse_schedule(
        html,
        date_value
    )


# ============================================================
# FONT SYSTEM
# ============================================================

_FONT_CACHE = {}


def find_font(bold=False):
    """
    Ищем шрифт несколькими способами.
    Не привязываемся к одному пути.
    """

    key = "bold" if bold else "regular"

    if key in _FONT_CACHE:
        return _FONT_CACHE[key]

    if bold:
        filenames = [
            "DejaVuSans-Bold.ttf",
            "NotoSans-Bold.ttf",
            "NotoSansDisplay-Bold.ttf",
        ]
    else:
        filenames = [
            "DejaVuSans.ttf",
            "NotoSans-Regular.ttf",
            "NotoSansDisplay-Regular.ttf",
        ]

    search_dirs = [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/local/share/fonts"),
    ]

    # Сначала известные пути
    for directory in search_dirs:

        if not directory.exists():
            continue

        for filename in filenames:

            path = directory / filename

            if path.exists():
                logger.info(
                    "Шрифт найден: %s",
                    path
                )

                _FONT_CACHE[key] = str(path)

                return str(path)

    # Потом вообще любой DejaVu/Noto
    patterns = [
        "/usr/share/fonts/**/*DejaVuSans*.ttf",
        "/usr/share/fonts/**/*NotoSans*.ttf",
        "/usr/local/share/fonts/**/*.ttf",
    ]

    import glob

    for pattern in patterns:

        found = glob.glob(
            pattern,
            recursive=True
        )

        if not found:
            continue

        # Для bold стараемся взять Bold
        if bold:
            bold_fonts = [
                x for x in found
                if "Bold" in x
            ]

            if bold_fonts:
                found = bold_fonts

        path = found[0]

        logger.info(
            "Шрифт найден через glob: %s",
            path
        )

        _FONT_CACHE[key] = path

        return path

    raise RuntimeError(
        "В контейнере НЕ НАЙДЕН кириллический шрифт. "
        "Новый Dockerfile должен устанавливать "
        "fonts-dejavu и fonts-noto-core."
    )


def get_font(size, bold=False):
    path = find_font(bold)

    return ImageFont.truetype(
        path,
        size=size
    )


# ============================================================
# IMAGE HELPERS
# ============================================================

def text_size(draw, text, font):
    box = draw.textbbox(
        (0, 0),
        text,
        font=font
    )

    return (
        box[2] - box[0],
        box[3] - box[1]
    )


def rounded_rectangle(
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


def fit_text(
    draw,
    text,
    font,
    max_width
):
    """
    Обрезает длинный текст, чтобы карточка
    не разъезжалась.
    """

    text = clean_text(text)

    if not text:
        return ""

    width, _ = text_size(
        draw,
        text,
        font
    )

    if width <= max_width:
        return text

    suffix = "…"

    while text:

        candidate = text + suffix

        width, _ = text_size(
            draw,
            candidate,
            font
        )

        if width <= max_width:
            return candidate

        text = text[:-1]

    return suffix


# ============================================================
# IMAGE RENDER
# ============================================================

def render_schedule(schedule):
    """
    Современный минималистичный дизайн.

    Формат:
    - мягкий серый фон
    - большая шапка
    - акцентный синий
    - белые карточки
    - крупное время
    - отдельные блоки кабинет/преподаватель
    """

    lessons = schedule["lessons"]

    date_value = datetime.strptime(
        schedule["date"],
        "%Y-%m-%d"
    ).date()

    WIDTH = 1200

    # --------------------------------------------------------
    # Fonts
    # --------------------------------------------------------

    font_title = get_font(
        52,
        bold=True
    )

    font_date = get_font(
        30,
        bold=False
    )

    font_pair = get_font(
        30,
        bold=True
    )

    font_time = get_font(
        40,
        bold=True
    )

    font_subject = get_font(
        31,
        bold=True
    )

    font_info = get_font(
        24,
        bold=False
    )

    font_small = get_font(
        21,
        bold=False
    )

    font_number = get_font(
        25,
        bold=True
    )

    # --------------------------------------------------------
    # Geometry
    # --------------------------------------------------------

    margin = 55
    card_gap = 24

    header_height = 255

    card_height = 205

    if not lessons:
        card_height = 190

    height = (
        header_height
        + 40
        + max(
            1,
            len(lessons)
        ) * card_height
        + max(
            0,
            len(lessons) - 1
        ) * card_gap
        + 70
    )

    image = Image.new(
        "RGB",
        (WIDTH, height),
        "#F5F7FB"
    )

    draw = ImageDraw.Draw(image)

    # --------------------------------------------------------
    # Colors
    # --------------------------------------------------------

    BLUE = "#4F46E5"
    BLUE_LIGHT = "#EEF0FF"

    TEXT = "#172033"
    MUTED = "#70798A"

    WHITE = "#FFFFFF"

    BORDER = "#E4E8F0"

    GREEN = "#16A34A"
    GREEN_LIGHT = "#EAF8EF"

    # --------------------------------------------------------
    # Header
    # --------------------------------------------------------

    draw.rectangle(
        (0, 0, WIDTH, header_height),
        fill=WHITE
    )

    # accent
    draw.rectangle(
        (0, 0, 16, header_height),
        fill=BLUE
    )

    # маленький label
    label = "РАСПИСАНИЕ"

    draw.text(
        (margin, 38),
        label,
        font=font_small,
        fill=BLUE
    )

    # группа
    group_text = GROUP_NAME

    draw.text(
        (margin, 78),
        group_text,
        font=font_title,
        fill=TEXT
    )

    # дата
    date_text = format_date(
        date_value
    )

    draw.text(
        (margin, 153),
        date_text,
        font=font_date,
        fill=MUTED
    )

    # количество занятий
    if lessons:
        count_text = (
            f"{len(lessons)} "
            + (
                "занятие"
                if len(lessons) == 1
                else "занятий"
            )
        )
    else:
        count_text = "занятий нет"

    badge_w = 235
    badge_h = 58

    badge_x = WIDTH - margin - badge_w
    badge_y = 48

    rounded_rectangle(
        draw,
        (
            badge_x,
            badge_y,
            badge_x + badge_w,
            badge_y + badge_h
        ),
        29,
        fill=BLUE_LIGHT
    )

    count_width, count_height = text_size(
        draw,
        count_text,
        font_small
    )

    draw.text(
        (
            badge_x + (badge_w - count_width) / 2,
            badge_y + (badge_h - count_height) / 2 - 2
        ),
        count_text,
        font=font_small,
        fill=BLUE
    )

    # --------------------------------------------------------
    # Empty schedule
    # --------------------------------------------------------

    if not lessons:

        box_x1 = margin
        box_y1 = header_height + 45

        box_x2 = WIDTH - margin
        box_y2 = box_y1 + 190

        rounded_rectangle(
            draw,
            (box_x1, box_y1, box_x2, box_y2),
            28,
            fill=WHITE,
            outline=BORDER,
            width=2
        )

        title = "Занятий нет"

        tw, th = text_size(
            draw,
            title,
            font_subject
        )

        draw.text(
            (
                (WIDTH - tw) / 2,
                box_y1 + 55
            ),
            title,
            font=font_subject,
            fill=TEXT
        )

        subtitle = "Расписание на этот день не опубликовано."

        sw, sh = text_size(
            draw,
            subtitle,
            font_small
        )

        draw.text(
            (
                (WIDTH - sw) / 2,
                box_y1 + 105
            ),
            subtitle,
            font=font_small,
            fill=MUTED
        )

    # --------------------------------------------------------
    # Lesson cards
    # --------------------------------------------------------

    else:

        y = header_height + 40

        for index, lesson in enumerate(lessons):

            x1 = margin
            x2 = WIDTH - margin

            y1 = y
            y2 = y + card_height

            # shadow
            rounded_rectangle(
                draw,
                (
                    x1 + 4,
                    y1 + 6,
                    x2 + 4,
                    y2 + 6
                ),
                26,
                fill="#E9EDF5"
            )

            # card
            rounded_rectangle(
                draw,
                (
                    x1,
                    y1,
                    x2,
                    y2
                ),
                26,
                fill=WHITE,
                outline=BORDER,
                width=2
            )

            # ------------------------------------------------
            # Number
            # ------------------------------------------------

            circle_size = 62

            cx = x1 + 40
            cy = y1 + 34

            draw.ellipse(
                (
                    cx,
                    cy,
                    cx + circle_size,
                    cy + circle_size
                ),
                fill=BLUE
            )

            roman = lesson["pair_roman"]

            rw, rh = text_size(
                draw,
                roman,
                font_number
            )

            draw.text(
                (
                    cx + (circle_size - rw) / 2,
                    cy + (circle_size - rh) / 2 - 3
                ),
                roman,
                font=font_number,
                fill=WHITE
            )

            # ------------------------------------------------
            # Time
            # ------------------------------------------------

            time_text = (
                f"{lesson['start']} — "
                f"{lesson['end']}"
            )

            draw.text(
                (
                    x1 + 125,
                    y1 + 29
                ),
                time_text,
                font=font_time,
                fill=TEXT
            )

            # ------------------------------------------------
            # Subject
            # ------------------------------------------------

            subject = lesson["subject"] or "Предмет не указан"

            subject = fit_text(
                draw,
                subject,
                font_subject,
                WIDTH - 250
            )

            draw.text(
                (
                    x1 + 125,
                    y1 + 93
                ),
                subject,
                font=font_subject,
                fill=BLUE
            )

            # ------------------------------------------------
            # Bottom info
            # ------------------------------------------------

            info_y = y1 + 153

            room = lesson["room"]

            if room:
                room_text = f"ауд. {room}"
            else:
                room_text = "ауд. —"

            teacher = lesson["teacher"]

            if not teacher:
                teacher = "Преподаватель не указан"

            teacher = fit_text(
                draw,
                teacher,
                font_info,
                570
            )

            # room badge
            room_width, room_height = text_size(
                draw,
                room_text,
                font_small
            )

            room_box_width = room_width + 34

            rounded_rectangle(
                draw,
                (
                    x1 + 125,
                    info_y - 7,
                    x1 + 125 + room_box_width,
                    info_y + 40
                ),
                23,
                fill=GREEN_LIGHT
            )

            draw.text(
                (
                    x1 + 125 + 17,
                    info_y + 2
                ),
                room_text,
                font=font_small,
                fill=GREEN
            )

            # teacher
            draw.text(
                (
                    x1 + 125 + room_box_width + 24,
                    info_y + 2
                ),
                teacher,
                font=font_info,
                fill=MUTED
            )

            y += card_height + card_gap

    # --------------------------------------------------------
    # Footer
    # --------------------------------------------------------

    footer_y = height - 42

    footer = "ИНК • расписание"

    fw, fh = text_size(
        draw,
        footer,
        font_small
    )

    draw.text(
        (
            WIDTH - margin - fw,
            footer_y
        ),
        footer,
        font=font_small,
        fill="#A1A8B5"
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    filename = (
        f"{GROUP_NAME}_"
        f"{date_value.isoformat()}.png"
    )

    path = IMAGE_DIR / filename

    image.save(
        path,
        "PNG",
        optimize=True
    )

    logger.info(
        "Изображение сохранено: %s",
        path
    )

    return path


# ============================================================
# SIGNATURE
# ============================================================

def schedule_signature(schedule):
    data = json.dumps(
        schedule,
        ensure_ascii=False,
        sort_keys=True
    )

    return hashlib.sha256(
        data.encode("utf-8")
    ).hexdigest()


# ============================================================
# TELEGRAM TEXT
# ============================================================

def schedule_text(schedule):
    date_value = datetime.strptime(
        schedule["date"],
        "%Y-%m-%d"
    ).date()

    lines = [
        f"📚 <b>{GROUP_NAME}</b>",
        f"📅 {format_date(date_value)}",
        ""
    ]

    lessons = schedule["lessons"]

    if not lessons:
        lines.append(
            "☕ <b>Занятий нет.</b>"
        )

        return "\n".join(lines)

    for lesson in lessons:

        lines.append(
            f"<b>{lesson['pair_roman']} пара</b> "
            f"· {lesson['start']}–{lesson['end']}"
        )

        lines.append(
            f"📘 {lesson['subject'] or '—'}"
        )

        lines.append(
            f"📍 {lesson['room'] or 'аудитория не указана'}"
        )

        if lesson["teacher"]:
            lines.append(
                f"👤 {lesson['teacher']}"
            )

        lines.append("")

    return "\n".join(lines)


# ============================================================
# SEND SCHEDULE
# ============================================================

async def send_schedule(
    message,
    date_value=None
):
    if date_value is None:
        date_value = today()

    try:

        schedule = await get_schedule(
            date_value
        )

        image_path = render_schedule(
            schedule
        )

        caption = (
            f"📚 <b>{GROUP_NAME}</b>\n"
            f"📅 {format_date(date_value)}"
        )

        await message.answer_photo(
            photo=FSInputFile(
                image_path
            ),
            caption=caption
        )

        return schedule

    except Exception:

        logger.exception(
            "Ошибка send_schedule"
        )

        await message.answer(
            "Не удалось получить расписание. "
            "Подробность ошибки записана в лог."
        )

        return None


# ============================================================
# COMMANDS
# ============================================================

dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):

    text = (
        f"📚 <b>Расписание {GROUP_NAME}</b>\n\n"
        "Команды:\n"
        "• /schedule — расписание на сегодня\n"
        "• /tomorrow — расписание на завтра\n"
        "• /scheduletext — расписание текстом\n"
        "• /subscribe — включить уведомления\n"
        "• /unsubscribe — отключить уведомления\n"
        "• /checknow — проверить завтра\n\n"
        "🔔 При изменении расписания подписчики "
        "получат новое расписание автоматически."
    )

    await message.answer(text)


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):

    await send_schedule(
        message,
        today()
    )


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message):

    await send_schedule(
        message,
        tomorrow()
    )


@dp.message(Command("scheduletext"))
async def cmd_scheduletext(message: Message):

    try:

        schedule = await get_schedule(
            today()
        )

        await message.answer(
            schedule_text(schedule)
        )

    except Exception:

        logger.exception(
            "Ошибка scheduletext"
        )

        await message.answer(
            "Не удалось получить расписание."
        )


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):

    subscribers = load_subscribers()

    user_id = message.from_user.id

    if user_id not in subscribers:

        subscribers.append(user_id)

        save_subscribers(
            subscribers
        )

        await message.answer(
            "🔔 <b>Уведомления включены.</b>\n\n"
            "Я буду автоматически проверять "
            "изменения расписания."
        )

    else:

        await message.answer(
            "🔔 Вы уже подписаны."
        )


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):

    subscribers = load_subscribers()

    user_id = message.from_user.id

    if user_id in subscribers:

        subscribers.remove(user_id)

        save_subscribers(
            subscribers
        )

        await message.answer(
            "🔕 <b>Уведомления отключены.</b>"
        )

    else:

        await message.answer(
            "Вы не были подписаны."
        )


@dp.message(Command("checknow"))
async def cmd_checknow(message: Message):

    # Проверяем завтра.
    # Если завтра расписания нет —
    # отправляем сегодняшнее.

    target = tomorrow()

    try:

        schedule = await get_schedule(
            target
        )

        if schedule["lessons"]:

            await send_schedule(
                message,
                target
            )

            return

        # ----------------------------------------------------
        # Завтра пусто -> сегодня
        # ----------------------------------------------------

        logger.info(
            "На %s занятий нет. "
            "Используем сегодняшнее расписание.",
            target
        )

        await message.answer(
            "ℹ️ На завтра расписание не опубликовано.\n"
            "Показываю сегодняшнее:"
        )

        await send_schedule(
            message,
            today()
        )

    except Exception:

        logger.exception(
            "Ошибка checknow"
        )

        await message.answer(
            "Не удалось проверить расписание."
        )


# ============================================================
# AUTOMATIC NOTIFICATIONS
# ============================================================

async def check_date_and_notify(
    bot,
    date_value
):
    """
    Проверяет конкретную дату.

    Первый увиденный вариант просто сохраняется.
    Пользователям он НЕ отправляется.

    Если расписание реально изменилось —
    отправляем новую картинку.
    """

    date_key = date_value.isoformat()

    try:

        schedule = await get_schedule(
            date_value
        )

        signature = schedule_signature(
            schedule
        )

        cache = load_cache()

        old_signature = cache.get(
            date_key
        )

        # ----------------------------------------------------
        # Первый запуск
        # ----------------------------------------------------

        if old_signature is None:

            cache[date_key] = signature

            save_cache(cache)

            logger.info(
                "Первичная фиксация расписания: %s",
                date_key
            )

            return

        # ----------------------------------------------------
        # Ничего не изменилось
        # ----------------------------------------------------

        if old_signature == signature:

            logger.info(
                "Без изменений: %s",
                date_key
            )

            return

        # ----------------------------------------------------
        # ИЗМЕНЕНИЕ
        # ----------------------------------------------------

        logger.info(
            "РАСПИСАНИЕ ИЗМЕНИЛОСЬ: %s",
            date_key
        )

        cache[date_key] = signature

        save_cache(cache)

        subscribers = load_subscribers()

        if not subscribers:
            return

        image_path = render_schedule(
            schedule
        )

        caption = (
            "🔄 <b>Расписание изменилось</b>\n\n"
            f"📚 <b>{GROUP_NAME}</b>\n"
            f"📅 {format_date(date_value)}"
        )

        dead_users = []

        for user_id in subscribers:

            try:

                await bot.send_photo(
                    user_id,
                    photo=FSInputFile(
                        image_path
                    ),
                    caption=caption
                )

                await asyncio.sleep(0.08)

            except Exception as error:

                logger.warning(
                    "Не удалось отправить %s: %s",
                    user_id,
                    error
                )

                # Если пользователь заблокировал бота,
                # при следующей проверке уберём его.
                error_text = str(error).lower()

                if (
                    "blocked" in error_text
                    or "chat not found" in error_text
                    or "deactivated" in error_text
                ):
                    dead_users.append(
                        user_id
                    )

        if dead_users:

            subscribers = [
                x for x in subscribers
                if x not in dead_users
            ]

            save_subscribers(
                subscribers
            )

    except Exception:

        logger.exception(
            "Ошибка автоматической проверки %s",
            date_key
        )


async def automatic_checker(bot):

    logger.info(
        "Автоматическая проверка запущена."
    )

    while True:

        try:

            current = today()

            # Проверяем сегодня
            await check_date_and_notify(
                bot,
                current
            )

            # Проверяем завтра
            await check_date_and_notify(
                bot,
                current + timedelta(days=1)
            )

        except Exception:

            logger.exception(
                "Ошибка automatic_checker"
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info("=" * 60)
    logger.info(
        "Schedule bot started"
    )
    logger.info(
        "Group: %s",
        GROUP_NAME
    )
    logger.info(
        "URL: %s",
        SCHEDULE_URL
    )
    logger.info(
        "Check interval: %s sec",
        CHECK_INTERVAL
    )

    # Проверяем шрифт ДО запуска Telegram.
    # Если его нет — сразу увидим нормальную ошибку.
    logger.info(
        "Regular font: %s",
        find_font(False)
    )

    logger.info(
        "Bold font: %s",
        find_font(True)
    )

    bot = Bot(
        token=BOT_TOKEN
    )

    checker_task = asyncio.create_task(
        automatic_checker(bot)
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
    asyncio.run(main())
