# -*- coding: utf-8 -*-
"""
Telegram-бот расписания группы ЭС7-24 (Институт нефти и газа, ishnk.ru).

- Получает расписание напрямую по HTTP (aiohttp), БЕЗ браузера.
- Разбирает HTML через BeautifulSoup (только карточки div.card.myCard с .card-header).
- Игнорирует недельную таблицу.
- Генерирует современную PNG-картинку через Pillow.
- Поддержка подписок (SQLite), фоновый мониторинг изменений.
- Работает в Docker, кодировка UTF-8.
- Все даты считаются в часовом поясе Asia/Yekaterinburg (UTC+5),
  локальное время сервера не используется.
- Версия 2.1.5 — система версий и changelog: versioning.py.
- Расписание преподавателей использует единый справочник staff_directory.py.

"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    ChatMemberUpdated,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from staff_directory import (
    STAFF_BY_ID,
    STAFF_DIRECTORY,
    STAFF_MEMBERS,
    StaffMember,
    search_staff,
)

# Публичные имена для интеграций: справочник остаётся одним объектом.
STAFF_MAPPING = STAFF_DIRECTORY
from versioning import (
    BOT_VERSION,
    CHANGELOG,
    changelog_text,
    get_released_at,
    pending_versions,
    version_key,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

load_dotenv()

# Брендинг и группа
BOT_NAME = "ИНК • Расписание"
GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "508"))
BASE_URL = os.getenv(
    "BASE_URL", "http://www.ishnk.ru/2025/site/schedule/group/508"
).rstrip("/")
STAFF_BASE_URL = os.getenv(
    "STAFF_BASE_URL", "http://www.ishnk.ru/2025/site/schedule/staff"
).rstrip("/")
# Домашняя страница колледжа содержит блок happyCard. URL можно заменить,
# не меняя scheduler или обработчики.
BIRTHDAY_URL = os.getenv(
    "BIRTHDAY_URL", "http://www.ishnk.ru/2025/site"
).rstrip("/")
BIRTHDAY_CHAT_ID_RAW = next(
    (
        os.getenv(key, "").strip()
        for key in (
            "BIRTHDAY_CHAT_ID", "BIRTHDAY_GROUP_ID",
            "TELEGRAM_GROUP_ID", "TELEGRAM_CHAT_ID",
        )
        if os.getenv(key, "").strip()
    ),
    "",
)
try:
    BIRTHDAY_CHAT_ID = int(BIRTHDAY_CHAT_ID_RAW) if BIRTHDAY_CHAT_ID_RAW else None
except ValueError:
    BIRTHDAY_CHAT_ID = None

# Токен берётся только из переменной окружения / .env, не из кода.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Rate limiter. Значения достаточно мягкие для обычного просмотра расписания,
# но защищают сайт и генератор от автоматического шквала запросов.
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "10"))
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "8"))
RATE_LIMIT_BLACKLIST_AFTER = int(os.getenv("RATE_LIMIT_BLACKLIST_AFTER", "2"))
RATE_LIMIT_WARNING_COOLDOWN = int(
    os.getenv("RATE_LIMIT_WARNING_COOLDOWN", "30")
)
SPAM_WARNING_TEXT = "⚠️ Слишком много запросов подряд.\nПожалуйста, немного подождите."

# Период автоматической проверки (секунды). 5 минут = 300
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))

# Таймаут HTTP-запроса
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))

# Часовой пояс бота — фиксированный. Нельзя использовать локальное
# время сервера, поэтому переменная TIMEZONE не берётся из окружения.
TIMEZONE = "Asia/Yekaterinburg"  # UTC+5
TZ = ZoneInfo(TIMEZONE)

# Каталоги / файлы
BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
IMAGE_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "bot.db"

# Шрифты (только из папки fonts рядом с bot.py)
FONT_REGULAR = FONTS_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONTS_DIR / "DejaVuSans-Bold.ttf"


# ============================================================
# МОДЕЛИ ДАННЫХ
# ============================================================

# Римские номера пар и их порядок
ROMAN_PAIRS = {
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


@dataclass
class Lesson:
    """Одно занятие / одна подгруппа в рамках пары.

    ``groups`` используется на странице расписания преподавателя: сайт может
    показать несколько групп в одной паре. Для группового расписания поле
    пустое и не меняет поведение версии 2.1.4.
    """

    pair: str          # римский номер, например "I"
    time: str          # "08:30 - 09:50"
    subject: str
    teacher: str
    room: str
    start: str = ""    # "08:30"
    end: str = ""      # "09:50"
    subgroup: Optional[str] = None  # "1", "2" или None (обычное занятие)
    break_duration: str = ""        # например "15 мин"
    groups: str = ""                # «ЭС7-24, БС1-23» для staff-расписания

    @property
    def key(self) -> tuple:
        """Устойчивый ключ для сопоставления старого и нового расписания."""
        return (clean_text(self.pair).upper(), clean_text(self.subgroup) or None)


@dataclass
class Pair:
    """Одна пара, которая может содержать несколько занятий/подгрупп."""

    number: str
    start: str
    end: str
    break_duration: str
    lessons: list


@dataclass
class ScheduleChange:
    """Одно изменение расписания (добавление, удаление или изменение)."""

    kind: str               # "added", "removed", "changed"
    pair: str
    subgroup: Optional[str]
    old: Optional[dict]
    new: Optional[dict]
    details: list           # для changed: [{"field","label","old","new"}]

    @property
    def key(self) -> tuple:
        return (clean_text(self.pair).upper(), clean_text(self.subgroup) or None)


@dataclass
class Schedule:
    """Расписание на конкретный день.

    `lessons` остаётся плоским списком всех занятий/подгрупп (это удобно
    для подписи/хэша и совместимости), а `pairs` собирает их в пары.
    ``schedule_type`` различает основную группу и преподавателя, поэтому
    callback-и, состояние и рендер не смешивают эти два вида расписания.
    """

    date: date
    group: str
    lessons: list
    fallback: bool = False
    schedule_type: str = "group"
    staff_id: Optional[int] = None
    staff_name: str = ""

    @property
    def pairs(self) -> list:
        """Группировка flat-списка занятий в пары."""
        return group_into_pairs(self.lessons)


class ScheduleUnavailable(Exception):
    """Сайт недоступен / сеть не работает."""


def lesson_slot_key(lesson) -> tuple:
    """Идентификатор пары / временного слота.

    Все подгруппы одной пары дают ОДИН и тот же ключ, поэтому он подходит
    и для группировки в карточки, и для подсчёта количества занятий.
    Основной идентификатор — номер пары; если его нет, используется
    время начала и окончания.
    """
    if isinstance(lesson, dict):
        pair = clean_text(lesson.get("pair", ""))
        start = clean_text(lesson.get("start", ""))
        end = clean_text(lesson.get("end", ""))
        time_str = clean_text(lesson.get("time", ""))
    else:
        pair = clean_text(getattr(lesson, "pair", ""))
        start = clean_text(getattr(lesson, "start", ""))
        end = clean_text(getattr(lesson, "end", ""))
        time_str = clean_text(getattr(lesson, "time", ""))

    if (not start or not end) and time_str:
        match = re.search(
            r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})", time_str
        )
        if match:
            start = start or match.group(1)
            end = end or match.group(2)

    return (pair.upper(), start, end)


def count_lessons(source) -> int:
    """Количество занятий = количество уникальных пар / временных слотов.

    Принимает `Schedule`, список `Lesson` или список словарей
    (нормализованное расписание из БД).

    Пара с несколькими подгруппами — это ОДНО занятие:
    количество отображаемых блоков (подгрупп) может быть больше,
    чем количество занятий.
    """
    lessons = getattr(source, "lessons", source) or []
    return len({lesson_slot_key(item) for item in lessons})


def group_into_pairs(lessons: list) -> list:
    """Группирует flat-список занятий в пары.

    Порядок пар сохраняет порядок первого появления / порядок по номеру.
    Внутри пары занятия сортируются по подгруппе (None идёт первым).
    """
    groups = OrderedDict()
    for lesson in lessons:
        key = lesson_slot_key(lesson)
        groups.setdefault(key, []).append(lesson)

    result = []
    for key, group in groups.items():
        number, start, end = key
        group.sort(key=lambda item: _subgroup_sort_key(item.subgroup))
        result.append(
            Pair(
                number=number,
                start=start or (group[0].start or ""),
                end=end or (group[0].end or ""),
                break_duration=getattr(group[0], "break_duration", "") or "",
                lessons=list(group),
            )
        )

    # Сортировка по римскому номеру пары.
    result.sort(key=lambda pair: ROMAN_PAIRS.get(pair.number, 99))
    return result


def _subgroup_sort_key(subgroup) -> tuple:
    if subgroup is None or subgroup == "":
        return (0, "", "")
    try:
        number = int(subgroup)
        return (1, f"{number:010d}", "")
    except (TypeError, ValueError):
        return (1, "", clean_text(subgroup))


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("schedule_bot")


def _log_time_converter(timestamp: float, *_args):
    """Время в логах тоже показываем по Asia/Yekaterinburg, а не по серверу."""
    return now_local().timetuple()


logging.Formatter.converter = _log_time_converter


# ============================================================
# КАТАЛОГИ / ИНИЦИАЛИЗАЦИЯ
# ============================================================

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# ДАТА (Asia/Yekaterinburg, UTC+5)
# ============================================================

WEEKDAYS = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]

MONTHS_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def now_local() -> datetime:
    """Единственная точка получения текущего времени.

    Всегда Asia/Yekaterinburg (UTC+5). Локальное время сервера
    (datetime.now() без часового пояса) в логике не используется.
    """
    return datetime.now(TZ)


def get_today() -> date:
    """Сегодняшняя дата по Asia/Yekaterinburg. Пересчитывается каждый вызов."""
    return now_local().date()


def is_day_off(value: date) -> bool:
    """Воскресенье — выходной, расписания в этот день не бывает."""
    return value.weekday() == 6


def get_tomorrow() -> date:
    """Строго следующий календарный день по Asia/Yekaterinburg.

    Без «пропуска воскресенья» и без подстановки другой даты:
    /schedule должен показывать ровно завтра.
    """
    return get_today() + timedelta(days=1)


def parse_db_datetime(value: str) -> Optional[datetime]:
    """Разбирает дату/время из БД как время Asia/Yekaterinburg."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def day_label_for(day: date) -> str:
    """«Сегодня» / «Завтра» для однозначных уведомлений."""
    today = get_today()
    if day == today:
        return "Сегодня"
    if day == today + timedelta(days=1):
        return "Завтра"
    return format_date_header(day)


MONTH_NAMES = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "май": 5, "мая": 5, "мае": 5,
    "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

# Поддерживаемые числовые форматы даты.
_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m",
    "%d/%m",
)


def parse_user_date(raw: str):
    """Разбирает дату, введённую пользователем. Возвращает date или None.

    Понимает: 2026-09-04, 04.09.2026, 4.9, «сегодня», «завтра»,
    «вчера», «4 сентября», «4 сентября 2026».
    """
    text = clean_text(raw).lower().replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return None

    today = get_today()

    # Ключевые слова.
    if text in ("сегодня", "today"):
        return today
    if text in ("завтра", "tomorrow"):
        return get_tomorrow()
    if text in ("послезавтра",):
        return today + timedelta(days=2)
    if text in ("вчера", "yesterday"):
        return today - timedelta(days=1)

    # День недели: «понедельник» -> ближайший такой день (включая сегодня).
    for index, name in enumerate(WEEKDAYS):
        if text == name.lower():
            delta = (index - today.weekday()) % 7
            return today + timedelta(days=delta)

    # Двухзначный год: 04.09.26 -> 2026 (однозначное правило,
    # без угадывания века; не полагаемся на платформенный %y).
    match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2})", text)
    if match:
        try:
            return date(
                2000 + int(match.group(3)),
                int(match.group(2)),
                int(match.group(1)),
            )
        except ValueError:
            return None

    # Числовые форматы.
    for pattern in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if "%Y" not in pattern:
            parsed = parsed.replace(year=today.year)
        return parsed.date()

    # Текстовый месяц: «4 сентября», «4 сентября 2026»,
    # «4 сентября 2026 года», «4 сентября 2026г», «4 сентябрь»,
    # «4 сентября 26».
    match = re.fullmatch(
        r"(\d{1,2})\s+([а-яё]+)"
        r"(?:\s+(\d{2,4})\s*(?:год[а-яё]*|г\.?)?)?",
        text,
    )
    if match:
        day_num = int(match.group(1))
        month_word = match.group(2)
        raw_year = match.group(3)

        if raw_year:
            year = int(raw_year)
            if len(raw_year) == 2:
                # Однозначное правило: 26 -> 2026.
                year = 2000 + year
        else:
            year = today.year

        month = None
        for stem, number in MONTH_NAMES.items():
            if month_word.startswith(stem):
                month = number
                break

        if month is not None:
            try:
                return date(year, month, day_num)
            except ValueError:
                return None

    return None


# Единое имя для компонентов, которым не важно, откуда пришла дата.
parse_date = parse_user_date


# ============================================================
# ТЕКСТОВАЯ КОМАНДА «РАСПИСАНИЕ»
# ============================================================

@dataclass
class ScheduleTextRequest:
    """Единый результат разбора естественного запроса расписания.

    ``schedule_type`` равен ``group`` или ``staff``. Дата всегда проходит
    через один и тот же :func:`parse_user_date`; отдельного date parser для
    преподавателей нет.
    """

    matched: bool = False
    date: Optional[date] = None
    error: bool = False
    schedule_type: str = "group"
    staff_query: str = ""


_SCHEDULE_TEXT_RE = re.compile(r"^расписание(?:\s+(.*))?$", re.IGNORECASE)


def _extract_staff_date_and_query(raw: str) -> tuple[str, Optional[date], bool]:
    """Возвращает (запрос преподавателя, дата, ошибка даты).

    Суффикс ``на <дата>`` отделяется только если весь суффикс является
    корректной датой. Это не создаёт второго парсера и не ломает фамилии.
    """
    rest = clean_text(raw)
    if not rest:
        return "", None, False

    if rest.casefold().endswith(" на"):
        return clean_text(rest[:-2]), None, True

    if rest.casefold().startswith("на "):
        date_raw = clean_text(rest[3:])
        if not date_raw:
            return "", None, True
        parsed = parse_user_date(date_raw)
        return "", parsed, parsed is None

    # Берём последнее « на »: имя и фамилия остаются запросом, а дата
    # распознаётся тем же parse_user_date, что и для основной группы.
    marker = re.search(r"\s+на\s+(.+)$", rest, flags=re.IGNORECASE)
    if marker:
        date_raw = clean_text(marker.group(1))
        parsed = parse_user_date(date_raw)
        if parsed is not None:
            query = clean_text(rest[: marker.start()])
            return query, parsed, False
        # Похожий на дату суффикс нельзя молча считать частью ФИО.
        return clean_text(rest[: marker.start()]), None, True

    return rest, None, False


def parse_schedule_text(text: str) -> ScheduleTextRequest:
    """Разбирает групповые и преподавательские запросы.

    Поддерживаются, в частности:
    ``расписание`` -> завтра;
    ``расписание на сегодня`` -> сегодня;
    ``расписание преподавателя Аглиуллиной на 9 сентября``;
    ``расписание Аглиуллиной`` -> расписание преподавателя на завтра.
    """
    if not text:
        return ScheduleTextRequest()

    normalized = clean_text(text).lower()
    match = _SCHEDULE_TEXT_RE.fullmatch(normalized)
    if not match:
        return ScheduleTextRequest()

    rest = clean_text(match.group(1) or "")
    if not rest:
        return ScheduleTextRequest(matched=True)

    # Групповой запрос имеет единственный допустимый префикс «на».
    if rest.casefold().startswith("на") and (
        rest.casefold() == "на" or rest[2:3].isspace()
    ):
        arg = clean_text(rest[2:])
        if not arg:
            return ScheduleTextRequest(matched=True, error=True)
        target = parse_user_date(arg)
        return ScheduleTextRequest(
            matched=True, date=target, error=target is None
        )

    # «расписание преподавателя …» и короткая форма
    # «расписание Аглиуллиной» — один и тот же маршрут.
    if rest.casefold() == "преподавателя":
        return ScheduleTextRequest(
            matched=True, error=True, schedule_type="staff"
        )
    if rest.casefold().startswith("преподавателя "):
        staff_raw = clean_text(rest[len("преподавателя "):])
    else:
        staff_raw = rest

    staff_query, target, date_error = _extract_staff_date_and_query(staff_raw)
    if not staff_query:
        return ScheduleTextRequest(
            matched=True,
            date=target,
            error=True if date_error or target is None else False,
            schedule_type="staff",
            staff_query="",
        )

    return ScheduleTextRequest(
        matched=True,
        date=target,
        error=date_error,
        schedule_type="staff",
        staff_query=staff_query,
    )


