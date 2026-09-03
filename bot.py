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

GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24")

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
# REGEX
# ============================================================

TIME_RE = re.compile(
    r"\b([01]?\d|2[0-3]):([0-5]\d)\s*[-–—]\s*([01]?\d|2[0-3]):([0-5]\d)\b"
)

PAIR_RE = re.compile(
    r"\b("
    r"I{1,3}|IV|V|VI{0,3}|IX|X"
    r")\s*(?:пара|п\.?)\b",
    re.IGNORECASE,
)

ROOM_RE = re.compile(
    r"(?:ауд(?:итория)?\.?\s*)"
    r"([А-ЯA-ZЁ0-9][А-ЯA-ZЁа-яa-zё0-9./_-]*)",
    re.IGNORECASE,
)

TEACHER_RE = re.compile(
    r"\b"
    r"([А-ЯЁ][а-яё-]{2,})"
    r"\s+"
    r"([А-ЯЁ])\.?\s*"
    r"([А-ЯЁ])\.?"
    r"\b"
)

BREAK_RE = re.compile(
    r"\bперемена\s*\d*\s*мин(?:ут[а-я]*)?\.?\b",
    re.IGNORECASE,
)


# ============================================================
# DATA
# ============================================================

def load_json(path: Path, default):
    try:
        if not path.exists():
            return default

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        logger.exception("Ошибка чтения %s", path)
        return default


def save_json(path: Path, data):
    temp = path.with_suffix(".tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp.replace(path)


def load_subscribers():
    data = load_json(SUBSCRIBERS_FILE, [])

    if not isinstance(data, list):
        return []

    result = []

    for item in data:
        try:
            result.append(int(item))
        except Exception:
            pass

    return sorted(set(result))


def save_subscribers(subscribers):
    save_json(
        SUBSCRIBERS_FILE,
        sorted(set(int(x) for x in subscribers)),
    )


def load_cache():
    data = load_json(CACHE_FILE, {})

    return data if isinstance(data, dict) else {}


def save_cache(cache):
    save_json(CACHE_FILE, cache)


# ============================================================
# DATE / URL
# ============================================================

RU_DAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]

