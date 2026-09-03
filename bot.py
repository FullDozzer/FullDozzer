import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
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

# Базовый адрес группы.
# Дата будет добавляться автоматически:
# https://www.ishnk.ru/2025/site/schedule/group/508/2026-09-03
SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://www.ishnk.ru/2025/site/schedule/group/508"
).rstrip("/")

GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24")

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
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("schedule_bot")


# ============================================================
# TELEGRAM
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. Добавь его в .env"
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# ОБЩИЕ УТИЛИТЫ
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
    r"\b([01]?\d|2[0-3]):[0-5]\d\s*[-–—]\s*([01]?\d|2[0-3]):[0-5]\d\b"
)

TIME_SINGLE_RE = re.compile(
    r"\b([01]?\d|2[0-3]):[0-5]\d\b"
)

DATE_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
)

PAIR_RE = re.compile(
    r"\b(?:I|II|III|IV|V|VI|VII|VIII|IX|X)\s*(?:пара)?\b",
    re.IGNORECASE,
)


def clean_text(text: str) -> str:
    """
    Нормализует пробелы и HTML-мусор.
    """

    text = text.replace("\xa0", " ")
    text = text.replace("\u200b", "")
    text = text.replace("\r", " ")
    text = text.replace("\n", " ")

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize_for_hash(text: str) -> str:
    text = clean_text(text).lower()

    # Убираем несущественные различия тире.
    text = text.replace("—", "-")
    text = text.replace("–", "-")

    return text


def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return now_local().date()


def parse_date_argument(value: str) -> date | None:
    """
    Поддерживает:
      03.09.2026
      03-09-2026
      2026-09-03
    """

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


def format_date_ru(value: date) -> str:
    return (
        f"{WEEKDAYS[value.weekday()].capitalize()}, "
        f"{value.day} {MONTHS[value.month - 1]} "
        f"{value.year} года"
    )


def build_schedule_url(value: date) -> str:
    return f"{SCHEDULE_URL}/{value.isoformat()}"


# ============================================================
# JSON ХРАНИЛИЩЕ
# ============================================================

def load_json(path: Path, default):
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