# Явное имя удобно для интеграционных тестов и не создаёт отдельной логики.
parse_staff_schedule_text = parse_schedule_text


def format_date_full(value: date) -> str:
    """4 сентября 2026"""
    return f"{value.day} {MONTHS_GEN[value.month]} {value.year}"


def format_date_header(value: date) -> str:
    """Пятница, 4 сентября"""
    return f"{WEEKDAYS[value.weekday()]}, {value.day} {MONTHS_GEN[value.month]}"


# ============================================================
# HTTP (только HTTP, без перехода по редиректам)
# ============================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def build_url(day: date) -> str:
    return f"{BASE_URL}/{day.isoformat()}"


def build_staff_url(staff_id: int, day: date) -> str:
    """Строит URL только для ID из STAFF_DIRECTORY."""
    if int(staff_id) not in STAFF_BY_ID:
        raise ValueError(f"Неизвестный STAFF_ID: {staff_id}")
    return f"{STAFF_BASE_URL}/{int(staff_id)}/{day.isoformat()}"


async def fetch_url(url: str, label: str = "страницы колледжа"):
    """Получает HTML без перехода по редиректам.

    Один низкоуровневый HTTP-компонент используется группой, staff-страницей
    и скрытой ежедневной проверкой страницы колледжа.
    """
    logger.info("Получение %s: %s", label, url)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    try:
        async with aiohttp.ClientSession(
            headers=HEADERS,
            timeout=timeout,
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                status = response.status
                logger.info("HTTP статус (%s): %s", label, status)
                if status in (300, 301, 302, 303, 307, 308):
                    logger.error(
                        "HTTP редирект %s на «%s» — не переходим.",
                        status,
                        response.headers.get("Location", ""),
                    )
                    return None
                if status != 200:
                    logger.error("HTTP ошибка (%s): %s", label, status)
                    return None
                raw = await response.read()
                if not raw:
                    logger.error("Пустой HTML (%s)", label)
                    return None
                return raw.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        logger.error("Таймаут при получении %s: %s", label, url)
        return None
    except aiohttp.ClientError as error:
        logger.error("HTTP ошибка при получении %s: %s", label, error)
        return None
    except Exception:
        logger.exception("Не удалось получить %s", label)
        return None


async def fetch_html(day: date):
    return await fetch_url(
        build_url(day), f"расписания группы на {day.isoformat()}"
    )


async def fetch_staff_html(staff_id: int, day: date):
    return await fetch_url(
        build_staff_url(staff_id, day),
        f"расписания преподавателя {staff_id} на {day.isoformat()}",
    )


async def fetch_birthday_html():
    return await fetch_url(BIRTHDAY_URL, "страницы college")


# ============================================================
# ТЕКСТОВЫЕ ХЕЛПЕРЫ
# ============================================================

def clean_text(value) -> str:
    """Убирает NBSP и лишние пробелы."""
    if not value:
        return ""
    text = str(value).replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Загружает шрифт ТОЛЬКО из папки fonts рядом с bot.py."""
    path = FONT_BOLD if bold else FONT_REGULAR

    if not path.exists():
        raise RuntimeError(
            "Шрифт не найден. Проверь папку fonts."
        )

    return ImageFont.truetype(str(path), size)


# ============================================================
# ПАРСИНГ HTML
# ============================================================

_SUBGROUP_CLASS_RE = re.compile(r"\bsubGroup\d+\b", re.IGNORECASE)
_SUBGROUP_TEXT_RE = re.compile(r"(\d{1,2})\s*п/гр\.?", re.IGNORECASE)


def _find_room(node) -> str:
    """
    Аудитория в элементе с текстом «ауд.»:

        <span>ауд.<span class="h5">УК107</span></span>

    Возвращает только номер (УК107). Если нет — "".
    """
    # Способ 1: элемент .h5 рядом с текстом «ауд.»
    for text_node in node.find_all(string=True):
        if text_node and ("ауд." in text_node.lower() or "аудитори" in text_node.lower()):
            parent = text_node.parent
            if parent is None:
                continue

            h5 = parent.find(class_="h5") or parent.select_one("h5")
            if h5 is not None:
                value = clean_text(h5.get_text(" ", strip=True))
                if value:
                    return value

    # Способ 2: regex по очищенному тексту карточки
    text = clean_text(node.get_text(" ", strip=True))
    match = re.search(
        r"(?:ауд\.|аудитория)\s*([A-Za-zА-Яа-я0-9№.\-()/]+)",
        text,
        re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1))

    return ""


def _find_subject(node) -> str:
    """Предмет из карточки / блока подгруппы."""
    selectors = (
        ".d-md-none.text-center.text-truncate",
        ".d-none.d-md-block b",
        ".d-none.d-md-block",
        "b",
        "strong",
    )
    for selector in selectors:
        el = node.select_one(selector)
        if el is not None:
            value = clean_text(el.get_text(" ", strip=True))
            if value:
                return value

    # Иногда предмет может быть выделен классом, но не ловится выше.
    for el in node.select(".subject, .discipline, [class*=subject], [class*=Subject]"):
        value = clean_text(el.get_text(" ", strip=True))
        if value and "ауд." not in value.lower():
            return value

    return ""


def _find_teacher(node) -> str:
    """Преподаватель из видимого текста / title."""
    staff = node.select_one(".Staff")
    if staff is not None:
        teacher = clean_text(staff.get_text(" ", strip=True))
        if not teacher and staff.get("title"):
            teacher = clean_text(staff.get("title"))
        return teacher

    for el in node.select(
        ".teacher, [class*=teacher], [class*=Teacher], .staff, [class*=staff]"
    ):
        value = clean_text(el.get_text(" ", strip=True))
        if value and "ауд." not in value.lower():
            return value
        if el.get("title"):
            value = clean_text(el.get("title"))
            if value:
                return value

    return ""


_GROUP_TOKEN_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9])([A-Za-zА-Яа-яЁё]{1,8}\s*\d{1,3}\s*[-–—]\s*\d{1,3})(?![A-Za-zА-Яа-яЁё0-9])",
    re.IGNORECASE,
)


def _find_groups(node) -> str:
    """Группы на странице преподавателя, без вывода ID из HTML."""
    values = []
    text = clean_text(node.get_text(" ", strip=True))
    for match in _GROUP_TOKEN_RE.finditer(text):
        value = re.sub(r"\s*[-–—]\s*", "-", clean_text(match.group(1)))
        value = re.sub(r"\s+", "", value)
        if value.casefold() not in {item.casefold() for item in values}:
            values.append(value)

    # Некоторые варианты страницы помещают группу только в title/aria-label.
    for attr in ("title", "data-group", "aria-label"):
        for element in node.select(f"[{attr}]"):
            raw = clean_text(element.get(attr, ""))
            for match in _GROUP_TOKEN_RE.finditer(raw):
                value = re.sub(r"\s*[-–—]\s*", "-", clean_text(match.group(1)))
                value = re.sub(r"\s+", "", value)
                if value.casefold() not in {item.casefold() for item in values}:
                    values.append(value)

    return ", ".join(values)


def _find_break_duration(header) -> str:
    """«перемена 15 мин» из заголовка пары."""
    for node in header.select("span"):
        text = clean_text(node.get_text(" ", strip=True))
        match = re.search(
            r"перемена\s+(\d+)\s*(мин|минуты|минут)?",
            text,
            re.IGNORECASE,
        )
        if match:
            minutes = match.group(1)
            unit = clean_text(match.group(2) or "мин")
            return f"{minutes} {unit}"
    return ""


def _find_subgroup(node, css_class: str = "") -> Optional[str]:
    """Номер подгруппы.

    Приоритет — текст «N п/гр.» (самый надёжный источник). Если его нет,
    берём номер из CSS-класса `.subGroupN`. Если нет ни того, ни другого,
    возвращаем None — подгруппу НЕ придумываем.
    """
    text = clean_text(node.get_text(" ", strip=True))
    match = _SUBGROUP_TEXT_RE.search(text)
    if match:
        return match.group(1)

    class_match = _SUBGROUP_CLASS_RE.search(css_class or "")
    if class_match:
        return re.search(r"\d+", class_match.group(0)).group(0)

    return None


def _extract_fallback_subject(node, room: str, teacher: str, subgroup) -> str:
    """Fallback для предмета, если в блоке нет стандартных классов.

    Аккуратно убирает известные служебные части (аудитория, преподаватель,
    «N п/гр.», «ауд.») и оставляет то, что похоже на предмет.
    """
    text = clean_text(node.get_text(" ", strip=True))
    if not text:
        return ""

    if subgroup:
        text = re.sub(
            rf"\b{re.escape(subgroup)}\s*п/гр\.?",
            " ",
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(r"\bп/гр\.?\b", " ", text, flags=re.IGNORECASE)
    if room:
        text = text.replace(room, " ")
    if teacher:
        text = text.replace(teacher, " ")
    text = re.sub(r"ауд\.", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(Перемена[а-яё]*|[1-5]\s*пар[а-яё]*|Пара\s*[IVX]+)", " ", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _looks_like_teacher(value: str) -> bool:
    """«Мурзабулатова Ф.Ф.», «Иванов И.И.» — эвристика для plain-text HTML."""
    text = clean_text(value)
    if not text or len(text) < 4:
        return False
    if re.fullmatch(
        r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s*[А-ЯЁ]\.\s*[А-ЯЁ]\.?",
        text,
    ):
        return True
    return False


def _text_fragments(node, room: str, subgroup) -> list:
    """Строки блока без аудитории / подгруппы (запасной источник данных)."""
    fragments = []
    for raw in node.get_text("\n", strip=True).split("\n"):
        line = clean_text(raw)
        if not line:
            continue
        lowered = line.lower()
        if "ауд." in lowered:
            continue
        if subgroup and re.fullmatch(
            rf"{re.escape(subgroup)}\s*п/гр\.?", line, re.IGNORECASE
        ):
            continue
        if room and room.lower() in lowered:
            continue
        fragments.append(line)
    return fragments


def _parse_lesson_from_node(
    node,
    pair: str,
    start: str,
    end: str,
    time_str: str,
    break_duration: str,
    css_class: str = "",
) -> Optional[Lesson]:
    subject = _find_subject(node)
    teacher = _find_teacher(node)
    room = _find_room(node)
    subgroup = _find_subgroup(node, css_class=css_class)
    groups = _find_groups(node)

    # Запасная эвристика для plain-text HTML без классов .Staff/.d-md-none:
    # преподавателя ищем по паттерну «Фамилия И.О.».
    if not teacher:
        fragments = _text_fragments(node, room, subgroup)
        for fragment in fragments:
            if _looks_like_teacher(fragment):
                teacher = clean_text(fragment)
                break

    if not subject:
        subject = _extract_fallback_subject(node, room, teacher, subgroup)

    if not subject and not teacher and not room:
        return None

    return Lesson(
        pair=pair,
        time=time_str,
        start=start,
        end=end,
        subject=subject or "Предмет не указан",
        teacher=teacher or "—",
        room=room or "—",
        subgroup=subgroup,
        break_duration=break_duration,
        groups=groups,
    )


def _iter_subgroup_blocks(body):
    """Все блоки подгрупп внутри card-body.

    Классы могут быть `.subGroup1`, `.subGroup2`, `.subGroup3` и т.д.
    Архитектура не ограничена двумя подгруппами.
    """
    candidates = []
    for el in body.find_all(class_=_SUBGROUP_CLASS_RE):
        # Исключаем вложенные элементы, если родитель тоже подгруппа.
        parent = el.parent
        if parent is not None and parent.get("class"):
            classes = " ".join(str(c) for c in parent.get("class"))
            if _SUBGROUP_CLASS_RE.search(classes):
                continue
        candidates.append(el)

    return candidates


def parse_schedule(
    html: str,
    day: date,
    group: Optional[str] = None,
    *,
    schedule_type: str = "group",
    staff_id: Optional[int] = None,
    staff_name: str = "",
) -> Schedule:
    """Общий parser карточек группы и staff-страницы.

    Структура источника одна: карточки ``myCard`` и их пары. Для staff
    передаются только метаданные из локального справочника, а не ID,
    найденный в HTML.
    """
    soup = BeautifulSoup(html, "html.parser")

    # В обычной странице это div.card.myCard. Некоторые версии staff-
    # страницы теряют класс myCard, но сохраняют card-header; запасной
    # селектор не меняет фильтр по обязательным элементам шапки.
    cards = soup.select("div.card.myCard") or soup.select("div.card")

    lessons: list = []

    for card in cards:
        header = card.select_one(".card-header")

        # Без .card-header — карточка игнорируется (защита от посторонних блоков).
        if not header:
            continue

        # Номер пары — в .card-header .h3 (римская цифра)
        pair_node = header.select_one(".h3")
        # Время — в .card-header .h4
        time_node = header.select_one(".h4")

        # Если этих элементов нет — карточка игнорируется.
        if not pair_node or not time_node:
            continue

        pair_text = clean_text(pair_node.get_text(" ", strip=True)).upper()
        pair = None
        for roman, _order in ROMAN_PAIRS.items():
            if re.search(rf"(^|\s){roman}(\s|$)", pair_text) or pair_text == roman:
                pair = roman
                break

        if not pair:
            continue

        # Время. В HTML цифры могут быть обёрнуты в <sup> (08<sup>30</sup>),
        # поэтому убираем пробелы и уже потом ищем «0830-0950» -> «08:30 - 09:50».
        time_raw = clean_text(time_node.get_text())
        time_compact = re.sub(r"\s+", "", time_raw)
        time_match = re.search(
            r"(\d{2})(\d{2})[-–—:.](\d{2})(\d{2})",
            time_compact,
        )
        if not time_match:
            continue

        h1, m1, h2, m2 = time_match.groups()

        if not (0 <= int(h1) <= 23 and 0 <= int(h2) <= 23
                and 0 <= int(m1) <= 59 and 0 <= int(m2) <= 59):
            continue

        start = f"{h1}:{m1}"
        end = f"{h2}:{m2}"
        time_str = f"{start} - {end}"
        break_duration = _find_break_duration(header)

        body = card.select_one(".card-body") or card
        subgroup_blocks = _iter_subgroup_blocks(body)

        # Если в паре есть подгруппы — каждая из них становится своим
        # занятием. Ни в коем случае не оставляем только первую.
        if subgroup_blocks:
            for block in subgroup_blocks:
                css_classes = " ".join(
                    str(c) for c in (block.get("class") or [])
                )
                lesson = _parse_lesson_from_node(
                    block,
                    pair=pair,
                    start=start,
                    end=end,
                    time_str=time_str,
                    break_duration=break_duration,
                    css_class=css_classes,
                )
                if lesson is not None:
                    lessons.append(lesson)
        else:
            lesson = _parse_lesson_from_node(
                body,
                pair=pair,
                start=start,
                end=end,
                time_str=time_str,
                break_duration=break_duration,
            )
            if lesson is not None:
                lessons.append(lesson)

    logger.info("Найдено подходящих карточек: %s", len(cards))

    # Убираем дубликаты и сортируем по номеру пары и подгруппе.
    unique: list = []
    seen = set()

    for lesson in lessons:
        key = (
            lesson.pair,
            lesson.time,
            lesson.subject,
            lesson.teacher,
            lesson.room,
            lesson.subgroup,
            lesson.groups,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(lesson)

    unique.sort(
        key=lambda x: (
            ROMAN_PAIRS.get(x.pair, 99),
            _subgroup_sort_key(x.subgroup),
            clean_text(x.subject).casefold(),
        )
    )

    logger.info(
        "Найдено занятий (пар): %s, записей (с подгруппами): %s",
        count_lessons(unique),
        len(unique),
    )

    for lesson in unique:
        subgroup_s = f" | {lesson.subgroup} п/гр." if lesson.subgroup else ""
        logger.info(
            "Пара %s%s | %s | %s | %s | %s",
            lesson.pair,
            subgroup_s,
            lesson.time,
            lesson.room,
            lesson.teacher,
            lesson.subject,
        )

    effective_name = staff_name or (group if schedule_type == "staff" else "")
    return Schedule(
        date=day,
        group=group or GROUP_NAME,
        lessons=unique,
        schedule_type=schedule_type,
        staff_id=staff_id,
        staff_name=effective_name,
    )


async def get_schedule(day: date) -> Schedule:
    """Единая функция получения расписания ровно на переданную дату.

    ВАЖНО: без скрытого fallback. Если расписания на `day` нет —
    возвращается пустое расписание (lessons == []), а не данные
    другой даты. При ошибке сети/источника бросается ScheduleUnavailable.
    """
    html = await fetch_html(day)

    if html is None:
        raise ScheduleUnavailable(f"Сайт недоступен для {day.isoformat()}")

    try:
        return parse_schedule(html, day)
    except Exception:
        logger.exception("Ошибка парсинга HTML")
        raise ScheduleUnavailable("Ошибка парсинга расписания")


async def get_staff_schedule(staff_id: int, day: date) -> Schedule:
    """Получает расписание преподавателя по разрешённому STAFF_ID."""
    member = STAFF_BY_ID.get(int(staff_id))
    if member is None:
        raise ScheduleUnavailable("Неизвестный преподаватель")
    html = await fetch_staff_html(member.staff_id, day)
    if html is None:
        raise ScheduleUnavailable(
            f"Сайт недоступен для преподавателя {member.staff_id}"
        )
    try:
        return parse_schedule(
            html,
            day,
            group=member.full_name,
            schedule_type="staff",
            staff_id=member.staff_id,
            staff_name=member.full_name,
        )
    except Exception:
        logger.exception("Ошибка парсинга staff HTML")
        raise ScheduleUnavailable("Ошибка парсинга расписания преподавателя")


fetch_staff_schedule = get_staff_schedule


# ============================================================
# НОРМАЛИЗАЦИЯ, ХЭШ И СРАВНЕНИЕ РАСПИСАНИЙ
# ============================================================

LESSON_FIELDS = (
    "pair", "start", "end", "subgroup", "subject", "room", "teacher", "groups"
)
FIELD_LABELS = {
    "subject": "Предмет",
    "room": "Аудитория",
    "teacher": "Преподаватель",
    "groups": "Группы",
    "time": "Время",
}


def _split_time(lesson) -> tuple:
    start = clean_text(getattr(lesson, "start", ""))
    end = clean_text(getattr(lesson, "end", ""))
    if not start or not end:
        match = re.search(
            r"(\d{2}:\d{2})\s*[-–—]\s*(\d{2}:\d{2})",
            clean_text(getattr(lesson, "time", "")),
        )
        if match:
            start = start or match.group(1)
            end = end or match.group(2)
    return start, end


def normalize_value(value) -> str:
    if value is None:
        return ""
    return clean_text(str(value))


def normalize_schedule(schedule) -> list:
    """Нормализованный список занятий для сравнения/хранения.

    Убирает лишние пробелы и неоднозначное форматирование. Сравнение
    дополнительно использует кейс-независимые ключи (см. _field_key).
    """
    items = []
    for lesson in schedule.lessons:
        start, end = _split_time(lesson)
        subgroup = normalize_value(lesson.subgroup) or None
        pair = normalize_value(lesson.pair).upper()
        item = {
            "pair": pair,
            "start": start,
            "end": end,
            "subgroup": subgroup,
            "subject": normalize_value(lesson.subject),
            "room": normalize_value(lesson.room),
            "teacher": normalize_value(lesson.teacher),
        }
        groups = normalize_value(getattr(lesson, "groups", ""))
        if groups or getattr(schedule, "schedule_type", "group") == "staff":
            item["groups"] = groups
        items.append(item)

    items.sort(
        key=lambda x: (
            ROMAN_PAIRS.get(x["pair"], 99),
            _subgroup_sort_key(x["subgroup"]),
            x["subject"].casefold(),
        )
    )
    return items


def _field_key(value: str) -> str:
    return normalize_value(value).casefold().strip()


def schedule_from_storage(stored, day: date) -> Schedule:
    """Восстанавливает Schedule из JSON в БД (значения display-нормализованы)."""
    if stored is None:
        return Schedule(date=day, group=GROUP_NAME, lessons=[])
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except (ValueError, TypeError):
            return Schedule(date=day, group=GROUP_NAME, lessons=[])
    if isinstance(stored, dict):
        stored = stored.get("lessons") or []

    lessons = []
    for item in stored or []:
        if not isinstance(item, dict):
            continue
        pair = clean_text(item.get("pair", "")).upper()
        subgroup = clean_text(item.get("subgroup") or "") or None
        start = clean_text(item.get("start", ""))
        end = clean_text(item.get("end", ""))
        subject = clean_text(item.get("subject", "")) or "Предмет не указан"
        teacher = clean_text(item.get("teacher", "")) or "—"
        room = clean_text(item.get("room", "")) or "—"
        groups = clean_text(item.get("groups", ""))
        lessons.append(
            Lesson(
                pair=pair,
                time=f"{start} - {end}" if start and end else "",
                subject=subject,
                teacher=teacher,
                room=room,
                start=start,
                end=end,
                subgroup=subgroup,
                groups=groups,
            )
        )
    return Schedule(date=day, group=GROUP_NAME, lessons=lessons)


def schedule_signature(schedule: Schedule) -> str:
    """
    Стабильная подпись, зависящая от: даты, пары, времени, подгруппы,
    предмета, преподавателя и аудитории.

    Пробелы и регистр не влияют на подпись — это защищает от ложных
    изменений при косметических правках на сайте.
    """
    parts = [schedule.date.isoformat(), normalize_value(schedule.group)]
    for item in normalize_schedule(schedule):
        parts.extend(
            [
                item["pair"],
                item["start"],
                item["end"],
                item["subgroup"] or "",
                _field_key(item["subject"]),
                _field_key(item["room"]),
                _field_key(item["teacher"]),
            ]
        )
        # Пустое поле staff-групп не меняет hash основной группы 2.1.4;
        # непустые группы учитываются на staff-страницах.
        if item.get("groups"):
            parts.append(_field_key(item["groups"]))

    data = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def compare_schedules(old_schedule, new_schedule) -> list:
    """Возвращает список ScheduleChange.

    Сопоставление идёт по устойчивому ключу «пара + подгруппа»:
    - совпал ключ  -> сравниваются предмет/аудитория/преподаватель/время;
    - появился ключ -> добавлено занятие/подгруппа;
    - пропал ключ   -> удалено занятие/подгруппа.
    """
    old_items = normalize_schedule(old_schedule)
    new_items = normalize_schedule(new_schedule)

    old_by_key = {}
    for item in old_items:
        old_by_key.setdefault((item["pair"], item["subgroup"]), item)
    new_by_key = {}
    for item in new_items:
        new_by_key.setdefault((item["pair"], item["subgroup"]), item)

    changes = []
    all_keys = sorted(set(old_by_key) | set(new_by_key))

    for key in all_keys:
        pair, subgroup = key
        old_item = old_by_key.get(key)
        new_item = new_by_key.get(key)

        if old_item is not None and new_item is None:
            changes.append(
                ScheduleChange(
                    kind="removed",
                    pair=pair,
                    subgroup=subgroup,
                    old=old_item,
                    new=None,
                    details=[],
                )
            )
            continue

        if old_item is None and new_item is not None:
            changes.append(
                ScheduleChange(
                    kind="added",
                    pair=pair,
                    subgroup=subgroup,
                    old=None,
                    new=new_item,
                    details=[],
                )
            )
            continue

        details = []
        for key_name, label in (
            ("subject", FIELD_LABELS["subject"]),
            ("room", FIELD_LABELS["room"]),
            ("teacher", FIELD_LABELS["teacher"]),
            ("groups", FIELD_LABELS["groups"]),
        ):
            old_val = normalize_value(old_item.get(key_name))
            new_val = normalize_value(new_item.get(key_name))
            if old_val != new_val and _field_key(old_val) != _field_key(new_val):
                details.append(
                    {
                        "field": key_name,
                        "label": label,
                        "old": old_val,
                        "new": new_val,
                    }
                )

        old_start = normalize_value(old_item.get("start"))
        old_end = normalize_value(old_item.get("end"))
        new_start = normalize_value(new_item.get("start"))
        new_end = normalize_value(new_item.get("end"))
        if (old_start, old_end) != (new_start, new_end):
            details.append(
                {
                    "field": "time",
                    "label": FIELD_LABELS["time"],
                    "old": f"{old_start} - {old_end}" if old_start or old_end else "",
                    "new": f"{new_start} - {new_end}" if new_start or new_end else "",
                }
            )

        if details:
            changes.append(
                ScheduleChange(
                    kind="changed",
                    pair=pair,
                    subgroup=subgroup,
                    old=old_item,
                    new=new_item,
                    details=details,
                )
            )

    changes.sort(key=_schedule_change_sort_key)
    return changes


def _schedule_change_sort_key(change):
    pair = clean_text(change.pair).upper()
    return (
        ROMAN_PAIRS.get(pair, 99),
        _subgroup_sort_key(change.subgroup),
        clean_text(change.kind),
    )


# ============================================================
# SQLite (подписчики + состояние расписания)
# ============================================================

def db_connect() -> sqlite3.Connection:
    """Открывает короткое SQLite-соединение с безопасными настройками.

    Включён WAL и busy timeout: middleware и фоновые задачи могут обратиться
    к БД параллельно, не теряя атомарные решения rate limiter/blacklist.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    user_id    INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )

            # Миграция: поддержка групповых чатов.
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(subscribers)")
            }
            if "chat_type" not in existing:
                conn.execute(
                    "ALTER TABLE subscribers ADD COLUMN chat_type TEXT"
                    " NOT NULL DEFAULT 'private'"
                )
            if "title" not in existing:
                conn.execute(
                    "ALTER TABLE subscribers ADD COLUMN title TEXT"
                    " NOT NULL DEFAULT ''"
                )

            # 2.1.4: последняя версия changelog, успешно доставленная.
            if "last_notified_version" not in existing:
                conn.execute(
                    "ALTER TABLE subscribers ADD COLUMN"
                    " last_notified_version TEXT NOT NULL DEFAULT ''"
                )

            # Безопасная миграция created_at: если дату создания восстановить
            # нельзя, считаем пользователя существовавшим до любых релизов —
            # тогда старые пользователи получат changelog 2.1.4,
            # а новые (созданные после релиза) его не получат.
            conn.execute(
                "UPDATE subscribers SET created_at = '1970-01-01 00:00:00'"
                " WHERE created_at IS NULL OR created_at = ''"
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_state (
                    date       TEXT PRIMARY KEY,
                    hash       TEXT NOT NULL,
                    data       TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )

            # 2.1.4: в состоянии храним не только hash, но и нормализованные
            # данные — без них невозможно показать «было -> стало».
            state_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(schedule_state)")
            }
            if "data" not in state_columns:
                conn.execute(
                    "ALTER TABLE schedule_state ADD COLUMN data TEXT"
                    " NOT NULL DEFAULT ''"
                )

            # 2.1.4: фактическая доставка расписания каждому подписчику
            # (комбинация «подписчик + дата»), чтобы не отправлять
            # повторно то же самое и не терять изменения при сбоях.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_notifications (
                    user_id INTEGER NOT NULL,
                    date    TEXT NOT NULL,
                    hash    TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, date)
                )
                """
            )

            # Источник истины накопления — одна запись на фактически
            # завершённую пару. Подгруппы не входят в уникальный ключ.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subjects (
                    normalized_subject TEXT PRIMARY KEY,
                    original_subject   TEXT NOT NULL,
                    created_at         TEXT NOT NULL,
                    updated_at         TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lesson_history (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name         TEXT NOT NULL,
                    date               TEXT NOT NULL,
                    pair_number        TEXT NOT NULL,
                    start_time         TEXT NOT NULL,
                    end_time           TEXT NOT NULL,
                    subject            TEXT NOT NULL,
                    normalized_subject TEXT NOT NULL,
                    duration_minutes   INTEGER NOT NULL,
                    subgroup_info      TEXT NOT NULL DEFAULT '',
                    teacher            TEXT NOT NULL DEFAULT '',
                    room               TEXT NOT NULL DEFAULT '',
                    completed_at       TEXT NOT NULL,
                    created_at         TEXT NOT NULL,
                    UNIQUE (group_name, date, pair_number)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lesson_history_subject
                ON lesson_history (group_name, normalized_subject, date)
                """
            )
            # День считается обработанным только после успешного получения
            # HTML. При ошибке строка не создаётся и дата будет повторена.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lesson_backfill_days (
                    group_name TEXT NOT NULL,
                    date       TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (group_name, date)
                )
                """
            )

            # Скрытая ежедневная автоматизация поздравлений.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS birthday_notifications (
                    date       TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    sent_at    TEXT NOT NULL,
                    people     TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (date, group_name)
                )
                """
            )

            # Rate limiter и внутренний blacklist переживают перезапуск.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blacklist (
                    user_id  INTEGER PRIMARY KEY,
                    added_at TEXT NOT NULL,
                    reason   TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limit_state (
                    user_id          INTEGER PRIMARY KEY,
                    events_json      TEXT NOT NULL DEFAULT '[]',
                    warning_count    INTEGER NOT NULL DEFAULT 0,
                    last_warning_at  REAL NOT NULL DEFAULT 0,
                    updated_at       TEXT NOT NULL
                )
                """
            )
        logger.info("База данных готова: %s", DB_PATH)
    except Exception:
        logger.exception("Ошибка инициализации SQLite")


def subscribe_user(
    user_id: int, chat_type: str = "private", title: str = ""
) -> bool:
    """Добавляет подписчика (ЛС или групповой чат).

    True если добавлен, False если уже был.
    """
    created = now_local().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO subscribers"
                " (user_id, created_at, chat_type, title)"
                " VALUES (?, ?, ?, ?)",
                (user_id, created, chat_type, title or ""),
            )
            if cur.rowcount == 0:
                # Обновляем название чата, оно могло измениться.
                conn.execute(
                    "UPDATE subscribers SET chat_type = ?, title = ?"
                    " WHERE user_id = ?",
                    (chat_type, title or "", user_id),
                )
                return False
            return True
    except Exception:
        logger.exception("Ошибка SQLite (subscribe)")
        return False


def unsubscribe_user(user_id: int) -> bool:
    try:
        with db_connect() as conn:
            cur = conn.execute(
                "DELETE FROM subscribers WHERE user_id = ?",
                (user_id,),
            )
            return cur.rowcount > 0
    except Exception:
        logger.exception("Ошибка SQLite (unsubscribe)")
        return False


def load_subscribers() -> list:
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM subscribers ORDER BY user_id"
            ).fetchall()
            return [int(row["user_id"]) for row in rows]
    except Exception:
        logger.exception("Ошибка SQLite (load subscribers)")
        return []


def subscriber_info(user_id: int):
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM subscribers WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row["created_at"] if row else None
    except Exception:
        logger.exception("Ошибка SQLite (subscriber info)")
        return None


def load_subscriber_rows() -> list:
    """Все подписчики с created_at и last_notified_version."""
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT user_id, created_at, last_notified_version"
                " FROM subscribers ORDER BY user_id"
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception:
        logger.exception("Ошибка SQLite (load subscriber rows)")
        return []


def mark_changelog_notified(user_id: int, version: str) -> bool:
    """Отмечает версию changelog как успешно доставленную пользователю.

    Сохраняет максимальную доставленную версию — это не мешает
    будущим версиям (2.1.5, 2.1.6…), потому что eligibility
    проверяется отдельно для каждой версии.
    """
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT last_notified_version FROM subscribers"
                " WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return False
            current = row["last_notified_version"] or ""
            if version_key(current) >= version_key(version):
                return True
            conn.execute(
                "UPDATE subscribers SET last_notified_version = ?"
                " WHERE user_id = ?",
                (version, user_id),
            )
            return True
    except Exception:
        logger.exception("Ошибка SQLite (mark changelog notified)")
        return False


def load_state() -> dict:
    """Состояние расписания: {date: {hash, data}}.

    `data` — JSON-строка с нормализованным списком занятий (для сравнения
    «было -> стало»). Совместимо со старыми базами без колонки data.
    """
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT date, hash, data, updated_at FROM schedule_state"
            ).fetchall()
            result = {}
            for row in rows:
                raw = row["data"] or ""
                payload = None
                if raw:
                    try:
                        payload = json.loads(raw)
                    except (ValueError, TypeError):
                        payload = None
                result[row["date"]] = {
                    "hash": row["hash"],
                    "data": payload,
                    "updated_at": row["updated_at"] or "",
                }
            return result
    except Exception:
        logger.exception("Ошибка SQLite (load state)")
        return {}


def save_state(state: dict) -> None:
    try:
        now = now_local().strftime("%Y-%m-%d %H:%M:%S")
        with db_connect() as conn:
            for date_key, value in state.items():
                if isinstance(value, str):
                    # Совместимость со старым вызовом save_state({...: hash}).
                    digest = value
                    data = "[]"
                else:
                    digest = value.get("hash") or ""
                    data = json.dumps(
                        value.get("data") or [], ensure_ascii=False
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO schedule_state"
                    " (date, hash, data, updated_at) VALUES (?, ?, ?, ?)",
                    (date_key, digest, data, now),
                )
    except Exception:
        logger.exception("Ошибка SQLite (save state)")


def load_schedule_notifications() -> dict:
    """{дата: {user_id: hash}} — последнее доставленное состояние каждому."""
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT user_id, date, hash FROM schedule_notifications"
            ).fetchall()
            result = {}
            for row in rows:
                result.setdefault(row["date"], {})[
                    int(row["user_id"])
                ] = row["hash"]
            return result
    except Exception:
        logger.exception("Ошибка SQLite (load notifications)")
        return {}


def record_schedule_notification(
    user_id: int, date_key: str, signature: str
) -> bool:
    """Фиксирует успешную отправку расписания подписчику."""
    try:
        now = now_local().strftime("%Y-%m-%d %H:%M:%S")
        with db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schedule_notifications"
                " (user_id, date, hash, sent_at) VALUES (?, ?, ?, ?)",
                (user_id, date_key, signature, now),
            )
            return True
    except Exception:
        logger.exception("Ошибка SQLite (record notification)")
        return False


# ============================================================
# ИСТОРИЯ ЗАВЕРШЁННЫХ ЗАНЯТИЙ И НАКОПЛЕННЫЕ ЧАСЫ
# ============================================================


def normalize_subject_name(value: str) -> str:
    """Стабильный ключ предмета без неуверенного fuzzy matching."""
    text = clean_text(value).casefold().replace("ё", "е")
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s*([,.;:()/\\\-])\s*", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text


# Короткие алиасы остаются переиспользуемыми для внешних тестов/миграций.
normalize_subject = normalize_subject_name


def _local_aware(value: Optional[datetime] = None) -> datetime:
    current = value or now_local()
    if current.tzinfo is None:
        return current.replace(tzinfo=TZ)
    return current.astimezone(TZ)


def parse_clock(value: str) -> Optional[datetime_time]:
    value = clean_text(value)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime_time(hour, minute)


def duration_minutes(start_time: str, end_time: str) -> int:
    """Реальная длительность пары в минутах, без округления."""
    start = parse_clock(start_time)
    end = parse_clock(end_time)
    if start is None or end is None:
        return 0
    start_total = start.hour * 60 + start.minute
    end_total = end.hour * 60 + end.minute
    if end_total < start_total:
        # Защита от редкого перехода через полночь.
        end_total += 24 * 60
    return max(0, end_total - start_total)


calculate_duration_minutes = duration_minutes


def lesson_duration_minutes(lesson: Lesson) -> int:
    start, end = _split_time(lesson)
    return duration_minutes(start, end)


calculate_lesson_duration = lesson_duration_minutes


def format_duration(minutes: int) -> str:
    minutes = max(0, int(minutes or 0))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def get_academic_year_start(day: Optional[date] = None) -> date:
    """1 сентября текущего учебного года в календаре Екатеринбурга."""
    value = day or get_today()
    year = value.year if value.month >= 9 else value.year - 1
    return date(year, 9, 1)


academic_year_start = get_academic_year_start


def is_lesson_completed(
    day: date, end_time: str, current: Optional[datetime] = None
) -> bool:
    """Пара считается завершённой начиная с момента её окончания."""
    end_clock = parse_clock(end_time)
    if end_clock is None:
        return False
    end_at = datetime.combine(day, end_clock).replace(tzinfo=TZ)
    return _local_aware(current) >= end_at


lesson_is_completed = is_lesson_completed


def _pair_representative(pair: Pair) -> Optional[Lesson]:
    if not pair.lessons:
        return None
    # Если подгруппы имеют один предмет, это ровно одна история пары. При
    # различиях сохраняем первый элемент, не умножая длительность на число
    # подгрупп.
    return next((item for item in pair.lessons if clean_text(item.subject)), pair.lessons[0])


def _pair_subgroups(pair: Pair) -> str:
    values = []
    for item in pair.lessons:
        value = clean_text(item.subgroup)
        if value and value not in values:
            values.append(value)
    return ", ".join(values)


def _upsert_subject(conn: sqlite3.Connection, original: str, normalized: str) -> None:
    if not normalized:
        return
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO subjects (normalized_subject, original_subject, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(normalized_subject) DO UPDATE SET
            original_subject = CASE
                WHEN subjects.original_subject = '' THEN excluded.original_subject
                ELSE subjects.original_subject
            END,
            updated_at = excluded.updated_at
        """,
        (normalized, clean_text(original), stamp, stamp),
    )


