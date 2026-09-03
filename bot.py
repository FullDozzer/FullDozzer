import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://www.ishnk.ru/2025/site/schedule/group/508"
).rstrip("/")

GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(TIMEZONE_NAME)

SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CACHE_FILE = DATA_DIR / "schedule_cache.json"
IMAGES_DIR = DATA_DIR / "images"

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("schedule_bot")


# ============================================================
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "Не указан BOT_TOKEN. Добавь его в .env"
    )


# ============================================================
# GLOBALS
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

http_session: aiohttp.ClientSession | None = None

subscribers_lock = asyncio.Lock()
cache_lock = asyncio.Lock()
fetch_lock = asyncio.Lock()


# ============================================================
# FILE HELPERS
# ============================================================

def load_json(path: Path, default):
    try:
        if not path.exists():
            return default

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        logger.exception("Ошибка чтения %s", path)
        return default


def save_json(path: Path, data):
    temp = path.with_suffix(path.suffix + ".tmp")

    with temp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp.replace(path)


# ============================================================
# SUBSCRIBERS
# ============================================================

def load_subscribers() -> set[int]:
    data = load_json(SUBSCRIBERS_FILE, [])

    result = set()

    if isinstance(data, list):
        for item in data:
            try:
                result.add(int(item))
            except Exception:
                pass

    return result


SUBSCRIBERS: set[int] = load_subscribers()


async def save_subscribers():
    async with subscribers_lock:
        save_json(
            SUBSCRIBERS_FILE,
            sorted(SUBSCRIBERS),
        )


# ============================================================
# CACHE
# ============================================================

SCHEDULE_CACHE = load_json(CACHE_FILE, {})


async def save_cache():
    async with cache_lock:
        save_json(
            CACHE_FILE,
            SCHEDULE_CACHE,
        )


# ============================================================
# DATE HELPERS
# ============================================================

RU_WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}