RU_MONTHS = [
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


def russian_date(d: date):
    return (
        f"{RU_DAYS[d.weekday()]}, "
        f"{d.day} {RU_MONTHS[d.month - 1]} {d.year} года"
    )


def build_schedule_url(d: date):
    return f"{SCHEDULE_URL}/{d.isoformat()}"


def parse_user_date(value: str):
    value = value.strip().lower()

    today = datetime.now(TZ).date()

    if value in ("сегодня", "today", ""):
        return today

    if value in ("завтра", "tomorrow"):
        return today + timedelta(days=1)

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
# HTML / ENCODING
# ============================================================

def encoding_score(text: str):
    if not text:
        return -100000

    score = 0

    # Нормальная кириллица
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    score += min(cyr, 500) * 3

    # Типичный мусор при неправильной кодировке
    bad_words = [
        "Рђ",
        "Рџ",
        "РЎ",
        "Рќ",
        "Рё",
        "Р°",
        "Рµ",
        "РЅ",
        "Рї",
        "Р°",
        "Рі",
        "СЃ",
        "С‚",
        "СЏ",
    ]

    for bad in bad_words:
        score -= text.count(bad) * 15

    score -= text.count("�") * 50

    return score


def decode_html(raw: bytes):
    candidates = []

    for encoding in [
        "utf-8",
        "cp1251",
        "windows-1251",
        "koi8-r",
    ]:
        try:
            text = raw.decode(encoding, errors="replace")
            candidates.append((encoding_score(text), text))
        except Exception:
            pass

    if not candidates:
        return raw.decode("utf-8", errors="replace")

    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    selected = candidates[0][1]

    logger.info(
        "HTML encoding selected, score=%s",
        candidates[0][0],
    )

    return selected


def clean_text(value: str):
    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")
    value = value.replace("\ufeff", "")

    value = re.sub(r"[ \t]+", " ", value)

    value = re.sub(
        r"\n\s*\n+",
        "\n",
        value,
    )

    return value.strip()


def clean_line(value: str):
    value = clean_text(value)
    value = BREAK_RE.sub("", value)
    return value.strip(" -–—|•·")


# ============================================================
# PARSER
# ============================================================

def extract_pair(text: str):
    match = PAIR_RE.search(text)

    if match:
        return match.group(1).upper()

    return None


def extract_room(text: str):
    match = ROOM_RE.search(text)

    if not match:
        return None

    return match.group(1).strip()


def extract_teacher(text: str):
    match = TEACHER_RE.search(text)

    if not match:
        return None

    surname = match.group(1)
    first = match.group(2)
    patronymic = match.group(3)

    return f"{surname} {first}.{patronymic}."


def normalize_teacher(value: str):
    if not value:
        return None

    value = clean_line(value)

    return value


def normalize_subject(value: str):
    if not value:
        return None

    value = clean_line(value)

    # Убираем типичные остатки интерфейса сайта
    value = re.sub(
        r"^(дисциплина|предмет)\s*[:\-]?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )

    return value.strip()


def parse_card_text(text: str, fallback_pair=None):
    """
    Разбирает один потенциальный блок занятия.

    Ожидаем что-то примерно такое:

    I пара
    08:30 - 09:50
    ауд. УК107
    Дроздов А.П.
    Экспл Н/Г мест
    """

    text = clean_text(text)

    if not TIME_RE.search(text):
        return None

    time_match = TIME_RE.search(text)

    start_time = time_match.group(1) + ":" + time_match.group(2)
    end_time = time_match.group(3) + ":" + time_match.group(4)

    pair = extract_pair(text)

    if not pair:
        pair = fallback_pair

    room = extract_room(text)
    teacher = normalize_teacher(extract_teacher(text))

    # --------------------------------------------------------
    # Определяем предмет.
    # --------------------------------------------------------

    subject = ""

    lines = [
        clean_line(x)
        for x in text.splitlines()
        if clean_line(x)
    ]

    # Удаляем строки, которые явно являются служебными
    filtered = []

    for line in lines:
        if TIME_RE.fullmatch(line):
            continue

        if PAIR_RE.search(line):
            continue

        if ROOM_RE.search(line):
            # Если в строке есть только аудитория — пропускаем.
            without_room = ROOM_RE.sub("", line).strip()
            if not without_room:
                continue
            line = without_room

        if BREAK_RE.search(line):
            continue

        filtered.append(line)

    # Ищем преподавателя и убираем его из текста.
    teacher_line = None

    for i, line in enumerate(filtered):
        if TEACHER_RE.search(line):
            teacher_line = i
            break

    if teacher_line is not None:
        teacher_match = TEACHER_RE.search(filtered[teacher_line])

        if teacher_match:
            teacher = (
                f"{teacher_match.group(1)} "
                f"{teacher_match.group(2)}."
                f"{teacher_match.group(3)}."
            )

            before = filtered[:teacher_line]
            after = filtered[teacher_line + 1:]

            # Иногда предмет находится до преподавателя.
            subject_parts = before + after
        else:
            subject_parts = filtered
    else:
        subject_parts = filtered

    # Убираем всякий мусор.
    subject_parts_clean = []

    for part in subject_parts:
        part = clean_line(part)

        if not part:
            continue

        if part.lower() in {
            "расписание",
            "занятия",
            "аудитория",
            "преподаватель",
        }:
            continue

        if part == room:
            continue

        subject_parts_clean.append(part)

    # Часто после парсинга аудитория остаётся в объединённой строке.
    if room:
        subject_parts_clean = [
            ROOM_RE.sub("", x).strip()
            for x in subject_parts_clean
        ]

    subject = " ".join(
        x for x in subject_parts_clean
        if x
    )

    # Если преподаватель попал внутрь subject — удаляем.
    if teacher:
        subject = subject.replace(teacher, "")

    subject = clean_text(subject)

    # Не даём в предмет попасть заголовку страницы.
    subject = re.sub(
        r"Расписание занятий группы.*?$",
        "",
        subject,
        flags=re.IGNORECASE,
    ).strip()

    if not pair:
        return None

    return {
        "pair": pair,
        "start": start_time,
        "end": end_time,
        "room": room or "—",
        "teacher": teacher or "—",
        "subject": subject or "Занятие",
    }


def candidate_elements(soup):
    """
    Ищем потенциальные карточки без привязки
    к конкретным CSS-классам сайта.
    """

    elements = soup.find_all(
        [
            "article",
            "section",
            "li",
            "div",
            "td",
            "tr",
        ]
    )

    candidates = []

    for element in elements:
        try:
            text = element.get_text(
                "\n",
                strip=True,
            )
        except Exception:
            continue

        text = clean_text(text)

        if not text:
            continue

        times = TIME_RE.findall(text)

        # В карточке должна быть ровно одна пара.
        if len(times) != 1:
            continue

        if len(text) < 15:
            continue

        # Не берём гигантские контейнеры.
        if len(text) > 800:
            continue

        lower = text.lower()

        has_schedule_marker = (
            "пара" in lower
            or "ауд" in lower
            or bool(TEACHER_RE.search(text))
        )

        if not has_schedule_marker:
            continue

        candidates.append(
            (
                len(text),
                text,
            )
        )

    # Сначала самые маленькие элементы.
    candidates.sort(key=lambda x: x[0])

    unique = []

    for _, text in candidates:
        normalized = re.sub(
            r"\s+",
            " ",
            text.lower(),
        )

        duplicate = False

        for existing in unique:
            existing_normalized = re.sub(
                r"\s+",
                " ",
                existing.lower(),
            )

            if normalized == existing_normalized:
                duplicate = True
                break

        if not duplicate:
            unique.append(text)

    return unique


def parse_from_dom(soup):
    cards = candidate_elements(soup)

    result = []

    for text in cards:
        item = parse_card_text(text)

        if not item:
            continue

        result.append(item)

    # Удаляем дубликаты по времени + паре.
    unique = {}

    for item in result:
        key = (
            item["pair"],
            item["start"],
            item["end"],
        )

        # Если встретили более информативную карточку,
        # оставляем её.
        old = unique.get(key)

        if old is None:
            unique[key] = item
            continue

        old_score = len(
            old["subject"]
        ) + len(
            old["teacher"]
        ) + len(
            old["room"]
        )

        new_score = len(
            item["subject"]
        ) + len(
            item["teacher"]
        ) + len(
            item["room"]
        )

        if new_score > old_score:
            unique[key] = item

    result = list(unique.values())

    return sort_lessons(result)


def parse_from_visible_text(soup):
    """
    Запасной парсер.

    Он вообще не зависит от HTML-классов:
    берёт видимый текст страницы и режет его
    по времени занятий.
    """

    text = soup.get_text(
        "\n",
        strip=True,
    )

    text = clean_text(text)

    matches = list(
        TIME_RE.finditer(text)
    )

    if not matches:
        return []

    result = []

    roman_pairs = [
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

    for index, match in enumerate(matches):
        start_pos = match.start()
        end_pos = match.end()

        previous_start = max(
            0,
            start_pos - 180,
        )

        next_end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else min(
                len(text),
                end_pos + 500,
            )
        )

        before = text[
            previous_start:start_pos
        ]

        after = text[
            end_pos:next_end
        ]

        pair = extract_pair(before)

        if not pair and index < len(roman_pairs):
            pair = roman_pairs[index]

        combined = (
            before[-100:]
            + "\n"
            + match.group(0)
            + "\n"
            + after[:400]
        )

        item = parse_card_text(
            combined,
            fallback_pair=pair,
        )

        if item:
            result.append(item)

    # Дедупликация.
    unique = {}

    for item in result:
        key = (
            item["pair"],
            item["start"],
            item["end"],
        )

        old = unique.get(key)

        if old is None:
            unique[key] = item
        else:
            if len(item["subject"]) > len(old["subject"]):
                unique[key] = item

    return sort_lessons(
        list(unique.values())
    )


def sort_lessons(lessons):
    order = {
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

    return sorted(
        lessons,
        key=lambda x: (
            order.get(x["pair"], 99),
            x["start"],
        ),
    )


def parse_schedule(html: str):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # Удаляем явно ненужное.
    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
        ]
    ):
        tag.decompose()

    result = parse_from_dom(soup)

    # Если DOM-парсер что-то нашёл — используем его.
    if result:
        logger.info(
            "DOM parser: найдено занятий: %s",
            len(result),
        )
        return result

    # Второй уровень.
    result = parse_from_visible_text(soup)

    if result:
        logger.info(
            "Text parser: найдено занятий: %s",
            len(result),
        )
        return result

    # Важная проверка:
    # если на странице вообще есть времена,
    # значит занятий потенциально много, но
    # парсер не смог их разобрать.
    all_text = soup.get_text(" ", strip=True)

    time_count = len(
        TIME_RE.findall(all_text)
    )

    if time_count:
        raise ValueError(
            f"На странице найдено {time_count} "
            f"временных интервалов, но карточки "
            f"занятий не удалось разобрать."
        )

    # Реально пустой день.
    return []


# ============================================================
# HTTP
# ============================================================

async def fetch_schedule(d: date):
    url = build_schedule_url(d)

    logger.info(
        "Загрузка расписания: %s",
        url,
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": (
            "ru-RU,ru;q=0.9,en;q=0.5"
        ),
        "Connection": "keep-alive",
    }

    timeout = aiohttp.ClientTimeout(
        total=30
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
    ) as session:

        # ssl=False нужен из-за сертификата ishnk.ru.
        async with session.get(
            url,
            ssl=False,
            allow_redirects=True,
        ) as response:

            response.raise_for_status()

            raw = await response.read()

            logger.info(
                "Получено HTML: %s байт",
                len(raw),
            )

            html = decode_html(raw)

            lessons = parse_schedule(html)

            logger.info(
                "Итог: %s занятий",
                len(lessons),
            )

            return lessons