def record_completed_lesson(
    group_name: str,
    day: date,
    pair_number: str,
    start_time: str,
    end_time: str,
    subject: str,
    *,
    subgroup_info: str = "",
    teacher: str = "",
    room: str = "",
    duration: Optional[int] = None,
) -> bool:
    """Идемпотично записывает одну завершённую пару.

    ``INSERT OR IGNORE`` и UNIQUE(group, date, pair_number) делают повторный
    backfill/перезапуск безопасным и не считают подгруппы дважды.
    """
    normalized = normalize_subject_name(subject)
    minutes = duration if duration is not None else duration_minutes(start_time, end_time)
    if not normalized or minutes <= 0:
        return False
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_connect() as conn:
            _upsert_subject(conn, subject, normalized)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO lesson_history
                (group_name, date, pair_number, start_time, end_time, subject,
                 normalized_subject, duration_minutes, subgroup_info, teacher,
                 room, completed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_text(group_name) or GROUP_NAME,
                    day.isoformat(),
                    clean_text(pair_number).upper() or f"{start_time}-{end_time}",
                    clean_text(start_time),
                    clean_text(end_time),
                    clean_text(subject),
                    normalized,
                    int(minutes),
                    clean_text(subgroup_info),
                    clean_text(teacher),
                    clean_text(room),
                    stamp,
                    stamp,
                ),
            )
            return cursor.rowcount > 0
    except Exception:
        logger.exception("Ошибка записи истории занятия")
        return False