def save_json(path: Path, data):
    tmp_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with tmp_path.open(
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    tmp_path.replace(path)


def load_subscribers() -> set[int]:
    data = load_json(
        SUBSCRIBERS_FILE,
        []
    )

    result = set()

    if isinstance(data, list):
        for item in data:
            try:
                result.add(int(item))
            except (TypeError, ValueError):
                pass

    return result


def save_subscribers(subscribers: set[int]):
    save_json(
        SUBSCRIBERS_FILE,
        sorted(subscribers)
    )


# ============================================================
# КОДИРОВКА HTML
# ============================================================

def extract_charset_from_content_type(content_type: str) -> str | None:
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


def extract_charset_from_html(raw: bytes) -> str | None:
    """
    Пытаемся найти charset непосредственно в HTML.

    Например:

      <meta charset="utf-8">

    или:

      <meta http-equiv="Content-Type"
            content="text/html; charset=windows-1251">
    """

    # Для поиска meta нам не обязательно идеально декодировать весь документ.
    preview = raw[:100_000].decode(
        "latin-1",
        errors="ignore"
    )

    match = re.search(
        r'<meta[^>]+charset\s*=\s*["\']?\s*'
        r'([a-zA-Z0-9._-]+)',
        preview,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    match = re.search(
        r'charset\s*=\s*["\']?\s*'
        r'([a-zA-Z0-9._-]+)',
        preview,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return None


def cyrillic_score(text: str) -> int:
    """
    Оцениваем, насколько текст похож на нормальный русский.
    """

    if not text:
        return -100000

    score = 0

    russian = len(
        re.findall(
            r"[А-Яа-яЁё]",
            text
        )
    )

    latin = len(
        re.findall(
            r"[A-Za-z]",
            text
        )
    )

    replacement = text.count("�")

    mojibake = (
        text.count("Р")
        + text.count("С")
        + text.count("Ð")
        + text.count("Ñ")
    )

    score += russian * 5
    score += min(latin, 500)

    score -= replacement * 100
    score -= mojibake * 3

    return score


def decode_html(
    raw: bytes,
    content_type: str = ""
) -> tuple[str, str]:
    """
    Надёжное декодирование HTML.

    Приоритет:
      1. charset из HTTP Content-Type
      2. charset из <meta>
      3. UTF-8
      4. CP1251
      5. Windows-1251

    BeautifulSoup также умеет определять кодировку самостоятельно,
    но для русскоязычного старого сайта лучше иметь явные fallback.
    """

    candidates: list[str] = []

    http_charset = extract_charset_from_content_type(
        content_type
    )

    html_charset = extract_charset_from_html(
        raw
    )

    for encoding in (
        http_charset,
        html_charset,
        "utf-8",
        "cp1251",
        "windows-1251",
    ):
        if encoding:
            encoding = encoding.strip().lower()

            if encoding not in candidates:
                candidates.append(encoding)

    best_text = None
    best_encoding = None
    best_score = -10**9

    for encoding in candidates:
        try:
            text = raw.decode(
                encoding,
                errors="strict"
            )

            # Берём начало + title + основной текст.
            sample = text[:200_000]

            score = cyrillic_score(
                sample
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
        # Последний fallback.
        best_encoding = "utf-8"
        best_text = raw.decode(
            "utf-8",
            errors="replace"
        )

    logger.info(
        "Выбрана кодировка HTML: %s",
        best_encoding
    )

    return best_text, best_encoding


# ============================================================
# HTTP
# ============================================================

async def fetch_html(value: date) -> tuple[str, str]:
    url = build_schedule_url(value)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/120 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": (
            "ru-RU,ru;q=0.9,en;q=0.7"
        ),
        "Connection": "keep-alive",
    }

    timeout = aiohttp.ClientTimeout(
        total=40,
        connect=15,
        sock_read=30,
    )

    logger.info(
        "Запрос расписания: %s",
        url
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        # У сайта проблемный сертификат,
        # поэтому здесь отключаем его проверку.
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
                "HTTP %s | %s bytes | encoding=%s",
                response.status,
                len(raw),
                encoding
            )

            return html, encoding


# ============================================================
# ПАРСЕР
# ============================================================

def extract_room(text: str) -> str:
    """
    Ищем аудиторию.

    Примеры:
      УК107
      ПК107
      УК303
      ауд. УК107
    """

    patterns = [
        r"\bауд\.?\s*([А-ЯЁA-Z]{1,5}\s*[-]?\s*\d{2,4})\b",
        r"\b([А-ЯЁA-Z]{1,5}\s*[-]?\s*\d{2,4})\b",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            room = match.group(1)
            room = clean_text(room)

            return room

    return ""


def extract_time(text: str) -> str:
    match = TIME_RE.search(text)

    if not match:
        return ""

    return (
        f"{int(match.group(1)):02d}:{text[match.start(1) + len(match.group(1)):match.start(1) + len(match.group(1)) + 3]}"
    )


def normalize_time(match: re.Match) -> str:
    start_h = int(match.group(1))
    start_min = int(
        match.group(0)[
            match.group(0).find(":") + 1:
            match.group(0).find(":") + 3
        ]
    )

    # Найдём вторую часть времени отдельно.
    parts = re.split(
        r"\s*[-–—]\s*",
        match.group(0)
    )

    if len(parts) != 2:
        return match.group(0)

    return (
        f"{parts[0]} - {parts[1]}"
    )


def extract_time_from_text(text: str) -> str:
    match = TIME_RE.search(text)

    if match:
        return normalize_time(match)

    return ""


def extract_pair_number(text: str, fallback: int) -> str:
    roman_match = PAIR_RE.search(text)

    if roman_match:
        raw = roman_match.group(0)

        roman = re.search(
            r"I{1,3}|IV|V|VI{0,3}|IX|X",
            raw,
            re.IGNORECASE
        )

        if roman:
            return roman.group(0).upper()

    # Арабские варианты.
    match = re.search(
        r"\b(?:пара|№)\s*([1-9][0-9]?)\b",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return str(fallback)


def looks_like_schedule_text(text: str) -> bool:
    lower = text.lower()

    keywords = [
        "пара",
        "занят",
        "ауд",
        "преподав",
        "дисцип",
        "лекц",
        "практик",
        "экспл",
    ]

    return any(
        keyword in lower
        for keyword in keywords
    )


def text_without_time(text: str) -> str:
    text = TIME_RE.sub(
        " ",
        text
    )

    text = PAIR_RE.sub(
        " ",
        text
    )

    text = re.sub(
        r"\b(?:пара|ауд\.?)\b",
        " ",
        text,
        flags=re.IGNORECASE
    )

    return clean_text(text)


def remove_room_from_text(
    text: str,
    room: str
) -> str:

    if not room:
        return text

    text = re.sub(
        rf"\bауд\.?\s*{re.escape(room)}\b",
        " ",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        rf"\b{re.escape(room)}\b",
        " ",
        text,
        flags=re.IGNORECASE
    )

    return clean_text(text)


def is_bad_subject(text: str) -> bool:
    if not text:
        return True

    lower = text.lower()

    bad_words = [
        "расписание",
        "группа",
        "личный кабинет",
        "войти",
        "выход",
        "главная",
        "сайт",
        "меню",
        "предыдущ",
        "следующ",
        "сегодня",
        "завтра",
    ]

    if len(text) < 3:
        return True

    if any(
        word in lower
        for word in bad_words
    ):
        return True

    # Если это почти только номер/время.
    if re.fullmatch(
        r"[\d\s:.\-–—]+",
        text
    ):
        return True

    return False


def guess_subject_and_teacher(
    lines: list[str],
    room: str
) -> tuple[str, str]:

    cleaned = []

    for line in lines:
        line = clean_text(line)

        if not line:
            continue

        line = TIME_RE.sub(
            " ",
            line
        )

        line = remove_room_from_text(
            line,
            room
        )

        line = clean_text(line)

        if not line:
            continue

        if is_bad_subject(line):
            continue

        if line not in cleaned:
            cleaned.append(line)

    if not cleaned:
        return "Занятие", ""

    # Сначала пытаемся определить преподавателя
    # по типичным ФИО:
    # Иванов И.И.
    teacher_index = None

    teacher_re = re.compile(
        r"^[А-ЯЁ][а-яё-]+"
        r"(?:\s+[А-ЯЁ][а-яё-]+)?"
        r"\s+[А-ЯЁ]\.[А-ЯЁ]\.?$"
    )

    for i, line in enumerate(cleaned):
        if teacher_re.search(line):
            teacher_index = i
            break

    if teacher_index is not None:
        teacher = cleaned[teacher_index]

        subjects = [
            x for i, x in enumerate(cleaned)
            if i != teacher_index
        ]

        subject = " ".join(
            subjects[:2]
        ).strip()

        return (
            subject or "Занятие",
            teacher
        )

    # Дополнительная попытка:
    # строка с двумя инициалами.
    for i, line in enumerate(cleaned):
        if re.search(
            r"\b[А-ЯЁ]\.[А-ЯЁ]\.?\b",
            line
        ):
            teacher = line

            subjects = [
                x for j, x in enumerate(cleaned)
                if j != i
            ]

            return (
                " ".join(subjects[:2])
                or "Занятие",
                teacher
            )

    # Если преподаватель не определён,
    # первая строка считается предметом.
    subject = cleaned[0]

    teacher = (
        cleaned[1]
        if len(cleaned) > 1
        else ""
    )

    return subject, teacher


def parse_schedule(html: str) -> list[dict]:
    """
    Универсальный парсер.

    Сначала пробует таблицы.
    Затем — карточки/блоки вокруг времени.
    """

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # Удаляем служебные элементы.
    for tag in soup(
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

    results = []

    # --------------------------------------------------------
    # 1. TABLE
    # --------------------------------------------------------

    for table in soup.find_all("table"):

        rows = table.find_all("tr")

        for row in rows:

            cells = row.find_all(
                ["td", "th"]
            )

            if not cells:
                continue

            cell_texts = [
                clean_text(
                    cell.get_text(
                        " ",
                        strip=True
                    )
                )
                for cell in cells
            ]

            full_text = " | ".join(
                x for x in cell_texts
                if x
            )

            time_match = TIME_RE.search(
                full_text
            )

            if not time_match:
                continue

            time_value = normalize_time(
                time_match
            )

            room = extract_room(
                full_text
            )

            pair_number = extract_pair_number(
                full_text,
                len(results) + 1
            )

            # Обычно таблица содержит отдельные
            # колонки: пара / время / предмет / преподаватель / аудитория.
            subject = ""
            teacher = ""

            for cell in cell_texts:

                if not cell:
                    continue

                if TIME_RE.search(cell):
                    continue

                if room and room.lower() in cell.lower():
                    continue

                if re.fullmatch(
                    r"(I|II|III|IV|V|VI|VII|VIII|IX|X)",
                    cell,
                    re.IGNORECASE
                ):
                    continue

                if not subject:
                    subject = cell
                    continue

                if not teacher:
                    teacher = cell

            if not subject:
                subject, teacher = (
                    guess_subject_and_teacher(
                        cell_texts,
                        room
                    )
                )

            item = {
                "number": pair_number,
                "time": time_value,
                "subject": subject,
                "teacher": teacher,
                "room": room,
            }

            results.append(item)

    # --------------------------------------------------------
    # 2. БЛОКИ С ВРЕМЕНЕМ
    # --------------------------------------------------------

    if not results:

        time_nodes = []

        for element in soup.find_all(
            True
        ):

            text = clean_text(
                element.get_text(
                    " ",
                    strip=True
                )
            )

            if TIME_RE.search(text):
                time_nodes.append(
                    element
                )

        for element in time_nodes:

            # Ищем ближайший разумный контейнер.
            container = element

            for _ in range(5):

                if container.parent is None:
                    break

                parent = container.parent

                parent_text = clean_text(
                    parent.get_text(
                        " ",
                        strip=True
                    )
                )

                # Не позволяем контейнеру разрастаться
                # на всю страницу.
                if (
                    len(parent_text) <= 1200
                    and TIME_RE.search(parent_text)
                ):
                    container = parent
                else:
                    break

            text = clean_text(
                container.get_text(
                    " ",
                    strip=True
                )
            )

            time_match = TIME_RE.search(
                text
            )

            if not time_match:
                continue

            time_value = normalize_time(
                time_match
            )

            room = extract_room(
                text
            )

            pair_number = extract_pair_number(
                text,
                len(results) + 1
            )

            # Получаем отдельные текстовые строки
            # из контейнера.
            lines = []

            for child in container.find_all(
                ["div", "span", "p", "td", "li", "a"],
                recursive=True
            ):
                child_text = clean_text(
                    child.get_text(
                        " ",
                        strip=True
                    )
                )

                if (
                    child_text
                    and len(child_text) < 500
                    and child_text not in lines
                ):
                    lines.append(child_text)

            if not lines:
                lines = [
                    text
                ]

            subject, teacher = (
                guess_subject_and_teacher(
                    lines,
                    room
                )
            )

            # Отсекаем явно не относящиеся
            # к расписанию элементы.
            if (
                not looks_like_schedule_text(text)
                and not subject
            ):
                continue

            item = {
                "number": pair_number,
                "time": time_value,
                "subject": subject,
                "teacher": teacher,
                "room": room,
            }

            results.append(item)

    # --------------------------------------------------------
    # 3. НОРМАЛИЗАЦИЯ + DEDUPE
    # --------------------------------------------------------

    normalized = []

    seen = set()

    for item in results:

        number = clean_text(
            item.get("number", "")
        )

        time_value = clean_text(
            item.get("time", "")
        )

        subject = clean_text(
            item.get("subject", "")
        )

        teacher = clean_text(
            item.get("teacher", "")
        )

        room = clean_text(
            item.get("room", "")
        )

        if not time_value:
            continue

        # Иногда парсер получает слишком общий
        # контейнер. Не сохраняем полностью мусорные строки.
        if len(subject) > 250:
            subject = subject[:247] + "..."

        if len(teacher) > 150:
            teacher = teacher[:147] + "..."

        key = (
            normalize_for_hash(number),
            normalize_for_hash(time_value),
            normalize_for_hash(subject),
            normalize_for_hash(teacher),
            normalize_for_hash(room),
        )

        if key in seen:
            continue

        seen.add(key)

        normalized.append(
            {
                "number": number,
                "time": time_value,
                "subject": subject or "Занятие",
                "teacher": teacher,
                "room": room,
            }
        )

    # --------------------------------------------------------
    # Сортировка по времени
    # --------------------------------------------------------

    def time_sort_key(item):
        match = re.search(
            r"(\d{1,2}):(\d{2})",
            item["time"]
        )

        if match:
            return (
                int(match.group(1)),
                int(match.group(2))
            )

        return (99, 99)

    normalized.sort(
        key=time_sort_key
    )

    # Перенумеруем только если номер
    # совсем не удалось определить.
    for index, item in enumerate(
        normalized,
        start=1
    ):
        if not item["number"]:
            item["number"] = str(index)

    return normalized


# ============================================================
# ПРОВЕРКА, ЧТО СТРАНИЦА ДЕЙСТВИТЕЛЬНО РАСПИСАНИЕ
# ============================================================

def page_looks_like_schedule(
    html: str,
    schedule: list[dict]
) -> bool:

    text = clean_text(
        BeautifulSoup(
            html,
            "html.parser"
        ).get_text(
            " ",
            strip=True
        )
    )

    lower = text.lower()

    schedule_word = (
        "расписание" in lower
        or "расписания" in lower
    )

    group_word = (
        GROUP_NAME.lower() in lower
    )

    has_time = bool(
        TIME_RE.search(text)
    )

    # Нормальная страница расписания.
    if schedule_word and (
        group_word or has_time
    ):
        return True

    # Может быть расписание без явного названия группы.
    if has_time and schedule:
        return True

    return False


# ============================================================
# ЗАГРУЗКА РАСПИСАНИЯ
# ============================================================

async def get_schedule(
    value: date
) -> list[dict]:

    html, encoding = await fetch_html(
        value
    )

    schedule = parse_schedule(
        html
    )

    if not page_looks_like_schedule(
        html,
        schedule
    ):
        logger.warning(
            "Страница не похожа на расписание: %s",
            build_schedule_url(value)
        )

    logger.info(
        "На %s найдено занятий: %d",
        value.isoformat(),
        len(schedule)
    )

    return schedule


# ============================================================
# ХЕШ РАСПИСАНИЯ
# ============================================================

def schedule_signature(
    schedule: list[dict]
) -> str:

    normalized = []

    for item in schedule:

        normalized.append(
            {
                "number": normalize_for_hash(
                    item.get("number", "")
                ),
                "time": normalize_for_hash(
                    item.get("time", "")
                ),
                "subject": normalize_for_hash(
                    item.get("subject", "")
                ),
                "teacher": normalize_for_hash(
                    item.get("teacher", "")
                ),
                "room": normalize_for_hash(
                    item.get("room", "")
                ),
            }
        )

    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


# ============================================================
# CACHE
# ============================================================

def load_cache() -> dict:
    return load_json(
        CACHE_FILE,
        {}
    )


def save_cache(cache: dict):
    save_json(
        CACHE_FILE,
        cache
    )


# ============================================================
# ШРИФТЫ
# ============================================================

def get_font(
    size: int,
    bold: bool = False
):
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans-Bold.ttf",

            "/usr/share/fonts/dejavu/"
            "DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans.ttf",

            "/usr/share/fonts/dejavu/"
            "DejaVuSans.ttf",
        ]

    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(
                path,
                size
            )

    return ImageFont.load_default()


# ============================================================
# ПЕРЕНОС ТЕКСТА
# ============================================================

def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int
) -> list[str]:

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

        width = bbox[2] - bbox[0]

        if width <= max_width:
            current = test
            continue

        if current:
            lines.append(
                current
            )

        # Если одно слово само длиннее ширины,
        # просто оставляем его отдельной строкой.
        current = word

    if current:
        lines.append(
            current
        )

    return lines


# ============================================================
# КРАСИВАЯ КАРТОЧКА
# ============================================================

def render_schedule(
    schedule: list[dict],
    schedule_date: date
) -> Path:

    width = 1200

    margin = 70

    background = (247, 248, 250)
    card_background = (255, 255, 255)

    primary = (24, 27, 32)
    secondary = (105, 110, 120)
    divider = (229, 231, 235)

    title_font = get_font(
        48,
        bold=True
    )

    date_font = get_font(
        29
    )

    pair_font = get_font(
        27,
        bold=True
    )

    time_font = get_font(
        27,
        bold=True
    )

    subject_font = get_font(
        30,
        bold=True
    )

    teacher_font = get_font(
        23
    )

    room_font = get_font(
        23,
        bold=True
    )

    small_font = get_font(
        19
    )

    # --------------------------------------------------------
    # Высота карточек
    # --------------------------------------------------------

    row_heights = []

    for item in schedule:

        subject = item.get(
            "subject",
            "Занятие"
        )

        subject_lines = wrap_text(
            ImageDraw.Draw(
                Image.new(
                    "RGB",
                    (10, 10)
                )
            ),
            subject,
            subject_font,
            width - margin * 2 - 170
        )

        height = (
            165
            + max(
                0,
                len(subject_lines) - 1
            ) * 38
        )

        row_heights.append(
            height
        )

    if not schedule:
        row_heights = [170]

    header_height = 190
    footer_height = 70

    total_height = (
        header_height
        + sum(row_heights)
        + footer_height
    )

    image = Image.new(
        "RGB",
        (width, total_height),
        background
    )

    draw = ImageDraw.Draw(
        image
    )

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    x = margin
    y = 50

    draw.text(
        (x, y),
        GROUP_NAME,
        font=title_font,
        fill=primary
    )

    draw.text(
        (x, y + 72),
        format_date_ru(
            schedule_date
        ),
        font=date_font,
        fill=secondary
    )

    y = header_height

    # --------------------------------------------------------
    # НЕТ ЗАНЯТИЙ
    # --------------------------------------------------------

    if not schedule:

        card_x1 = margin - 20
        card_x2 = width - margin + 20
        card_y1 = y
        card_y2 = y + 160

        draw.rounded_rectangle(
            (
                card_x1,
                card_y1,
                card_x2,
                card_y2
            ),
            radius=24,
            fill=card_background
        )

        draw.text(
            (
                margin + 30,
                y + 45
            ),
            "Занятий нет",
            font=subject_font,
            fill=primary
        )

        draw.text(
            (
                margin + 30,
                y + 92
            ),
            "Свободный день",
            font=teacher_font,
            fill=secondary
        )

    # --------------------------------------------------------
    # ЗАНЯТИЯ
    # --------------------------------------------------------

    else:

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
                fill=card_background
            )

            # Номер пары.
            pair_number = item.get(
                "number",
                str(index + 1)
            )

            draw.text(
                (
                    margin,
                    y + 27
                ),
                pair_number,
                font=pair_font,
                fill=secondary
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
                fill=primary
            )

            # Аудитория справа.
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
                    fill=primary
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
                    fill=primary
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
                        card_y2 - 38
                    ),
                    teacher,
                    font=teacher_font,
                    fill=secondary
                )

            y += row_height

    # --------------------------------------------------------
    # FOOTER
    # --------------------------------------------------------

    footer = "Расписание занятий"

    bbox = draw.textbbox(
        (0, 0),
        footer,
        font=small_font
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
        font=small_font,
        fill=secondary
    )

    filename = (
        IMAGE_DIR
        / f"schedule_{schedule_date.isoformat()}.png"
    )

    image.save(
        filename,
        "PNG",
        optimize=True
    )

    return filename