RU_MONTHS = {
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


def now_moscow() -> datetime:
    return datetime.now(TZ)


def today_date() -> date:
    return now_moscow().date()


def format_date_ru(value: date) -> str:
    return (
        f"{RU_WEEKDAYS[value.weekday()]}, "
        f"{value.day} {RU_MONTHS[value.month]} {value.year} года"
    )


def format_date_short(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def parse_user_date(value: str) -> date | None:
    value = value.strip()

    formats = [
        "%d.%m.%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    return None


# ============================================================
# URL
# ============================================================

def build_schedule_url(target_date: date) -> str:
    return f"{SCHEDULE_URL}/{target_date.isoformat()}"


# ============================================================
# HTTP / ENCODING
# ============================================================

def decode_html(raw: bytes, content_type: str = "") -> str:
    """
    Сайт может отдавать HTML в Windows-1251.
    Сначала пытаемся определить кодировку из HTTP/meta,
    затем перебираем наиболее вероятные варианты.
    """

    # HTTP header
    match = re.search(
        r"charset\s*=\s*['\"]?([a-zA-Z0-9_\-]+)",
        content_type or "",
        re.I,
    )

    if match:
        encoding = match.group(1).lower()

        try:
            return raw.decode(encoding, errors="replace")
        except Exception:
            pass

    # HTML meta charset
    head = raw[:10000]

    meta_match = re.search(
        rb'<meta[^>]+charset\s*=\s*["\']?\s*([a-zA-Z0-9_\-]+)',
        head,
        re.I,
    )

    if meta_match:
        try:
            encoding = meta_match.group(1).decode(
                "ascii",
                errors="ignore",
            ).lower()

            return raw.decode(
                encoding,
                errors="replace",
            )

        except Exception:
            pass

    # Most likely encodings for this site
    candidates = [
        "utf-8",
        "cp1251",
        "windows-1251",
    ]

    best_text = None
    best_score = -1

    for encoding in candidates:
        try:
            text = raw.decode(
                encoding,
                errors="replace",
            )

            score = 0

            # Russian Cyrillic
            score += len(
                re.findall(
                    r"[А-Яа-яЁё]",
                    text,
                )
            )

            # Mojibake penalties
            score -= text.count("�") * 50
            score -= text.count("Р") * 2
            score -= text.count("С") * 2

            if score > best_score:
                best_score = score
                best_text = text

        except Exception:
            pass

    if best_text is not None:
        logger.info(
            "Выбрана кодировка HTML, score=%s",
            best_score,
        )
        return best_text

    return raw.decode(
        "utf-8",
        errors="replace",
    )


async def fetch_html(target_date: date) -> str:
    global http_session

    url = build_schedule_url(target_date)

    logger.info(
        "Загрузка расписания: %s",
        url,
    )

    headers = {
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
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        "Connection": "keep-alive",
    }

    async with fetch_lock:

        if http_session is None:
            timeout = aiohttp.ClientTimeout(
                total=30,
                connect=15,
            )

            http_session = aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            )

        try:
            async with http_session.get(
                url,
                ssl=False,
                allow_redirects=True,
            ) as response:

                logger.info(
                    "HTTP %s: %s",
                    response.status,
                    url,
                )

                response.raise_for_status()

                raw = await response.read()

                logger.info(
                    "Получено HTML: %s байт",
                    len(raw),
                )

                return decode_html(
                    raw,
                    response.headers.get(
                        "Content-Type",
                        "",
                    ),
                )

        except Exception:
            logger.exception(
                "Ошибка загрузки %s",
                url,
            )
            raise


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value: str) -> str:
    if not value:
        return ""

    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")
    value = value.replace("\r", " ")
    value = value.replace("\n", " ")

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def normalize_spaces(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


# ============================================================
# TIME PARSER
# ============================================================

TIME_RE = re.compile(
    r"""
    (?P<h1>\d{1,2})
    \s*
    (?P<m1>\d{2})
    \s*
    -
    \s*
    (?P<h2>\d{1,2})
    \s*
    (?P<m2>\d{2})
    """,
    re.VERBOSE,
)


def normalize_time_text(text: str) -> str:
    """
    Преобразует:
        08 30 - 09 50
    в:
        08:30 - 09:50
    """

    match = TIME_RE.search(text)

    if not match:
        return ""

    h1 = int(match.group("h1"))
    m1 = int(match.group("m1"))
    h2 = int(match.group("h2"))
    m2 = int(match.group("m2"))

    return (
        f"{h1:02d}:{m1:02d} - "
        f"{h2:02d}:{m2:02d}"
    )


# ============================================================
# ROMAN NUMERALS
# ============================================================

ROMAN_TO_INT = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
}


def parse_pair_number(header) -> int:
    pair = header.select_one(".h3")

    if pair:
        value = clean_text(pair.get_text(" ", strip=True))
    else:
        value = clean_text(
            header.get_text(" ", strip=True)
        )

    for roman, number in ROMAN_TO_INT.items():
        if re.search(
            rf"\b{re.escape(roman)}\b",
            value,
        ):
            return number

    return 0


# ============================================================
# SCHEDULE CARD PARSER
# ============================================================

def parse_schedule_cards(html: str) -> list[dict]:
    """
    Парсер именно фактической структуры ishnk.ru:

    <div class="card myCard">
        <div class="card-header ...">
            <span class="h3">I</span> пара
            <span class="h4">08<sup>30</sup> - 09<sup>50</sup></span>
        </div>

        <div class="card-body">
            ...
            <span>ауд.<span class="h5">УК107</span></span>
            <span class="Staff" title="...">...</span>
            ...
            <div ... title="...">Экспл Н/Г мест</div>
        </div>
    </div>
    """

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    cards = soup.select(
        "div.card.myCard"
    )

    result = []

    logger.info(
        "Найдено элементов .card.myCard: %s",
        len(cards),
    )

    for index, card in enumerate(cards, start=1):

        header = card.select_one(
            ".card-header"
        )

        if not header:
            # Это может быть большая таблица
            # "Основное расписание".
            continue

        header_text = normalize_spaces(
            header.get_text(
                " ",
                strip=True,
            )
        )

        time_match = TIME_RE.search(
            header_text
        )

        if not time_match:
            continue

        pair_number = parse_pair_number(
            header
        )

        time_value = normalize_time_text(
            header_text
        )

        if not time_value:
            continue

        # ----------------------------------------------------
        # АУДИТОРИЯ
        # ----------------------------------------------------

        room = ""

        room_node = card.select_one(
            ".card-body span span.h5"
        )

        if room_node:
            room = clean_text(
                room_node.get_text(
                    " ",
                    strip=True,
                )
            )

        # Более точный fallback.
        if not room:
            body_text = normalize_spaces(
                card.select_one(".card-body").get_text(
                    " ",
                    strip=True,
                )
                if card.select_one(".card-body")
                else ""
            )

            room_match = re.search(
                r"ауд\.\s*([A-Za-zА-Яа-яЁё0-9\-]+)",
                body_text,
                re.I,
            )

            if room_match:
                room = room_match.group(1)

        # ----------------------------------------------------
        # ПРЕПОДАВАТЕЛЬ
        # ----------------------------------------------------

        teacher = ""

        staff = card.select_one(
            ".Staff"
        )

        if staff:

            # На сайте полное имя находится
            # в title.
            teacher = clean_text(
                staff.get("title", "")
            )

            if not teacher:
                teacher = clean_text(
                    staff.get_text(
                        " ",
                        strip=True,
                    )
                )

        # Если Staff нет — пробуем desktop блок.
        if not teacher:

            teacher_node = card.select_one(
                ".d-none.d-md-block .h5"
            )

            if teacher_node:
                teacher = clean_text(
                    teacher_node.get_text(
                        " ",
                        strip=True,
                    )
                )

        # ----------------------------------------------------
        # ПРЕДМЕТ
        # ----------------------------------------------------

        subject = ""

        # Мобильный вариант.
        subject_node = card.select_one(
            ".card-body .d-md-none[title]"
        )

        if subject_node:
            subject = clean_text(
                subject_node.get_text(
                    " ",
                    strip=True,
                )
            )

        # Desktop fallback.
        if not subject:

            subject_node = card.select_one(
                ".card-body .d-none.d-md-block b"
            )

            if subject_node:
                subject = clean_text(
                    subject_node.get_text(
                        " ",
                        strip=True,
                    )
                )

        # Ещё один fallback через title.
        if not subject:

            candidates = card.select(
                "[title]"
            )

            for node in candidates:
                title = clean_text(
                    node.get("title", "")
                )

                if (
                    title
                    and len(title) > 5
                    and "Александр" not in title
                    and "Закария" not in title
                ):
                    subject = clean_text(
                        node.get_text(
                            " ",
                            strip=True,
                        )
                    )

                    if subject:
                        break

        # ----------------------------------------------------
        # ПЕРЕМЕНА
        # ----------------------------------------------------

        break_value = ""

        break_match = re.search(
            r"перемена\s+(\d+)\s*мин",
            header_text,
            re.I,
        )

        if break_match:
            break_value = (
                f"{break_match.group(1)} мин"
            )

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        lesson = {
            "pair": pair_number,
            "time": time_value,
            "room": room,
            "teacher": teacher,
            "subject": subject,
            "break": break_value,
        }

        # Защита от мусора.
        if (
            pair_number > 0
            and time_value
            and (
                subject
                or teacher
                or room
            )
        ):
            result.append(
                lesson
            )

            logger.info(
                "Пара %s | %s | %s | %s | %s",
                pair_number,
                time_value,
                room or "-",
                teacher or "-",
                subject or "-",
            )

    # Сортировка.
    result.sort(
        key=lambda x: (
            x.get("pair", 999),
            x.get("time", ""),
        )
    )

    # Защита от дублей.
    unique = []
    seen = set()

    for item in result:

        key = (
            item["pair"],
            item["time"],
            item["room"],
            item["teacher"],
            item["subject"],
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    logger.info(
        "Итог: %s занятий",
        len(unique),
    )

    return unique


# ============================================================
# MAIN PARSER
# ============================================================

async def get_schedule(
    target_date: date,
) -> list[dict]:

    html = await fetch_html(
        target_date
    )

    schedule = parse_schedule_cards(
        html
    )

    return schedule


# ============================================================
# TOMORROW FALLBACK
# ============================================================

async def get_schedule_with_fallback(
    requested_date: date,
) -> tuple[date, list[dict], bool]:

    """
    Возвращает:

        фактическую дату,
        расписание,
        использовался ли fallback.

    Если запрашивается завтра и завтра пусто,
    возвращаем расписание сегодня.
    """

    schedule = await get_schedule(
        requested_date
    )

    if schedule:
        return (
            requested_date,
            schedule,
            False,
        )

    # Особое правило только для завтра.
    if requested_date == today_date() + timedelta(days=1):

        today = today_date()

        logger.info(
            "На завтра (%s) занятий нет. "
            "Пробуем отправить сегодняшнее расписание (%s).",
            requested_date,
            today,
        )

        today_schedule = await get_schedule(
            today
        )

        if today_schedule:
            return (
                today,
                today_schedule,
                True,
            )

    return (
        requested_date,
        [],
        False,
    )


# ============================================================
# SCHEDULE SIGNATURE
# ============================================================

def schedule_signature(
    target_date: date,
    schedule: list[dict],
) -> str:

    payload = {
        "date": target_date.isoformat(),
        "group": GROUP_NAME,
        "schedule": schedule,
    }

    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# ============================================================
# FONT
# ============================================================

FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]

FONT_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def get_font(
    size: int,
    bold: bool = False,
):
    paths = (
        FONT_PATHS_BOLD
        if bold
        else FONT_PATHS_REGULAR
    )

    for path in paths:

        if os.path.exists(path):
            return ImageFont.truetype(
                path,
                size,
            )

    # Не падаем молча.
    raise RuntimeError(
        "Не найден шрифт DejaVu Sans. "
        "Пересобери Docker image с fonts-dejavu."
    )


# ============================================================
# IMAGE HELPERS
# ============================================================

def rounded_box(
    draw,
    xy,
    radius,
    fill,
    outline=None,
    width=1,
):
    draw.rounded_rectangle(
        xy,
        radius=radius,
        fill=fill,
        outline=outline,
        width=width,
    )


def fit_text(
    draw,
    text: str,
    font,
    max_width: int,
) -> str:

    if not text:
        return ""

    if draw.textbbox(
        (0, 0),
        text,
        font=font,
    )[2] <= max_width:
        return text

    result = text

    while len(result) > 3:

        result = result[:-1]

        candidate = result.rstrip() + "…"

        width = draw.textbbox(
            (0, 0),
            candidate,
            font=font,
        )[2]

        if width <= max_width:
            return candidate

    return "…"


def draw_label_value(
    draw,
    x,
    y,
    label,
    value,
    label_font,
    value_font,
    max_width,
):
    if not value:
        return

    draw.text(
        (x, y),
        label,
        font=label_font,
        fill="#8A94A6",
    )

    label_width = draw.textbbox(
        (0, 0),
        label,
        font=label_font,
    )[2]

    value = fit_text(
        draw,
        value,
        value_font,
        max_width - label_width - 10,
    )

    draw.text(
        (
            x + label_width + 8,
            y,
        ),
        value,
        font=value_font,
        fill="#1E293B",
    )


# ============================================================
# IMAGE RENDER
# ============================================================

def render_schedule(
    target_date: date,
    schedule: list[dict],
    fallback_used: bool = False,
) -> Path:

    width = 1200

    margin = 55
    header_height = 220

    card_gap = 24
    card_height = 205

    if schedule:
        height = (
            margin
            + header_height
            + len(schedule) * card_height
            + max(0, len(schedule) - 1) * card_gap
            + margin
        )
    else:
        height = (
            margin
            + header_height
            + 300
            + margin
        )

    image = Image.new(
        "RGB",
        (width, height),
        "#F5F7FB",
    )

    draw = ImageDraw.Draw(
        image
    )

    # Fonts
    font_title = get_font(
        46,
        bold=True,
    )

    font_date = get_font(
        27,
        bold=False,
    )

    font_small = get_font(
        23,
        bold=False,
    )

    font_pair = get_font(
        30,
        bold=True,
    )

    font_time = get_font(
        27,
        bold=True,
    )

    font_subject = get_font(
        31,
        bold=True,
    )

    font_value = get_font(
        24,
        bold=False,
    )

    font_footer = get_font(
        21,
        bold=False,
    )

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    # Акцентная вертикальная линия.
    draw.rounded_rectangle(
        (
            margin,
            margin,
            margin + 10,
            margin + 150,
        ),
        radius=5,
        fill="#2563EB",
    )

    draw.text(
        (
            margin + 35,
            margin + 3,
        ),
        "РАСПИСАНИЕ",
        font=font_title,
        fill="#111827",
    )

    draw.text(
        (
            margin + 37,
            margin + 70,
        ),
        f"Группа {GROUP_NAME}",
        font=font_date,
        fill="#64748B",
    )

    date_text = format_date_ru(
        target_date
    )

    draw.text(
        (
            margin + 37,
            margin + 115,
        ),
        date_text,
        font=font_small,
        fill="#2563EB",
    )

    # Маленький badge.
    badge_text = "ИНК"

    bbox = draw.textbbox(
        (0, 0),
        badge_text,
        font=font_small,
    )

    badge_w = (
        bbox[2] - bbox[0] + 38
    )
    badge_h = 48

    badge_x = width - margin - badge_w
    badge_y = margin + 15

    rounded_box(
        draw,
        (
            badge_x,
            badge_y,
            badge_x + badge_w,
            badge_y + badge_h,
        ),
        24,
        "#E8F0FF",
    )

    draw.text(
        (
            badge_x + 19,
            badge_y + 8,
        ),
        badge_text,
        font=font_small,
        fill="#2563EB",
    )

    # Fallback notice.
    if fallback_used:

        notice = (
            "На завтра расписания нет · "
            "показано расписание на сегодня"
        )

        notice_font = get_font(
            20,
            bold=False,
        )

        notice_y = (
            margin
            + header_height
            - 35
        )

        draw.text(
            (
                margin,
                notice_y,
            ),
            notice,
            font=notice_font,
            fill="#64748B",
        )

    # --------------------------------------------------------
    # EMPTY
    # --------------------------------------------------------

    if not schedule:

        box_top = (
            margin
            + header_height
        )

        rounded_box(
            draw,
            (
                margin,
                box_top,
                width - margin,
                box_top + 230,
            ),
            28,
            "#FFFFFF",
            "#E2E8F0",
            2,
        )

        empty_title = "Сегодня свободный день"

        bbox = draw.textbbox(
            (0, 0),
            empty_title,
            font=font_subject,
        )

        title_w = (
            bbox[2] - bbox[0]
        )

        draw.text(
            (
                (width - title_w) / 2,
                box_top + 65,
            ),
            empty_title,
            font=font_subject,
            fill="#1E293B",
        )

        empty_text = (
            "Занятий по расписанию нет"
        )

        bbox = draw.textbbox(
            (0, 0),
            empty_text,
            font=font_small,
        )

        text_w = (
            bbox[2] - bbox[0]
        )

        draw.text(
            (
                (width - text_w) / 2,
                box_top + 125,
            ),
            empty_text,
            font=font_small,
            fill="#94A3B8",
        )

        output = (
            IMAGES_DIR
            / f"schedule_{target_date.isoformat()}.png"
        )

        image.save(
            output,
            "PNG",
            optimize=True,
        )

        return output

    # --------------------------------------------------------
    # LESSON CARDS
    # --------------------------------------------------------

    y = (
        margin
        + header_height
    )

    for lesson in schedule:

        # Card
        rounded_box(
            draw,
            (
                margin,
                y,
                width - margin,
                y + card_height,
            ),
            28,
            "#FFFFFF",
            "#E2E8F0",
            2,
        )

        # Left accent.
        draw.rounded_rectangle(
            (
                margin,
                y + 25,
                margin + 7,
                y + card_height - 25,
            ),
            radius=4,
            fill="#2563EB",
        )

        # Pair circle.
        circle_x = margin + 38
        circle_y = y + 30
        circle_size = 68

        draw.ellipse(
            (
                circle_x,
                circle_y,
                circle_x + circle_size,
                circle_y + circle_size,
            ),
            fill="#EFF6FF",
        )

        pair_text = str(
            lesson.get("pair", "")
        )

        bbox = draw.textbbox(
            (0, 0),
            pair_text,
            font=font_pair,
        )

        pair_w = (
            bbox[2] - bbox[0]
        )

        pair_h = (
            bbox[3] - bbox[1]
        )

        draw.text(
            (
                circle_x
                + (circle_size - pair_w) / 2,
                circle_y
                + (circle_size - pair_h) / 2
                - 4,
            ),
            pair_text,
            font=font_pair,
            fill="#2563EB",
        )

        # "ПАРА"
        draw.text(
            (
                circle_x + 86,
                y + 27,
            ),
            "ПАРА",
            font=font_small,
            fill="#94A3B8",
        )

        # Time
        draw.text(
            (
                circle_x + 86,
                y + 59,
            ),
            lesson.get("time", ""),
            font=font_time,
            fill="#111827",
        )

        # Subject
        subject = (
            lesson.get("subject")
            or "Занятие"
        )

        subject = fit_text(
            draw,
            subject,
            font_subject,
            650,
        )

        draw.text(
            (
                circle_x + 86,
                y + 108,
            ),
            subject,
            font=font_subject,
            fill="#111827",
        )

        # Right info.
        right_x = 760
        info_y = y + 38

        room = lesson.get(
            "room",
            "",
        )

        teacher = lesson.get(
            "teacher",
            "",
        )

        if room:
            draw_label_value(
                draw,
                right_x,
                info_y,
                "АУД.",
                room,
                font_small,
                font_value,
                320,
            )

        if teacher:

            teacher_display = teacher

            # Полное имя помещаем в карточку.
            teacher_display = fit_text(
                draw,
                teacher_display,
                font_value,
                320,
            )

            draw.text(
                (
                    right_x,
                    info_y + 52,
                ),
                "ПРЕПОДАВАТЕЛЬ",
                font=font_small,
                fill="#8A94A6",
            )

            draw.text(
                (
                    right_x,
                    info_y + 85,
                ),
                teacher_display,
                font=font_value,
                fill="#1E293B",
            )

        # Break
        break_value = lesson.get(
            "break",
            "",
        )

        if break_value:

            draw.text(
                (
                    right_x,
                    y + 153,
                ),
                f"перемена {break_value}",
                font=font_footer,
                fill="#94A3B8",
            )

        y += (
            card_height
            + card_gap
        )

    # --------------------------------------------------------
    # FOOTER
    # --------------------------------------------------------

    footer_y = height - 45

    footer_text = (
        "ИНК · расписание автоматически"
    )

    draw.text(
        (
            margin,
            footer_y,
        ),
        footer_text,
        font=font_footer,
        fill="#94A3B8",
    )

    output = (
        IMAGES_DIR
        / f"schedule_{target_date.isoformat()}.png"
    )

    image.save(
        output,
        "PNG",
        optimize=True,
    )

    return output


# ============================================================
# TEXT VERSION
# ============================================================

def schedule_to_text(
    target_date: date,
    schedule: list[dict],
) -> str:

    if not schedule:

        return (
            f"📅 <b>{format_date_ru(target_date)}</b>\n\n"
            "Занятий нет."
        )

    lines = [
        f"📅 <b>{format_date_ru(target_date)}</b>",
        f"🎓 <b>{GROUP_NAME}</b>",
        "",
    ]

    for lesson in schedule:

        pair = lesson.get(
            "pair",
            "",
        )

        time_value = lesson.get(
            "time",
            "",
        )

        subject = lesson.get(
            "subject",
            "Занятие",
        )

        room = lesson.get(
            "room",
            "",
        )

        teacher = lesson.get(
            "teacher",
            "",
        )

        lines.append(
            f"<b>{pair} пара</b> · "
            f"<b>{time_value}</b>"
        )

        lines.append(
            f"📚 {subject}"
        )

        if room:
            lines.append(
                f"🚪 Ауд. {room}"
            )

        if teacher:
            lines.append(
                f"👨‍🏫 {teacher}"
            )

        lines.append("")

    return "\n".join(lines)


# ============================================================
# SEND SCHEDULE
# ============================================================

async def send_schedule(
    message: Message,
    requested_date: date,
):
    try:

        actual_date, schedule, fallback = (
            await get_schedule_with_fallback(
                requested_date
            )
        )

        image_path = render_schedule(
            actual_date,
            schedule,
            fallback_used=fallback,
        )

        caption = (
            f"📅 <b>{format_date_ru(actual_date)}</b>\n"
            f"🎓 <b>{GROUP_NAME}</b>"
        )

        if fallback:
            caption += (
                "\n\n"
                "ℹ️ На следующий день расписания нет, "
                "поэтому показываю сегодняшнее."
            )

        await message.answer_photo(
            photo=FSInputFile(
                image_path
            ),
            caption=caption,
        )

    except Exception:
        logger.exception(
            "Ошибка send_schedule"
        )

        await message.answer(
            "Не удалось обработать расписание.\n\n"
            "Подробность ошибки записана в лог."
        )


# ============================================================
# COMMAND: START
# ============================================================

@dp.message(Command("start"))
async def cmd_start(
    message: Message,
):

    text = (
        f"🎓 <b>{GROUP_NAME}</b>\n"
        f"Расписание Ишимбайского нефтяного колледжа.\n\n"
        "<b>Команды:</b>\n"
        "/schedule — расписание на сегодня\n"
        "/schedule tomorrow — расписание на завтра\n"
        "/date 04.09.2026 — расписание на дату\n"
        "/subscribe — получать изменения\n"
        "/unsubscribe — отключить уведомления\n"
        "/checknow — проверить расписание\n"
        "/scheduletext — текстовая версия\n\n"
        "Автоматические уведомления работают "
        "при изменении расписания."
    )

    await message.answer(
        text
    )


# ============================================================
# COMMAND: SCHEDULE
# ============================================================

@dp.message(Command("schedule"))
async def cmd_schedule(
    message: Message,
):

    args = (
        message.text or ""
    ).split(maxsplit=1)

    target_date = today_date()

    if len(args) > 1:

        value = args[1].strip().lower()

        if value in (
            "tomorrow",
            "завтра",
        ):
            target_date = (
                today_date()
                + timedelta(days=1)
            )

        elif value in (
            "today",
            "сегодня",
        ):
            target_date = today_date()

        else:

            parsed = parse_user_date(
                value
            )

            if parsed is None:

                await message.answer(
                    "Формат даты:\n"
                    "<code>/date 04.09.2026</code>"
                )

                return

            target_date = parsed

    await send_schedule(
        message,
        target_date,
    )


# ============================================================
# COMMAND: DATE
# ============================================================

@dp.message(Command("date"))
async def cmd_date(
    message: Message,
):

    args = (
        message.text or ""
    ).split(maxsplit=1)

    if len(args) < 2:

        await message.answer(
            "Укажи дату:\n"
            "<code>/date 04.09.2026</code>"
        )

        return

    target_date = parse_user_date(
        args[1]
    )

    if target_date is None:

        await message.answer(
            "Не понял дату.\n\n"
            "Используй:\n"
            "<code>/date 04.09.2026</code>"
        )

        return

    await send_schedule(
        message,
        target_date,
    )


# ============================================================
# COMMAND: SCHEDULE TEXT
# ============================================================

@dp.message(Command("scheduletext"))
async def cmd_schedule_text(
    message: Message,
):

    try:

        target_date = today_date()

        actual_date, schedule, fallback = (
            await get_schedule_with_fallback(
                target_date
            )
        )

        text = schedule_to_text(
            actual_date,
            schedule,
        )

        await message.answer(
            text
        )

    except Exception:
        logger.exception(
            "Ошибка scheduletext"
        )

        await message.answer(
            "Не удалось получить расписание."
        )


# ============================================================
# COMMAND: SUBSCRIBE
# ============================================================

@dp.message(Command("subscribe"))
async def cmd_subscribe(
    message: Message,
):

    chat_id = message.chat.id

    if chat_id in SUBSCRIBERS:

        await message.answer(
            "🔔 Вы уже подписаны на уведомления."
        )

        return

    SUBSCRIBERS.add(
        chat_id
    )

    await save_subscribers()

    await message.answer(
        "🔔 <b>Подписка включена.</b>\n\n"
        "Я буду проверять расписание и "
        "сообщать об изменениях."
    )


# ============================================================
# COMMAND: UNSUBSCRIBE
# ============================================================

@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(
    message: Message,
):

    chat_id = message.chat.id

    if chat_id not in SUBSCRIBERS:

        await message.answer(
            "Вы не были подписаны."
        )

        return

    SUBSCRIBERS.discard(
        chat_id
    )

    await save_subscribers()

    await message.answer(
        "🔕 Подписка отключена."
    )


# ============================================================
# COMMAND: CHECKNOW
# ============================================================

@dp.message(Command("checknow"))
async def cmd_checknow(
    message: Message,
):

    await message.answer(
        "🔎 Проверяю расписание..."
    )

    try:

        tomorrow = (
            today_date()
            + timedelta(days=1)
        )

        actual_date, schedule, fallback = (
            await get_schedule_with_fallback(
                tomorrow
            )
        )

        image_path = render_schedule(
            actual_date,
            schedule,
            fallback_used=fallback,
        )

        caption = (
            "🔎 <b>Проверка расписания</b>\n\n"
            f"📅 {format_date_ru(actual_date)}\n"
            f"🎓 {GROUP_NAME}"
        )

        if fallback:
            caption += (
                "\n\n"
                "ℹ️ На завтра расписания нет.\n"
                "Показываю сегодняшнее."
            )

        await message.answer_photo(
            photo=FSInputFile(
                image_path
            ),
            caption=caption,
        )

    except Exception:
        logger.exception(
            "Ошибка checknow"
        )

        await message.answer(
            "Ошибка при проверке расписания."
        )


# ============================================================
# BROADCAST
# ============================================================

async def broadcast_schedule(
    target_date: date,
    schedule: list[dict],
    reason: str = "изменение расписания",
):

    if not SUBSCRIBERS:
        logger.info(
            "Нет подписчиков — уведомление не отправляется."
        )
        return

    logger.info(
        "Отправка уведомления: %s",
        reason,
    )

    image_path = render_schedule(
        target_date,
        schedule,
    )

    caption = (
        "🔔 <b>Расписание изменилось</b>\n\n"
        f"📅 <b>{format_date_ru(target_date)}</b>\n"
        f"🎓 <b>{GROUP_NAME}</b>"
    )

    dead_users = []

    for chat_id in list(
        SUBSCRIBERS
    ):

        try:

            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(
                    image_path
                ),
                caption=caption,
            )

            logger.info(
                "Уведомление отправлено: %s",
                chat_id,
            )

            # Не долбим Telegram слишком быстро.
            await asyncio.sleep(
                0.05
            )

        except TelegramForbiddenError:

            logger.warning(
                "Пользователь заблокировал бота: %s",
                chat_id,
            )

            dead_users.append(
                chat_id
            )

        except TelegramBadRequest:

            logger.exception(
                "TelegramBadRequest для %s",
                chat_id,
            )

        except Exception:

            logger.exception(
                "Ошибка отправки %s",
                chat_id,
            )

    if dead_users:

        for chat_id in dead_users:
            SUBSCRIBERS.discard(
                chat_id
            )

        await save_subscribers()