def record_completed_lessons(
    schedule: Schedule, current: Optional[datetime] = None
) -> int:
    """Добавляет завершённые пары расписания основной группы."""
    if schedule.schedule_type != "group":
        return 0
    if schedule.group != GROUP_NAME:
        return 0
    if schedule.date < get_academic_year_start():
        return 0

    inserted = 0
    for pair in schedule.pairs:
        if not is_lesson_completed(schedule.date, pair.end, current):
            continue
        minutes = duration_minutes(pair.start, pair.end)
        representative = _pair_representative(pair)
        if representative is None or minutes <= 0:
            continue
        if record_completed_lesson(
            schedule.group,
            schedule.date,
            pair.number,
            pair.start,
            pair.end,
            representative.subject,
            subgroup_info=_pair_subgroups(pair),
            teacher=representative.teacher,
            room=representative.room,
            duration=minutes,
        ):
            inserted += 1
    return inserted


# Названия, встречающиеся в интеграциях проекта.
process_completed_lessons = record_completed_lessons


def load_subject_totals(
    group_name: str = GROUP_NAME,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict[str, int]:
    """Сумма фактически завершённых минут по нормализованному предмету."""
    clauses = ["group_name = ?"]
    params: list = [group_name]
    if start is not None:
        clauses.append("date >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("date <= ?")
        params.append(end.isoformat())
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT normalized_subject, SUM(duration_minutes) AS minutes "
                "FROM lesson_history WHERE " + " AND ".join(clauses) +
                " GROUP BY normalized_subject",
                params,
            ).fetchall()
            return {
                row["normalized_subject"]: int(row["minutes"] or 0)
                for row in rows
            }
    except Exception:
        logger.exception("Ошибка загрузки накопленных часов")
        return {}


def register_subjects_from_schedule(schedule: Schedule) -> int:
    """Регистрирует новые предметы без фиксированного справочника."""
    if schedule.schedule_type != "group" or schedule.group != GROUP_NAME:
        return 0
    values = {}
    for lesson in schedule.lessons:
        original = clean_text(lesson.subject)
        normalized = normalize_subject_name(original)
        if normalized:
            values.setdefault(normalized, original)
    try:
        with db_connect() as conn:
            for normalized, original in values.items():
                _upsert_subject(conn, original, normalized)
        return len(values)
    except Exception:
        logger.exception("Ошибка регистрации предметов")
        return 0


register_schedule_subjects = register_subjects_from_schedule


def get_subject_total_minutes(
    subject: str, group_name: str = GROUP_NAME
) -> int:
    return load_subject_totals(group_name).get(normalize_subject_name(subject), 0)


def get_subject_progress(subject: str, group_name: str = GROUP_NAME) -> str:
    normalized = normalize_subject_name(subject)
    minutes = get_subject_total_minutes(subject, group_name)
    if minutes > 0:
        return f"Изучено: {format_duration(minutes)}"
    try:
        with db_connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM subjects WHERE normalized_subject = ?",
                (normalized,),
            ).fetchone()
        # Даже если subject уже зарегистрирован текущим, но первая пара не
        # завершена, пользователь видит честный статус без выдуманных часов.
        return "Первое занятие по предмету" if exists or normalized else ""
    except Exception:
        logger.exception("Ошибка получения прогресса предмета")
        return "Первое занятие по предмету" if normalized else ""


def _backfill_day_processed(group_name: str, day: date) -> bool:
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM lesson_backfill_days WHERE group_name = ? AND date = ?",
                (group_name, day.isoformat()),
            ).fetchone()
            return row is not None
    except Exception:
        logger.exception("Ошибка проверки backfill-даты")
        return False


def _mark_backfill_day_processed(group_name: str, day: date) -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO lesson_backfill_days "
                "(group_name, date, processed_at) VALUES (?, ?, ?)",
                (group_name, day.isoformat(), now_local().strftime("%Y-%m-%d %H:%M:%S")),
            )
    except Exception:
        logger.exception("Ошибка сохранения backfill-даты")