# ============================================================
# SIGNATURE
# ============================================================

def schedule_signature(lessons):
    normalized = []

    for item in lessons:
        normalized.append(
            {
                "pair": item["pair"],
                "start": item["start"],
                "end": item["end"],
                "room": item["room"],
                "teacher": item["teacher"],
                "subject": item["subject"],
            }
        )

    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


# ============================================================
# FONTS
# ============================================================

FONT_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]

FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def get_font(paths, size):
    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(
                path,
                size,
            )

    raise RuntimeError(
        "Не найден Cyrillic-шрифт DejaVu Sans. "
        "Добавь fonts-dejavu в Dockerfile."
    )


# ============================================================
# IMAGE HELPERS
# ============================================================

def text_width(draw, text, font):
    box = draw.textbbox(
        (0, 0),
        text,
        font=font,
    )

    return box[2] - box[0]


def text_height(draw, text, font):
    box = draw.textbbox(
        (0, 0),
        text,
        font=font,
    )

    return box[3] - box[1]


def wrap_text(draw, text, font, max_width):
    words = text.split()

    if not words:
        return ""

    lines = []
    current = words[0]

    for word in words[1:]:
        test = current + " " + word

        if text_width(
            draw,
            test,
            font,
        ) <= max_width:
            current = test
        else:
            lines.append(current)
            current = word

    lines.append(current)

    return "\n".join(lines)


