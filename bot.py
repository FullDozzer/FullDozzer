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

GROUP_NAME = os.getenv(
    "GROUP_NAME",
    "ЭС7-24"
).strip()

CHECK_INTERVAL = int(
    os.getenv("CHECK_INTERVAL", "1800")
)

DATA_DIR = Path(
    os.getenv("DATA_DIR", "data")
)

TIMEZONE = os.getenv(
    "TIMEZONE",
    "Europe/Moscow"
)

TZ = ZoneInfo(TIMEZONE)

SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CACHE_FILE = DATA_DIR / "schedule_cache.json"
IMAGE_DIR = DATA_DIR / "images"

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

IMAGE_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("schedule_bot")


# ============================================================
# BOT
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. "
        "Добавь токен в .env"
    )

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()


# ============================================================
# CONSTANTS
# ============================================================

WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]

MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

TIME_RE = re.compile(
    r"\b"
    r"([01]?\d|2[0-3]):([0-5]\d)"
    r"\s*[-–—]\s*"
    r"([01]?\d|2[0-3]):([0-5]\d)"
    r"\b"
)

ROOM_RE = re.compile(
    r"\b"
    r"(?:ауд\.?\s*)?"
    r"([А-ЯЁA-Z]{1,8}"
    r"[- ]?\d{2,5})"
    r"\b",
    re.IGNORECASE
)

FIO_RE = re.compile(
    r"\b"
    r"[А-ЯЁ][а-яё]+"
    r"(?:\s+[А-ЯЁ][а-яё]+)?"
    r"\s+"
    r"[А-ЯЁ]\."
    r"[А-ЯЁ]\.?"
    r"\b"
)

PAIR_WORD_RE = re.compile(
    r"\b"
    r"(I{1,3}|IV|V|VI{0,3}|IX|X)"
    r"\s*"
    r"(?:пара)?"
    r"\b",
    re.IGNORECASE
)


# ============================================================
# DATE
# ============================================================

def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return now_local().date()


def format_date_ru(value: date) -> str:
    return (
        f"{WEEKDAYS[value.weekday()].capitalize()}, "
        f"{value.day} {MONTHS[value.month - 1]} "
        f"{value.year} года"
    )


def parse_date(value: str) -> date | None:
    value = value.strip()

    for fmt in (
        "%d.%m.%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(
                value,
                fmt
            ).date()
        except ValueError:
            pass

    return None


def build_url(target_date: date) -> str:
    return (
        f"{SCHEDULE_URL}/"
        f"{target_date.isoformat()}"
    )


# ============================================================
# JSON
# ============================================================

def load_json(
    path: Path,
    default
):
    try:
        if not path.exists():
            return default

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


def save_json(
    path: Path,
    data
):
    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temp.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    temp.replace(path)


def load_subscribers() -> set[int]:
    data = load_json(
        SUBSCRIBERS_FILE,
        []
    )

    result = set()

    if isinstance(data, list):

        for user_id in data:

            try:
                result.add(
                    int(user_id)
                )
            except Exception:
                pass

    return result


def save_subscribers(
    subscribers: set[int]
):
    save_json(
        SUBSCRIBERS_FILE,
        sorted(subscribers)
    )


# ============================================================
# TEXT CLEANING
# ============================================================