async def backfill_lesson_history(
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> int:
    """Backfill с 1 сентября до вчерашнего дня.

    День отмечается обработанным только после успешного HTTP+parse. Ошибка
    источника оставляет дату для следующей попытки и не создаёт фиктивных
    занятий.
    """
    today = get_today()
    first = start or get_academic_year_start(today)
    last = end or (today - timedelta(days=1))
    if last < first:
        return 0

    inserted = 0
    day = first
    while day <= last:
        if _backfill_day_processed(GROUP_NAME, day):
            day += timedelta(days=1)
            continue
        # Даже воскресенье запрашивается как историческая дата: пустой
        # ответ источника — это честный результат, а не придуманное занятие.
        try:
            schedule = await get_schedule(day)
        except ScheduleUnavailable:
            logger.warning("Backfill %s: источник недоступен, повторим позже", day)
            day += timedelta(days=1)
            continue
        register_subjects_from_schedule(schedule)
        inserted += record_completed_lessons(schedule)
        _mark_backfill_day_processed(GROUP_NAME, day)
        day += timedelta(days=1)
    return inserted


run_backfill = backfill_lesson_history


init_db()


# ============================================================
# РЕНДЕР PNG
# ============================================================

# Палитра
COL_BG = "#F3F5FA"
COL_WHITE = "#FFFFFF"
COL_INK = "#14202F"
COL_MUTED = "#64748B"
COL_ACCENT = "#4F46E5"
COL_ACCENT_LIGHT = "#ECECFB"
COL_GREEN = "#0E9F5F"
COL_GREEN_LIGHT = "#E5F6ED"
COL_BORDER = "#E2E7F0"
COL_FOOTER = "#98A1B0"
COL_WARN = "#B45309"
COL_WARN_LIGHT = "#FEF3C7"
COL_RED = "#B91C1C"
COL_RED_LIGHT = "#FEE2E2"

SUMMARY_MAX_LINES = 24
SUMMARY_MAX_CHANGES = 20


def _wrap_lines(text: str, font, max_width: float) -> list:
    """Переносит текст по словам; слишком длинное слово обрезается."""
    text = clean_text(text)
    if not text:
        return [""]

    words = text.split(" ")
    lines: list = []
    current = ""

    for word in words:
        trial = (current + " " + word).strip()
        if font.getlength(trial) <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
            current = word
            # если само слово не помещается — режем его
            while font.getlength(current) > max_width and len(current) > 1:
                current = current[:-1]
        else:
            while font.getlength(word) > max_width and len(word) > 1:
                word = word[:-1]
            current = word

    if current:
        lines.append(current)

    return lines or [""]


def _text_h(font) -> int:
    """Примерная высота строки с отступом."""
    return int(font.size * 1.35)


def _truncate(text: str, font, max_width: float) -> str:
    text = clean_text(text)
    if not text or font.getlength(text) <= max_width:
        return text
    while font.getlength(text) > max_width and len(text) > 2:
        text = text[:-1]
    return text + "…"


def _subgroup_label(subgroup) -> str:
    if subgroup is None or subgroup == "":
        return ""
    return f"{clean_text(subgroup)} п/гр."


def _lesson_from_normalized_item(item: dict) -> Lesson:
    start = clean_text(item.get("start", ""))
    end = clean_text(item.get("end", ""))
    return Lesson(
        pair=clean_text(item.get("pair", "")).upper(),
        time=f"{start} - {end}" if start and end else "",
        subject=clean_text(item.get("subject", "")) or "Предмет не указан",
        teacher=clean_text(item.get("teacher", "")) or "—",
        room=clean_text(item.get("room", "")) or "—",
        start=start,
        end=end,
        subgroup=clean_text(item.get("subgroup") or "") or None,
        groups=clean_text(item.get("groups", "")),
    )


def _render_items_for_pair(pair: Pair, change_by_key: dict, removed_by_pair: dict) -> list:
    items = []
    for lesson in pair.lessons:
        key = (clean_text(lesson.pair).upper(), clean_text(lesson.subgroup) or None)
        change = change_by_key.get(key)
        if change is not None and change.kind == "changed":
            items.append({"lesson": lesson, "kind": "changed", "details": change.details})
        elif change is not None and change.kind == "added":
            items.append({"lesson": lesson, "kind": "added", "details": []})
        else:
            items.append({"lesson": lesson, "kind": "normal", "details": []})

    for change in removed_by_pair.get(clean_text(pair.number).upper(), []):
        old_item = change.old or {}
        lesson = _lesson_from_normalized_item(old_item)
        items.append({"lesson": lesson, "kind": "removed", "details": []})

    return items


def _change_summary_lines(changes: list) -> list:
    """Короткий текстовый блок «Что изменилось» для картинки."""
    lines = ["Что изменилось:"]
    count = 0
    for change in changes:
        if count >= SUMMARY_MAX_CHANGES:
            break
        count += 1
        pair = clean_text(change.pair).upper()
        subgroup = clean_text(change.subgroup) or None
        label = f"{pair} пара" + (f" • {subgroup} п/гр." if subgroup else "")

        if change.kind == "added":
            new = change.new or {}
            lines.append(
                f"Добавлено: {label} — {new.get('subject') or 'Предмет не указан'}"
                + (f", {new.get('room') or '—'}" if new.get("room") else "")
            )
            continue
        if change.kind == "removed":
            old = change.old or {}
            lines.append(
                f"Удалено: {label} — {old.get('subject') or 'Предмет не указан'}"
                + (f", {old.get('room') or '—'}" if old.get("room") else "")
            )
            continue

        lines.append(f"Изменено: {label}")
        for detail in change.details:
            label_name = detail.get("label") or detail.get("field", "")
            old_val = clean_text(detail.get("old", ""))
            new_val = clean_text(detail.get("new", ""))
            if old_val or new_val:
                lines.append(
                    f"* {label_name}: {old_val or '—'} -> {new_val or '—'}"
                )

    if len(changes) > count:
        lines.append(f"… и ещё {len(changes) - count} изменений")

    return lines[:SUMMARY_MAX_LINES]


# ============================================================
# РЕНДЕР 2.1.5: строгая горизонтальная лента карточек
# ============================================================


def render_schedule_image(
    schedule: Schedule,
    changes=None,
    title: Optional[str] = None,
) -> Path:
    """Рисует расписание в одном горизонтальном ряду.

    В отличие от старой вертикальной разметки, каждая пара — отдельная
    колонка. Длинный текст переносится только внутри своей карточки, а
    ширина изображения растёт вместе с числом пар. Для основной группы
    прогресс предмета берётся из ``lesson_history``; незавершённая первая
    пара не увеличивает историю.
    """
    changes = list(changes or [])
    pairs = schedule.pairs
    font_kicker = get_font(22, bold=True)
    font_title = get_font(46, bold=True)
    font_staff = get_font(28, bold=True)
    font_date = get_font(25)
    font_count = get_font(20, bold=True)
    font_pair = get_font(24, bold=True)
    font_time = get_font(25, bold=True)
    font_subject = get_font(27, bold=True)
    font_info = get_font(20)
    font_small = get_font(18)
    font_status = get_font(17, bold=True)
    font_summary = get_font(20, bold=True)
    font_footer = get_font(18)

    margin = 44
    gap = 20
    card_width = 360 if pairs else 620
    # Увеличиваем ширину, когда в расписании есть очень длинные названия.
    longest = max(
        [len(clean_text(lesson.subject)) for pair in pairs for lesson in pair.lessons]
        + [0]
    )
    if longest > 34:
        card_width = min(520, max(card_width, 360 + (longest - 34) * 3))
    card_width = max(320, card_width)
    width = max(920, margin * 2 + max(1, len(pairs)) * card_width + max(0, len(pairs) - 1) * gap)

    inner = 24
    text_width = card_width - inner * 2
    header_height = 210
    cards_top = header_height + 34
    card_padding = 22
    card_header_height = 82

    def pair_header_extra(pair: Pair) -> int:
        return 26 if clean_text(pair.break_duration) else 0

    change_by_key = {
        (clean_text(item.pair).upper(), clean_text(item.subgroup) or None): item
        for item in changes
    }
    removed_by_pair: dict[str, list] = {}
    for item in changes:
        if item.kind == "removed":
            removed_by_pair.setdefault(clean_text(item.pair).upper(), []).append(item)

    def entries_for(pair: Pair) -> list[dict]:
        result = []
        for lesson in pair.lessons:
            key = (clean_text(lesson.pair).upper(), clean_text(lesson.subgroup) or None)
            change = change_by_key.get(key)
            result.append({
                "lesson": lesson,
                "kind": change.kind if change else "normal",
                "details": change.details if change else [],
            })
        for change in removed_by_pair.get(clean_text(pair.number).upper(), []):
            result.append({
                "lesson": _lesson_from_normalized_item(change.old or {}),
                "kind": "removed",
                "details": [],
            })
        return result

    def item_height(item: dict) -> int:
        lesson = item["lesson"]
        subject_lines = _wrap_lines(
            lesson.subject or "Предмет не указан", font_subject, text_width
        )
        height = 16 + len(subject_lines) * 32 + 8
        height += 27  # room
        if clean_text(lesson.teacher) not in ("", "—"):
            height += 52
        if clean_text(getattr(lesson, "groups", "")):
            height += 52
        if clean_text(lesson.subgroup):
            height += 24
        if schedule.schedule_type == "group":
            height += 25
        if item["kind"] != "normal":
            height += 23
        if item["kind"] == "changed":
            height += min(3, len(item["details"])) * 22
        return height

    card_heights = []
    for pair in pairs:
        entries = entries_for(pair)
        entries_height = sum(item_height(item) for item in entries)
        entries_height += max(0, len(entries) - 1) * 12
        card_heights.append(
            card_padding + card_header_height + pair_header_extra(pair)
            + entries_height + card_padding
        )
    card_height = max(card_heights or [230])

    summary_lines = _change_summary_lines(changes) if changes else []
    summary_height = 0
    if summary_lines:
        summary_height = 42 + len(summary_lines) * 27 + 18
    footer_gap = 50
    footer_height = 28
    content_bottom = cards_top + card_height
    summary_top = content_bottom + 26 if summary_lines else None
    if summary_lines:
        content_bottom = summary_top + summary_height
    footer_top = content_bottom + footer_gap
    image_height = footer_top + footer_height + 28

    # Очень светлый нейтральный фон вокруг белых карточек сохраняет
    # минималистичный белый вид и не перегружает изображение.
    image = Image.new("RGB", (width, image_height), COL_BG)
    draw = ImageDraw.Draw(image)

    # Шапка — белая и спокойная, без логотипа.
    draw.rectangle((0, 0, width, header_height), fill=COL_WHITE)
    draw.rectangle((0, header_height - 1, width, header_height), fill=COL_BORDER)
    draw.text((margin, 28), title or ("РАСПИСАНИЕ ИЗМЕНИЛОСЬ" if changes else "РАСПИСАНИЕ"),
              font=font_kicker, fill=COL_ACCENT)
    if schedule.schedule_type == "staff":
        draw.text((margin, 64), "Преподаватель", font=font_staff, fill=COL_INK)
        name = schedule.staff_name or schedule.group
        name_lines = _wrap_lines(name, font_title, width - margin * 2 - 270)
        for index, line in enumerate(name_lines[:2]):
            draw.text((margin, 96 + index * 48), line, font=font_title, fill=COL_INK)
        date_y = 96 + min(2, len(name_lines)) * 48 + 6
    else:
        draw.text((margin, 64), schedule.group, font=font_title, fill=COL_INK)
        date_y = 132
    date_label = (
        f"Дата: {format_date_full(schedule.date)}"
        if schedule.schedule_type == "staff"
        else format_date_full(schedule.date)
    )
    draw.text((margin, date_y), date_label, font=font_date, fill=COL_MUTED)

    count = count_lessons(schedule)
    count_text = f"{count} " + (
        "занятие" if count == 1 else "занятия" if count < 5 else "занятий"
    )
    count_w = draw.textlength(count_text, font=font_count) + 34
    draw.rounded_rectangle(
        (width - margin - count_w, 42, width - margin, 84),
        radius=21, fill=COL_ACCENT_LIGHT,
    )
    draw.text((width - margin - count_w + 17, 54), count_text,
              font=font_count, fill=COL_ACCENT)

    # Новый предмет регистрируется сразу при появлении, а история
    # пополняется только после фактического окончания пары.
    if schedule.schedule_type == "group":
        register_subjects_from_schedule(schedule)
    # История читается один раз на изображение.
    totals = load_subject_totals() if schedule.schedule_type == "group" else {}

    def progress_text(lesson: Lesson) -> str:
        if schedule.schedule_type != "group":
            return ""
        normalized = normalize_subject_name(lesson.subject)
        minutes = totals.get(normalized, 0)
        if minutes:
            return f"Изучено: {format_duration(minutes)}"
        return "Первое занятие по предмету"

    def draw_wrapped(x, y, text, font, fill, max_lines=3):
        lines = _wrap_lines(text, font, text_width)
        for index, line in enumerate(lines[:max_lines]):
            draw.text((x, y + index * int(font.size * 1.2)), line, font=font, fill=fill)
        return min(len(lines), max_lines) * int(font.size * 1.2)

    if not pairs:
        empty_left, empty_top = margin, cards_top
        draw.rounded_rectangle(
            (empty_left, empty_top, width - margin, empty_top + card_height),
            radius=20, fill=COL_WHITE, outline=COL_BORDER, width=2,
        )
        text = "Занятий нет"
        tw = draw.textlength(text, font=font_staff)
        draw.text(((width - tw) / 2, empty_top + 58), text, font=font_staff, fill=COL_INK)
        sub = "Расписание на этот день не опубликовано."
        sw = draw.textlength(sub, font=font_info)
        draw.text(((width - sw) / 2, empty_top + 112), sub, font=font_info, fill=COL_MUTED)
    else:
        for index, pair in enumerate(pairs):
            x = margin + index * (card_width + gap)
            y = cards_top
            entries = entries_for(pair)
            has_change = any(item["kind"] != "normal" for item in entries)
            draw.rounded_rectangle(
                (x + 3, y + 5, x + card_width + 3, y + card_height + 5),
                radius=20, fill="#E8EDF4",
            )
            draw.rounded_rectangle(
                (x, y, x + card_width, y + card_height), radius=20,
                fill=COL_WHITE, outline=COL_ACCENT if has_change else COL_BORDER,
                width=2 if not has_change else 3,
            )
            # Верх карточки: номер и время — горизонтально.
            draw.ellipse((x + inner, y + 18, x + inner + 48, y + 66), fill=COL_ACCENT)
            roman = clean_text(pair.number).upper()
            rw = draw.textlength(roman, font=font_pair)
            draw.text((x + inner + (48 - rw) / 2, y + 30), roman,
                      font=font_pair, fill=COL_WHITE)
            draw.text((x + inner + 62, y + 22), f"{pair.start} — {pair.end}",
                      font=font_time, fill=COL_ACCENT)
            if clean_text(pair.break_duration):
                draw.text((x + inner + 62, y + 50),
                          f"перемена {pair.break_duration}",
                          font=font_small, fill=COL_MUTED)
            by = y + card_padding + card_header_height + pair_header_extra(pair)
            for entry_index, item in enumerate(entries):
                lesson = item["lesson"]
                kind = item["kind"]
                ih = item_height(item)
                if kind != "normal":
                    fill = {"added": COL_GREEN_LIGHT, "changed": COL_WARN_LIGHT,
                            "removed": COL_RED_LIGHT}.get(kind, COL_WHITE)
                    outline = {"added": COL_GREEN, "changed": COL_WARN,
                               "removed": COL_RED}.get(kind, COL_BORDER)
                    draw.rounded_rectangle(
                        (x + inner - 8, by - 5, x + card_width - inner + 8, by + ih),
                        radius=10, fill=fill, outline=outline, width=1,
                    )
                iy = by + 8
                status = {"added": "ДОБАВЛЕНО", "changed": "ИЗМЕНЕНО",
                          "removed": "УДАЛЕНО"}.get(kind)
                if status:
                    draw.text((x + inner, iy), status, font=font_status,
                              fill={"ДОБАВЛЕНО": COL_GREEN, "ИЗМЕНЕНО": COL_WARN,
                                    "УДАЛЕНО": COL_RED}[status])
                    iy += 23
                if clean_text(lesson.subgroup):
                    draw.text((x + inner, iy), f"{lesson.subgroup} п/гр.",
                              font=font_small, fill=COL_ACCENT)
                    iy += 24
                used = draw_wrapped(x + inner, iy, lesson.subject or "Предмет не указан",
                                    font_subject, COL_INK)
                iy += used + 3
                room = clean_text(lesson.room) or "—"
                draw.text((x + inner, iy), f"ауд. {room}", font=font_info, fill=COL_MUTED)
                iy += 26
                teacher = clean_text(lesson.teacher)
                if teacher and teacher != "—":
                    draw_wrapped(x + inner, iy, f"Преподаватель: {teacher}",
                                 font_info, COL_MUTED, max_lines=2)
                    iy += 52
                groups = clean_text(getattr(lesson, "groups", ""))
                if groups:
                    draw_wrapped(x + inner, iy, f"Группы: {groups}", font_info, COL_INK, max_lines=2)
                    iy += 52
                if schedule.schedule_type == "group":
                    draw.text((x + inner, iy), progress_text(lesson),
                              font=font_small, fill=COL_GREEN if totals.get(normalize_subject_name(lesson.subject), 0) else COL_MUTED)
                    iy += 25
                if kind == "changed":
                    for detail in item["details"][:3]:
                        before = clean_text(detail.get("old", "")) or "—"
                        after = clean_text(detail.get("new", "")) or "—"
                        draw.text((x + inner, iy), _truncate(
                            f"{detail.get('label', detail.get('field', ''))}: {before} → {after}",
                            font_small, text_width), font=font_small, fill=COL_WARN)
                        iy += 22
                by += ih + 12

    if summary_lines:
        sy = summary_top
        draw.rounded_rectangle((margin, sy, width - margin, sy + summary_height),
                               radius=18, fill=COL_WHITE, outline=COL_WARN, width=2)
        draw.text((margin + 20, sy + 14), "Что изменилось:", font=font_summary, fill=COL_WARN)
        sy += 43
        for line in summary_lines:
            draw.text((margin + 28, sy), _truncate(line, font_info, width - margin * 2 - 56),
                      font=font_info, fill=COL_INK)
            sy += 27

    footer = BOT_NAME
    fw = draw.textlength(footer, font=font_footer)
    draw.text((width - margin - fw, footer_top), footer, font=font_footer, fill=COL_FOOTER)

    kind = "staff" if schedule.schedule_type == "staff" else "group"
    staff_part = f"_{schedule.staff_id}" if schedule.staff_id else ""
    suffix = "_changed" if changes else ""
    path = IMAGE_DIR / f"schedule_{kind}{staff_part}_{schedule.date.isoformat()}{suffix}.png"
    image.save(path, "PNG", optimize=True)
    logger.info("Горизонтальное изображение сохранено: %s (%sx%s)", path, width, image_height)
    return path


# ============================================================
# КЛАВИАТУРА
# ============================================================

def staff_keyboard(staff_id: int, target_day: date) -> InlineKeyboardMarkup:
    """Навигация staff-расписания: callback type:id:date."""
    if int(staff_id) not in STAFF_BY_ID:
        raise ValueError(f"Неизвестный STAFF_ID: {staff_id}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Предыдущий день",
                    callback_data=f"staff:{int(staff_id)}:{(target_day - timedelta(days=1)).isoformat()}",
                ),
                InlineKeyboardButton(
                    text="📅 Сегодня",
                    callback_data=f"staff:{int(staff_id)}:{get_today().isoformat()}",
                ),
                InlineKeyboardButton(
                    text="Следующий день ▶️",
                    callback_data=f"staff:{int(staff_id)}:{(target_day + timedelta(days=1)).isoformat()}",
                ),
            ]
        ]
    )