# ============================================================
# MODERN DESIGN
# ============================================================

def render_schedule(d: date, lessons):
    """
    Современная минималистичная карточка расписания.

    Не копирует сайт.
    Это отдельный дизайн для Telegram.
    """

    WIDTH = 1200

    # Палитра
    BG = "#F5F7FA"
    WHITE = "#FFFFFF"
    TEXT = "#17202A"
    SECONDARY = "#718096"
    BLUE = "#1683FF"
    BLUE_LIGHT = "#EAF3FF"
    BORDER = "#E7ECF2"
    TIME_BG = "#F0F6FF"

    font_title = get_font(
        FONT_BOLD,
        52,
    )

    font_group = get_font(
        FONT_BOLD,
        30,
    )

    font_date = get_font(
        FONT_REGULAR,
        27,
    )

    font_pair = get_font(
        FONT_BOLD,
        27,
    )

    font_time = get_font(
        FONT_BOLD,
        37,
    )

    font_subject = get_font(
        FONT_BOLD,
        31,
    )

    font_info = get_font(
        FONT_REGULAR,
        25,
    )

    font_small = get_font(
        FONT_REGULAR,
        22,
    )

    # --------------------------------------------------------
    # Размеры
    # --------------------------------------------------------

    HEADER_HEIGHT = 230

    CARD_WIDTH = 1080
    CARD_X = (WIDTH - CARD_WIDTH) // 2

    CARD_GAP = 26

    CARD_MIN_HEIGHT = 205

    cards_data = []

    dummy = Image.new(
        "RGB",
        (WIDTH, 100),
        BG,
    )

    dummy_draw = ImageDraw.Draw(dummy)

    for item in lessons:
        subject = wrap_text(
            dummy_draw,
            item["subject"],
            font_subject,
            CARD_WIDTH - 100,
        )

        subject_lines = subject.count("\n") + 1

        card_height = max(
            CARD_MIN_HEIGHT,
            150 + subject_lines * 38,
        )

        cards_data.append(
            (
                item,
                subject,
                card_height,
            )
        )

    if not lessons:
        total_height = HEADER_HEIGHT + 300
    else:
        total_height = (
            HEADER_HEIGHT
            + 40
            + sum(
                x[2]
                for x in cards_data
            )
            + CARD_GAP * (
                len(cards_data) - 1
            )
            + 50
        )

    image = Image.new(
        "RGB",
        (WIDTH, total_height),
        BG,
    )

    draw = ImageDraw.Draw(image)

    # --------------------------------------------------------
    # Header
    # --------------------------------------------------------

    # Маленькая синяя точка / акцент
    draw.rounded_rectangle(
        (
            60,
            55,
            74,
            69,
        ),
        radius=7,
        fill=BLUE,
    )

    draw.text(
        (95, 43),
        "РАСПИСАНИЕ",
        font=font_small,
        fill=SECONDARY,
    )

    draw.text(
        (60, 88),
        GROUP_NAME,
        font=font_title,
        fill=TEXT,
    )

    draw.text(
        (60, 158),
        russian_date(d).capitalize(),
        font=font_date,
        fill=SECONDARY,
    )

    # --------------------------------------------------------
    # Empty
    # --------------------------------------------------------

    if not lessons:
        y = HEADER_HEIGHT + 45

        draw.rounded_rectangle(
            (
                CARD_X,
                y,
                CARD_X + CARD_WIDTH,
                y + 190,
            ),
            radius=28,
            fill=WHITE,
            outline=BORDER,
            width=2,
        )

        draw.text(
            (
                CARD_X + 45,
                y + 45,
            ),
            "Свободный день",
            font=font_subject,
            fill=TEXT,
        )

        draw.text(
            (
                CARD_X + 45,
                y + 100,
            ),
            "Занятий по расписанию нет.",
            font=font_info,
            fill=SECONDARY,
        )

        path = (
            IMAGES_DIR
            / f"{d.isoformat()}.png"
        )

        image.save(
            path,
            "PNG",
            optimize=True,
        )

        return path

    # --------------------------------------------------------
    # Cards
    # --------------------------------------------------------

    y = HEADER_HEIGHT + 40

    for index, (
        item,
        subject,
        card_height,
    ) in enumerate(cards_data):

        # Тень
        draw.rounded_rectangle(
            (
                CARD_X + 4,
                y + 7,
                CARD_X + CARD_WIDTH + 4,
                y + card_height + 7,
            ),
            radius=28,
            fill="#E9EDF2",
        )

        # Карточка
        draw.rounded_rectangle(
            (
                CARD_X,
                y,
                CARD_X + CARD_WIDTH,
                y + card_height,
            ),
            radius=28,
            fill=WHITE,
            outline=BORDER,
            width=2,
        )

        # Левая синяя полоска
        draw.rounded_rectangle(
            (
                CARD_X,
                y + 28,
                CARD_X + 8,
                y + card_height - 28,
            ),
            radius=4,
            fill=BLUE,
        )

        # ----------------------------------------------------
        # Pair
        # ----------------------------------------------------

        pair_text = f"{item['pair']} пара"

        draw.text(
            (
                CARD_X + 40,
                y + 28,
            ),
            pair_text,
            font=font_pair,
            fill=BLUE,
        )

        # ----------------------------------------------------
        # Time
        # ----------------------------------------------------

        time_text = (
            f"{item['start']} — {item['end']}"
        )

        time_x = CARD_X + CARD_WIDTH - 370

        draw.rounded_rectangle(
            (
                time_x,
                y + 24,
                CARD_X + CARD_WIDTH - 35,
                y + 78,
            ),
            radius=27,
            fill=TIME_BG,
        )

        tw = text_width(
            draw,
            time_text,
            font_time,
        )

        draw.text(
            (
                CARD_X
                + CARD_WIDTH
                - 35
                - tw
                - 25,
                y + 29,
            ),
            time_text,
            font=font_time,
            fill=TEXT,
        )

        # ----------------------------------------------------
        # Room
        # ----------------------------------------------------

        room = item["room"]

        draw.text(
            (
                CARD_X + 40,
                y + 87,
            ),
            room,
            font=font_small,
            fill=SECONDARY,
        )

        # ----------------------------------------------------
        # Teacher
        # ----------------------------------------------------

        teacher = item["teacher"]

        teacher_x = CARD_X + 300

        draw.text(
            (
                teacher_x,
                y + 87,
            ),
            teacher,
            font=font_small,
            fill=SECONDARY,
        )

        # ----------------------------------------------------
        # Subject
        # ----------------------------------------------------

        draw.multiline_text(
            (
                CARD_X + 40,
                y + 125,
            ),
            subject,
            font=font_subject,
            fill=TEXT,
            spacing=8,
        )

        y += card_height + CARD_GAP

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    path = (
        IMAGES_DIR
        / f"{d.isoformat()}.png"
    )

    image.save(
        path,
        "PNG",
        optimize=True,
    )

    return path


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
)