# ============================================================
# ТЕКСТОВОЕ РАСПИСАНИЕ
# ============================================================

def schedule_to_text(
    schedule: list[dict],
    schedule_date: date
) -> str:

    lines = [
        f"<b>{GROUP_NAME}</b>",
        f"{format_date_ru(schedule_date)}",
        "",
    ]

    if not schedule:
        lines.append(
            "Занятий нет."
        )

        return "\n".join(lines)

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

    return "\n".join(lines).strip()


# ============================================================
# ОТПРАВКА РАСПИСАНИЯ
# ============================================================

async def send_schedule(
    message: Message,
    schedule_date: date
):

    try:

        schedule = await get_schedule(
            schedule_date
        )

        image_path = render_schedule(
            schedule,
            schedule_date
        )

        caption = (
            f"<b>{GROUP_NAME}</b>\n"
            f"{format_date_ru(schedule_date)}"
        )

        await message.answer_photo(
            photo=FSInputFile(
                image_path
            ),
            caption=caption
        )

    except aiohttp.ClientResponseError as e:

        logger.exception(
            "HTTP ошибка"
        )

        await message.answer(
            "Не удалось получить расписание "
            f"(HTTP {e.status})."
        )

    except (
        aiohttp.ClientError,
        asyncio.TimeoutError
    ):

        logger.exception(
            "Ошибка соединения"
        )

        await message.answer(
            "Не удалось подключиться к сайту "
            "с расписанием. Попробуй ещё раз."
        )

    except Exception:

        logger.exception(
            "Ошибка получения расписания"
        )

        await message.answer(
            "Произошла ошибка при обработке "
            "расписания."
        )