def staff_choice_keyboard(matches: list[StaffMember], target_day: date) -> InlineKeyboardMarkup:
    rows = []
    for member in matches:
        rows.append([
            InlineKeyboardButton(
                text=member.short_name,
                callback_data=f"staff:{member.staff_id}:{target_day.isoformat()}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_keyboard(is_subscribed: bool) -> InlineKeyboardMarkup:
    """Кнопки «Сегодня» (/today) и «Завтра» (/schedule)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 Сегодня", callback_data="today"
                ),
                InlineKeyboardButton(
                    text="📅 Завтра", callback_data="schedule"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔕 Отключить" if is_subscribed else "🔔 Подписаться",
                    callback_data="unsubscribe" if is_subscribed
                    else "subscribe",
                ),
                InlineKeyboardButton(text="ℹ️ Статус", callback_data="status"),
            ],
            [
                InlineKeyboardButton(text="🔎 По дате", callback_data="date"),
                InlineKeyboardButton(text="🆘 Помощь", callback_data="help"),
            ],
        ]
    )


# ============================================================
# ОТПРАВКА РАСПИСАНИЯ
# ============================================================

def _photo_caption(schedule: Schedule) -> str:
    if schedule.schedule_type == "staff":
        lines = [
            "👨‍🏫 <b>Преподаватель</b>",
            f"<b>{schedule.staff_name or schedule.group}</b>",
            f"📅 {format_date_full(schedule.date)}",
        ]
    else:
        lines = [
            f"📚 <b>{schedule.group}</b>",
            f"📅 {format_date_full(schedule.date)}",
        ]
    if schedule.lessons:
        lines.append(f"🕐 Занятий: {count_lessons(schedule)}")
    return "\n".join(lines)


async def _send_photo(destination, schedule: Schedule, reply_markup=None) -> bool:
    """Генерирует PNG и отправляет его. Временный файл удаляется после отправки."""
    path = render_schedule_image(schedule)
    try:
        caption = _photo_caption(schedule)
        kwargs = {"caption": caption}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if isinstance(destination, Message):
            await destination.answer_photo(FSInputFile(path), **kwargs)
        else:
            await destination.message.answer_photo(FSInputFile(path), **kwargs)
        return True
    except Exception:
        logger.exception("Ошибка отправки изображения")
        return False
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


async def _send_text(destination, text: str) -> None:
    try:
        if _is_message_destination(destination):
            await destination.answer(text)
        else:
            await destination.message.answer(text)
    except Exception:
        logger.exception("Ошибка отправки текста")


async def _handle_today(destination):
    """Команда /today: расписание ТОЛЬКО на сегодняшний день.

    Никакого fallback на завтра/другую дату. Если расписания нет —
    сообщаем об этом.
    """
    try:
        today = get_today()
        schedule = await get_schedule(today)
        register_subjects_from_schedule(schedule)
        record_completed_lessons(schedule)

        if not schedule.lessons:
            await _send_text(
                destination,
                "📅 <b>Расписание на сегодня</b>\n\n"
                f"👥 Группа: <b>{GROUP_NAME}</b>\n"
                f"🗓 {format_date_header(today)}\n\n"
                "☕ Занятий нет или расписание ещё не опубликовано.",
            )
            return

        ok = await _send_photo(destination, schedule)
        if not ok:
            await _send_text(destination, "Не удалось отправить расписание.")
    except ScheduleUnavailable:
        await _send_text(
            destination,
            f"😔 Не удалось получить расписание на "
            f"{format_date_full(get_today())}. Сайт недоступен, попробуй позже.",
        )
    except Exception:
        logger.exception("Ошибка /today")


async def _handle_schedule(destination):
    """Команда /schedule: расписание ТОЛЬКО на завтрашний день.

    Никакого fallback на сегодня/другую дату. Если расписания нет —
    сообщаем об этом.
    """
    try:
        tomorrow = get_tomorrow()
        schedule = await get_schedule(tomorrow)
        register_subjects_from_schedule(schedule)
        record_completed_lessons(schedule)

        if not schedule.lessons:
            await _send_text(
                destination,
                "📅 <b>Расписание на завтра</b>\n\n"
                f"👥 Группа: <b>{GROUP_NAME}</b>\n"
                f"🗓 {format_date_header(tomorrow)}\n\n"
                "☕ Занятий нет — расписание ещё не опубликовано.",
            )
            return

        ok = await _send_photo(destination, schedule)
        if not ok:
            await _send_text(destination, "Не удалось отправить расписание.")
    except ScheduleUnavailable:
        await _send_text(
            destination,
            f"😔 Не удалось получить расписание на "
            f"{format_date_full(get_tomorrow())}. Сайт недоступен, попробуй позже.",
        )
    except Exception:
        logger.exception("Ошибка /schedule")


async def _handle_date(destination, target: date):
    """Показывает расписание на конкретную дату (без подстановки «сегодня»)."""
    try:
        if is_day_off(target):
            await _send_text(
                destination,
                f"📅 <b>{format_date_header(target)}</b>\n\n"
                f"Группа: <b>{GROUP_NAME}</b>\n"
                f"{format_date_full(target)}\n\n"
                "☕ Воскресенье — занятий нет.",
            )
            return

        schedule = await get_schedule(target)
        register_subjects_from_schedule(schedule)
        record_completed_lessons(schedule)

        if not schedule.lessons:
            await _send_text(
                destination,
                f"📅 <b>{format_date_header(target)}</b>\n\n"
                f"Группа: <b>{GROUP_NAME}</b>\n"
                f"{format_date_full(target)}\n\n"
                "☕ Занятий нет или расписание ещё не опубликовано.",
            )
            return

        ok = await _send_photo(destination, schedule)
        if not ok:
            await _send_text(destination, "Не удалось отправить расписание.")
    except ScheduleUnavailable:
        await _send_text(
            destination,
            "😔 Не удалось получить расписание. Сайт недоступен, попробуй позже.",
        )
    except Exception:
        logger.exception("Ошибка /date")


def _is_message_destination(destination) -> bool:
    return isinstance(destination, Message) or (
        hasattr(destination, "answer") and not isinstance(destination, CallbackQuery)
    )


async def _send_staff_schedule(
    destination,
    member: StaffMember,
    target_day: date,
    *,
    edit_existing: bool = False,
) -> bool:
    """Получает и отправляет staff-расписание с навигацией."""
    try:
        schedule = await get_staff_schedule(member.staff_id, target_day)
        keyboard = staff_keyboard(member.staff_id, target_day)
        if not schedule.lessons:
            text = (
                "👨‍🏫 <b>Расписание преподавателя</b>\n"
                f"<b>{member.full_name}</b>\n\n"
                f"📅 {format_date_full(target_day)}\n\n"
                "Занятий нет или расписание ещё не опубликовано."
            )
            if _is_message_destination(destination):
                await destination.answer(text, reply_markup=keyboard)
            else:
                await destination.message.answer(text, reply_markup=keyboard)
            return True

        path = render_schedule_image(schedule)
        try:
            caption = _photo_caption(schedule)
            if edit_existing and isinstance(destination, CallbackQuery):
                try:
                    await destination.message.edit_media(
                        media=InputMediaPhoto(
                            media=FSInputFile(path),
                            caption=caption,
                        ),
                        reply_markup=keyboard,
                    )
                except Exception:
                    # Старые сообщения или тестовые mock-и могут не уметь
                    # edit_media; сохраняем функциональность отправкой новой
                    # картинки, а не теряем ответ навигации.
                    await destination.message.answer_photo(
                        FSInputFile(path), caption=caption, reply_markup=keyboard
                    )
            elif _is_message_destination(destination):
                await destination.answer_photo(
                    FSInputFile(path), caption=caption, reply_markup=keyboard
                )
            else:
                await destination.message.answer_photo(
                    FSInputFile(path), caption=caption, reply_markup=keyboard
                )
            return True
        finally:
            path.unlink(missing_ok=True)
    except ScheduleUnavailable:
        text = (
            "😔 Не удалось получить расписание преподавателя. "
            "Сайт недоступен, попробуй позже."
        )
        if _is_message_destination(destination):
            await destination.answer(text)
        else:
            await destination.message.answer(text)
        return False
    except Exception:
        logger.exception("Ошибка отправки расписания преподавателя")
        return False


async def _handle_staff_request(destination, request: ScheduleTextRequest) -> None:
    if request.error:
        await _send_text(
            destination,
            "Не удалось определить преподавателя или дату.\n\n"
            "Примеры:\n"
            "• расписание преподавателя Аглиуллиной\n"
            "• расписание Аглиуллина на сегодня\n"
            "• расписание преподавателя Аглиуллиной на 9 сентября\n"
            "• расписание преподавателя Аглиуллиной на 09.09.2026",
        )
        return
    matches = search_staff(request.staff_query)
    if not matches:
        await _send_text(
            destination,
            f"👨‍🏫 Преподаватель «{clean_text(request.staff_query)}» не найден.\n"
            "Попробуй указать фамилию, имя, ФИО или инициалы.",
        )
        return
    target_day = request.date or get_tomorrow()
    if len(matches) > 1:
        text = "👨‍🏫 <b>Выберите преподавателя:</b>"
        if _is_message_destination(destination):
            await destination.answer(
                text, reply_markup=staff_choice_keyboard(matches, target_day)
            )
        else:
            await destination.message.answer(
                text, reply_markup=staff_choice_keyboard(matches, target_day)
            )
        return
    await _send_staff_schedule(destination, matches[0], target_day)


async def _status_text(chat_id: int) -> str:
    created = subscriber_info(chat_id)
    total = len(load_subscribers())
    lines = [
        f"📚 <b>{GROUP_NAME}</b>",
        "—" * 18,
    ]
    if created:
        lines.append("🔔 Этот чат подписан на уведомления.")
        lines.append(f"Подписка оформлена: <i>{created}</i>")
    else:
        lines.append("🔕 Этот чат не подписан на уведомления.")
    lines.append(f"Всего подписок: <b>{total}</b>")
    lines.append(f"Проверка изменений каждые {CHECK_INTERVAL // 60} мин.")
    lines.append(f"Часовой пояс: <b>{TIMEZONE}</b> (UTC+5)")
    return "\n".join(lines)


# ============================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================

dp = Dispatcher()


async def _send_rate_warning(event) -> None:
    try:
        if isinstance(event, Message):
            await event.answer(SPAM_WARNING_TEXT)
        elif isinstance(event, CallbackQuery):
            await event.answer(SPAM_WARNING_TEXT, show_alert=False)
        elif hasattr(event, "answer"):
            await event.answer(SPAM_WARNING_TEXT)
    except Exception:
        logger.exception("Не удалось отправить предупреждение rate limit")


class AntiSpamMiddleware(BaseMiddleware):
    """Самый ранний фильтр update для сообщений и callback-ов."""

    async def __call__(self, handler, event, data):
        actor = getattr(event, "from_user", None)
        user_id = getattr(actor, "id", None)
        if user_id is None:
            return await handler(event, data)
        # Blacklist проверяется до любых handler/parser/site операций.
        if is_blacklisted(user_id):
            return None
        decision = check_rate_limit(user_id)
        if decision.blacklisted:
            return None
        if not decision.allowed:
            if decision.warning:
                await _send_rate_warning(event)
            return None
        return await handler(event, data)


_spam_middleware = AntiSpamMiddleware()
dp.message.outer_middleware(_spam_middleware)
dp.callback_query.outer_middleware(_spam_middleware)
dp.my_chat_member.outer_middleware(_spam_middleware)


def _is_subscribed(user_id: int) -> bool:
    return subscriber_info(user_id) is not None


async def _is_group_admin(message: Message) -> bool:
    """Проверяет, что автор сообщения — админ/создатель группы."""
    if message.chat.type not in ("group", "supergroup"):
        return True

    if message.from_user is None:
        # Анонимный админ / сообщение от имени канала.
        return True

    try:
        member = await message.bot.get_chat_member(
            message.chat.id, message.from_user.id
        )
        return member.status in ("creator", "administrator")
    except Exception:
        logger.exception("Не удалось проверить права администратора")
        return False


def _configured_admin_ids() -> set[int]:
    values = set()
    for raw in os.getenv("ADMIN_IDS", "").split(","):
        raw = raw.strip()
        if raw:
            try:
                values.add(int(raw))
            except ValueError:
                continue
    return values


async def _is_administrator(message: Message) -> bool:
    user_id = getattr(message.from_user, "id", None)
    if user_id in _configured_admin_ids():
        return True
    if message.chat.type in ("group", "supergroup"):
        return await _is_group_admin(message)
    return False


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    """Административное снятие внутреннего blacklist."""
    if not await _is_administrator(message):
        await message.answer("⛔ Команда доступна только администраторам.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /unban USER_ID")
        return
    try:
        target_id = int(parts[1].strip())
    except ValueError:
        await message.answer("USER_ID должен быть числом.")
        return
    removed = remove_from_blacklist(target_id)
    await message.answer(
        "✅ Пользователь разблокирован." if removed
        else "Пользователь не найден во внутреннем blacklist."
    )


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    chat_id = message.chat.id
    text = (
        f"👋 Привет!\n\n"
        f"Я бот <b>{BOT_NAME}</b> для группы <b>{GROUP_NAME}</b>.\n\n"
        f"Доступные действия:\n"
        f"📅 <b>Сегодня</b> — /today\n"
        f"📅 <b>Завтра</b> — /schedule\n"
        f"✍️ <b>Текстом</b> — просто напиши «расписание»\n"
        f"    или «расписание на 4 сентября»\n"
        f"👨‍🏫 <b>Преподаватель</b> — «расписание преподавателя Фамилия»\n"
        f"🔎 <b>Поиск по дате</b> — /date 04.09.2026 или /date сегодня\n"
        f"🔔 <b>Уведомления</b> — /subscribe\n"
        f"🔕 <b>Отключить уведомления</b> — /unsubscribe\n"
        f"ℹ️ <b>Статус</b> — /status\n"
        f"🆘 <b>Помощь</b> — /help\n\n"
        f"🔔 Подписчики автоматически получают обновлённое "
        f"расписание при его изменении.\n"
        f"👥 Меня можно добавить в группу — админ включает "
        f"рассылку в чат командой /subscribe."
    )
    await message.answer(text, reply_markup=main_keyboard(_is_subscribed(chat_id)))


@dp.message(Command("today"))
async def cmd_today(message: Message):
    """Показывает расписание только на сегодня."""
    await _handle_today(message)


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):
    await _handle_schedule(message)


@dp.message(Command("date"))
async def cmd_date(message: Message):
    """Поиск расписания по дате: /date 04.09.2026"""
    parts = (message.text or "").split(maxsplit=1)
    argument = parts[1] if len(parts) > 1 else ""

    if not argument.strip():
        await message.answer(
            "🔎 <b>Поиск расписания по дате</b>\n\n"
            "Использование: <code>/date ДАТА</code>\n\n"
            "Примеры:\n"
            "• <code>/date 04.09.2026</code>\n"
            "• <code>/date 2026-09-04</code>\n"
            "• <code>/date 4 сентября</code>\n"
            "• <code>/date понедельник</code>\n"
            "• <code>/date завтра</code>"
        )
        return

    target = parse_user_date(argument)

    if target is None:
        await message.answer(
            "❌ Не понял дату.\n\n"
            "Попробуй так: <code>/date 04.09.2026</code>, "
            "<code>/date 4 сентября</code> или <code>/date завтра</code>."
        )
        return

    await _handle_date(message, target)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    chat = message.chat
    is_group = chat.type in ("group", "supergroup")

    if is_group and not await _is_group_admin(message):
        await message.answer(
            "⛔ Подписывать группу на расписание могут только администраторы чата."
        )
        return

    title = chat.title or chat.full_name or ""

    if subscribe_user(chat.id, chat_type=chat.type, title=title):
        where = "Этот чат" if is_group else "Вы"
        await message.answer(
            f"🔔 <b>Уведомления включены.</b>\n\n"
            f"{where} будет получать обновлённое расписание "
            f"при его изменении.\n\n"
            f"Отключить: /unsubscribe"
        )
    else:
        await message.answer(
            "🔔 Этот чат уже подписан на уведомления."
            if is_group
            else "🔔 Вы уже подписаны на уведомления."
        )


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    chat = message.chat
    is_group = chat.type in ("group", "supergroup")

    if is_group and not await _is_group_admin(message):
        await message.answer(
            "⛔ Отписывать группу могут только администраторы чата."
        )
        return

    if unsubscribe_user(chat.id):
        await message.answer("🔕 <b>Уведомления отключены.</b>")
    else:
        await message.answer(
            "Этот чат не был подписан на уведомления."
            if is_group
            else "Вы не были подписаны на уведомления."
        )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(await _status_text(message.chat.id))


# ============================================================
# ТЕКСТОВАЯ КОМАНДА «РАСПИСАНИЕ»
# ============================================================

SCHEDULE_TEXT_HELP = (
    "Не удалось определить дату.\n\n"
    "Примеры:\n"
    "• расписание\n"
    "• расписание на сегодня\n"
    "• расписание на завтра\n"
    "• расписание на 4 сентября\n"
    "• расписание на 04.09.2026"
)


@dp.message(F.text)
async def cmd_text_schedule(message: Message):
    """«расписание» / «расписание на <дата>» — без слэша.

    «расписание» -> завтра; дата разбирается parse_schedule_text и
    передаётся в ту же строгую функцию _handle_date/get_schedule,
    что и у команд с «/». Никакого fallback на другую дату.
    """
    request = parse_schedule_text(message.text or "")

    if not request.matched:
        # Сообщение не про расписание — молчим.
        return

    if request.schedule_type == "staff":
        await _handle_staff_request(message, request)
        return

    if request.error:
        await message.answer(SCHEDULE_TEXT_HELP)
        return

    target = request.date or get_tomorrow()
    await _handle_date(message, target)


@dp.my_chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    """Приветствие при добавлении в группу и автоочистка при удалении."""
    chat = event.chat

    if chat.type not in ("group", "supergroup"):
        return

    new_status = event.new_chat_member.status

    if new_status in ("member", "administrator"):
        await event.bot.send_message(
            chat.id,
            f"👋 Привет! Я бот <b>{BOT_NAME}</b> для группы <b>{GROUP_NAME}</b>.\n\n"
            f"• /today — расписание на сегодня\n"
            f"• /schedule — расписание на завтра\n"
            f"• Напиши «расписание» или «расписание на дату» — без слэша\n"
            f"• /date ДАТА — поиск по дате (например /date сегодня)\n"
            f"• /subscribe — присылать расписание в этот чат "
            f"при изменениях\n"
            f"• /unsubscribe — отключить\n\n"
            f"ℹ️ Подписать чат может администратор командой /subscribe.",
        )
    elif new_status in ("left", "kicked"):
        unsubscribe_user(chat.id)
        logger.info("Бот удалён из чата %s, подписка снята.", chat.id)


# ============================================================
# INLINE-КНОПКИ
# ============================================================


def _parse_staff_callback(data: str, prefix: str = "staff"):
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        staff_id = int(parts[1])
        target_day = date.fromisoformat(parts[2])
    except (TypeError, ValueError):
        return None
    if staff_id not in STAFF_BY_ID:
        return None
    return STAFF_BY_ID[staff_id], target_day


@dp.callback_query(F.data.startswith("staffpick:"))
async def cb_staff_pick(callback: CallbackQuery):
    parsed = _parse_staff_callback(callback.data or "", prefix="staffpick")
    if parsed is None:
        await callback.answer("Некорректный преподаватель", show_alert=True)
        return
    member, target_day = parsed
    await callback.answer()
    await _send_staff_schedule(callback, member, target_day, edit_existing=False)


@dp.callback_query(F.data.startswith("staff:"))
async def cb_staff_navigation(callback: CallbackQuery):
    parsed = _parse_staff_callback(callback.data or "", prefix="staff")
    if parsed is None:
        await callback.answer("Некорректная дата", show_alert=True)
        return
    member, target_day = parsed
    await callback.answer()
    await _send_staff_schedule(callback, member, target_day, edit_existing=True)


@dp.callback_query(F.data == "schedule")
async def cb_schedule(callback: CallbackQuery):
    """Кнопка «Завтра» = команда /schedule (только завтра)."""
    await callback.answer()
    await _handle_schedule(callback)


@dp.callback_query(F.data.in_(["today", "tomorrow"]))
async def cb_legacy_days(callback: CallbackQuery):
    """Кнопки «Сегодня»/«Завтра» из ранее отправленных сообщений.

    «today» -> /today (только сегодня), «tomorrow» -> /schedule
    (только завтра). Никакого fallback.
    """
    await callback.answer()
    if callback.data == "today":
        await _handle_today(callback)
    else:
        await _handle_schedule(callback)


@dp.callback_query(F.data == "subscribe")
async def cb_subscribe(callback: CallbackQuery):
    await callback.answer()
    chat = callback.message.chat
    if subscribe_user(
        chat.id, chat_type=chat.type, title=chat.title or ""
    ):
        await callback.message.answer("🔔 <b>Уведомления включены.</b>")
    else:
        await callback.message.answer("🔔 Этот чат уже подписан.")


@dp.callback_query(F.data == "unsubscribe")
async def cb_unsubscribe(callback: CallbackQuery):
    await callback.answer()
    if unsubscribe_user(callback.message.chat.id):
        await callback.message.answer("🔕 <b>Уведомления отключены.</b>")
    else:
        await callback.message.answer("Этот чат не был подписан.")


@dp.callback_query(F.data == "status")
async def cb_status(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(await _status_text(callback.message.chat.id))


@dp.callback_query(F.data == "date")
async def cb_date(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🔎 <b>Поиск расписания по дате</b>\n\n"
        "Отправь команду <code>/date ДАТА</code>.\n\n"
        "Примеры:\n"
        "• <code>/date 04.09.2026</code>\n"
        "• <code>/date 2026-09-04</code>\n"
        "• <code>/date 4 сентября</code>\n"
        "• <code>/date понедельник</code>\n"
        "• <code>/date завтра</code>"
    )


@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f"🆘 <b>Помощь</b>\n\n"
        f"Я показываю расписание группы <b>{GROUP_NAME}</b>.\n\n"
        f"• /today — расписание только на сегодня\n"
        f"• /schedule — расписание только на завтра\n"
        f"• Просто напиши «расписание» или «расписание на дату» "
        f"(без слэша)\n"
        f"• Для преподавателя: «расписание преподавателя Фамилия» "
        f"или «расписание Фамилия на сегодня»\n"
        f"• /date ДАТА — расписание на любую дату "
        f"(например <code>/date сегодня</code>)\n"
        f"• /subscribe — уведомления об изменениях\n"
        f"• /unsubscribe — отключить уведомления\n"
        f"• /status — статус чата\n\n"
        f"Примеры /date: <code>сегодня</code>, <code>04.09.2026</code>, "
        f"<code>2026-09-04</code>, <code>4 сентября</code>, "
        f"<code>понедельник</code>."
    )


# ============================================================
# СКРЫТАЯ ЕЖЕДНЕВНАЯ ПРОВЕРКА ДНЕЙ РОЖДЕНИЯ
# ============================================================

@dataclass(frozen=True)
class BirthdayPerson:
    name: str
    group: str = GROUP_NAME


_BIRTHDAY_SECTION_TITLE = "поздравляем с днем рождения!"
_BIRTHDAY_GROUP_RE = re.compile(
    r"(?:\(\s*)?(?:гр\.?\s*)?эс\s*7\s*[-–—]\s*24\b\s*(?:\))?",
    re.IGNORECASE,
)


def _is_target_birthday_group(text: str, group_name: str = GROUP_NAME) -> bool:
    normalized = clean_text(text).casefold().replace("ё", "е")
    target = clean_text(group_name).casefold().replace("ё", "е")
    # Для основной группы допускаем небольшие пробелы/скобки, но не
    # подменяем ЭС7-24 другими группами.
    if target == "эс7-24":
        return bool(_BIRTHDAY_GROUP_RE.search(normalized))
    escaped = re.escape(target).replace(r"\-", r"\s*[-–—]\s*")
    return bool(re.search(rf"(?:гр\.?\s*)?{escaped}(?!\d)", normalized))


def parse_birthdays(html: str, group_name: str = GROUP_NAME) -> list[BirthdayPerson]:
    """Извлекает только людей после заголовка «Поздравляем…».

    Блок «Готовимся поздравлять…» намеренно не рассматривается: обход
    останавливается на первом следующем h4.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    card = soup.find(id="happyCard")
    if card is None:
        return []

    heading = None
    for item in card.find_all("h4"):
        text = clean_text(item.get_text(" ", strip=True)).casefold().replace("ё", "е")
        if text == _BIRTHDAY_SECTION_TITLE:
            heading = item
            break
    if heading is None:
        return []

    result: list[BirthdayPerson] = []
    seen = set()
    for node in heading.next_elements:
        if getattr(node, "name", None) == "h4":
            break
        if getattr(node, "name", None) not in ("span", "a", "li"):
            continue
        if node.find_parent(id="happyCard") is not card and node is not card:
            continue
        visible = clean_text(node.get_text(" ", strip=True))
        raw_group_text = " ".join(
            part for part in (visible, clean_text(node.get("title", ""))) if part
        )
        if not _is_target_birthday_group(raw_group_text, group_name):
            continue
        name = clean_text(node.get("title", ""))
        if not name:
            name = re.sub(
                r"\(?\s*гр\.?\s*[А-ЯЁA-Z0-9]+\s*[-–—]\s*\d+\s*\)?",
                "",
                visible,
                flags=re.IGNORECASE,
            )
            name = clean_text(name).strip("-—,;:")
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(BirthdayPerson(name=name, group=group_name))
    return result


extract_birthdays = parse_birthdays


def birthday_message(people: list[BirthdayPerson]) -> str:
    if not people:
        return ""
    if len(people) == 1:
        return (
            "🎉 Сегодня день рождения!\n"
            f"Поздравляем {people[0].name}! 🎂\n"
            "Желаем отличного настроения, успехов в учёбе и всего самого лучшего! 🥳"
        )
    lines = ["🎉 Сегодня день рождения!", "Сегодня поздравляем:"]
    lines.extend(f"🎂 {person.name}" for person in people)
    lines.append("С днём рождения! Желаем отличного настроения, успехов и всего самого лучшего! 🥳")
    return "\n".join(lines)


def birthday_notification_sent(day: date, group_name: str = GROUP_NAME) -> bool:
    try:
        with db_connect() as conn:
            return conn.execute(
                "SELECT 1 FROM birthday_notifications WHERE date = ? AND group_name = ?",
                (day.isoformat(), group_name),
            ).fetchone() is not None
    except Exception:
        logger.exception("Ошибка чтения marker поздравления")
        return False


def record_birthday_notification(
    day: date, group_name: str, people: list[BirthdayPerson]
) -> bool:
    try:
        with db_connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO birthday_notifications "
                "(date, group_name, sent_at, people) VALUES (?, ?, ?, ?)",
                (
                    day.isoformat(), group_name,
                    now_local().strftime("%Y-%m-%d %H:%M:%S"),
                    json.dumps([person.name for person in people], ensure_ascii=False),
                ),
            )
            return cursor.rowcount > 0
    except Exception:
        logger.exception("Ошибка сохранения marker поздравления")
        return False


def _birthday_destination() -> Optional[int]:
    if BIRTHDAY_CHAT_ID is not None:
        return BIRTHDAY_CHAT_ID
    # Если отдельный ID не задан, используем уже зарегистрированный
    # Telegram-групповой чат с названием основной группы.
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT user_id, title FROM subscribers WHERE chat_type IN ('group', 'supergroup')"
            ).fetchall()
        target = GROUP_NAME.casefold()
        for row in rows:
            title = clean_text(row["title"] or "").casefold()
            if title == target or target in title:
                return int(row["user_id"])
    except Exception:
        logger.exception("Ошибка определения чата для поздравления")
    return None


_BIRTHDAY_LOCK = asyncio.Lock()


async def _check_birthdays_locked(bot: Bot, day: Optional[date] = None) -> Optional[bool]:
    """Проверяет поздравления один раз в календарную дату Екатеринбурга.

    ``True`` — marker записан после успешной отправки, ``False`` — страница
    успешно проверена, но именинников нет/marker уже есть, ``None`` — ошибка
    источника или Telegram, поэтому следующая проверка может повторить попытку.
    """
    target_day = day or get_today()
    if birthday_notification_sent(target_day, GROUP_NAME):
        return False
    html = await fetch_birthday_html()
    if html is None:
        return None
    people = parse_birthdays(html, GROUP_NAME)
    if not people:
        return False
    destination = _birthday_destination()
    if destination is None:
        logger.warning("Не задан Telegram-чат для скрытого поздравления %s", GROUP_NAME)
        return None
    try:
        await bot.send_message(destination, birthday_message(people))
    except Exception:
        # Marker намеренно НЕ создаётся: следующая проверка повторит попытку.
        logger.exception("Ошибка отправки поздравления")
        return None
    return True if record_birthday_notification(target_day, GROUP_NAME, people) else None


async def check_birthdays(bot: Bot, day: Optional[date] = None) -> Optional[bool]:
    async with _BIRTHDAY_LOCK:
        return await _check_birthdays_locked(bot, day)


check_birthday = check_birthdays


# ============================================================
# ANTI-SPAM / BLACKLIST
# ============================================================

@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    warning: bool = False
    blacklisted: bool = False


def is_blacklisted(user_id: int) -> bool:
    try:
        with db_connect() as conn:
            return conn.execute(
                "SELECT 1 FROM blacklist WHERE user_id = ?", (int(user_id),)
            ).fetchone() is not None
    except Exception:
        # При проблеме чтения не блокируем всех пользователей, но пишем лог.
        logger.exception("Ошибка проверки blacklist")
        return False


def add_to_blacklist(user_id: int, reason: str = "rate_limit") -> bool:
    try:
        with db_connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO blacklist (user_id, added_at, reason) VALUES (?, ?, ?)",
                (int(user_id), now_local().strftime("%Y-%m-%d %H:%M:%S"), clean_text(reason)),
            )
            return cursor.rowcount > 0
    except Exception:
        logger.exception("Ошибка добавления в blacklist")
        return False