dp = Dispatcher()


async def send_schedule(
    message: Message,
    d: date,
):
    try:
        lessons = await fetch_schedule(d)

    except Exception:
        logger.exception(
            "Ошибка получения расписания"
        )

        await message.answer(
            "Не удалось загрузить расписание.\n"
            "Попробуй ещё раз через несколько секунд."
        )

        return

    try:
        image_path = render_schedule(
            d,
            lessons,
        )

    except Exception:
        logger.exception(
            "Ошибка создания изображения"
        )

        await message.answer(
            "Расписание загрузилось, "
            "но не удалось создать изображение."
        )

        return

    caption = (
        f"📚 <b>{GROUP_NAME}</b>\n"
        f"🗓 {russian_date(d).capitalize()}\n\n"
    )

    if lessons:
        caption += (
            f"Найдено занятий: <b>{len(lessons)}</b>"
        )
    else:
        caption += "Занятий нет."

    await message.answer_photo(
        photo=FSInputFile(image_path),
        caption=caption,
    )


# ============================================================
# COMMANDS
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"📚 <b>{GROUP_NAME}</b>\n\n"
        "Расписание занятий в удобном формате.\n\n"
        "<b>Команды:</b>\n"
        "/schedule — расписание на сегодня\n"
        "/schedule завтра — расписание на завтра\n"
        "/date 04.09.2026 — расписание на дату\n"
        "/subscribe — получать изменения\n"
        "/unsubscribe — отключить уведомления\n"
        "/checknow — проверить завтра\n",
        parse_mode="HTML",
    )


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) == 1:
        d = datetime.now(TZ).date()
    else:
        d = parse_user_date(args[1])

        if d is None:
            await message.answer(
                "Не понял дату.\n\n"
                "Примеры:\n"
                "/schedule\n"
                "/schedule завтра\n"
                "/schedule 04.09.2026"
            )
            return

    await send_schedule(
        message,
        d,
    )