# ============================================================
# /start
# ============================================================

@dp.message(Command("start"))
async def cmd_start(
    message: Message
):

    await message.answer(
        "<b>Расписание группы "
        f"{GROUP_NAME}</b>\n\n"
        "Команды:\n"
        "• /schedule — расписание на сегодня\n"
        "• /schedule ДД.ММ.ГГГГ — на выбранную дату\n"
        "• /subscribe — получать изменения автоматически\n"
        "• /unsubscribe — отключить уведомления\n"
        "• /checknow — проверить расписание на завтра",
        parse_mode="HTML"
    )


# ============================================================
# /schedule
# ============================================================

@dp.message(Command("schedule"))
async def cmd_schedule(
    message: Message
):

    args = message.text.split(
        maxsplit=1
    )

    target_date = today_local()

    if len(args) > 1:

        value = parse_date_argument(
            args[1]
        )

        if value is None:

            await message.answer(
                "Неверная дата.\n\n"
                "Пример:\n"
                "/schedule 04.09.2026"
            )

            return

        target_date = value

    await send_schedule(
        message,
        target_date
    )


# ============================================================
# /subscribe
# ============================================================

@dp.message(Command("subscribe"))
async def cmd_subscribe(
    message: Message
):

    if message.from_user is None:
        return

    subscribers = load_subscribers()

    user_id = message.from_user.id

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
        "Подписка включена.\n\n"
        "Я буду проверять расписание на завтра "
        "и отправлять уведомление, если оно изменится."
    )