blacklist_user = add_to_blacklist
check_blacklist = is_blacklisted


def remove_from_blacklist(user_id: int) -> bool:
    try:
        with db_connect() as conn:
            cursor = conn.execute("DELETE FROM blacklist WHERE user_id = ?", (int(user_id),))
            return cursor.rowcount > 0
    except Exception:
        logger.exception("Ошибка снятия blacklist")
        return False


unblacklist_user = remove_from_blacklist


def check_rate_limit(
    user_id: int,
    current: Optional[datetime] = None,
) -> RateLimitDecision:
    """Атомарный sliding-window limiter.

    Решение и обновление счётчиков происходят в одной BEGIN IMMEDIATE
    транзакции, поэтому параллельные update не видят устаревшее состояние.
    Первый выход за лимит только предупреждает; следующий устойчивый шквал
    добавляет пользователя в сохраняемый blacklist.
    """
    user_id = int(user_id)
    timestamp = (
        _local_aware(current).timestamp() if current is not None else time.time()
    )
    cutoff = timestamp - max(1, RATE_LIMIT_WINDOW_SECONDS)
    try:
        with db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,)
            ).fetchone() is not None:
                conn.commit()
                return RateLimitDecision(False, blacklisted=True)

            row = conn.execute(
                "SELECT events_json, warning_count, last_warning_at "
                "FROM rate_limit_state WHERE user_id = ?", (user_id,)
            ).fetchone()
            events = []
            warnings = 0
            last_warning = 0.0
            if row:
                try:
                    events = [float(value) for value in json.loads(row["events_json"] or "[]")]
                except (TypeError, ValueError, json.JSONDecodeError):
                    events = []
                warnings = int(row["warning_count"] or 0)
                last_warning = float(row["last_warning_at"] or 0)
            events = [value for value in events if value > cutoff]
            had_recent_events = bool(events)
            events.append(timestamp)

            if len(events) <= max(1, RATE_LIMIT_MAX_REQUESTS):
                # После спокойного окна старое предупреждение не превращает
                # новый обычный всплеск в мгновенный бан.
                if not had_recent_events:
                    warnings = 0
                conn.execute(
                    "INSERT OR REPLACE INTO rate_limit_state "
                    "(user_id, events_json, warning_count, last_warning_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, json.dumps(events), warnings, last_warning,
                     now_local().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                return RateLimitDecision(True)

            warnings += 1
            if warnings >= max(1, RATE_LIMIT_BLACKLIST_AFTER):
                conn.execute(
                    "INSERT OR IGNORE INTO blacklist (user_id, added_at, reason) VALUES (?, ?, ?)",
                    (user_id, now_local().strftime("%Y-%m-%d %H:%M:%S"), "rate_limit"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO rate_limit_state "
                    "(user_id, events_json, warning_count, last_warning_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, json.dumps(events), warnings, timestamp,
                     now_local().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                return RateLimitDecision(False, blacklisted=True)

            should_warn = (
                last_warning <= 0
                or timestamp - last_warning >= RATE_LIMIT_WARNING_COOLDOWN
            )
            conn.execute(
                "INSERT OR REPLACE INTO rate_limit_state "
                "(user_id, events_json, warning_count, last_warning_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, json.dumps(events), warnings,
                 timestamp if should_warn else last_warning,
                 now_local().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            return RateLimitDecision(False, warning=should_warn)
    except sqlite3.OperationalError:
        logger.exception("SQLite busy/error in rate limiter")
        # При ошибке БД безопаснее не запускать тяжёлую операцию.
        return RateLimitDecision(False, warning=True)
    except Exception:
        logger.exception("Ошибка rate limiter")
        return RateLimitDecision(False, warning=True)


rate_limit_check = check_rate_limit


# ============================================================
# МОНИТОРИНГ ИЗМЕНЕНИЙ
# ============================================================

def _change_title_text(change) -> str:
    pair = clean_text(change.pair).upper()
    subgroup = clean_text(change.subgroup) or None
    label = f"{pair} пара"
    if subgroup:
        label += f" • {subgroup} п/гр."
    return label


def _format_change_text(change) -> str:
    """Короткий текст изменения для caption/уведомления."""
    label = _change_title_text(change)
    if change.kind == "added":
        new = change.new or {}
        return (
            f"🟢 <b>Добавлено:</b> {label}\n"
            f"{clean_text(new.get('subject') or 'Предмет не указан')}"
            + (
                f", {clean_text(new.get('room') or '—')}"
                if new.get("room")
                else ""
            )
        )
    if change.kind == "removed":
        old = change.old or {}
        return (
            f"🔴 <b>Удалено:</b> {label}\n"
            f"{clean_text(old.get('subject') or 'Предмет не указан')}"
            + (
                f", {clean_text(old.get('room') or '—')}"
                if old.get("room")
                else ""
            )
        )

    lines = [f"🟡 <b>Изменено:</b> {label}"]
    for detail in change.details[:8]:
        field_label = detail.get("label") or detail.get("field", "")
        old_val = clean_text(detail.get("old", ""))
        new_val = clean_text(detail.get("new", ""))
        lines.append(
            f"• {field_label}: {old_val or '—'} → {new_val or '—'}"
        )
    return "\n".join(lines)


def _changes_text_summary(changes: list, max_char: int = 800) -> str:
    if not changes:
        return ""
    parts = []
    total_chars = 0
    for change in changes[:12]:
        text = _format_change_text(change)
        if total_chars + len(text) > max_char:
            break
        parts.append(text)
        total_chars += len(text)
    if len(changes) > len(parts):
        parts.append(
            f"… и ещё {len(changes) - len(parts)} изменений"
        )
    return "\n\n".join(parts)


def _notification_caption(
    schedule: Schedule,
    day: date,
    first_time: bool,
    changes=None,
) -> str:
    """Подпись уведомления — всегда с явным указанием дня.

    Пример: «📅 Сегодня, 6 сентября 2026» или «📅 Завтра, 7 сентября 2026».
    """
    label = day_label_for(day)
    if label in ("Сегодня", "Завтра"):
        # Сохраняем короткий относительный маркер и полный календарный день:
        # уведомление однозначно читается и до, и после полуночи.
        day_str = f"{label}, {format_date_header(day)} {day.year}"
    else:
        day_str = label  # полный заголовок, без дублирования
    if first_time:
        action = "🆕 <b>Расписание опубликовано!</b>"
    else:
        action = "🔄 <b>Расписание изменилось!</b>"

    lines = [
        action,
        "",
        f"📅 {day_str}",
        f"👥 Группа: <b>{schedule.group}</b>",
        f"🕐 Занятий: {count_lessons(schedule)}",
    ]

    if not first_time and changes:
        summary = _changes_text_summary(changes)
        if summary:
            lines.extend(["", "Что изменилось:", summary])

    return "\n".join(lines)[:1000]


async def _notify_changed(
    bot: Bot,
    schedule: Schedule,
    day: date,
    signature: str,
    first_time: bool,
    changes=None,
) -> int:
    """Отправляет актуальное расписание ТЕМ, кто его ещё не получил.

    Возвращает (доставлено, не_доставлено).
    У каждой пары «подписчик + дата» хранится последний доставленный
    hash, поэтому повторных уведомлений не будет, а сбой у одного
    получателя не считается доставкой.
    """
    subscribers = load_subscribers()
    if not subscribers:
        return 0, 0

    date_key = day.isoformat()
    notifications = load_schedule_notifications().get(date_key, {})
    pending = [
        user_id
        for user_id in subscribers
        if notifications.get(user_id) != signature
    ]
    if not pending:
        logger.info(
            "Все подписчики уже получили %s (%s) — пропускаем.",
            date_key,
            signature[:12],
        )
        return 0, 0

    changes = changes or []
    image_path = render_schedule_image(
        schedule,
        changes=changes,
        title="РАСПИСАНИЕ ИЗМЕНИЛОСЬ" if not first_time else "РАСПИСАНИЕ ОПУБЛИКОВАНО",
    )
    caption = _notification_caption(
        schedule, day, first_time, changes
    )
    delivered = 0
    still_pending = 0  # получатели, которым сообщение реально не ушло

    try:
        for user_id in pending:
            try:
                await bot.send_photo(
                    user_id,
                    photo=FSInputFile(image_path),
                    caption=caption,
                )
                record_schedule_notification(user_id, date_key, signature)
                delivered += 1
            except Exception as error:
                text = str(error).lower()
                if any(
                    marker in text
                    for marker in (
                        "bot was blocked",
                        "bot was kicked",
                        "chat not found",
                        "user is deactivated",
                        "group chat was upgraded",
                    )
                ):
                    # Чат недоступен — подписка снимается, доставка
                    # ему больше не нужна.
                    unsubscribe_user(user_id)
                    logger.info(
                        "Чат %s недоступен — подписка снята.", user_id
                    )
                else:
                    still_pending += 1
                    logger.warning(
                        "Не удалось уведомить %s: %s", user_id, error
                    )
    finally:
        try:
            image_path.unlink(missing_ok=True)
        except Exception:
            pass

    logger.info(
        "Уведомление %s доставлено: %s из %s.",
        date_key,
        delivered,
        len(pending),
    )
    return delivered, still_pending


async def _check_date(bot: Bot, day: date) -> None:
    """Проверка расписания на КОНКРЕТНУЮ дату.

    Сценарии:
    - расписания нет (пусто)     -> состояние не меняем, ничего не шлём;
    - ошибка источника           -> состояние не меняем, ничего не шлём;
    - расписание появилось впервые -> уведомление «опубликовано»;
    - расписание изменилось      -> уведомление «изменилось»;
    - совпадает с последним      -> ничего не шлём;
    - доставка не удалась        -> состояние не обновляется,
                                    повторим в следующем цикле.
    """
    date_key = day.isoformat()

    if is_day_off(day):
        logger.info("Проверка %s: воскресенье, пропускаем.", date_key)
        return

    subscribers = load_subscribers()
    try:
        schedule = await get_schedule(day)
    except ScheduleUnavailable:
        # Ошибка загрузки/парсинга — не считается изменением и также не
        # завершает backfill/историю.
        logger.warning(
            "Проверка %s: источник недоступен — изменением не считаем.",
            date_key,
        )
        return

    # Накопление не зависит от наличия подписчиков. Предметы регистрируются
    # при первом появлении, завершённые пары — только после своего конца.
    if schedule.schedule_type == "group":
        register_subjects_from_schedule(schedule)
        record_completed_lessons(schedule)

    if not subscribers:
        # Некому отправлять — baseline не фиксируем, чтобы первый подписавшийся
        # получил уведомление о текущем опубликованном расписании.
        logger.info("Проверка %s: подписчиков нет, уведомление пропускаем.", date_key)
        return

    if not schedule.lessons:
        # Отсутствие расписания — тоже не «новая версия».
        logger.info(
            "Проверка %s: расписание отсутствует — состояние не меняем.",
            date_key,
        )
        return

    signature = schedule_signature(schedule)
    logger.info("Hash расписания %s: %s", date_key, signature)

    state = load_state()
    old_state = state.get(date_key)
    old_hash = old_state.get("hash") if old_state else None

    if old_hash == signature:
        logger.info("Изменений нет: %s", date_key)
        return

    first_time = old_state is None
    changes = []
    if not first_time:
        old_data = old_state.get("data") if old_state else None
        if old_data is None:
            # Старая база без сохранённого data: не выдумываем «было пусто
            # -> стало всё добавлено». Одноразово сообщим об обновлении.
            logger.info(
                "Проверка %s: старое data отсутствует — построчное сравнение "
                "не выполняется.",
                date_key,
            )
        else:
            old_schedule = schedule_from_storage(old_data, day)
            changes = compare_schedules(old_schedule, schedule)
            logger.info(
                "Найдено изменений %s: %s", date_key, len(changes)
            )

    logger.info(
        "%s: %s (%s)",
        "РАСПИСАНИЕ ПОЯВИЛОСЬ" if first_time else "РАСПИСАНИЕ ИЗМЕНИЛОСЬ",
        date_key,
        f"занятий: {count_lessons(schedule)}, изменений: {len(changes)}",
    )

    delivered, still_pending = await _notify_changed(
        bot,
        schedule,
        day,
        signature,
        first_time,
        changes=changes,
    )

    # Состояние фиксируем только когда расписание реально ушло всем
    # получателям (недоступные чаты снимаются с подписки и не считаются).
    # Если хоть один получатель не получил уведомление — состояние не
    # обновляется, и в следующем цикле отправка повторится только ему.
    if still_pending == 0:
        state[date_key] = {
            "hash": signature,
            "data": normalize_schedule(schedule),
        }
        save_state(state)
        logger.info(
            "Состояние %s обновлено (доставлено: %s).", date_key, delivered
        )
    else:
        logger.warning(
            "Уведомление %s не доставлено %s получателям — состояние "
            "не обновлено, повторим в следующем цикле (доставлено: %s).",
            date_key,
            still_pending,
            delivered,
        )


# ============================================================
# CHANGELOG
# ============================================================

_changelog_warned = set()


async def _deliver_changelog(bot: Bot, user_id: int, version: str) -> bool:
    """Отправляет changelog. True — только при реальной доставке."""
    text = changelog_text(version)
    try:
        await bot.send_message(user_id, text)
        return True
    except Exception as error:
        error_text = str(error).lower()
        if any(
            marker in error_text
            for marker in (
                "bot was blocked",
                "bot was kicked",
                "chat not found",
                "user is deactivated",
                "group chat was upgraded",
            )
        ):
            unsubscribe_user(user_id)
            logger.info(
                "Чат %s недоступен — подписка снята.", user_id
            )
        else:
            logger.warning(
                "Не удалось отправить changelog %s в %s: %s",
                version,
                user_id,
                error,
            )
        return False


async def _process_changelog(bot: Bot) -> None:
    """Рассылка changelog. Вызывается в каждом цикле мониторинга.

    Правила (для каждой версии отдельно):
    - changelog получают только пользователи, существовавшие ДО релиза
      версии (created_at < released_at);
    - новые пользователи (created_at >= released_at) версию не получают;
    - после успешной отправки last_notified_version обновляется в БД,
      поэтому перезапуск повторно ничего не шлёт;
    - при ошибке отправки версия НЕ помечается доставленной —
      попытка повторится в следующем цикле;
    - если для версии не задан released_at — она не рассылается.
    """
    rows = load_subscriber_rows()
    sent = 0

    for row in rows:
        user_id = int(row["user_id"])
        last = row["last_notified_version"] or ""

        for version in pending_versions(last):
            released = get_released_at(version)
            if released is None:
                if version not in _changelog_warned:
                    _changelog_warned.add(version)
                    logger.warning(
                        "Для версии %s не задан released_at (%s) — "
                        "changelog не рассылается.",
                        version,
                        CHANGELOG.get(version, {}).get(
                            "released_at_env", ""
                        ),
                    )
                # Версия ещё не выпущена — не считаем пропуском.
                continue

            created = parse_db_datetime(row["created_at"])
            if created is None:
                created = parse_db_datetime("1970-01-01 00:00:00")

            # НЕЛЬЗЯ просто «last != текущая версия -> отправить»:
            # новые пользователи созданы после релиза и старый
            # changelog не получают.
            if created >= released:
                logger.info(
                    "Changelog %s: пользователь %s создан после релиза "
                    "(%s >= %s) — пропускаем.",
                    version,
                    user_id,
                    created,
                    released,
                )
                continue

            ok = await _deliver_changelog(bot, user_id, version)
            if ok:
                mark_changelog_notified(user_id, version)
                sent += 1
            else:
                # Не помечаем: в следующем цикле повторим. Старшие
                # версии не обгоняем — сохраняем порядок.
                break

    if sent:
        logger.info("Changelog отправлен: %s сообщения(й).", sent)


async def schedule_monitor(bot: Bot) -> None:
    """Фоновая задача. Не блокирует polling, одна на процесс.

    Каждые 5 минут даты сегодня/завтра пересчитываются заново
    (переход через полночь обрабатывается автоматически), обе даты
    проверяются независимо, затем рассылается changelog.
    """
    logger.info(
        "Мониторинг запущен. Интервал: %s сек (%s мин). Версия: %s.",
        CHECK_INTERVAL,
        CHECK_INTERVAL // 60,
        BOT_VERSION,
    )

    birthday_checked_dates: set[str] = set()
    while True:
        # История запускается в существующем scheduler, а не во втором
        # независимом фоне. Уже обработанные даты пропускаются по БД.
        try:
            await backfill_lesson_history()
        except Exception:
            logger.exception("Ошибка backfill истории занятий")

        # Каждый цикл даты вычисляются заново через Asia/Yekaterinburg.
        today = get_today()
        for day, label in (
            (today, "сегодня"),
            (get_tomorrow(), "завтра"),
        ):
            try:
                await _check_date(bot, day)
            except Exception:
                logger.exception(
                    "Ошибка проверки расписания на %s (%s)",
                    label,
                    day.isoformat(),
                )

        if today.isoformat() not in birthday_checked_dates:
            try:
                birthday_result = await check_birthdays(bot, today)
                if birthday_result is not None:
                    birthday_checked_dates.add(today.isoformat())
            except Exception:
                logger.exception("Ошибка скрытой ежедневной проверки")

        try:
            await _process_changelog(bot)
        except Exception:
            logger.exception("Ошибка рассылки changelog")

        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================
# MAIN
# ============================================================

COMMANDS = [
    BotCommand(command="today", description="Расписание на сегодня"),
    BotCommand(command="schedule", description="Расписание на завтра"),
    BotCommand(command="date", description="Поиск расписания по дате"),
    BotCommand(command="subscribe", description="Включить уведомления"),
    BotCommand(command="unsubscribe", description="Отключить уведомления"),
    BotCommand(command="status", description="Статус подписки"),
    BotCommand(command="help", description="Помощь"),
]


async def _setup_commands(bot: Bot) -> None:
    """Регистрирует меню команд в ЛС и в группах."""
    try:
        await bot.set_my_commands(
            COMMANDS, scope=BotCommandScopeAllPrivateChats()
        )
        await bot.set_my_commands(
            COMMANDS, scope=BotCommandScopeAllGroupChats()
        )
        logger.info("Меню команд зарегистрировано.")
    except Exception:
        logger.exception("Не удалось зарегистрировать меню команд")


async def main() -> None:
    logger.info("=" * 60)
    logger.info("Бот расписания группы %s (версия %s)", GROUP_NAME, BOT_VERSION)
    logger.info("Group ID: %s", GROUP_ID)
    logger.info("URL: %s", BASE_URL)
    logger.info("Check interval: %s сек (%s мин)", CHECK_INTERVAL, CHECK_INTERVAL // 60)
    logger.info("Timezone: %s (UTC+5)", TIMEZONE)

    if not BOT_TOKEN:
        logger.error(
            "BOT_TOKEN не задан. Добавь переменную BOT_TOKEN в .env "
            "или окружение."
        )
        return

    # Проверяем шрифты до старта бота.
    try:
        get_font(10)
        get_font(10, bold=True)
    except RuntimeError as error:
        logger.error("%s", error)
        return

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    await _setup_commands(bot)

    # Один фоновый монитор на процесс: повторный вызов main()
    # не создаёт второй scheduler.
    monitor_task = getattr(main, "_monitor_task", None)
    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(schedule_monitor(bot))
        main._monitor_task = monitor_task

    try:
        await dp.start_polling(bot)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