@dp.message(Command("date"))
async def cmd_date(message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "Укажи дату:\n"
            "/date 04.09.2026"
        )
        return

    d = parse_user_date(args[1])

    if d is None:
        await message.answer(
            "Неверный формат даты.\n"
            "Используй: /date 04.09.2026"
        )
        return

    await send_schedule(
        message,
        d,
    )


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    subscribers = load_subscribers()

    if message.chat.id not in subscribers:
        subscribers.append(message.chat.id)
        save_subscribers(subscribers)

    await message.answer(
        "🔔 <b>Уведомления включены.</b>\n\n"
        "Я буду автоматически проверять расписание "
        "и отправлять сообщение, если оно изменится.",
        parse_mode="HTML",
    )


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    subscribers = load_subscribers()

    if message.chat.id in subscribers:
        subscribers.remove(message.chat.id)
        save_subscribers(subscribers)

    await message.answer(
        "🔕 Уведомления отключены."
    )


@dp.message(Command("checknow"))
async def cmd_checknow(message: Message):
    tomorrow = (
        datetime.now(TZ).date()
        + timedelta(days=1)
    )

    await message.answer(
        "🔎 Проверяю расписание на завтра..."
    )

    await send_schedule(
        message,
        tomorrow,
    )


# ============================================================
# AUTOMATIC CHECK
# ============================================================