# ============================================================
# CHECK ONE DATE
# ============================================================

async def check_date_for_changes(
    target_date: date,
    notify: bool = True,
):

    try:

        schedule = await get_schedule(
            target_date
        )

        signature = schedule_signature(
            target_date,
            schedule,
        )

        key = target_date.isoformat()

        old_signature = (
            SCHEDULE_CACHE.get(key)
        )

        # Первый запуск:
        # просто запоминаем расписание.
        if old_signature is None:

            SCHEDULE_CACHE[key] = signature

            await save_cache()

            logger.info(
                "Первичное сохранение кэша: %s | %s занятий",
                target_date,
                len(schedule),
            )

            return False

        # Без изменений.
        if old_signature == signature:

            logger.info(
                "Без изменений: %s",
                target_date,
            )

            return False

        # Изменилось.
        SCHEDULE_CACHE[key] = signature

        await save_cache()

        logger.info(
            "РАСПИСАНИЕ ИЗМЕНИЛОСЬ: %s",
            target_date,
        )

        if notify:

            await broadcast_schedule(
                target_date,
                schedule,
                reason=(
                    f"изменение на "
                    f"{format_date_short(target_date)}"
                ),
            )

        return True

    except Exception:

        logger.exception(
            "Ошибка проверки даты %s",
            target_date,
        )

        return False