def clean_text(
    text: str
) -> str:

    text = text.replace(
        "\xa0",
        " "
    )

    text = text.replace(
        "\u200b",
        ""
    )

    text = text.replace(
        "\r",
        " "
    )

    text = text.replace(
        "\n",
        " "
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def normalize_text(
    text: str
) -> str:

    text = clean_text(
        text
    )

    text = text.replace(
        "—",
        "-"
    )

    text = text.replace(
        "–",
        "-"
    )

    return text.lower()


# ============================================================
# ENCODING
# ============================================================

def find_charset(
    content_type: str
) -> str | None:

    if not content_type:
        return None

    match = re.search(
        r"charset\s*=\s*[\"']?([a-zA-Z0-9._-]+)",
        content_type,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return None


def find_html_charset(
    raw: bytes
) -> str | None:

    preview = raw[
        :100_000
    ].decode(
        "latin-1",
        errors="ignore"
    )

    match = re.search(
        r'<meta[^>]+charset\s*=\s*'
        r'["\']?\s*([a-zA-Z0-9._-]+)',
        preview,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    match = re.search(
        r'charset\s*=\s*'
        r'["\']?\s*([a-zA-Z0-9._-]+)',
        preview,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return None


def encoding_score(
    text: str
) -> int:

    if not text:
        return -100000

    russian = len(
        re.findall(
            r"[А-Яа-яЁё]",
            text
        )
    )

    replacement = text.count(
        "�"
    )

    mojibake = (
        text.count("Р")
        + text.count("С")
        + text.count("Ð")
        + text.count("Ñ")
    )

    score = 0

    score += russian * 10
    score -= replacement * 100
    score -= mojibake * 5

    return score


def decode_html(
    raw: bytes,
    content_type: str
) -> tuple[str, str]:

    candidates = []

    http_encoding = find_charset(
        content_type
    )

    html_encoding = find_html_charset(
        raw
    )

    for encoding in (
        http_encoding,
        html_encoding,
        "utf-8",
        "cp1251",
        "windows-1251",
    ):

        if not encoding:
            continue

        encoding = encoding.lower()

        if encoding not in candidates:
            candidates.append(
                encoding
            )

    best_text = None
    best_encoding = None
    best_score = -10**9

    for encoding in candidates:

        try:

            text = raw.decode(
                encoding,
                errors="strict"
            )

            score = encoding_score(
                text[:200_000]
            )

            if score > best_score:

                best_score = score
                best_text = text
                best_encoding = encoding

        except (
            UnicodeDecodeError,
            LookupError
        ):
            continue

    if best_text is None:

        best_encoding = "utf-8"

        best_text = raw.decode(
            "utf-8",
            errors="replace"
        )

    logger.info(
        "Кодировка HTML: %s",
        best_encoding
    )

    return (
        best_text,
        best_encoding
    )


# ============================================================
# HTTP
# ============================================================

async def fetch_html(
    target_date: date
) -> str:

    url = build_url(
        target_date
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
            "ru-RU,ru;q=0.9"
        ),
        "Connection": "keep-alive",
    }

    timeout = aiohttp.ClientTimeout(
        total=40
    )

    logger.info(
        "Запрашиваю: %s",
        url
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        # У ishnk.ru проблемный сертификат.
        async with session.get(
            url,
            ssl=False,
            allow_redirects=True
        ) as response:

            response.raise_for_status()

            raw = await response.read()

            content_type = response.headers.get(
                "Content-Type",
                ""
            )

            html, encoding = decode_html(
                raw,
                content_type
            )

            logger.info(
                "HTTP %s | %d bytes | %s",
                response.status,
                len(raw),
                encoding
            )

            return html


# ============================================================
# HTML HELPERS
# ============================================================

def get_element_lines(
    element
) -> list[str]:

    result = []

    # Сначала пытаемся получить реальные
    # визуальные строки.
    for child in element.find_all(
        [
            "div",
            "span",
            "p",
            "li",
            "td",
            "th",
            "a",
        ]
    ):

        text = clean_text(
            child.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        if len(text) > 300:
            continue

        if text not in result:
            result.append(
                text
            )

    if not result:

        text = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if text:
            result.append(
                text
            )

    return result


def find_time(
    text: str
) -> str:

    match = TIME_RE.search(
        text
    )

    if not match:
        return ""

    return (
        f"{int(match.group(1)):02d}:"
        f"{match.group(2)} - "
        f"{int(match.group(3)):02d}:"
        f"{match.group(4)}"
    )


def find_room(
    text: str
) -> str:

    # Сначала ищем именно после "ауд".
    match = re.search(
        r"ауд\.?\s*"
        r"([А-ЯЁA-Z]{1,8}"
        r"[- ]?\d{2,5})",
        text,
        re.IGNORECASE
    )

    if match:
        return clean_text(
            match.group(1)
        )

    # Затем обычную аудиторию.
    matches = ROOM_RE.findall(
        text
    )

    for value in matches:

        value = clean_text(
            value
        )

        # Не считаем временем/номером.
        if re.fullmatch(
            r"\d+",
            value
        ):
            continue

        return value

    return ""


def find_teacher(
    text: str
) -> str:

    match = FIO_RE.search(
        text
    )

    if match:
        return clean_text(
            match.group(0)
        )

    return ""


def remove_known_parts(
    text: str,
    time_value: str,
    room: str,
    teacher: str
) -> str:

    result = text

    if time_value:
        result = result.replace(
            time_value,
            " "
        )

        # На случай другого тире.
        result = re.sub(
            r"\b"
            r"\d{1,2}:\d{2}"
            r"\s*[-–—]\s*"
            r"\d{1,2}:\d{2}"
            r"\b",
            " ",
            result
        )

    if room:

        result = re.sub(
            r"ауд\.?\s*"
            + re.escape(room),
            " ",
            result,
            flags=re.IGNORECASE
        )

        result = re.sub(
            r"\b"
            + re.escape(room)
            + r"\b",
            " ",
            result,
            flags=re.IGNORECASE
        )

    if teacher:
        result = result.replace(
            teacher,
            " "
        )

    result = PAIR_WORD_RE.sub(
        " ",
        result
    )

    result = re.sub(
        r"\bпара\b",
        " ",
        result,
        flags=re.IGNORECASE
    )

    return clean_text(
        result
    )


def bad_subject(
    text: str
) -> bool:

    if not text:
        return True

    text = clean_text(
        text
    )

    if len(text) < 3:
        return True

    if len(text) > 250:
        return True

    lower = text.lower()

    bad = [
        "расписание занятий",
        "расписание",
        "группа",
        "главная",
        "меню",
        "войти",
        "личный кабинет",
        "предыдущий",
        "следующий",
        "сегодня",
        "завтра",
    ]

    if any(
        word in lower
        for word in bad
    ):
        return True

    if TIME_RE.fullmatch(
        text
    ):
        return True

    return False


def choose_subject(
    lines: list[str],
    time_value: str,
    room: str,
    teacher: str
) -> str:

    candidates = []

    for line in lines:

        line = remove_known_parts(
            line,
            time_value,
            room,
            teacher
        )

        if not line:
            continue

        if bad_subject(
            line
        ):
            continue

        # Не добавляем повторяющиеся строки.
        if line not in candidates:
            candidates.append(
                line
            )

    if not candidates:
        return "Занятие"

    # На сайте название дисциплины обычно
    # оказывается самой содержательной строкой.
    candidates.sort(
        key=lambda x: (
            len(x),
            x.count(" ")
        ),
        reverse=True
    )

    return candidates[0]


# ============================================================
# PARSER
# ============================================================

def parse_table(
    soup: BeautifulSoup
) -> list[dict]:

    result = []

    for table in soup.find_all(
        "table"
    ):

        rows = table.find_all(
            "tr"
        )

        for row in rows:

            cells = row.find_all(
                ["td", "th"]
            )

            if not cells:
                continue

            texts = []

            for cell in cells:

                text = clean_text(
                    cell.get_text(
                        " ",
                        strip=True
                    )
                )

                if text:
                    texts.append(
                        text
                    )

            full_text = " | ".join(
                texts
            )

            time_value = find_time(
                full_text
            )

            if not time_value:
                continue

            room = find_room(
                full_text
            )

            teacher = find_teacher(
                full_text
            )

            subject = choose_subject(
                texts,
                time_value,
                room,
                teacher
            )

            result.append(
                {
                    "number": "",
                    "time": time_value,
                    "subject": subject,
                    "teacher": teacher,
                    "room": room,
                }
            )

    return result


def parse_schedule_blocks(
    soup: BeautifulSoup
) -> list[dict]:

    result = []

    # Ищем элементы, непосредственно содержащие время.
    candidates = []

    for element in soup.find_all(
        [
            "div",
            "section",
            "article",
            "li",
            "td",
            "tr",
        ]
    ):

        text = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not TIME_RE.search(
            text
        ):
            continue

        # Не берём огромный контейнер всей страницы.
        if len(text) > 1500:
            continue

        candidates.append(
            element
        )

    # Отбрасываем вложенные дубликаты:
    # если маленький элемент уже содержит время,
    # не используем его родителя с тем же временем.
    selected = []

    for element in candidates:

        is_parent_of_smaller = False

        for other in candidates:

            if other is element:
                continue

            if element in other.parents:

                element_text = clean_text(
                    element.get_text(
                        " ",
                        strip=True
                    )
                )

                other_text = clean_text(
                    other.get_text(
                        " ",
                        strip=True
                    )
                )

                if (
                    len(other_text)
                    < len(element_text)
                ):
                    is_parent_of_smaller = True
                    break

        if not is_parent_of_smaller:
            selected.append(
                element
            )

    for element in selected:

        text = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        time_value = find_time(
            text
        )

        if not time_value:
            continue

        room = find_room(
            text
        )

        teacher = find_teacher(
            text
        )

        lines = get_element_lines(
            element
        )

        subject = choose_subject(
            lines,
            time_value,
            room,
            teacher
        )

        # Защита от мусора.
        if subject == "Занятие" and not (
            room or teacher
        ):
            continue

        result.append(
            {
                "number": "",
                "time": time_value,
                "subject": subject,
                "teacher": teacher,
                "room": room,
            }
        )

    return result


def assign_pair_numbers(
    schedule: list[dict]
):

    roman = [
        "I",
        "II",
        "III",
        "IV",
        "V",
        "VI",
        "VII",
        "VIII",
        "IX",
        "X",
    ]

    for index, item in enumerate(
        schedule
    ):

        if not item["number"]:

            if index < len(roman):
                item["number"] = roman[index]
            else:
                item["number"] = str(
                    index + 1
                )


def deduplicate_schedule(
    schedule: list[dict]
) -> list[dict]:

    result = []

    seen = set()

    for item in schedule:

        key = (
            normalize_text(
                item.get(
                    "time",
                    ""
                )
            ),
            normalize_text(
                item.get(
                    "subject",
                    ""
                )
            ),
            normalize_text(
                item.get(
                    "teacher",
                    ""
                )
            ),
            normalize_text(
                item.get(
                    "room",
                    ""
                )
            ),
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append(
            item
        )

    return result


def parse_schedule(
    html: str
) -> list[dict]:

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # Убираем мусор.
    for tag in soup.find_all(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "footer",
        ]
    ):
        tag.decompose()

    # Сначала таблицы.
    schedule = parse_table(
        soup
    )

    # Если таблиц нет — карточки.
    if not schedule:

        schedule = parse_schedule_blocks(
            soup
        )

    schedule = deduplicate_schedule(
        schedule
    )

    # Сортировка по времени.
    def sort_key(item):

        match = re.search(
            r"(\d{1,2}):(\d{2})",
            item["time"]
        )

        if not match:
            return (
                99,
                99
            )

        return (
            int(match.group(1)),
            int(match.group(2))
        )

    schedule.sort(
        key=sort_key
    )

    assign_pair_numbers(
        schedule
    )

    logger.info(
        "Найдено занятий: %d",
        len(schedule)
    )

    for item in schedule:

        logger.info(
            "%s | %s | %s | %s | %s",
            item["number"],
            item["time"],
            item["subject"],
            item["teacher"],
            item["room"]
        )

    return schedule


# ============================================================
# SCHEDULE SIGNATURE
# ============================================================

def schedule_signature(
    schedule: list[dict]
) -> str:

    data = []

    for item in schedule:

        data.append(
            {
                "number": normalize_text(
                    item["number"]
                ),
                "time": normalize_text(
                    item["time"]
                ),
                "subject": normalize_text(
                    item["subject"]
                ),
                "teacher": normalize_text(
                    item["teacher"]
                ),
                "room": normalize_text(
                    item["room"]
                ),
            }
        )

    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# ============================================================
# FONTS
# ============================================================

FONT_REGULAR_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
]

FONT_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]


def get_font(
    size: int,
    bold: bool = False
):

    paths = (
        FONT_BOLD_PATHS
        if bold
        else FONT_REGULAR_PATHS
    )

    for path in paths:

        if os.path.isfile(
            path
        ):

            logger.debug(
                "Используется шрифт: %s",
                path
            )

            return ImageFont.truetype(
                path,
                size
            )

    raise RuntimeError(
        "Не найден шрифт с поддержкой "
        "кириллицы. Установи fonts-dejavu "
        "в Dockerfile."
    )


# ============================================================
# TEXT WRAPPING
# ============================================================

def wrap_text(
    draw,
    text,
    font,
    max_width
):

    words = text.split()

    if not words:
        return []

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
# IMAGE
# ============================================================

def render_schedule(
    schedule: list[dict],
    target_date: date
) -> Path:

    width = 1200

    margin = 70

    # Минималистичная палитра.
    BG = (247, 248, 250)
    CARD = (255, 255, 255)
    TEXT = (24, 27, 32)
    SECONDARY = (105, 110, 120)

    title_font = get_font(
        48,
        True
    )

    date_font = get_font(
        28
    )

    pair_font = get_font(
        26,
        True
    )

    time_font = get_font(
        27,
        True
    )

    subject_font = get_font(
        30,
        True
    )

    teacher_font = get_font(
        23
    )

    room_font = get_font(
        23,
        True
    )

    footer_font = get_font(
        18
    )

    # --------------------------------------------------------
    # Высота карточек
    # --------------------------------------------------------

    dummy = Image.new(
        "RGB",
        (10, 10)
    )

    dummy_draw = ImageDraw.Draw(
        dummy
    )

    row_heights = []

    for item in schedule:

        subject = item.get(
            "subject",
            "Занятие"
        )

        lines = wrap_text(
            dummy_draw,
            subject,
            subject_font,
            width - margin * 2 - 170
        )

        height = (
            165
            + max(
                0,
                len(lines) - 1
            ) * 38
        )

        row_heights.append(
            height
        )

    if not row_heights:
        row_heights = [180]

    header_height = 190
    footer_height = 70

    total_height = (
        header_height
        + sum(row_heights)
        + footer_height
    )

    image = Image.new(
        "RGB",
        (
            width,
            total_height
        ),
        BG
    )

    draw = ImageDraw.Draw(
        image
    )

    # --------------------------------------------------------
    # Заголовок
    # --------------------------------------------------------

    draw.text(
        (
            margin,
            45
        ),
        GROUP_NAME,
        font=title_font,
        fill=TEXT
    )

    draw.text(
        (
            margin,
            115
        ),
        format_date_ru(
            target_date
        ),
        font=date_font,
        fill=SECONDARY
    )

    y = header_height

    # --------------------------------------------------------
    # Если занятий нет
    # --------------------------------------------------------

    if not schedule:

        card_x1 = margin - 20
        card_x2 = width - margin + 20

        draw.rounded_rectangle(
            (
                card_x1,
                y,
                card_x2,
                y + 170
            ),
            radius=24,
            fill=CARD
        )

        draw.text(
            (
                margin + 30,
                y + 45
            ),
            "Занятий нет",
            font=subject_font,
            fill=TEXT
        )

        draw.text(
            (
                margin + 30,
                y + 95
            ),
            "Свободный день",
            font=teacher_font,
            fill=SECONDARY
        )

    # --------------------------------------------------------
    # Занятия
    # --------------------------------------------------------

    for index, item in enumerate(
        schedule
    ):

        row_height = row_heights[
            index
        ]

        card_x1 = margin - 20
        card_x2 = width - margin + 20

        card_y1 = y
        card_y2 = (
            y
            + row_height
            - 12
        )

        draw.rounded_rectangle(
            (
                card_x1,
                card_y1,
                card_x2,
                card_y2
            ),
            radius=24,
            fill=CARD
        )

        # Номер пары.
        number = item.get(
            "number",
            str(index + 1)
        )

        draw.text(
            (
                margin,
                y + 27
            ),
            number,
            font=pair_font,
            fill=SECONDARY
        )

        # Время.
        time_value = item.get(
            "time",
            ""
        )

        draw.text(
            (
                margin + 85,
                y + 28
            ),
            time_value,
            font=time_font,
            fill=TEXT
        )

        # Аудитория.
        room = clean_text(
            item.get(
                "room",
                ""
            )
        )

        if room:

            bbox = draw.textbbox(
                (0, 0),
                room,
                font=room_font
            )

            room_width = (
                bbox[2] - bbox[0]
            )

            draw.text(
                (
                    width
                    - margin
                    - room_width,
                    y + 29
                ),
                room,
                font=room_font,
                fill=TEXT
            )

        # Предмет.
        subject = clean_text(
            item.get(
                "subject",
                "Занятие"
            )
        )

        subject_lines = wrap_text(
            draw,
            subject,
            subject_font,
            width
            - margin * 2
            - 170
        )

        subject_y = y + 78

        for line in subject_lines:

            draw.text(
                (
                    margin + 85,
                    subject_y
                ),
                line,
                font=subject_font,
                fill=TEXT
            )

            subject_y += 38

        # Преподаватель.
        teacher = clean_text(
            item.get(
                "teacher",
                ""
            )
        )

        if teacher:

            draw.text(
                (
                    margin + 85,
                    card_y2 - 39
                ),
                teacher,
                font=teacher_font,
                fill=SECONDARY
            )

        y += row_height

    # --------------------------------------------------------
    # Footer
    # --------------------------------------------------------

    footer = (
        "Расписание занятий"
    )

    bbox = draw.textbbox(
        (0, 0),
        footer,
        font=footer_font
    )

    footer_width = (
        bbox[2] - bbox[0]
    )

    draw.text(
        (
            (width - footer_width) // 2,
            total_height - 42
        ),
        footer,
        font=footer_font,
        fill=SECONDARY
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    path = (
        IMAGE_DIR
        / f"schedule_{target_date.isoformat()}.png"
    )

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
# TEXT SCHEDULE
# ============================================================

def schedule_to_text(
    schedule: list[dict],
    target_date: date
) -> str:

    lines = [
        f"<b>{GROUP_NAME}</b>",
        format_date_ru(
            target_date
        ),
        "",
    ]

    if not schedule:

        lines.append(
            "Занятий нет."
        )

        return "\n".join(
            lines
        )

    for item in schedule:

        number = item.get(
            "number",
            ""
        )

        time_value = item.get(
            "time",
            ""
        )

        subject = item.get(
            "subject",
            "Занятие"
        )

        teacher = item.get(
            "teacher",
            ""
        )

        room = item.get(
            "room",
            ""
        )

        header = (
            f"<b>{number}</b>  "
            f"<b>{time_value}</b>"
        )

        if room:
            header += (
                f"  ·  <b>{room}</b>"
            )

        lines.append(
            header
        )

        lines.append(
            subject
        )

        if teacher:
            lines.append(
                f"<i>{teacher}</i>"
            )

        lines.append("")

    return "\n".join(
        lines
    ).strip()


# ============================================================
# GET SCHEDULE
# ============================================================

async def get_schedule(
    target_date: date
) -> list[dict]:

    html = await fetch_html(
        target_date
    )

    schedule = parse_schedule(
        html
    )

    return schedule


# ============================================================
# SEND SCHEDULE
# ============================================================

async def send_schedule(
    message: Message,
    target_date: date
):

    try:

        schedule = await get_schedule(
            target_date
        )

        image_path = render_schedule(
            schedule,
            target_date
        )

        caption = (
            f"<b>{GROUP_NAME}</b>\n"
            f"{format_date_ru(target_date)}"
        )

        await message.answer_photo(
            photo=FSInputFile(
                image_path
            ),
            caption=caption,
            parse_mode="HTML"
        )

    except aiohttp.ClientResponseError as e:

        logger.exception(
            "HTTP ошибка"
        )

        await message.answer(
            f"Сайт вернул ошибку HTTP {e.status}."
        )

    except (
        aiohttp.ClientError,
        asyncio.TimeoutError
    ):

        logger.exception(
            "Ошибка подключения"
        )

        await message.answer(
            "Не удалось подключиться "
            "к сайту с расписанием."
        )

    except Exception:

        logger.exception(
            "Ошибка расписания"
        )

        await message.answer(
            "Не удалось обработать расписание."
        )


# ============================================================
# /START
# ============================================================

@dp.message(
    Command("start")
)
async def start_command(
    message: Message
):

    await message.answer(
        f"<b>Расписание {GROUP_NAME}</b>\n\n"
        "📅 /schedule — расписание на сегодня\n"
        "📅 /schedule 04.09.2026 — расписание на дату\n\n"
        "🔔 /subscribe — включить уведомления\n"
        "🔕 /unsubscribe — выключить уведомления\n"
        "🔄 /checknow — проверить завтра\n",
        parse_mode="HTML"
    )


# ============================================================
# /SCHEDULE
# ============================================================

@dp.message(
    Command("schedule")
)
async def schedule_command(
    message: Message
):

    parts = message.text.split(
        maxsplit=1
    )

    target_date = today_local()

    if len(parts) == 2:

        parsed = parse_date(
            parts[1]
        )

        if parsed is None:

            await message.answer(
                "Неверный формат даты.\n\n"
                "Пример:\n"
                "/schedule 04.09.2026"
            )

            return

        target_date = parsed

    await send_schedule(
        message,
        target_date
    )


# ============================================================
# /SUBSCRIBE
# ============================================================

@dp.message(
    Command("subscribe")
)
async def subscribe_command(
    message: Message
):

    if not message.from_user:
        return

    user_id = message.from_user.id

    subscribers = load_subscribers()

    if user_id in subscribers:

        await message.answer(
            "Ты уже подписан на уведомления."
        )

        return

    subscribers.add(
        user_id
    )

    save_subscribers(
        subscribers
    )

    await message.answer(
        "🔔 Подписка включена.\n\n"
        "Я буду проверять расписание "
        "на завтра и сообщу, если оно изменится."
    )


# ============================================================
# /UNSUBSCRIBE
# ============================================================

@dp.message(
    Command("unsubscribe")
)
async def unsubscribe_command(
    message: Message
):

    if not message.from_user:
        return

    user_id = message.from_user.id

    subscribers = load_subscribers()

    if user_id not in subscribers:

        await message.answer(
            "Ты не подписан на уведомления."
        )

        return

    subscribers.remove(
        user_id
    )

    save_subscribers(
        subscribers
    )

    await message.answer(
        "🔕 Подписка отключена."
    )


# ============================================================
# /CHECKNOW
# ============================================================

@dp.message(
    Command("checknow")
)
async def checknow_command(
    message: Message
):

    tomorrow = (
        today_local()
        + timedelta(days=1)
    )

    try:

        schedule = await get_schedule(
            tomorrow
        )

        text = schedule_to_text(
            schedule,
            tomorrow
        )

        await message.answer(
            text,
            parse_mode="HTML"
        )

    except Exception:

        logger.exception(
            "Ошибка /checknow"
        )

        await message.answer(
            "Не удалось проверить расписание."
        )


# ============================================================
# AUTOMATIC CHECK
# ============================================================

async def check_tomorrow():

    tomorrow = (
        today_local()
        + timedelta(days=1)
    )

    date_key = tomorrow.isoformat()

    try:

        schedule = await get_schedule(
            tomorrow
        )

        signature = schedule_signature(
            schedule
        )

        cache = load_json(
            CACHE_FILE,
            {}
        )

        old_signature = cache.get(
            date_key
        )

        # Первый запуск для этой даты.
        if old_signature is None:

            cache[date_key] = signature

            save_json(
                CACHE_FILE,
                cache
            )

            logger.info(
                "Первый запуск для %s. "
                "Уведомления не отправляем.",
                date_key
            )

            return

        # Ничего не изменилось.
        if old_signature == signature:

            logger.info(
                "Расписание %s без изменений.",
                date_key
            )

            return

        # Изменилось.
        cache[date_key] = signature

        save_json(
            CACHE_FILE,
            cache
        )

        logger.info(
            "РАСПИСАНИЕ ИЗМЕНИЛОСЬ: %s",
            date_key
        )

        subscribers = load_subscribers()

        if not subscribers:

            logger.info(
                "Подписчиков нет."
            )

            return

        image_path = render_schedule(
            schedule,
            tomorrow
        )

        caption = (
            "🔔 <b>Расписание изменилось</b>\n\n"
            f"<b>{GROUP_NAME}</b>\n"
            f"{format_date_ru(tomorrow)}"
        )

        success = 0
        failed = 0

        for user_id in subscribers:

            try:

                await bot.send_photo(
                    chat_id=user_id,
                    photo=FSInputFile(
                        image_path
                    ),
                    caption=caption,
                    parse_mode="HTML"
                )

                success += 1

                await asyncio.sleep(
                    0.05
                )

            except Exception as e:

                failed += 1

                logger.warning(
                    "Ошибка отправки %s: %s",
                    user_id,
                    e
                )

        logger.info(
            "Рассылка завершена: "
            "успешно=%d, ошибок=%d",
            success,
            failed
        )

    except Exception:

        logger.exception(
            "Ошибка автоматической проверки"
        )


async def scheduler():

    logger.info(
        "Автоматическая проверка запущена."
    )

    logger.info(
        "Интервал: %d секунд",
        CHECK_INTERVAL
    )

    while True:

        await check_tomorrow()

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "======================================"
    )

    logger.info(
        "BOT START"
    )

    logger.info(
        "Группа: %r",
        GROUP_NAME
    )

    logger.info(
        "URL: %s",
        SCHEDULE_URL
    )

    logger.info(
        "Timezone: %s",
        TIMEZONE
    )

    logger.info(
        "Check interval: %d",
        CHECK_INTERVAL
    )

    logger.info(
        "======================================"
    )

    asyncio.create_task(
        scheduler()
    )

    await dp.start_polling(
        bot
    )


# ============================================================
# RUN
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