check_lock = asyncio.Lock()


async def check_date_and_notify(
    d: date,
    subscribers,
):
    async with check_lock:

        try:
            lessons = await fetch_schedule(d)

        except Exception:
            logger.exception(
                "Автопроверка %s завершилась ошибкой",
                d,
            )
            return

        signature = schedule_signature(
            lessons
        )

        cache = load_cache()

        key = d.isoformat()

        old_signature = cache.get(key)

        # Первый запуск:
        # просто запоминаем состояние.
        if old_signature is None:
            cache[key] = signature
            save_cache(cache)

            logger.info(
                "Первое состояние сохранено: %s",
                d,
            )

            return

        # Ничего не изменилось.
        if old_signature == signature:
            logger.info(
                "Без изменений: %s",
                d,
            )
            return

        # Изменилось.
        cache[key] = signature
        save_cache(cache)

        logger.info(
            "РАСПИСАНИЕ ИЗМЕНИЛОСЬ: %s",
            d,
        )

        try:
            image_path = render_schedule(
                d,
                lessons,
            )
        except Exception:
            logger.exception(
                "Не удалось создать изображение "
                "для уведомления"
            )
            return

        if lessons:
            caption = (
                "🔄 <b>Расписание изменилось</b>\n\n"
                f"📚 <b>{GROUP_NAME}</b>\n"
                f"🗓 {russian_date(d).capitalize()}\n\n"
                f"Занятий: <b>{len(lessons)}</b>"
            )
        else:
            caption = (
                "🔄 <b>Расписание изменилось</b>\n\n"
                f"📚 <b>{GROUP_NAME}</b>\n"
                f"🗓 {russian_date(d).capitalize()}\n\n"
                "На этот день занятий больше нет."
            )

        dead_users = []

        for chat_id in subscribers:
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(
                        image_path
                    ),
                    caption=caption,
                    parse_mode="HTML",
                )

                logger.info(
                    "Уведомление отправлено: %s",
                    chat_id,
                )

                await asyncio.sleep(0.05)

            except Exception as e:
                logger.warning(
                    "Не удалось отправить %s: %s",
                    chat_id,
                    e,
                )

                # Если пользователь удалил чат с ботом,
                # Telegram обычно вернёт ошибку.
                error_text = str(e).lower()

                if (
                    "bot was blocked" in error_text
                    or "chat not found" in error_text
                    or "user is deactivated" in error_text
                ):
                    dead_users.append(
                        chat_id
                    )

        if dead_users:
            subscribers = [
                x
                for x in subscribers
                if x not in dead_users
            ]

            save_subscribers(
                subscribers
            )


async def automatic_checker():
    await asyncio.sleep(10)

    while True:
        try:
            subscribers = load_subscribers()

            today = datetime.now(TZ).date()

            # Проверяем сегодня и завтра.
            await check_date_and_notify(
                today,
                subscribers,
            )

            await check_date_and_notify(
                today + timedelta(days=1),
                subscribers,
            )

            # Чистим старые записи cache.
            cache = load_cache()

            cutoff = today - timedelta(days=7)

            changed = False

            for key in list(cache.keys()):
                try:
                    d = date.fromisoformat(key)

                    if d < cutoff:
                        del cache[key]
                        changed = True

                except Exception:
                    pass

            if changed:
                save_cache(cache)

        except Exception:
            logger.exception(
                "Ошибка фонового мониторинга"
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# MAIN
# ============================================================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не указан в .env"
        )

    logger.info("=" * 60)
    logger.info("Schedule bot started")
    logger.info("Group: %s", GROUP_NAME)
    logger.info("URL: %s", SCHEDULE_URL)
    logger.info(
        "Check interval: %s sec",
        CHECK_INTERVAL,
    )
    logger.info("=" * 60)

    asyncio.create_task(
        automatic_checker()
    )

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