# ============================================================
# AUTOMATIC MONITOR
# ============================================================

async def monitor_loop():

    logger.info(
        "Мониторинг расписания запущен."
    )

    while True:

        try:

            today = today_date()
            tomorrow = (
                today
                + timedelta(days=1)
            )

            # Проверяем сегодня.
            await check_date_for_changes(
                today,
                notify=True,
            )

            # Проверяем завтра.
            await check_date_for_changes(
                tomorrow,
                notify=True,
            )

        except asyncio.CancelledError:
            raise

        except Exception:

            logger.exception(
                "Ошибка monitor_loop"
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# STARTUP
# ============================================================

async def on_startup():

    logger.info(
        "=" * 60
    )

    logger.info(
        "Schedule bot started"
    )

    logger.info(
        "Group: %s",
        GROUP_NAME,
    )

    logger.info(
        "URL: %s",
        SCHEDULE_URL,
    )

    logger.info(
        "Check interval: %s sec",
        CHECK_INTERVAL,
    )

    logger.info(
        "Subscribers: %s",
        len(SUBSCRIBERS),
    )

    logger.info(
        "Timezone: %s",
        TIMEZONE_NAME,
    )

    logger.info(
        "=" * 60
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    global http_session

    await on_startup()

    monitor_task = asyncio.create_task(
        monitor_loop()
    )

    try:

        logger.info(
            "Start polling"
        )

        await dp.start_polling(
            bot
        )

    finally:

        monitor_task.cancel()

        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        if http_session is not None:

            await http_session.close()

        await bot.session.close()

        logger.info(
            "Bot stopped"
        )


if __name__ == "__main__":

    try:
        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Stopped by user"
        )