# ============================================================
# /unsubscribe
# ============================================================

@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(
    message: Message
):

    if message.from_user is None:
        return

    subscribers = load_subscribers()

    user_id = message.from_user.id

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
        "Подписка отключена."
    )


# ============================================================
# /checknow
# ============================================================

@dp.message(Command("checknow"))
async def cmd_checknow(
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

        await message.answer(
            "Проверил расписание на завтра.\n\n"
            + schedule_to_text(
                schedule,
                tomorrow
            ),
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
# АВТОМАТИЧЕСКАЯ ПРОВЕРКА
# ============================================================

async def check_tomorrow_once(
    notify: bool = True
):

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

        cache = load_cache()

        old_signature = cache.get(
            date_key
        )

        # ----------------------------------------------------
        # Первый запуск.
        # Просто сохраняем текущее состояние.
        # Не отправляем уведомление всем.
        # ----------------------------------------------------

        if old_signature is None:

            cache[date_key] = signature

            save_cache(
                cache
            )

            logger.info(
                "Создан initial cache для %s",
                date_key
            )

            return

        # ----------------------------------------------------
        # Ничего не изменилось.
        # ----------------------------------------------------

        if old_signature == signature:

            logger.info(
                "Расписание %s не изменилось",
                date_key
            )

            return

        # ----------------------------------------------------
        # Расписание изменилось.
        # Сначала обновляем cache.
        # Это важно, чтобы при следующем цикле
        # не отправлять одно и то же уведомление.
        # ----------------------------------------------------

        cache[date_key] = signature

        save_cache(
            cache
        )

        logger.info(
            "РАСПИСАНИЕ ИЗМЕНИЛОСЬ: %s",
            date_key
        )

        if not notify:
            return

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

        # ----------------------------------------------------
        # Рассылаем по одному пользователю.
        # ----------------------------------------------------

        failed = []

        for user_id in subscribers:

            try:

                await bot.send_photo(
                    chat_id=user_id,
                    photo=FSInputFile(
                        image_path
                    ),
                    caption=caption
                )

                # Небольшая пауза, чтобы не долбить API.
                await asyncio.sleep(
                    0.05
                )

            except Exception as e:

                logger.warning(
                    "Не удалось отправить %s: %s",
                    user_id,
                    e
                )

                failed.append(
                    user_id
                )

        # ----------------------------------------------------
        # Не удаляем подписчика автоматически.
        # Ошибка может быть временной.
        # ----------------------------------------------------

        logger.info(
            "Уведомление отправлено. "
            "Успешных: %d, ошибок: %d",
            len(subscribers) - len(failed),
            len(failed)
        )

    except Exception:

        logger.exception(
            "Ошибка автоматической проверки"
        )


async def scheduler_loop():

    logger.info(
        "Автоматическая проверка запущена. "
        "Интервал: %d секунд",
        CHECK_INTERVAL
    )

    while True:

        try:

            await check_tomorrow_once(
                notify=True
            )

        except Exception:

            logger.exception(
                "Ошибка scheduler_loop"
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# STARTUP
# ============================================================

async def main():

    logger.info(
        "========================================"
    )

    logger.info(
        "Бот запускается"
    )

    logger.info(
        "Группа: %s",
        GROUP_NAME
    )

    logger.info(
        "URL: %s",
        SCHEDULE_URL
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
        "========================================"
    )

    # Запускаем автоматическую проверку
    # отдельной задачей.
    asyncio.create_task(
        scheduler_loop()
    )

    # Telegram polling.
    await dp.start_polling(
        bot
    )


if __name__ == "__main__":

    try:
        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Бот остановлен."
        )
