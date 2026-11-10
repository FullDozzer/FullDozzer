# -*- coding: utf-8 -*-
"""
Telegram-бот расписания всех групп и преподавателей колледжа (ishnk.ru).

- Получает расписание напрямую по HTTP (aiohttp), БЕЗ браузера.
- Разбирает HTML через BeautifulSoup (только карточки div.card.myCard с .card-header).
- Игнорирует недельную таблицу.
- Генерирует современную PNG-картинку через Pillow: одна дата = одна
  вертикальная карточка (1080×1620, 2:3; при плотном дне холст растёт по
  лестнице форматов). Карточка пары: номер, время, предмет, преподаватель
  и лавандовая колонка аудитории («Ауд.» + крупный код комнаты);
  изменения и отмены показываются внутри карточки пары.
- Поддержка подписок (SQLite), фоновый мониторинг изменений.
- Работает в Docker, кодировка UTF-8.
- Все даты считаются в часовом поясе Asia/Yekaterinburg (UTC+5),
  локальное время сервера не используется.
- Расписание преподавателей использует единый справочник staff_directory.py.
- История учёбы: каждая подгруппа пары пишется отдельной строкой со своим
  предметом, но в общей сумме группы пара считается один раз.
- Прогноз «Изучено: X / Y акад. ч»: время считается академическими
  часами (1 акад. ч = 40 мин), X — фактическое время с 1 сентября,
  Y — экстраполяция темпа до 30 июня (R = X * Dr / De); обыкновенное
  время приводится в серой курсивной сноске внизу картинки.
- /status — PNG-карточка состояния бота в дизайне расписания
  (подписки, прогресс учёбы, текущее потребление ресурсов).

"""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
from functools import lru_cache
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import schedule_icons

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.filters.command import CommandException
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
    LabeledPrice,
    Message,
)
from college import (
    FALLBACK_GROUPS,
    GroupInfo,
    available_courses,
    available_specialties,
    group_specialty,
    groups_from_payload,
    groups_to_payload,
    parse_groups_payload,
    sorted_groups,
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

# ============================================================
# НАСТРОЙКИ
# ============================================================

load_dotenv()

# Брендинг. GROUP_* оставлены для миграции старых баз и совместимости.
BOT_NAME = "ИНК · Расписание"
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
RATE_LIMIT_WARNING_COOLDOWN = int(
    os.getenv("RATE_LIMIT_WARNING_COOLDOWN", "30")
)
SPAM_WARNING_TEXT = "⚠️ Слишком много запросов подряд.\nПожалуйста, немного подождите."

# Период автоматической проверки (секунды). 5 минут = 300
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))

# Таймаут HTTP-запроса
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))
GROUPS_URL = os.getenv("GROUPS_URL", "http://ishnk.ru/2025/json/groups")
GROUP_CATALOG_TTL = int(os.getenv("GROUP_CATALOG_TTL", "21600"))
MONITOR_CONCURRENCY = max(1, int(os.getenv("MONITOR_CONCURRENCY", "8")))
BROADCAST_PRICE = 50
BROADCAST_AD_PRICE = 150
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()
ADMIN_IDS = {
    int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().lstrip("-").isdigit()
}
PREMIUM_EMOJI_ID = os.getenv("PREMIUM_EMOJI_ID", "").strip()
# Telegram collectible/premium usernames не возвращаются методом getMe как
# основной username. Команды с этими упоминаниями принимаем явно и только
# для перечисленных алиасов, не перехватывая команды других ботов.
BOT_USERNAME_ALIASES = {
    value.strip().lstrip("@").casefold()
    for value in os.getenv("BOT_USERNAME_ALIASES", "mellowagent").split(",")
    if value.strip().lstrip("@")
}

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
    пустое и не меняет существующую обработку подгрупп.
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

GROUPS: dict[int, GroupInfo] = dict(FALLBACK_GROUPS)
_CACHE_LOCKS: dict[str, asyncio.Lock] = {}


def get_group(group_id: Optional[int] = None) -> GroupInfo:
    group_id = int(group_id or GROUP_ID)
    return GROUPS.get(group_id) or FALLBACK_GROUPS.get(group_id) or GroupInfo(
        id=group_id, name=f"Группа {group_id}", course=0, semester=0,
        study="Не указано", education="9", is_correspondence=False,
        has_schedule=True,
    )


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


def build_url(day: date, group_id: Optional[int] = None) -> str:
    selected_id = int(group_id or GROUP_ID)
    if selected_id == GROUP_ID and BASE_URL.endswith(f"/{GROUP_ID}"):
        return f"{BASE_URL}/{day.isoformat()}"
    base = BASE_URL.rsplit("/", 1)[0] if BASE_URL.rsplit("/", 1)[-1].isdigit() else BASE_URL
    return f"{base}/{selected_id}/{day.isoformat()}"


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


async def fetch_html(day: date, group_id: Optional[int] = None):
    group = get_group(group_id)
    return await fetch_url(
        build_url(day, group.id), f"расписания {group.name} на {day.isoformat()}"
    )


async def refresh_group_catalog(force: bool = False) -> int:
    """Обновляет все группы одним POST-запросом; при сбое сохраняет снимок."""
    global GROUPS
    if not force:
        try:
            updated = float(get_meta_value("groups_updated_at", "0"))
            if time.time() - updated < GROUP_CATALOG_TTL:
                return len(GROUPS)
        except ValueError:
            pass
    try:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                GROUPS_URL,
                data={"search": ""},
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                },
            ) as response:
                if response.status != 200:
                    raise ScheduleUnavailable(f"Каталог групп: HTTP {response.status}")
                payload = await response.json(content_type=None)
        fresh = parse_groups_payload(payload)
        if not fresh:
            raise ScheduleUnavailable("Каталог групп пуст")
        GROUPS = fresh
        set_meta_value("college_groups", json.dumps(groups_to_payload(fresh), ensure_ascii=False))
        set_meta_value("groups_updated_at", str(time.time()))
        return len(fresh)
    except Exception as error:
        logger.warning("Каталог групп не обновлён: %s", error)
        return len(GROUPS)


def restore_group_catalog() -> None:
    global GROUPS
    raw = get_meta_value("college_groups")
    if raw:
        try:
            saved = groups_from_payload(json.loads(raw))
            if saved:
                GROUPS = saved
        except (TypeError, ValueError):
            logger.warning("Повреждён локальный каталог групп, используется снимок")


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


_GROUP_LABEL_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё/])(?:гр\.|групп[аы]?(?![A-Za-zА-Яа-яЁё]))",
    re.IGNORECASE,
)
_GROUP_TOKEN_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9])([A-Za-zА-Яа-яЁё]{1,8}\s*\d{0,3}\s*[-–—]\s*\d{1,3})(?![A-Za-zА-Яа-яЁё0-9])",
    re.IGNORECASE,
)
_GROUP_STRICT_TOKEN_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9])([A-Za-zА-Яа-яЁё]{1,8}\s*\d{1,3}\s*[-–—]\s*\d{1,3})(?![A-Za-zА-Яа-яЁё0-9])",
    re.IGNORECASE,
)


def _normalize_group_code(raw: str) -> str:
    value = re.sub(r"\s*[-–—]\s*", "-", clean_text(raw)).strip(" .,:;")
    if re.fullmatch(
        r"[A-Za-zА-Яа-яЁё]{1,8}\s*\d{0,3}-\d{1,3}[A-Za-zА-Яа-яЁё]?", value
    ):
        value = re.sub(r"\s+", "", value)
    return value


def _append_group_tokens(values: list, seen: set, raw: str) -> bool:
    added = False
    for chunk in re.split(r"[,;]+", clean_text(raw)):
        value = _normalize_group_code(chunk)
        if not value or value in ("-", "—", "–"):
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            values.append(value)
        added = True
    return added


def _non_subject_text(node, room: str = "", subject: str = "") -> str:
    """Текст блока без предмета, аудитории и преподавателя для поиска групп."""
    parts = []
    excluded_classes = {"changesPair", "d-md-none", "Staff", "teacher", "staff"}
    for text_node in node.find_all(string=True):
        if not text_node:
            continue
        skip = False
        for ancestor in text_node.parents:
            if ancestor is node:
                break
            classes = set(ancestor.get("class") or [])
            if classes & excluded_classes or (
                "d-none" in classes and "d-md-block" in classes
            ):
                skip = True
                break
        if not skip:
            parts.append(str(text_node))
    text = clean_text(" ".join(parts))
    if room:
        text = text.replace(room, " ")
    if subject:
        text = text.replace(subject, " ")
    text = re.sub(r"ауд\.?\s*\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d*\s*п/гр\.?", " ", text, flags=re.IGNORECASE)
    return clean_text(text)


def _find_groups(
    node,
    *,
    schedule_type: str = "group",
    room: str = "",
    subject: str = "",
) -> str:
    """Группы на странице преподавателя, без вывода ID из HTML."""
    values: list = []
    seen: set = set()
    saw_explicit_label = False

    # Способ 1: явная разметка «гр.<span class="h5">СТ-26</span>» / «группа ...»
    # (не путаем с подгруппой «1 п/гр.»).
    for text_node in node.find_all(string=True):
        if not text_node:
            continue
        raw_str = str(text_node)
        match = _GROUP_LABEL_RE.search(raw_str)
        if not match:
            continue
        saw_explicit_label = True
        parent = text_node.parent
        found_here = False

        tail = clean_text(raw_str[match.end():]).lstrip(": ").strip()
        if tail:
            tail = re.split(
                r"\b(?:ауд\.|аудитори|\d*\s*п/гр)", tail, maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()
        if tail:
            if (
                parent is not None
                and parent.name in ("span", "b", "strong", "small", "em")
                and "ауд." not in parent.get_text(" ", strip=True).lower()
            ):
                found_here = _append_group_tokens(values, seen, tail) or found_here
            else:
                for token_match in _GROUP_TOKEN_RE.finditer(tail):
                    found_here = (
                        _append_group_tokens(values, seen, token_match.group(1))
                        or found_here
                    )

        sib = text_node.next_sibling
        while isinstance(sib, NavigableString) and not clean_text(str(sib)):
            sib = sib.next_sibling
        if isinstance(sib, Tag):
            sib_classes = set(sib.get("class") or [])
            sib_text = clean_text(sib.get_text(" ", strip=True))
            if (
                sib.name == "h5"
                or "h5" in sib_classes
                or (
                    sib.name in ("span", "b", "strong")
                    and "changesPair" not in sib_classes
                )
            ):
                if sib_text and "ауд." not in sib_text.lower():
                    found_here = (
                        _append_group_tokens(values, seen, sib_text) or found_here
                    )

        if not found_here and parent is not None:
            parent_text = clean_text(parent.get_text(" ", strip=True))
            if (
                "ауд." not in parent_text.lower()
                and not parent.select_one(".changesPair")
            ):
                h5 = parent.find(class_="h5") or parent.select_one("h5")
                if h5 is not None:
                    h5_text = clean_text(h5.get_text(" ", strip=True))
                    if h5_text:
                        found_here = (
                            _append_group_tokens(values, seen, h5_text)
                            or found_here
                        )
            if not found_here and _GROUP_LABEL_RE.fullmatch(
                parent_text.rstrip(": ")
            ):
                parent_sib = parent.next_sibling
                while isinstance(parent_sib, NavigableString) and not clean_text(
                    str(parent_sib)
                ):
                    parent_sib = parent_sib.next_sibling
                if isinstance(parent_sib, Tag) and (
                    parent_sib.name == "h5"
                    or "h5" in (parent_sib.get("class") or [])
                ):
                    _append_group_tokens(
                        values, seen, parent_sib.get_text(" ", strip=True)
                    )

    # Способ 2: явные атрибуты/классы группы.
    for element in node.select("[data-group]"):
        raw = clean_text(element.get("data-group", ""))
        if raw:
            _append_group_tokens(values, seen, raw)
    for element in node.select(".group, .groups"):
        raw = clean_text(element.get_text(" ", strip=True))
        if raw and "ауд." not in raw.lower() and "п/гр" not in raw.lower():
            _append_group_tokens(values, seen, raw)

    if values or saw_explicit_label:
        return ", ".join(values)

    # Способ 3: запасной regex для plain-text HTML и title/aria-label без «гр.».
    text = _non_subject_text(node, room=room, subject=subject)
    token_re = (
        _GROUP_TOKEN_RE if schedule_type == "staff" else _GROUP_STRICT_TOKEN_RE
    )
    for match in token_re.finditer(text):
        _append_group_tokens(values, seen, match.group(1))

    if schedule_type != "staff" and not values:
        known_by_key = {
            _normalize_group_code(g.name).casefold(): g.name
            for g in GROUPS.values()
        }
        for match in _GROUP_TOKEN_RE.finditer(text):
            normalized = _normalize_group_code(match.group(1))
            if normalized.casefold() in known_by_key:
                _append_group_tokens(values, seen, known_by_key[normalized.casefold()])

    # Некоторые варианты страницы помещают группу только в title/aria-label.
    excluded_attr_classes = {"changesPair", "d-md-none", "Staff", "teacher", "staff"}
    for attr in ("title", "aria-label"):
        for element in node.select(f"[{attr}]"):
            el_classes = set(element.get("class") or [])
            if el_classes & excluded_attr_classes or (
                "d-none" in el_classes and "d-md-block" in el_classes
            ):
                continue
            raw = clean_text(element.get(attr, ""))
            for match in token_re.finditer(raw):
                _append_group_tokens(values, seen, match.group(1))

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


def _extract_fallback_subject(
    node, room: str, teacher: str, subgroup, groups: str = ""
) -> str:
    """Fallback для предмета, если в блоке нет стандартных классов.

    Аккуратно убирает известные служебные части (аудитория, преподаватель,
    «N п/гр.», «ауд.», «гр.») и оставляет то, что похоже на предмет.
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
    if groups:
        for grp in groups.split(","):
            token = clean_text(grp)
            if token:
                text = text.replace(token, " ")
    text = re.sub(r"(?<![A-Za-zА-Яа-яЁё/])гр\.?", " ", text, flags=re.IGNORECASE)
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


def _text_fragments(node, room: str, subgroup, groups: str = "") -> list:
    """Строки блока без аудитории / подгруппы / групп (запасной источник данных)."""
    group_tokens = {
        clean_text(part).casefold()
        for part in (groups or "").split(",")
        if clean_text(part)
    }
    fragments = []
    for raw in node.get_text("\n", strip=True).split("\n"):
        line = clean_text(raw)
        if not line:
            continue
        lowered = line.lower()
        if "ауд." in lowered:
            continue
        if _GROUP_LABEL_RE.fullmatch(line.rstrip(": ")):
            continue
        if line.casefold() in group_tokens:
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
    schedule_type: str = "group",
) -> Optional[Lesson]:
    subject = _find_subject(node)
    teacher = _find_teacher(node)
    room = _find_room(node)
    subgroup = _find_subgroup(node, css_class=css_class)
    groups = _find_groups(
        node, schedule_type=schedule_type, room=room, subject=subject
    )

    # Запасная эвристика для plain-text HTML без классов .Staff/.d-md-none:
    # преподавателя ищем по паттерну «Фамилия И.О.».
    if not teacher:
        fragments = _text_fragments(node, room, subgroup, groups=groups)
        for fragment in fragments:
            if _looks_like_teacher(fragment):
                teacher = clean_text(fragment)
                break

    if not subject:
        subject = _extract_fallback_subject(
            node, room, teacher, subgroup, groups=groups
        )

    if not subject and not teacher and not room and not groups:
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


def _iter_lesson_rows(body, *, schedule_type: str = "group"):
    """Строки занятий внутри card-body, если нет явных блоков .subGroupN."""
    rows = []
    for el in body.select("div.d-flex.justify-content-between"):
        if (
            _find_subject(el)
            or _find_room(el)
            or _find_groups(el, schedule_type=schedule_type)
            or _find_subgroup(el)
        ):
            rows.append(el)
    return rows


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

    # В обычной странице это div.card.myCard. Некоторые варианты staff-
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
                    schedule_type=schedule_type,
                )
                if lesson is not None:
                    lessons.append(lesson)
        else:
            row_blocks = _iter_lesson_rows(body, schedule_type=schedule_type)
            if len(row_blocks) > 1:
                row_lessons = []
                for row in row_blocks:
                    parsed = _parse_lesson_from_node(
                        row,
                        pair=pair,
                        start=start,
                        end=end,
                        time_str=time_str,
                        break_duration=break_duration,
                        schedule_type=schedule_type,
                    )
                    if parsed is not None:
                        row_lessons.append(parsed)
                merged_rows = []
                for parsed in row_lessons:
                    if (
                        parsed.subgroup is None
                        and merged_rows
                        and merged_rows[-1].subgroup is None
                        and merged_rows[-1].subject == parsed.subject
                        and merged_rows[-1].room == parsed.room
                        and merged_rows[-1].teacher == parsed.teacher
                    ):
                        combined: list = []
                        seen_grp: set = set()
                        _append_group_tokens(
                            combined, seen_grp, merged_rows[-1].groups
                        )
                        _append_group_tokens(combined, seen_grp, parsed.groups)
                        merged_rows[-1].groups = ", ".join(combined)
                    else:
                        merged_rows.append(parsed)
                lessons.extend(merged_rows)
            else:
                lesson = _parse_lesson_from_node(
                    body,
                    pair=pair,
                    start=start,
                    end=end,
                    time_str=time_str,
                    break_duration=break_duration,
                    schedule_type=schedule_type,
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
        groups_s = f" | гр. {lesson.groups}" if lesson.groups else ""
        logger.info(
            "Пара %s%s%s | %s | %s | %s | %s",
            lesson.pair,
            subgroup_s,
            groups_s,
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


async def get_schedule(day: date, group_id: Optional[int] = None) -> Schedule:
    """Единая функция получения расписания ровно на переданную дату.

    ВАЖНО: без скрытого fallback. Если расписания на `day` нет —
    возвращается пустое расписание (lessons == []), а не данные
    другой даты. При ошибке сети/источника бросается ScheduleUnavailable.
    """
    group = get_group(group_id)
    html = await (fetch_html(day) if group.id == GROUP_ID else fetch_html(day, group.id))

    if html is None:
        raise ScheduleUnavailable(f"Сайт недоступен для {day.isoformat()}")

    try:
        return parse_schedule(html, day, group=group.name)
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


def schedule_from_storage(stored, day: date, group: Optional[str] = None) -> Schedule:
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
    return Schedule(date=day, group=group or GROUP_NAME, lessons=lessons)


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
        # Пустое поле staff-групп не меняет hash основной группы;
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
    к БД параллельно, не теряя атомарные решения rate limiter.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ============================================================
# СХЕМА БД: v2 — история занятий с записью по подгруппам
# ============================================================

SCHEMA_VERSION = 2


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM bot_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO bot_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def get_meta_value(key: str, default: str = "") -> str:
    try:
        with db_connect() as conn:
            return _meta_get(conn, key, default)
    except Exception:
        logger.exception("Ошибка чтения bot_meta[%s]", key)
        return default


def set_meta_value(key: str, value: str) -> None:
    try:
        with db_connect() as conn:
            _meta_set(conn, key, value)
    except Exception:
        logger.exception("Ошибка записи bot_meta[%s]", key)


def _migrate_lesson_history(conn: sqlite3.Connection) -> None:
    """Переводит lesson_history на схему v2 (строка на подгруппу).

    v1: UNIQUE(group_name, date, pair_number) — одна строка на пару,
    предмет только у «представителя», время подгрупп терялось.
    v2: UNIQUE(group_name, date, pair_number, subgroup_key) — каждая
    подгруппа пишет свою строку со своим предметом.

    После миграции строки и backfill-метки текущего учебного года
    сбрасываются и ставится флаг study_recalc_from: при старте монитора
    дни пересчитываются заново уже по подгруппам.
    """
    _ensure_meta_table(conn)

    version_raw = _meta_get(conn, "schema_version", "")
    try:
        version = int(version_raw) if version_raw else 1
    except ValueError:
        version = 1

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(lesson_history)")
    }
    rebuilt = "subgroup_key" not in columns
    if rebuilt:
        logger.info("Миграция lesson_history: v1 -> v2 (строка на подгруппу)")
        # IF NOT EXISTS и INSERT OR IGNORE — защита от «полу-мigrated»
        # состояния, если прошлый запуск упал между CREATE и COMMIT.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lesson_history_v2 (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name         TEXT NOT NULL,
                date               TEXT NOT NULL,
                pair_number        TEXT NOT NULL,
                subgroup_key       TEXT NOT NULL DEFAULT '',
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
                UNIQUE (group_name, date, pair_number, subgroup_key)
            )
            """
        )
        # Старые строки сохраняются (даты прошлых лет считаются «пара один
        # раз» и без подгрупп); текущий учебный год ниже пересчитается.
        conn.execute(
            """
            INSERT OR IGNORE INTO lesson_history_v2
                (group_name, date, pair_number, subgroup_key, start_time,
                 end_time, subject, normalized_subject, duration_minutes,
                 subgroup_info, teacher, room, completed_at, created_at)
            SELECT group_name, date, pair_number, '', start_time,
                   end_time, subject, normalized_subject, duration_minutes,
                   subgroup_info, teacher, room, completed_at, created_at
            FROM lesson_history
            """
        )
        conn.execute("DROP TABLE lesson_history")
        conn.execute("ALTER TABLE lesson_history_v2 RENAME TO lesson_history")

    if version < SCHEMA_VERSION:
        if rebuilt:
            # Старые строки текущего года записаны «представителем» пары:
            # сбрасываем их и метки backfill, дни пересчитаются по подгруппам.
            start = get_academic_year_start()
            conn.execute(
                "DELETE FROM lesson_history WHERE date >= ?",
                (start.isoformat(),),
            )
            conn.execute(
                "DELETE FROM lesson_backfill_days WHERE date >= ?",
                (start.isoformat(),),
            )
            _meta_set(conn, "study_recalc_from", start.isoformat())
            logger.info(
                "История занятий с %s будет пересчитана по подгруппам",
                start.isoformat(),
            )
        _meta_set(conn, "schema_version", str(SCHEMA_VERSION))


def _migrate_user_settings(conn: sqlite3.Connection) -> None:
    """Переводит user_settings на nullable group_id без DEFAULT 508
    и переносит существующих подписчиков старой версии на legacy GROUP_ID.
    """
    _ensure_meta_table(conn)

    columns = {
        row["name"]: dict(row)
        for row in conn.execute("PRAGMA table_info(user_settings)")
    }
    group_col = columns.get("group_id")
    if group_col and (group_col.get("notnull") or group_col.get("dflt_value") is not None):
        logger.info("Миграция user_settings: group_id поддерживает NULL без DEFAULT 508")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings_v2 (
                chat_id INTEGER PRIMARY KEY,
                group_id INTEGER,
                staff_id INTEGER,
                role TEXT NOT NULL DEFAULT 'student',
                pending_action TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_settings_v2
                (chat_id, group_id, staff_id, role, pending_action,
                 display_name, username, updated_at)
            SELECT chat_id, group_id, staff_id, role, pending_action,
                   display_name, username, updated_at
            FROM user_settings
            """
        )
        conn.execute("DROP TABLE user_settings")
        conn.execute("ALTER TABLE user_settings_v2 RENAME TO user_settings")

    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT OR IGNORE INTO user_settings (chat_id, group_id, updated_at)
        SELECT s.user_id, ?, COALESCE(NULLIF(s.created_at, ''), ?)
        FROM subscribers s
        LEFT JOIN user_settings u ON u.chat_id = s.user_id
        WHERE u.chat_id IS NULL
        """,
        (GROUP_ID, stamp),
    )
    _meta_set(conn, "legacy_subscribers_migrated", "1")


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

            # Старые базы могут содержать legacy-поле created_at; оно
            # сохраняется как дата регистрации подписчика.
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

            # В состоянии храним hash и нормализованные данные — без них
            # невозможно показать «было -> стало».
            state_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(schedule_state)")
            }
            if "data" not in state_columns:
                conn.execute(
                    "ALTER TABLE schedule_state ADD COLUMN data TEXT"
                    " NOT NULL DEFAULT ''"
                )

            # Фактическая доставка расписания каждому подписчику
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

            # Источник истины накопления — записи завершённых пар.
            # Схема v2: каждая подгруппа пары даёт свою строку со своим
            # предметом; уникальность — (группа, дата, пара, подгруппа).
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
                    subgroup_key       TEXT NOT NULL DEFAULT '',
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
                    UNIQUE (group_name, date, pair_number, subgroup_key)
                )
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

            # Rate limiter — предупреждения переживают перезапуск.
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

            # Персональный выбор студента/преподавателя. У новых пользователей
            # group_id = NULL до явного выбора; старые подписчики мигрируются.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    chat_id INTEGER PRIMARY KEY,
                    group_id INTEGER,
                    staff_id INTEGER,
                    role TEXT NOT NULL DEFAULT 'student',
                    pending_action TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS teacher_subscribers (
                    chat_id INTEGER NOT NULL,
                    staff_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, staff_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedule_cache (
                    target_key TEXT NOT NULL,
                    date TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    data TEXT NOT NULL,
                    image_path TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (target_key, date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS study_jobs (
                    group_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    original_chat_id INTEGER NOT NULL,
                    original_message_id INTEGER NOT NULL,
                    is_ad INTEGER NOT NULL DEFAULT 0,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payment_charge_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    moderator_id INTEGER,
                    moderation_note TEXT NOT NULL DEFAULT ''
                )
            """)

            # У одного чата могут быть независимые подписки на разные группы.
            notification_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(schedule_notifications)"
                )
            }
            if "target_key" not in notification_columns:
                conn.execute("ALTER TABLE schedule_notifications RENAME TO schedule_notifications_legacy")
                conn.execute("""
                    CREATE TABLE schedule_notifications (
                        user_id INTEGER NOT NULL,
                        date TEXT NOT NULL,
                        target_key TEXT NOT NULL DEFAULT 'group:508',
                        hash TEXT NOT NULL,
                        sent_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, date, target_key)
                    )
                """)
                conn.execute("""
                    INSERT INTO schedule_notifications
                        (user_id, date, target_key, hash, sent_at)
                    SELECT user_id, date, 'group:508', hash, sent_at
                    FROM schedule_notifications_legacy
                """)
                conn.execute("DROP TABLE schedule_notifications_legacy")

            # Миграция истории занятий на схему v2 выполняется в самом
            # конце: ей нужны уже созданные lesson_backfill_days/bot_meta.
            _migrate_user_settings(conn)
            _migrate_lesson_history(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lesson_history_subject
                ON lesson_history (group_name, normalized_subject, date)
                """
            )
            cache_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(schedule_cache)")
            }
            if "image_context" not in cache_columns:
                conn.execute("ALTER TABLE schedule_cache ADD COLUMN image_context TEXT NOT NULL DEFAULT ''")
            if _meta_get(conn, "staff_groups_cache_version", "") != "2":
                stale_rows = conn.execute(
                    "SELECT image_path FROM schedule_cache WHERE target_key LIKE 'staff:%' AND image_path != ''"
                ).fetchall()
                for row in stale_rows:
                    try:
                        Path(row["image_path"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                conn.execute("DELETE FROM schedule_cache WHERE target_key LIKE 'staff:%'")
                _meta_set(conn, "staff_groups_cache_version", "2")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcasts_status ON broadcasts(status, id)")
        logger.info("База данных готова: %s", DB_PATH)
    except Exception:
        logger.exception("Ошибка инициализации SQLite")


def subscribe_user(
    user_id: int, chat_type: str = "private", title: str = "",
    group_id: Optional[int] = None,
) -> bool:
    """Добавляет подписчика (ЛС или групповой чат).

    True если добавлен, False если уже был или группа не выбрана.
    """
    previous_group_id = get_user_settings(user_id).get("group_id")
    target_group_id = group_id if group_id is not None else previous_group_id
    if target_group_id is None:
        return False
    target_group_id = int(target_group_id)
    created = now_local().strftime("%Y-%m-%d %H:%M:%S")
    added = False
    try:
        with db_connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO subscribers"
                " (user_id, created_at, chat_type, title)"
                " VALUES (?, ?, ?, ?)",
                (user_id, created, chat_type, title or ""),
            )
            added = cur.rowcount > 0
            if not added:
                conn.execute(
                    "UPDATE subscribers SET chat_type = ?, title = ? WHERE user_id = ?",
                    (chat_type, title or "", user_id),
                )
        set_user_group(user_id, target_group_id)
        if added or (
            previous_group_id is not None
            and int(previous_group_id) != target_group_id
        ):
            seed_user_seen_schedules(user_id, f"group:{target_group_id}")
        return added
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


def load_subscribers(group_id: Optional[int] = None) -> list:
    selected_id = int(group_id or GROUP_ID)
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT s.user_id FROM subscribers s "
                "LEFT JOIN user_settings u ON u.chat_id = s.user_id "
                "WHERE (CASE WHEN u.chat_id IS NULL THEN ? ELSE u.group_id END) = ? "
                "ORDER BY s.user_id",
                (GROUP_ID, selected_id),
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


def get_user_settings(chat_id: int) -> dict:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    if row:
        return dict(row)
    return {
        "chat_id": chat_id, "group_id": None, "staff_id": None,
        "role": "student", "pending_action": "", "display_name": "",
        "username": "",
    }


def save_user_profile(user, chat_id: Optional[int] = None) -> None:
    if user is None:
        return
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    target_chat_id = chat_id if chat_id is not None else user.id
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO user_settings
                (chat_id, group_id, display_name, username, updated_at)
            VALUES (?, NULL, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                display_name = excluded.display_name,
                username = excluded.username, updated_at = excluded.updated_at
        """, (target_chat_id, user.full_name or "",
              user.username or "", stamp))


def set_user_group(chat_id: int, group_id: int) -> None:
    previous_group_id = get_user_settings(chat_id).get("group_id")
    target_group_id = int(group_id)
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO user_settings (chat_id, group_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                group_id = excluded.group_id, role = 'student',
                staff_id = NULL, updated_at = excluded.updated_at
        """, (chat_id, target_group_id, stamp))
    if (
        subscriber_info(chat_id) is not None
        and (previous_group_id is None or int(previous_group_id) != target_group_id)
    ):
        seed_user_seen_schedules(chat_id, f"group:{target_group_id}")


def set_user_staff(chat_id: int, staff_id: int) -> None:
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO user_settings (chat_id, staff_id, role, updated_at)
            VALUES (?, ?, 'teacher', ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                staff_id = excluded.staff_id, role = 'teacher',
                updated_at = excluded.updated_at
        """, (chat_id, staff_id, stamp))


def set_pending_action(chat_id: int, action: str = "") -> None:
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO user_settings (chat_id, group_id, pending_action, updated_at)
            VALUES (?, NULL, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                pending_action = excluded.pending_action,
                updated_at = excluded.updated_at
        """, (chat_id, action, stamp))


def active_group_ids() -> list[int]:
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT DISTINCT group_id FROM (
                SELECT group_id FROM user_settings WHERE group_id IS NOT NULL
                UNION
                SELECT ? AS group_id FROM subscribers s
                LEFT JOIN user_settings u ON u.chat_id = s.user_id
                WHERE u.chat_id IS NULL
            ) ORDER BY group_id
        """, (GROUP_ID,)).fetchall()
    return [int(row["group_id"]) for row in rows if int(row["group_id"]) in GROUPS]


def subscribe_teacher(chat_id: int, staff_id: int, enabled: bool = True) -> None:
    was_subscribed = teacher_subscription(chat_id, staff_id) if enabled else False
    with db_connect() as conn:
        if enabled:
            conn.execute(
                "INSERT OR REPLACE INTO teacher_subscribers VALUES (?, ?, ?)",
                (chat_id, staff_id, now_local().strftime("%Y-%m-%d %H:%M:%S")),
            )
        else:
            conn.execute(
                "DELETE FROM teacher_subscribers WHERE chat_id = ? AND staff_id = ?",
                (chat_id, staff_id),
            )
    if enabled and not was_subscribed:
        seed_user_seen_schedules(chat_id, f"staff:{int(staff_id)}")


def teacher_subscription(chat_id: int, staff_id: int) -> bool:
    with db_connect() as conn:
        return conn.execute(
            "SELECT 1 FROM teacher_subscribers WHERE chat_id = ? AND staff_id = ?",
            (chat_id, staff_id),
        ).fetchone() is not None


def load_teacher_subscribers(staff_id: int) -> list[int]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM teacher_subscribers WHERE staff_id = ? ORDER BY chat_id",
            (staff_id,),
        ).fetchall()
    return [int(row["chat_id"]) for row in rows]


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


def load_schedule_notifications(target_key: Optional[str] = None) -> dict:
    """{дата: {user_id: hash}} — последнее доставленное состояние каждому."""
    target_key = target_key or f"group:{GROUP_ID}"
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT user_id, date, hash FROM schedule_notifications WHERE target_key = ?",
                (target_key,),
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
    user_id: int, date_key: str, signature: str,
    target_key: Optional[str] = None,
) -> bool:
    """Фиксирует успешную отправку расписания подписчику."""
    try:
        now = now_local().strftime("%Y-%m-%d %H:%M:%S")
        with db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schedule_notifications"
                " (user_id, date, target_key, hash, sent_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, date_key, target_key or f"group:{GROUP_ID}", signature, now),
            )
            return True
    except Exception:
        logger.exception("Ошибка SQLite (record notification)")
        return False


def seed_user_seen_schedules(user_id: int, target_key: str) -> None:
    """Фиксирует уже известные/кэшированные расписания для нового подписчика,
    чтобы уже опубликованное расписание не отправлялось ему как «новое»."""
    now = now_local().strftime("%Y-%m-%d %H:%M:%S")
    known: dict[str, str] = {}
    prefix = f"{target_key}:"
    try:
        with db_connect() as conn:
            for row in conn.execute(
                "SELECT date, hash, data FROM schedule_state"
            ).fetchall():
                raw_key = row["date"] or ""
                digest = row["hash"] or ""
                if not digest or row["data"] == "[]":
                    continue
                if target_key == f"group:{GROUP_ID}" and ":" not in raw_key and len(raw_key) == 10:
                    known[raw_key] = digest
                elif raw_key.startswith(prefix):
                    known[raw_key[len(prefix):]] = digest

            for row in conn.execute(
                "SELECT date, hash, data FROM schedule_cache WHERE target_key = ?",
                (target_key,),
            ).fetchall():
                digest = row["hash"] or ""
                if not digest or row["data"] in ("", "[]"):
                    continue
                known[row["date"]] = digest

            for date_key, digest in known.items():
                conn.execute(
                    "INSERT OR REPLACE INTO schedule_notifications"
                    " (user_id, date, target_key, hash, sent_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, date_key, target_key, digest, now),
                )
    except Exception:
        logger.exception(
            "Ошибка фиксации уже известных расписаний (%s, %s)",
            user_id,
            target_key,
        )


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


# «Заглушка» предмета: сайт рисует «~..............», когда пара для
# подгруппы ОТМЕНЕНА — занятия у этой подгруппы нет, подгруппа свободна.
# Такое «предметом» не считается: ни в историю, ни в подсчёт времени.
_PLACEHOLDER_SUBJECT_RE = re.compile(r"^[\s.\-–—~_=*#]+$")

# Символы, которых нет в Inter/DejaVu: Pillow подставил бы «квадрат».
_UNPRINTABLE_RE = re.compile(r"[^\w\s.,;:!?()\[\]{}«»„“”'\"\\/|+\-—–=*&%$#№@^~…°]",
                            flags=re.UNICODE)


def sanitize_text(value) -> str:
    """Убрать символы, которые шрифт нарисует «квадратами»."""
    text = clean_text(value)
    if not text:
        return ""
    cleaned = _UNPRINTABLE_RE.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def is_placeholder_subject(value) -> bool:
    """True для отменённых занятий («~..............», «---», пусто)."""
    text = clean_text(value)
    return not text or bool(_PLACEHOLDER_SUBJECT_RE.fullmatch(text))


CANCELLED_SUBJECT_TEXT = "Занятие отменено"


def display_subject_text(subject) -> str:
    """Предмет для показа: отмена рисуется словами, а не точками сайта."""
    text = clean_text(subject)
    if text and is_placeholder_subject(text):
        return CANCELLED_SUBJECT_TEXT
    return text or "Предмет не указан"


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


def _pair_history_lessons(pair: Pair) -> list:
    """Занятия пары для записи в историю: по строке на каждую подгруппу.

    Пара без подгрупп — одно занятие (первое с осмысленным предметом).
    Подгруппа с отменённым занятием («~..............» на сайте — пару
    отменили, подгруппа свободна) в историю не попадает: занятия не было.
    """
    rows = []
    seen_keys = set()
    for lesson in pair.lessons:
        if is_placeholder_subject(lesson.subject):
            continue
        key = clean_text(lesson.subgroup)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append(lesson)
    return rows


history_lessons_for_pair = _pair_history_lessons


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
    subgroup: Optional[str] = None,
    subgroup_info: str = "",
    teacher: str = "",
    room: str = "",
    duration: Optional[int] = None,
) -> bool:
    """Идемпотично записывает одно завершённое занятие (подгруппу пары).

    ``INSERT OR IGNORE`` и UNIQUE(group, date, pair, subgroup_key) делают
    повторный backfill/перезапуск безопасным: одна и та же подгруппа
    пары не считается дважды, а разные подгруппы одной пары пишутся
    отдельными строками — каждая со своим предметом.
    """
    normalized = normalize_subject_name(subject)
    minutes = duration if duration is not None else duration_minutes(start_time, end_time)
    if is_placeholder_subject(subject) or not normalized or minutes <= 0:
        return False
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_connect() as conn:
            _upsert_subject(conn, subject, normalized)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO lesson_history
                (group_name, date, pair_number, subgroup_key, start_time,
                 end_time, subject, normalized_subject, duration_minutes,
                 subgroup_info, teacher, room, completed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_text(group_name) or GROUP_NAME,
                    day.isoformat(),
                    clean_text(pair_number).upper() or f"{start_time}-{end_time}",
                    clean_text(subgroup),
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
    """Добавляет завершённые пары расписания основной группы.

    Пара, разбитая на подгруппы, даёт по строке на подгруппу — у каждой
    своё время (длительность пары) и свой предмет. В общей сумме группы
    такая пара всё равно считается один раз: агрегация идёт по слоту
    (дата + пара), а не по строкам (см. load_total_study_minutes).
    """
    if schedule.schedule_type != "group":
        return 0
    if schedule.date < get_academic_year_start():
        return 0

    inserted = 0
    for pair in schedule.pairs:
        if not is_lesson_completed(schedule.date, pair.end, current):
            continue
        minutes = duration_minutes(pair.start, pair.end)
        if minutes <= 0:
            continue
        for lesson in _pair_history_lessons(pair):
            if record_completed_lesson(
                schedule.group,
                schedule.date,
                pair.number,
                pair.start,
                pair.end,
                lesson.subject,
                subgroup=lesson.subgroup,
                subgroup_info=_subgroup_label(lesson.subgroup),
                teacher=lesson.teacher,
                room=lesson.room,
                duration=minutes,
            ):
                inserted += 1
    if inserted:
        invalidate_group_image_cache(schedule.group)
    return inserted


# Названия, встречающиеся в интеграциях проекта.
process_completed_lessons = record_completed_lessons


def load_subject_totals(
    group_name: str = GROUP_NAME,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict[str, int]:
    """Сумма фактически завершённых минут по нормализованному предмету.

    Внутри одной пары предмет учитывается один раз: две подгруппы с
    одинаковым предметом не удваивают его время. Подгруппы с разными
    предметами получают время каждая — это и есть «своё время»
    подгруппы.
    """
    clauses = ["group_name = ?"]
    params: list = [group_name]
    if start is not None:
        clauses.append("date >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("date <= ?")
        params.append(end.isoformat())
    sql = (
        "SELECT normalized_subject, SUM(slot_minutes) AS minutes FROM ("
        "  SELECT date, pair_number, normalized_subject,"
        "         MAX(duration_minutes) AS slot_minutes"
        "  FROM lesson_history WHERE " + " AND ".join(clauses) +
        "  GROUP BY date, pair_number, normalized_subject"
        ") GROUP BY normalized_subject"
    )
    try:
        with db_connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return {
                row["normalized_subject"]: int(row["minutes"] or 0)
                for row in rows
            }
    except Exception:
        logger.exception("Ошибка загрузки накопленных часов")
        return {}


def load_total_study_minutes(group_name: str = GROUP_NAME) -> int:
    """Суммарное отученное время (минуты) по истории завершённых пар.

    Пара, разбитая на подгруппы, занимает ОДИН слот времени группы
    (подгруппы занимаются параллельно), поэтому группировка — по
    (дата, пара). Время подгрупп разделяется только на уровне строк и
    предметов, в общую сумму группы оно попадает без разделения.
    """
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT SUM(slot_minutes) AS total FROM ("
                "  SELECT date, pair_number, MAX(duration_minutes) AS slot_minutes"
                "  FROM lesson_history WHERE group_name = ?"
                "  GROUP BY date, pair_number"
                ")",
                (group_name,),
            ).fetchone()
            return int(row["total"] or 0) if row else 0
    except Exception:
        logger.exception("Ошибка подсчёта суммарного времени учёбы")
        return 0


total_study_minutes = load_total_study_minutes


def count_active_study_days(
    group_name: str = GROUP_NAME,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> int:
    """Сколько дней в диапазоне реально были завершённые пары."""
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
            row = conn.execute(
                "SELECT COUNT(DISTINCT date) AS days FROM lesson_history"
                " WHERE " + " AND ".join(clauses),
                params,
            ).fetchone()
            return int(row["days"] or 0) if row else 0
    except Exception:
        logger.exception("Ошибка подсчёта дней с занятиями")
        return 0


# ============================================================
# ПРОГНОЗ ИЗУЧЕННОГО ВРЕМЕНИ (1 сентября — 30 июня)
# ============================================================


def get_academic_year_end(day: Optional[date] = None) -> date:
    """30 июня учебного года: 1 сентября 2026 -> 30 июня 2027."""
    start = get_academic_year_start(day)
    return date(start.year + 1, 6, 30)


def count_study_days(first: date, last: date) -> int:
    """Учебные дни в диапазоне: все дни, кроме воскресений."""
    if last < first:
        return 0
    total = 0
    current = first
    while current <= last:
        if not is_day_off(current):
            total += 1
        current += timedelta(days=1)
    return total


@dataclass(frozen=True)
class StudyForecast:
    """Прогноз изученного времени на учебный год."""

    studied_minutes: int                  # X — фактически изучено
    remaining_minutes: Optional[int]      # R — осталось (None = нет данных)
    total_minutes: int                    # Y = X + R — прогноз на год
    elapsed_study_days: int               # De — учебных дней прошло
    remaining_study_days: int             # Dr — учебных дней осталось
    active_days: int                      # дни, когда реально были пары
    pace_minutes_per_day: Optional[float] # X / De — минут на учебный день
    start: date                           # 1 сентября
    end: date                             # 30 июня


def get_study_forecast(
    group_name: str = GROUP_NAME,
    today: Optional[date] = None,
) -> StudyForecast:
    """Сколько примерно осталось учиться при текущем темпе.

    Учебный год: с 1 сентября по 30 июня. Учебный день — любой день,
    кроме воскресенья (в субботу пары могут быть или не быть).

    Модель — пропорциональная экстраполяция «такими темпами»:

        X  — фактически изучено минут (каждая пара считается один раз)
        De — учебных дней прошло с 1 сентября по сегодня
        Dr — учебных дней осталось до 30 июня
        R  = X * Dr / De   — сколько часов осталось учиться
        Y  = X + R         — прогноз суммарного времени за учебный год

    Средний темп X / De учитывает и «пустые» учебные дни (субботы без
    пар, праздники): они входят в знаменатель с нулём часов, поэтому
    прогноз не завышается. active_days — дни, когда пары действительно
    были, — используется для справки в статусе бота.
    """
    today = today or get_today()
    start = get_academic_year_start(today)
    end = get_academic_year_end(today)

    studied = load_total_study_minutes(group_name)
    elapsed_last = min(today, end)
    elapsed_study_days = count_study_days(start, elapsed_last)
    remaining_study_days = count_study_days(
        max(today + timedelta(days=1), start), end
    )
    active_days = count_active_study_days(group_name, start, elapsed_last)

    remaining: Optional[int] = None
    pace: Optional[float] = None
    if studied > 0 and elapsed_study_days > 0:
        pace = studied / elapsed_study_days
        remaining = int(round(pace * remaining_study_days))

    return StudyForecast(
        studied_minutes=studied,
        remaining_minutes=remaining,
        total_minutes=studied + (remaining if remaining is not None else 0),
        elapsed_study_days=elapsed_study_days,
        remaining_study_days=remaining_study_days,
        active_days=active_days,
        pace_minutes_per_day=pace,
        start=start,
        end=end,
    )


# Академический час колледжа: ровно 40 минут.
ACADEMIC_HOUR_MINUTES = 40


def _format_academic_units(units: float) -> str:
    """Число академических часов без единицы: «2», «66,5», «0,75»."""
    if abs(units - round(units)) < 1e-9:
        return str(int(round(units)))
    for digits in (1, 2):
        text = f"{units:.{digits}f}"
        if abs(units - float(text)) < 1e-9:
            return text.replace(".", ",")
    return f"{units:.2f}".replace(".", ",")


def format_academic_hours(minutes: int) -> str:
    """Время в академических часах: «2 акад. ч», «66,5 акад. ч».

    Дробная часть — до двух знаков, без лишних нулей: 80 мин = «2 акад. ч»,
    60 мин = «1,5 акад. ч», 30 мин = «0,75 акад. ч».
    """
    minutes = max(0, int(minutes or 0))
    return _format_academic_units(minutes / ACADEMIC_HOUR_MINUTES) + " акад. ч"


def study_badge_text(forecast: StudyForecast) -> str:
    """Текст бейджа шапки: «Изучено: 66,5 / 1729 акад. ч».

    Время считается академическими часами (1 акад. ч = 40 мин);
    обыкновенное время приводится в сноске внизу картинки.
    До первых занятий бейдж не показывается вовсе; когда прогноз
    недоступен (или учебный год закончился) — только фактическое время.
    """
    studied = _format_academic_units(
        forecast.studied_minutes / ACADEMIC_HOUR_MINUTES
    )
    if forecast.remaining_minutes:
        total = _format_academic_units(
            forecast.total_minutes / ACADEMIC_HOUR_MINUTES
        )
        return f"Изучено: {studied} / {total} акад. ч"
    return f"Изучено: {format_academic_hours(forecast.studied_minutes)}"


def regular_study_time_text(forecast: StudyForecast) -> str:
    """То же время по обыкновенным часам: «44 ч 20 мин / 1152 ч 40 мин»."""
    studied = format_duration(forecast.studied_minutes)
    if forecast.remaining_minutes:
        return f"{studied} / {format_duration(forecast.total_minutes)}"
    return studied


STUDY_NOTE_EXPLANATION = (
    "Время считается по академическому часу: 1 акад. ч = 40 мин."
)


def study_note_lines(forecast: StudyForecast) -> list:
    """Строки сноски внизу картинки с изученным временем.

    Пояснение про академический час + тот же расчёт по обыкновенному
    времени. Пока занятий не было — сноска не нужна.
    """
    if forecast.studied_minutes <= 0:
        return []
    return [
        STUDY_NOTE_EXPLANATION,
        f"По обыкновенному времени: {regular_study_time_text(forecast)}",
    ]


def register_subjects_from_schedule(schedule: Schedule) -> int:
    """Регистрирует новые предметы без фиксированного справочника."""
    if schedule.schedule_type != "group":
        return 0
    values = {}
    for lesson in schedule.lessons:
        original = clean_text(lesson.subject)
        if is_placeholder_subject(original):
            continue
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
    if is_placeholder_subject(subject) or not normalized:
        return ""
    minutes = get_subject_total_minutes(subject, group_name)
    if minutes > 0:
        return f"Изучено: {format_academic_hours(minutes)}"
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
    group_id: Optional[int] = None,
) -> int:
    """Backfill с 1 сентября до вчерашнего дня.

    День отмечается обработанным только после успешного HTTP+parse. Ошибка
    источника оставляет дату для следующей попытки и не создаёт фиктивных
    занятий.
    """
    today = get_today()
    group = get_group(group_id)
    first = start or get_academic_year_start(today)
    last = end or (today - timedelta(days=1))
    if last < first:
        return 0

    inserted = 0
    day = first
    while day <= last:
        if _backfill_day_processed(group.name, day):
            day += timedelta(days=1)
            continue
        # Даже воскресенье запрашивается как историческая дата: пустой
        # ответ источника — это честный результат, а не придуманное занятие.
        try:
            schedule = (await get_schedule(day) if group.id == GROUP_ID
                        else await get_schedule(day, group.id))
        except ScheduleUnavailable:
            logger.warning("Backfill %s: источник недоступен, повторим позже", day)
            day += timedelta(days=1)
            continue
        register_subjects_from_schedule(schedule)
        inserted += record_completed_lessons(schedule)
        _mark_backfill_day_processed(group.name, day)
        day += timedelta(days=1)
    return inserted


run_backfill = backfill_lesson_history


def study_job_status(group_id: int) -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT status FROM study_jobs WHERE group_id = ?", (group_id,)
        ).fetchone()
    return row["status"] if row else ""


async def _run_study_job(group_id: int) -> None:
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    try:
        await backfill_lesson_history(group_id=group_id)
        with db_connect() as conn:
            conn.execute(
                "UPDATE study_jobs SET status = 'ready', finished_at = ?, error = '' WHERE group_id = ?",
                (now_local().strftime("%Y-%m-%d %H:%M:%S"), group_id),
            )
    except Exception as error:
        logger.exception("Первичный расчёт группы %s", group_id)
        with db_connect() as conn:
            conn.execute(
                "UPDATE study_jobs SET status = 'error', error = ? WHERE group_id = ?",
                (str(error)[:500], group_id),
            )


def ensure_study_history(group_id: int) -> bool:
    """Запускает расчёт один раз на группу. True — показать пояснение."""
    status = study_job_status(group_id)
    if status in ("running", "ready"):
        return False
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO study_jobs (group_id, status, started_at)
            VALUES (?, 'running', ?)
            ON CONFLICT(group_id) DO UPDATE SET status = 'running', started_at = excluded.started_at
        """, (group_id, now_local().strftime("%Y-%m-%d %H:%M:%S")))
    asyncio.create_task(_run_study_job(group_id))
    return True


async def recalculate_study_history() -> int:
    """Разовый пересчёт изученного времени текущего учебного года.

    Запускается автоматически после миграции истории на запись по
    подгруппам (флаг study_recalc_from в bot_meta): дни с 1 сентября
    по сегодня заново скачиваются с сайта и записываются по подгруппам.
    Флаг снимается ДО запуска — сбой посреди пересчёта не приводит к
    бесконечным повторам, неотмеченные дни доберёт обычный backfill.
    """
    try:
        raw = get_meta_value("study_recalc_from")
        if not raw:
            return 0
        try:
            start = date.fromisoformat(raw)
        except ValueError:
            logger.error("Некорректный флаг пересчёта: %s", raw)
            set_meta_value("study_recalc_from", "")
            return 0

        set_meta_value("study_recalc_from", "")
        today = get_today()
        if today < start:
            return 0

        logger.info(
            "Пересчёт изученного времени по подгруппам: %s — %s",
            start.isoformat(),
            today.isoformat(),
        )
        inserted = await backfill_lesson_history(start=start, end=today)
        logger.info("Пересчёт завершён, добавлено записей: %s", inserted)
        return inserted
    except Exception:
        logger.exception("Ошибка пересчёта изученного времени")
        return 0


init_db()
restore_group_catalog()


# ============================================================
# РЕНДЕР PNG
# ============================================================

# Палитра (историческая — картинка /status).
COL_BG = "#F3F5FA"
COL_WHITE = "#FFFFFF"
COL_INK = "#14202F"
COL_MUTED = "#64748B"
COL_ACCENT = "#4F46E5"
COL_ACCENT_LIGHT = "#ECECFB"
COL_GREEN = "#0E9F5F"
COL_GREEN_LIGHT = "#E5F6ED"
COL_BORDER = "#E2E7F0"
COL_WARN = "#B45309"
COL_WARN_LIGHT = "#FEF3C7"
COL_RED = "#B91C1C"
COL_RED_LIGHT = "#FEE2E2"

SUMMARY_MAX_LINES = 24
SUMMARY_MAX_CHANGES = 20

# Ширина PNG картинки статуса (1280 — максимум Telegram по стороне).
# Картинка расписания имеет собственный формат: см. SCHEDULE_WIDTH/HEIGHT.
IMAGE_WIDTH = 1280


# ============================================================
# РАЗДЕЛЬНЫЕ ХЕЛПЕРЫ РЕНДЕРА (общие для нового и статусного)
# ============================================================

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


# Наклон синтетического курсива (~12°) для DejaVu без italic-файла.
_ITALIC_SHEAR = 0.21


def _draw_italic_text(image: Image.Image, xy, text: str, font, fill) -> None:
    """Рисует текст курсивом: слой с текстом наклоняется аффинным сдвигом.

    DejaVu поставляется без отдельного italic-начертания, поэтому курсив
    получается сдвигом верхних пикселей вправо — как «synthetic italic»
    в графических редакторах.
    """
    text = clean_text(text)
    if not text:
        return
    bbox = font.getbbox(text)
    if not bbox:
        return
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if tw <= 0 or th <= 0:
        return
    x, y = int(xy[0]), int(xy[1])
    pad = 6
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (pad - bbox[0], pad - bbox[1]), text, font=font, fill=fill
    )
    width = layer.width + int(_ITALIC_SHEAR * layer.height)
    # x_input = x_output + shear * y - shear * height: низ неподвижен,
    # верх уезжает вправо. NEAREST — без интерполяции: штрихи остаются
    # такими же чёткими, как у прямого начертания.
    layer = layer.transform(
        (width, layer.height),
        Image.AFFINE,
        (1, _ITALIC_SHEAR, -_ITALIC_SHEAR * layer.height, 0, 1, 0),
        resample=Image.NEAREST,
    )
    image.paste(layer, (x - pad, y - pad), layer)


def _italic_text_width(text: str, font) -> float:
    """Ширина курсивного текста (с учётом наклона) для центрирования."""
    return font.getlength(text) + _ITALIC_SHEAR * _text_h(font)


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
            extra = (
                f", гр. {clean_text(new.get('groups'))}"
                if clean_text(new.get("groups")) else ""
            )
            lines.append(
                f"Добавлено: {label} — {display_subject_text(new.get('subject'))}"
                + (f", {new.get('room') or '—'}" if new.get("room") else "")
                + extra
            )
            continue
        if change.kind == "removed":
            old = change.old or {}
            extra = (
                f", гр. {clean_text(old.get('groups'))}"
                if clean_text(old.get("groups")) else ""
            )
            lines.append(
                f"Удалено: {label} — {display_subject_text(old.get('subject'))}"
                + (f", {old.get('room') or '—'}" if old.get("room") else "")
                + extra
            )
            continue

        lines.append(f"Изменено: {label}")
        for detail in change.details:
            label_name = detail.get("label") or detail.get("field", "")
            old_val = clean_text(detail.get("old", ""))
            new_val = clean_text(detail.get("new", ""))
            if detail.get("field") == "subject" or label_name == "Предмет":
                # «~..............» в уведомлении — это отмена занятия.
                old_val = display_subject_text(old_val) if old_val else old_val
                new_val = display_subject_text(new_val) if new_val else new_val
            if old_val or new_val:
                lines.append(
                    f"* {label_name}: {old_val or '—'} -> {new_val or '—'}"
                )

    if len(changes) > count:
        lines.append(f"… и ещё {len(changes) - count} изменений")

    return lines[:SUMMARY_MAX_LINES]



# ============================================================
# РЕНДЕР КАРТОЧКИ РАСПИСАНИЯ (Telegram-first: один день — одна картинка)
# ============================================================
#
# Дизайн (референс — спокойная лавандовая карточка дня, доведённая
# до плотности настоящего мобильного расписания):
#   - фон — тёплый почти-белый с очень лёгкой лавандой;
#   - шапка компактная: «РАСПИСАНИЕ», группа, дата, бейдж «Сегодня»,
#     счётчик занятий и прогресс — без лишней высоты;
#   - карточка: номер в лавандовом круге, время (начало жирное, конец
#     серое), предмет, преподаватель; справа — колонка аудитории;
#   - аудитория — самый быстрый ориентир: мелкая подпись «Ауд.»,
#     код комнаты крупно и жирно, часы ниже приглушённо. Колонка
#     одной ширины на всех карточках, лавандовая подложка, не зелёная;
#   - перемена — тихая пометка справа в строке времени;
#   - отмена: та же карточка, без красного блока. Мягкая пилюля
#     «Занятие отменено» рядом со временем и тонкий красный акцент;
#   - подвал тихий: академический час и обыкновенное время;
#   - иконки — Lucide SVG (schedule_icons), не нарисованные от руки;
#   - карточки не растягиваются, если занятий мало;
#   - ОДНА КАРТИНКА = ОДИН ДЕНЬ, всё важное — в safe area.
#
# Архитектура (layout → render; позиции нигде не хардкодятся):
#   Pen             — шрифт-измеритель: метрики снимаются с того же
#                     raster-размера, которым рисуем (× SUPER_SAMPLE), и
#                     возвращаются в дизайн-пикселях;
#   text_placement  — единственная функция позиционирования текста: её
#                     использует и layout (чтобы записать честный
#                     bounding box), и рендер (чтобы нарисовать там же);
#   layout_*        — ТОЛЬКО измеряют и возвращают ops (прямоугольники,
#                     текст, иконки) + высоту блока; ничего не рисуют;
#   build_plan      — подбирает масштаб и высоту холста по количеству
#                     занятий и раскладывает шапку, карточки, подвал;
#   draw_ops        — единственное место, которое рисует (холст 2x +
#                     даунскейл LANCZOS — чёткий текст);
#   validate_layout — проверка ПЕРЕД сохранением: ни один bounding box
#                     не вышел за свою зону, за карточку, за safe area и
#                     за canvas; пересечений текста нет. Контент не
#                     режется никогда: сначала уплотняется layout, в
#                     крайнем случае растёт холст.

import math as _math

# ------------------------------------------------------------
# ПАЛИТРА: тёплый светлый холст, белые карточки, лавандовый акцент
# ------------------------------------------------------------

S_BG         = "#F7F5F8"   # тёплый почти-белый с лёгкой лавандой
S_BG_LIGHT   = "#F9F8FB"   # диагональная подложка (светлее фона)
S_BG_DEEP    = "#EFEAF4"   # диагональная подложка (чуть глубже)
S_CARD       = "#FFFFFF"   # карточка пары
S_INK        = "#1A1A2E"   # основной текст — тёмный navy
S_MUTED      = "#8B8AA0"   # второстепенный текст
S_FAINT      = "#A6A4B8"   # самый тихий текст
S_PURPLE     = "#6E5CE6"   # мягкий лавандовый акцент
S_PURPLE_D   = "#5C4AD0"   # текст статусов
S_PURPLE_L   = "#F1EFFB"   # светлая лаванда: круг номера, пилюли
S_PURPLE_B   = "#E4E0F4"   # граница лавандовых плашек
S_ROOM_BG    = "#F3F1FB"   # подложка колонки аудитории
S_ROOM_LINE  = "#E6E2F4"   # тонкая граница колонки аудитории
S_GREEN      = "#1C9A58"   # только статус «добавлено»
S_GREEN_D    = "#17864C"
S_GREEN_M    = "#5F9179"
S_GREEN_L    = "#E8F6EE"
S_GREEN_B    = "#D5EFE0"
S_RED        = "#E15B70"   # тонкий акцент отмены
S_RED_D      = "#D14E64"   # текст «Занятие отменено»
S_RED_L      = "#FDECEF"
S_RED_PILL   = "#FBE7EB"   # фон пилюли «Занятие отменено»
S_RED_B      = "#F4D5DB"
S_BORDER     = "#E8E6F0"   # граница карточки
S_SHADOW     = "#C9C4DC"   # очень мягкая тень
S_TRACK      = "#E8E6F3"   # дорожка шкалы прогресса
S_DIVIDER    = "#E3E0EE"   # разделитель колонок подвала

# ------------------------------------------------------------
# ХОЛСТ
# ------------------------------------------------------------

SCHEDULE_WIDTH = 1080
SCHEDULE_HEIGHT = 1620                     # 2:3 — базовый формат

S_SCALE_MIN = 0.62
S_SCALE_MAX = 1.12          # занятий мало — карточки крупнее
S_SCALE_STEP = 0.02         # квант подбора масштаба (стабильный результат)

# Лестница форматов: (высота холста, минимально допустимый масштаб).
# Холст растёт только когда на более компактном формате текст уже упёрся
# в пол читаемости; вертикального кропа не используется никогда.
SCHEDULE_RATIO_LADDER = (
    (1620, 0.92),                          # 2:3 — основной формат
    (1740, 0.84),                          # плотный день
    (1860, 0.76),                          # 7–9 занятий
    (1980, S_SCALE_MIN),                   # потолок: выше — уже «чрезмерно»
)
SCHEDULE_HEIGHT_MAX = SCHEDULE_RATIO_LADDER[-1][0]

SAFE_AREA = 52            # единый безопасный отступ со всех четырёх сторон
SUPER_SAMPLE = 2          # рендер в 2x + LANCZOS -> чёткий текст

# --- Текстовая шкала (дизайн-пиксели холста 1080 при масштабе 1.0) ---
# Холст ≈ 2.8× превью 390 px, поэтому 32 px здесь — это ~12 px на телефоне.
S_TYPE = {
    "header_label": 16,     # «РАСПИСАНИЕ»
    "header_title": 50,     # группа / ФИО
    "header_date": 22,      # «Четверг, 24 сентября 2026»
    "header_pill": 26,      # число в счётчике занятий
    "header_pill_word": 18,  # «занятий»
    "header_progress": 16,  # «Изучено: … акад. ч»
    "roman": 18,            # номер пары в круге
    "time": 32,             # начало пары
    "subject": 28,          # предмет
    "group": 19,            # группа на расписании преподавателя
    "teacher": 19,          # преподаватель
    "room_label": 14,       # «Ауд.»
    "room": 32,             # код аудитории — главный ориентир справа
    "hours": 15,            # «8 акад. ч» — тихо, под кодом
    "small": 15,            # перемена / изменения
    "subgroup": 14,         # «2 П/ГР.»
    "cancel": 15,           # «Занятие отменено»
    "tag": 14,              # «Сегодня» / «ИЗМЕНЕНО»
    "note": 15,             # сноска подвала
}
# Полы: плотный день уплотняется, код аудитории не проседает.
S_TYPE_MIN = {
    "header_label": 13,
    "header_title": 34,
    "header_date": 16,
    "header_pill": 18,
    "header_pill_word": 14,
    "header_progress": 13,
    "roman": 14,
    "time": 22,
    "subject": 20,
    "group": 14,
    "teacher": 14,
    "room_label": 11,
    "room": 20,
    "hours": 12,
    "small": 12,
    "subgroup": 12,
    "cancel": 12,
    "tag": 12,
    "note": 12,
}
ROOM_FONT_MIN = S_TYPE_MIN["room"]

# --- Отступы и зазоры: база и пол (умножаются на тот же масштаб) ---
# radius 32 на холсте 1080 ≈ 12 px в превью телефона — мягко, без «пузырей».
S_SPACE = {
    "pad_x": 28, "pad_t": 20, "pad_b": 20,          # карточка
    "gap_badge": 14,                                # круг номера → время
    "gap_row": 14,                                  # строка времени → предмет
    "gap_subject": 6,                               # предмет → преподаватель
    "gap_items": 14,                                # между блоками подгрупп
    "gap_panel": 20,                                # левая колонка ↔ аудитория
    "gap_icon": 8,                                  # иконка → текст
    "card_gap": 12,                                 # между карточками
    "gap_header": 18,                               # шапка → карточки
    "panel_pad_x": 14, "panel_pad_y": 10,           # колонка аудитории
    "panel_line_gap": 2,                            # строки в колонке
    "pill_pad_x": 10, "pill_pad_y": 5,              # пилюли
    "band_pad_x": 12, "band_pad_y": 7,              # плашка изменений
    "badge": 44,                                    # диаметр круга с номером
    "icon": 18,                                     # иконки в строке
    "radius": 28, "row_radius": 14,                 # карточка / колонка аудитории
    "room_col": 196,                                # фиксированная ширина колонки
}
S_SPACE_MIN = {
    "pad_x": 18, "pad_t": 14, "pad_b": 14,
    "gap_badge": 10, "gap_row": 10, "gap_subject": 4, "gap_items": 10,
    "gap_panel": 12, "gap_icon": 5,
    "card_gap": 8, "gap_header": 12,
    "panel_pad_x": 10, "panel_pad_y": 7, "panel_line_gap": 1,
    "pill_pad_x": 8, "pill_pad_y": 4,
    "band_pad_x": 8, "band_pad_y": 4,
    "badge": 34, "icon": 14,
    "radius": 18, "row_radius": 10,
    "room_col": 148,
}
S_LINE_GAP_BASE = 4             # межстрочный зазор внутри блока текста
PILL_RADIUS_RATIO = 0.5         # пилюли — «капсулы»
CHANGE_LINES_MAX = 3            # сколько строк изменений показать в карточке

# Вес начертаний для ролей текста
_ROLE_WEIGHT = {
    "header_label": "semibold", "header_title": "bold", "header_date": "regular",
    "header_pill": "bold", "header_pill_word": "regular",
    "header_progress": "regular",
    "roman": "semibold", "time": "bold", "time_end": "regular",
    "subject": "semibold", "group": "medium", "teacher": "regular",
    "room_label": "medium", "room": "bold",
    "hours": "regular", "small": "regular", "subgroup": "medium",
    "cancel": "medium", "tag": "medium", "note": "regular",
}


def _type_floor(role: str) -> int:
    return S_TYPE_MIN.get(role, 8)


# ------------------------------------------------------------
# ШРИФТЫ
# ------------------------------------------------------------

_WEIGHT_FILES = {
    "regular":  ("Inter-Regular.ttf", "DejaVuSans.ttf"),
    "medium":   ("Inter-Medium.ttf", "DejaVuSans.ttf"),
    "semibold": ("Inter-SemiBold.ttf", "DejaVuSans-Bold.ttf"),
    "bold":     ("Inter-Bold.ttf", "DejaVuSans-Bold.ttf"),
}


@lru_cache(maxsize=512)
def _font_for(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    """Шрифт нужной насыщенности: Inter, если лежит в fonts/, иначе DejaVu.

    Размер — в пикселях РАСТЕРИЗАЦИИ, то есть для supersample-холста это
    дизайн-размер × SUPER_SAMPLE.
    """
    for name in _WEIGHT_FILES.get(weight, _WEIGHT_FILES["regular"]):
        path = FONTS_DIR / name
        if path.exists():
            return ImageFont.truetype(str(path), int(size))
    raise RuntimeError("Шрифт не найден. Проверь папку fonts.")


class Pen:
    """Шрифт-измеритель в дизайн-пикселях.

    Метрики снимаются со шрифта, отрендеренного в SUPER_SAMPLE раз
    крупнее — ровно тем же способом, каким текст будет нарисован, — и
    делятся на SUPER_SAMPLE. Поэтому «layout померил и влезло» означает
    «рендер нарисовал и влезло», а не «влезло примерно».
    """

    __slots__ = ("size", "weight", "font", "ascent", "descent")

    def __init__(self, size: int, weight: str = "regular"):
        self.size = int(size)
        self.weight = weight
        self.font = _font_for(self.size * SUPER_SAMPLE, weight)
        ascent, descent = self.font.getmetrics()
        self.ascent = ascent / SUPER_SAMPLE
        self.descent = descent / SUPER_SAMPLE

    # --- метрики ---
    def width(self, text: str) -> float:
        return self.font.getlength(text) / SUPER_SAMPLE

    def spaced_width(self, text: str, spacing: float) -> float:
        if not spacing:
            return self.width(text)
        return (sum(self.width(ch) for ch in text)
                + spacing * max(0, len(text) - 1))

    def ink(self, text: str) -> tuple:
        """Ink-бокс строки относительно точки отрисовки (дизайн-пиксели)."""
        box = self.font.getbbox(text or " ")
        return tuple(v / SUPER_SAMPLE for v in box)

    def line_h(self, ratio: float = 1.16) -> float:
        """Высота строки: не меньше ascent+descent, иначе глифы вылезут."""
        return max(self.ascent + self.descent, self.size * ratio)

    # --- раскладка текста ---
    def wrap(self, text: str, max_w: float) -> list:
        """Перенос по словам; слово шире строки режется по буквам (без «…»)."""
        text = clean_text(text)
        if not text:
            return [""]
        lines: list = []
        current = ""
        for word in text.split(" "):
            trial = (current + " " + word).strip()
            if not current or self.width(trial) <= max_w:
                current = trial
                continue
            lines.append(current)
            current = word
        if current:
            lines.append(current)
        out: list = []
        for line in lines:
            while self.width(line) > max_w and len(line) > 1:
                cut = len(line)
                while cut > 1 and self.width(line[:cut]) > max_w:
                    cut -= 1
                out.append(line[:cut])
                line = line[cut:]
            out.append(line)
        return out or [""]

    def shorten(self, text: str, max_w: float) -> str:
        """Сократить до ширины max_w и поставить «…»."""
        text = clean_text(text)
        if not text:
            return ""
        if self.width(text) <= max_w:
            return text
        while len(text) > 1 and self.width(text + "…") > max_w:
            text = text[:-1]
        return text + "…"

    def fits(self, text: str, max_w: float, max_lines: int = 1) -> bool:
        text = clean_text(text)
        if max_lines <= 1:
            return self.width(text) <= max_w
        return len(self.wrap(text, max_w)) <= max_lines

    def fit(self, text: str, max_w: float, min_size: int = 0,
            max_lines: int = 1) -> dict:
        """Подогнать текст в ширину: сначала многострочность, затем
        уменьшение кегля (не ниже min_size), и только в самом крайнем
        случае — многоточие.

        Возвращает {"lines", "size", "truncated", "line_h"} — ровно то,
        что потом нарисует рендер.
        """
        text = clean_text(text)
        if min_size is None:
            low = self.size
        else:
            low = max(8, min(int(min_size), self.size))
        chosen = None
        size = self.size
        while size >= low:
            pen = _pen(size, self.weight)
            if max_lines <= 1:
                if pen.width(text) <= max_w:
                    chosen = (size, [text], False)
                    break
            else:
                lines = pen.wrap(text, max_w)
                if len(lines) <= max_lines:
                    chosen = (size, lines, False)
                    break
                chosen = (size, lines, True)
            size -= 1
        if chosen is None:
            pen = _pen(low, self.weight)
            chosen = (low, [pen.shorten(text, max_w)], True)
        size, lines, truncated = chosen
        pen = _pen(size, self.weight)
        if truncated and max_lines > 1 and len(lines) > max_lines:
            kept = lines[: max_lines - 1]
            kept.append(pen.shorten(" ".join(lines[max_lines - 1:]), max_w))
            lines = kept
        return {"lines": lines, "size": size, "truncated": truncated,
                "line_h": pen.line_h()}


@lru_cache(maxsize=512)
def _pen(size: int, weight: str = "regular") -> Pen:
    """Кешированный Pen: повторный замер того же кегля бесплатен."""
    return Pen(size, weight)


def _metrics_for(scale: float) -> dict:
    """Адаптивные метрики: кегли и отступы зависят от количества занятий.

    Полы важнее масштаба: при плотном дне карточки становятся
    компактнее, но аудитория и время не мельчают ниже своих полов.
    """
    scale = max(S_SCALE_MIN, min(S_SCALE_MAX, float(scale)))
    m: dict = {"scale": round(scale, 3)}
    for key, base in S_TYPE.items():
        m[key] = max(_type_floor(key), int(round(base * scale)))
    m["room"] = max(m["room"], ROOM_FONT_MIN)
    for key, base in S_SPACE.items():
        m[key] = max(S_SPACE_MIN[key], int(round(base * scale)))
    m["line_gap"] = max(1, int(round(S_LINE_GAP_BASE * scale)))
    m["subject_lines"] = 3 if scale >= 0.92 else 2
    # Тень едва читается: карточку держит тонкая граница, не «подушка».
    m["shadow"] = max(6, int(round(10 * scale)))
    m["shadow_y"] = max(2, int(round(3 * scale)))
    m["shadow_alpha"] = 36
    return m


# ------------------------------------------------------------
# OPS-ПРИМИТИВЫ (ops — данные, а не вызовы рисования)
# ------------------------------------------------------------

def _rect_op(box, radius, fill=None, outline=None, width=0.0, *,
             kind="shape", uid="", owner=None) -> dict:
    x0, y0, x1, y1 = (float(v) for v in box)
    return {"op": "rect", "box": (min(x0, x1), min(y0, y1),
                                 max(x0, x1), max(y0, y1)),
            "radius": float(radius), "fill": fill, "outline": outline,
            "width": float(width or 0), "kind": kind, "id": uid,
            "owner": owner}


def _ellipse_op(box, fill=None, outline=None, width=0.0, *,
                kind="icon", uid="", owner=None) -> dict:
    x0, y0, x1, y1 = (float(v) for v in box)
    return {"op": "ellipse", "box": (min(x0, x1), min(y0, y1),
                                    max(x0, x1), max(y0, y1)),
            "fill": fill, "outline": outline, "width": float(width or 0),
            "kind": kind, "id": uid, "owner": owner}


def _poly_op(points, fill, *, kind="deco", uid="", owner=None) -> dict:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return {"op": "poly", "points": [(float(px), float(py)) for px, py in points],
            "fill": fill, "kind": kind, "id": uid, "owner": owner,
            "box": (min(xs), min(ys), max(xs), max(ys))}


def _line_op(x0, y0, x1, y1, color, width=1.0, *, uid="deco:line",
             owner=None) -> dict:
    x0, y0, x1, y1 = (float(v) for v in (x0, y0, x1, y1))
    return {"op": "line",
            "box": (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
            "color": color, "width": float(width), "kind": "deco",
            "id": uid, "owner": owner,
            "points": (x0, y0, x1, y1)}


def _shadow_op(box, radius: float, *, uid="deco:shadow", blur: float = 16.0,
               dy: float = 0.0, alpha: int = 70,
               fill: str = S_SHADOW) -> dict:
    """Мягкая тень: прямоугольник под карточкой, размытый на рендере.

    blur/dy/alpha описывают размытие: рендер рисует маску в 1x и после
    LANCZOS-даунскейла тень выглядит как настоящая (без «слоёных» полос).
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    return {"op": "shadow", "box": (x0, y0, x1, y1), "radius": float(radius),
            "blur": float(blur), "dy": float(dy), "alpha": int(alpha),
            "fill": fill, "kind": "shadow", "id": uid, "owner": None}


def text_placement(pen: Pen, lines: list, box, align: str = "left",
                   valign: str = "top", line_gap: float = 0.0,
                   spacing: float = 0.0):
    """Единственная функция позиционирования текста (layout И рендер).

    Возвращает (точки отрисовки по строкам, ink-бокс блока, высота блока).
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    w, h = x1 - x0, y1 - y0
    line_h = pen.line_h()
    count = max(1, len(lines))
    block_h = line_h * count + line_gap * (count - 1)
    if valign == "middle":
        top = y0 + (h - block_h) / 2
    elif valign == "bottom":
        top = y1 - block_h
    else:
        top = y0
    origins: list = []
    ink: Optional[tuple] = None
    for index, line in enumerate(lines):
        dy = top + index * (line_h + line_gap)
        advance = pen.spaced_width(line, spacing)
        if align == "center":
            dx = x0 + (w - advance) / 2
        elif align == "right":
            dx = x1 - advance
        else:
            dx = x0
        origins.append((dx, dy))
        if not line:
            continue
        if spacing:
            cursor = dx
            left = right = cursor
            top_i = bottom_i = None
            for ch in line:
                off = pen.ink(ch)
                left = min(left, cursor + off[0])
                right = max(right, cursor + off[2])
                top_i = (dy + off[1]) if top_i is None else min(top_i,
                                                               dy + off[1])
                bottom_i = (dy + off[3]) if bottom_i is None else max(
                    bottom_i, dy + off[3])
                cursor += pen.width(ch) + spacing
            box_ink = (left, top_i, right, bottom_i)
        else:
            off = pen.ink(line)
            box_ink = (dx + off[0], dy + off[1], dx + off[2], dy + off[3])
        if ink is None:
            ink = box_ink
        else:
            ink = (min(ink[0], box_ink[0]), min(ink[1], box_ink[1]),
                   max(ink[2], box_ink[2]), max(ink[3], box_ink[3]))
    if ink is None:
        ink = (x0, top, x0, top)
    return origins, ink, block_h


def _text_op(pen: Pen, box, text: str, fill: str, *, align="left",
             valign="top", max_lines=1, min_size=None, line_gap=0.0,
             spacing=0.0, uid="", owner=None, role="") -> dict:
    """Измерить текст и собрать text-op (строки и кегль фиксируются здесь).

    Рендер только рисует то, что посчитал layout, — расхождение
    «померили/нарисовали» невозможно по построению.
    """
    box = tuple(float(v) for v in box)
    floor = pen.size if min_size is None else int(min_size)
    res = pen.fit(text, max(8.0, box[2] - box[0]), floor, max_lines)
    font = _pen(res["size"], pen.weight)
    origins, ink, _block = text_placement(font, res["lines"], box, align,
                                         valign, line_gap, spacing)
    return {"op": "text", "lines": res["lines"], "size": res["size"],
            "weight": pen.weight, "fill": fill, "box": box,
            "origins": origins, "bbox": ink, "truncated": res["truncated"],
            "spacing": spacing, "line_gap": line_gap, "kind": "text",
            "id": uid, "owner": owner, "role": role}


def _flow_text(m: dict, x: float, y: float, w: float, text: str, role: str,
               fill: str, *, weight=None, max_lines=1, min_size=None,
               align="left", spacing=0.0, uid="", owner=None) -> tuple:
    """Текст в потоке блока: (ops, занятая высота)."""
    size = m[role]
    pen = _pen(size, weight or _ROLE_WEIGHT.get(role, "regular"))
    floor = _type_floor(role) if min_size is None else min_size
    res = pen.fit(text, w, floor, max_lines)
    block_h = (res["line_h"] * len(res["lines"])
               + m["line_gap"] * max(0, len(res["lines"]) - 1))
    op = _text_op(_pen(res["size"], pen.weight),
                  (x, y, x + w, y + block_h), text, fill, align=align,
                  valign="top", max_lines=max_lines, min_size=res["size"],
                  line_gap=m["line_gap"], spacing=spacing, uid=uid,
                  owner=owner, role=role)
    return [op], block_h


def _shift_op(op: dict, dx: float, dy: float) -> dict:
    """Сдвинуть op на (dx, dy) — так layout собирает блоки в карточки."""
    shifted = dict(op)
    box = shifted.get("box")
    if box is not None:
        shifted["box"] = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
    if shifted.get("op") == "text":
        shifted["origins"] = [(ox + dx, oy + dy)
                              for ox, oy in shifted["origins"]]
        b0, b1, b2, b3 = shifted["bbox"]
        shifted["bbox"] = (b0 + dx, b1 + dy, b2 + dx, b3 + dy)
    if shifted.get("op") == "poly":
        shifted["points"] = [(px + dx, py + dy) for px, py in shifted["points"]]
    return shifted


def _shift_ops(ops: list, dx: float, dy: float) -> list:
    return [_shift_op(op, dx, dy) for op in ops]


def _hex_rgb(value: str) -> tuple:
    text = str(value).lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_hex(*parts) -> str:
    return "#{:02X}{:02X}{:02X}".format(*[max(0, min(255, int(v)))
                                          for v in parts])


def _mix(color_a: str, color_b: str, t: float) -> str:
    """Смешать два цвета: t = 0 -> color_a, t = 1 -> color_b."""
    a, b = _hex_rgb(color_a), _hex_rgb(color_b)
    return _rgb_hex(*[a[i] + (b[i] - a[i]) * t for i in range(3)])


def _fade(color: str, t: float) -> str:
    """Приблизить цвет к цвету холста (мягкая имитация прозрачности)."""
    return _mix(color, S_BG, t)


# ------------------------------------------------------------
# ИКОНКИ: Lucide SVG (assets/icons), не рукописные глифы
# ------------------------------------------------------------

# Имена, которые layout может попросить. Растр — в schedule_icons.
_KNOWN_ICONS = frozenset(schedule_icons.ICON_FILES)


def _icon_ops(name: str, x: float, y: float, s: float, color: str, *,
              uid: str, owner=None, ink: str = S_CARD) -> list:
    """Иконка Lucide в квадрате (x, y, s, s).

    Рендер подставит вектор из SVG. ``ink`` оставлен для совместимости
    вызовов и не используется: цвет штриха — ``color``.
    """
    del ink
    if name not in _KNOWN_ICONS or s < 2:
        return []
    return [{
        "op": "icon", "name": name,
        "box": (float(x), float(y), float(x) + float(s), float(y) + float(s)),
        "color": color, "kind": "icon", "id": uid, "owner": owner,
    }]


def _decor_ops(W: int, H: int) -> list:
    """Едва заметные диагональные подложки фона (как в референсе)."""
    shapes = [
        ([(0.50 * W, 0), (W, 0), (W, 0.26 * H), (0.62 * W, 0.09 * H)],
         S_BG_LIGHT),
        ([(0, 0.70 * H), (0, H), (0.44 * W, H), (0.20 * W, 0.86 * H)],
         S_BG_DEEP),
        ([(W, 0.80 * H), (W, H), (0.70 * W, H)], S_BG_LIGHT),
    ]
    ops: list = []
    for index, (points, color) in enumerate(shapes):
        ops.append(_poly_op(points, color, kind="deco",
                            uid=f"deco:bg{index}"))
    return ops

# ------------------------------------------------------------
# LAYOUT: пилюли, плашки и строки с иконками
# ------------------------------------------------------------

def _pill_ops(m: dict, x: float, y: float, text: str, *, role: str,
              fill=None, text_fill: str = S_PURPLE_D, border=None,
              icon=None, icon_color=None, weight=None, uid="", owner=None,
              radius_ratio: float = PILL_RADIUS_RATIO) -> tuple:
    """Пилюля-капсула: опциональная иконка + текст. Возвращает (ops, w, h).

    Ширина — по содержимому: пилюли не растягиваются на всю карточку.
    """
    pen = _pen(m[role], weight or _ROLE_WEIGHT.get(role, "regular"))
    pad_x, pad_y = m["pill_pad_x"], m["pill_pad_y"]
    if icon not in _KNOWN_ICONS:            # «✗» и эмодзи не рисуем
        icon = None
    icon_size = m["icon"] * 0.84 if icon else 0.0
    gap = m["gap_icon"] if icon else 0.0
    text_w = pen.width(text)
    height = max(pad_y * 2 + pen.line_h(), pad_y * 2 + icon_size)
    width = pad_x * 2 + (icon_size + gap if icon else 0) + text_w
    ops: list = []
    if fill:
        ops.append(_rect_op((x, y, x + width, y + height),
                            height * radius_ratio, fill=fill, outline=border,
                            width=1.2 if border else 0.0, kind="chip",
                            uid=uid + ":bg", owner=owner))
    cursor = x + pad_x
    if icon:
        ops += _icon_ops(icon, cursor, y + (height - icon_size) / 2, icon_size,
                         icon_color or text_fill, uid=uid + ":icon",
                         owner=owner)
        cursor += icon_size + gap
    ops.append(_text_op(pen,
                        (cursor, y, cursor + text_w + 1, y + height), text,
                        text_fill, valign="middle", max_lines=1,
                        min_size=_type_floor(role), uid=uid + ":text",
                        owner=owner, role=role))
    return ops, width, height


def _meta_slot_ops(m: dict, right_x: float, y_center: float, label: str,
                   icon, *, uid: str, owner=None) -> tuple:
    """Тихая пометка справа в строке времени: «⏱ перемена 15 мин».

    Возвращает (ops, ширина). Без иконки — просто приглушённый текст.
    """
    pen = _pen(m["small"], _ROLE_WEIGHT["small"])
    if icon not in _KNOWN_ICONS:
        icon = None
    icon_size = m["icon"] * 0.78 if icon else 0.0
    gap = m["gap_icon"] if icon else 0.0
    text_w = pen.width(label)
    width = (icon_size + gap if icon else 0) + text_w
    x = right_x - width
    line_h = pen.line_h()
    ops: list = []
    if icon:
        ops += _icon_ops(icon, x, y_center - icon_size / 2, icon_size,
                         S_FAINT, uid=uid + ":icon", owner=owner)
    ops.append(_text_op(pen, (x + icon_size + gap, y_center - line_h / 2,
                              right_x, y_center + line_h / 2), label, S_MUTED,
                        valign="middle", max_lines=1,
                        min_size=_type_floor("small"), uid=uid, owner=owner,
                        role="small"))
    return ops, width


def _group_row_ops(m: dict, x: float, y: float, w: float, groups: str, *,
                   uid: str, owner=None) -> tuple:
    """Плашка «🎓 гр. СТ-26» для расписания преподавателя."""
    raw = clean_text(groups)
    if not raw or raw in ("—", "-"):
        return [], 0.0
    label = raw if re.match(r"^гр\.?\s+", raw, re.IGNORECASE) else f"гр. {raw}"
    role = "group"
    weight = _ROLE_WEIGHT[role]
    pad_x = m["pill_pad_x"]
    pad_y = max(3, int(round(m["pill_pad_y"] * 0.9)))
    icon_size = m["icon"] * 0.88
    gap = m["gap_icon"]
    avail_w = max(40.0, w - 2 * pad_x - icon_size - gap - 1)

    fit = _pen(m[role], weight).fit(
        label, avail_w, min_size=_type_floor(role), max_lines=1
    )
    if not fit["truncated"]:
        pen = _pen(fit["size"], weight)
        text_w = pen.width(fit["lines"][0])
        line_h = fit["line_h"]
        height = max(pad_y * 2 + line_h, pad_y * 2 + icon_size)
        width = min(w, pad_x * 2 + icon_size + gap + text_w + 1)
        ops: list = [_rect_op(
            (x, y, x + width, y + height),
            height * PILL_RADIUS_RATIO,
            fill=S_PURPLE_L, outline=S_PURPLE_B, width=1.2,
            kind="chip", uid=uid + ":bg", owner=owner,
        )]
        cursor = x + pad_x
        ops += _icon_ops(
            "cap", cursor, y + (height - icon_size) / 2,
            icon_size, S_PURPLE, uid=uid + ":icon", owner=owner,
        )
        cursor += icon_size + gap
        ops.append(_text_op(
            pen, (cursor, y, cursor + text_w + 1, y + height),
            fit["lines"][0], S_PURPLE_D, valign="middle",
            max_lines=1, min_size=fit["size"],
            uid=uid + ":text", owner=owner, role=role,
        ))
        return ops, height

    fit_multi = _pen(m[role], weight).fit(
        label, avail_w, min_size=_type_floor(role), max_lines=3
    )
    pen = _pen(fit_multi["size"], weight)
    line_h = fit_multi["line_h"]
    line_gap = m["line_gap"]
    est_h = (
        line_h * len(fit_multi["lines"])
        + line_gap * max(0, len(fit_multi["lines"]) - 1)
    )
    cursor = x + pad_x
    text_op = _text_op(
        pen, (cursor, y + pad_y, cursor + avail_w + 1, y + pad_y + est_h),
        label, S_PURPLE_D, valign="top",
        max_lines=len(fit_multi["lines"]), min_size=fit_multi["size"],
        line_gap=line_gap, uid=uid + ":text", owner=owner, role=role,
    )
    used_pen = _pen(text_op["size"], weight)
    used_lines = text_op["lines"]
    actual_w = max((used_pen.width(line) for line in used_lines), default=0.0)
    actual_h = (
        used_pen.line_h() * len(used_lines)
        + line_gap * max(0, len(used_lines) - 1)
    )
    text_op["box"] = (
        cursor, y + pad_y, cursor + actual_w + 1, y + pad_y + actual_h
    )
    height = max(pad_y * 2 + actual_h, pad_y * 2 + icon_size)
    width = min(w, pad_x * 2 + icon_size + gap + actual_w + 1)
    radius = min(float(m["row_radius"]), height * 0.35)
    ops = [_rect_op(
        (x, y, x + width, y + height), radius,
        fill=S_PURPLE_L, outline=S_PURPLE_B, width=1.2,
        kind="chip", uid=uid + ":bg", owner=owner,
    )]
    ops += _icon_ops(
        "cap", x + pad_x,
        y + pad_y + max(0.0, (used_pen.line_h() - icon_size) / 2),
        icon_size, S_PURPLE, uid=uid + ":icon", owner=owner,
    )
    ops.append(text_op)
    return ops, height


def _person_row_ops(m: dict, x: float, y: float, w: float, text: str, *,
                    uid: str, owner=None, fill=S_MUTED,
                    min_size=None) -> tuple:
    """Строка «👤 преподаватель»: иконка + приглушённый текст."""
    pen = _pen(m["teacher"], _ROLE_WEIGHT["teacher"])
    icon_size = m["icon"] * 0.92
    gap = m["gap_icon"]
    line_h = pen.line_h()
    height = max(line_h, icon_size)
    text_w = pen.shorten(text, max(40.0, w - icon_size - gap))
    ops = _icon_ops("person", x, y + (height - icon_size) / 2, icon_size,
                    _fade(fill, 0.18), uid=uid + ":icon", owner=owner)
    ops.append(_text_op(pen, (x + icon_size + gap, y,
                              x + icon_size + gap + pen.width(text_w) + 1,
                              y + height), text_w, fill, valign="middle",
                        max_lines=1,
                        min_size=_type_floor("teacher") if min_size is None
                        else min_size, uid=uid, owner=owner, role="teacher"))
    return ops, height


def _time_ops(m: dict, start: str, end: str, x: float, y: float, max_w: float,
              *, cancelled: bool = False, uid: str, owner=None) -> tuple:
    """Время пары: начало — основным цветом, конец — приглушённым.

    Возвращает (ops, ширина, высота). Кегль не опускается ниже пола:
    если строка не влезает, сокращается подпись конца.
    """
    size = m["time"]
    floor = _type_floor("time")
    start_text = start or "—"
    end_text = end or ""
    while True:
        p_start = _pen(size, "bold")
        p_sep = _pen(size, "regular")
        p_end = _pen(size, "regular")
        gap = size * 0.26
        need = p_start.width(start_text) + (gap * 2 + p_sep.width("—")
                                           + p_end.width(end_text)
                                           if end_text else 0)
        if need <= max_w or size <= floor:
            break
        size -= 1
    line_h = p_start.line_h()
    if end_text and need > max_w:
        room = max_w - p_start.width(start_text) - p_sep.width("—") - gap * 2
        end_text = p_end.shorten(end_text, max(0.0, room))
    ops: list = []
    cursor = x
    start_w = p_start.width(start_text)
    ops.append(_text_op(p_start, (cursor, y, cursor + start_w + 1, y + line_h),
                        start_text, S_INK,
                        valign="middle", max_lines=1, min_size=size,
                        uid=uid + ":start", owner=owner, role="time"))
    cursor += start_w
    if end_text:
        cursor += gap
        sep_w = p_sep.width("—")
        ops.append(_text_op(p_sep, (cursor, y, cursor + sep_w + 1, y + line_h),
                            "—", S_FAINT, valign="middle", max_lines=1,
                            min_size=size, uid=uid + ":sep", owner=owner,
                            role="time_end"))
        cursor += sep_w + gap
        end_w = p_end.width(end_text)
        ops.append(_text_op(p_end, (cursor, y, cursor + end_w + 1, y + line_h),
                            end_text, S_FAINT, valign="middle", max_lines=1,
                            min_size=size, uid=uid + ":end", owner=owner,
                            role="time_end"))
        cursor += end_w
    return ops, cursor - x, line_h


def _subgroup_caption(m: dict, x: float, y: float, w: float, text: str, *,
                      uid: str, owner=None, fill=S_PURPLE_D) -> tuple:
    """Подпись подгруппы «2 П/ГР.» — тихо, сразу над названием."""
    pen = _pen(m["subgroup"], "medium")
    spacing = 0.35
    label = clean_text(text).upper()
    if pen.spaced_width(label, spacing) > w:
        label = pen.shorten(label, w)
    # Коробка чуть ниже полной строки: подпись — заголовок блока, она
    # должна стоять вплотную к предмету, не разъезжаясь с ним.
    height = pen.line_h() * 0.92
    ops = [_text_op(pen, (x, y, x + w, y + height), label, fill,
                    valign="middle", max_lines=1, spacing=spacing,
                    min_size=_type_floor("subgroup"), uid=uid, owner=owner,
                    role="subgroup")]
    return ops, height


# ------------------------------------------------------------
# LAYOUT: колонка аудитории (лавандовая, код комнаты крупно)
# ------------------------------------------------------------

def _fit_room_code(text: str, max_w: float, size: int) -> tuple:
    """Код аудитории в одну строку. Ужимаем кегль, перенос — только крайний случай."""
    floor = ROOM_FONT_MIN
    size = max(floor, int(size))
    while size >= floor:
        pen = _pen(size, "bold")
        if pen.width(text) <= max_w:
            return size, [text]
        size -= 1
    pen = _pen(floor, "bold")
    lines = pen.wrap(text, max_w)[:2]
    if len(lines) == 2 and pen.width(lines[1]) > max_w:
        lines[1] = pen.shorten(lines[1], max_w)
    elif len(lines) == 1 and pen.width(lines[0]) > max_w:
        lines = [pen.shorten(lines[0], max_w)]
    return floor, lines or [text]


def _room_panel_ops(m: dict, item: dict, right_x: float, y: float,
                    max_w: float, *, uid: str, owner=None) -> tuple:
    """Лавандовая колонка: «Ауд.» мелко, код комнаты крупно, часы тихо.

    Ширина фиксирована (room_col), правый край общий — колонка
    аудиторий совпадает на всех карточках дня.
    """
    room = clean_text(item["room"])
    hours = clean_text(item.get("hours") or "")
    pad_x, pad_y = m["panel_pad_x"], m["panel_pad_y"]
    width = min(float(m["room_col"]), float(max_w))
    inner_w = max(36.0, width - 2 * pad_x)
    label_pen = _pen(m["room_label"], _ROLE_WEIGHT["room_label"])
    hours_pen = _pen(m["hours"], _ROLE_WEIGHT["hours"])
    code_size, code_lines = _fit_room_code(room, inner_w, m["room"])
    code_pen = _pen(code_size, "bold")
    gap = max(1, m["panel_line_gap"])
    label_h = label_pen.line_h() * 0.86
    code_h = (code_pen.line_h() * len(code_lines)
              + m["line_gap"] * max(0, len(code_lines) - 1))
    hours_h = hours_pen.line_h() * 0.92 if hours else 0.0
    if hours and hours_pen.width(hours) > inner_w:
        hours = hours_pen.shorten(hours, inner_w)
    content_h = label_h + 1 + code_h + ((gap + hours_h) if hours else 0)
    height = pad_y * 2 + content_h
    x = right_x - width
    ops: list = [_rect_op((x, y, x + width, y + height), m["row_radius"],
                          fill=S_ROOM_BG, outline=S_ROOM_LINE, width=1.0,
                          kind="panel", uid=uid + ":bg", owner=owner)]
    cursor = y + pad_y
    ops.append(_text_op(label_pen, (x + pad_x, cursor, x + width - pad_x,
                                    cursor + label_h), "Ауд.", S_FAINT,
                        valign="middle", max_lines=1,
                        min_size=_type_floor("room_label"),
                        uid=uid + ":label", owner=owner, role="room_label"))
    cursor += label_h + 1
    ops.append(_text_op(code_pen, (x + pad_x, cursor, x + width - pad_x,
                                   cursor + code_h), room, S_INK,
                        valign="top", max_lines=len(code_lines),
                        min_size=code_size, line_gap=m["line_gap"],
                        uid=uid + ":room", owner=owner, role="room"))
    if hours:
        cursor += code_h + gap
        ops.append(_text_op(hours_pen, (x + pad_x, cursor, x + width - pad_x,
                                        cursor + hours_h), hours, S_MUTED,
                            valign="middle", max_lines=1,
                            min_size=_type_floor("hours"),
                            uid=uid + ":hours", owner=owner, role="hours"))
    return ops, width, height


# ------------------------------------------------------------
# LAYOUT: изменения внутри карточки
# ------------------------------------------------------------

def layout_change_band(m: dict, item: dict, x: float, y: float,
                       w: float) -> tuple:
    """Плашка «что изменилось»: лавандовая, по ширине текста."""
    lines = list(item.get("change_lines") or [])
    if not lines:
        return [], 0.0
    shown = lines[:CHANGE_LINES_MAX]
    hidden = len(lines) - len(shown)
    if hidden > 0:
        shown = shown + [f"и ещё {hidden} изм."]
    pen = _pen(m["small"], "medium")
    pad_x, pad_y = m["band_pad_x"], m["band_pad_y"]
    inner_w = max(40.0, w - 2 * pad_x)
    cursor = y + pad_y
    ops: list = []
    widest = 0.0
    for index, line in enumerate(shown):
        text = line if pen.fits(line, inner_w) else pen.shorten(line, inner_w)
        text_w = pen.width(text)
        widest = max(widest, text_w)
        line_h = pen.line_h()
        ops.append(_text_op(pen, (x + pad_x, cursor, x + pad_x + text_w + 1,
                                  cursor + line_h), text, S_PURPLE_D,
                            valign="middle", max_lines=1,
                            min_size=_type_floor("small"),
                            uid=f"{item['uid']}:band{index}",
                            owner=item["owner"], role="small"))
        cursor += line_h + m["panel_line_gap"]
    height = cursor - m["panel_line_gap"] + pad_y - y
    band_w = min(w, widest + 2 * pad_x)
    ops.insert(0, _rect_op((x, y, x + band_w, y + height), m["row_radius"],
                           fill=S_PURPLE_L, kind="band",
                           uid=item["uid"] + ":band:bg",
                           owner=item["owner"]))
    return ops, height


# ------------------------------------------------------------
# LAYOUT: тело занятия (предмет → преподаватель → аудитория)
# ------------------------------------------------------------

def layout_item_body(m: dict, item: dict, x: float, w: float, *,
                     show_teacher: bool = True) -> tuple:
    """Одно занятие: предмет, преподаватель, плашка аудитории справа.

    Координаты относительные (x — левый край блока, y считается от 0).
    Возвращает (ops, высота): зазор добавляется ПЕРЕД блоком, поэтому
    высота всегда точная — хвостового отступа нет.
    """
    panel_ops: list = []
    panel_w = 0.0
    panel_h = 0.0
    if item["room"]:
        panel_ops, panel_w, panel_h = _room_panel_ops(
            m, item, x + w, 0.0, min(w * 0.46, float(m["room_col"])),
            uid=item["uid"] + ":panel", owner=item["owner"],
        )
    left_w = w if panel_w <= 0 else max(140.0, w - panel_w - m["gap_panel"])
    ops: list = []
    y = 0.0

    # 1. ПРЕДМЕТ. Плейсхолдер отмены не дублируем — его заменяет пилюля.
    subject = "" if (item["cancelled"] and item["placeholder"]) else item["subject"]
    if subject:
        subj_ops, subj_h = _flow_text(
            m, x, y, left_w, subject, "subject", S_INK,
            max_lines=m["subject_lines"], min_size=_type_floor("subject"),
            uid=item["uid"] + ":subject", owner=item["owner"],
        )
        ops += subj_ops
        y += subj_h

    # 2. ГРУППА (для расписания преподавателя)
    if item.get("groups"):
        if y > 0:
            y += m["gap_subject"]
        group_ops, group_h = _group_row_ops(
            m, x, y, left_w, item["groups"],
            uid=item["uid"] + ":group", owner=item["owner"],
        )
        ops += group_ops
        y += group_h

    # 3. ПРЕПОДАВАТЕЛЬ
    if show_teacher and item["teacher"]:
        if y > 0:
            y += m["gap_subject"]
        teacher_ops, teacher_h = _person_row_ops(
            m, x, y, left_w, item["teacher"],
            uid=item["uid"] + ":teacher", owner=item["owner"],
        )
        ops += teacher_ops
        y += teacher_h

    row_h = max(y, panel_h)
    ops += _shift_ops(panel_ops, 0, 0)      # плашка — по верху блока

    # 3. ИЗМЕНЕНИЯ — плашкой под занятием (на всю ширину блока)
    if item.get("change_lines"):
        band_ops, band_h = layout_change_band(m, item, x,
                                             row_h + m["gap_subject"], w)
        ops += band_ops
        row_h += m["gap_subject"] + band_h
    return ops, row_h


def layout_cancelled_item(m: dict, item: dict, x: float, w: float, *,
                          uid: str, owner=None,
                          show_teacher: bool = True) -> tuple:
    """Отменённое занятие: розовая плашка внутри карточки.

    Сверху — пилюля «Занятие отменено» и красный бейдж «ОТМЕНА», ниже —
    предмет (если он известен), преподаватель и аудитория.
    """
    pad_x, pad_y = m["panel_pad_x"], m["panel_pad_y"]
    inner_x = x + pad_x
    inner_w = w - 2 * pad_x
    pill_ops, pill_w, pill_h = _pill_ops(
        m, inner_x, pad_y, "Занятие отменено", role="cancel",
        fill=S_RED_PILL, text_fill=S_RED_D, uid=uid + ":cancel", owner=owner,
    )
    tag_ops, tag_w, tag_h = _pill_ops(
        m, 0.0, pad_y, "ОТМЕНА", role="tag", fill=S_RED, text_fill=S_CARD,
        weight="bold", uid=uid + ":tag", owner=owner,
    )
    tag_ops = _shift_ops(tag_ops, inner_x + inner_w - tag_w, 0)
    body_ops, body_h = layout_item_body(m, item, inner_x, inner_w,
                                        show_teacher=show_teacher)
    # Зазор нужен только если под пилюлей есть что показать: иначе
    # компактная отмена «раздувалась» бы до размера обычной карточки.
    body_y = pad_y + max(pill_h, tag_h) + (m["gap_subject"] if body_h else 0)
    height = body_y + body_h + pad_y
    ops: list = [_rect_op((x, 0, x + w, height), m["row_radius"],
                          fill=S_RED_L, outline=S_RED_B, width=1.1,
                          kind="panel", uid=uid + ":bg", owner=owner)]
    ops += pill_ops + tag_ops + _shift_ops(body_ops, 0, body_y)
    return ops, height


# ------------------------------------------------------------
# LAYOUT: карточка пары
# ------------------------------------------------------------

def _meta_label_for(spec: dict, has_next: bool) -> tuple:
    """Тихая пометка в строке времени: (текст, иконка).

    Приоритет — перемена: это то, что нужно знать до звонка. Если пауза
    в данных не пришла, но в паре несколько подгрупп, показываем их число.
    """
    break_text = clean_text(spec.get("break"))
    if break_text and has_next:
        return f"перемена {break_text}", "clock"
    items = spec["items"]
    if len(items) > 1:
        word = _plural(len(items), "подгруппа", "подгруппы", "подгрупп")
        return f"{len(items)} {word}", None
    return "", None


def _cancel_pill(m: dict, uid: str, owner) -> tuple:
    """Маленькая пилюля «Занятие отменено» — не плашка на всю карточку."""
    return _pill_ops(
        m, 0.0, 0.0, "Занятие отменено", role="cancel",
        fill=S_RED_PILL, text_fill=S_RED_D, border=S_RED_B,
        uid=uid, owner=owner,
    )


def layout_card(m: dict, spec: dict, w: float, *, has_next: bool = False,
                index: int = 0) -> tuple:
    """Карточка одной пары: номер + время, затем занятия/подгруппы.

    Возвращает (ops, высота) в относительных координатах (0,0) — левый
    верх карточки. Рамку, фон и тень добавляет build_plan, когда знает
    итоговую высоту. Отмена не меняет конструкцию карточки.
    """
    ops: list = []
    owner = spec["owner"]
    uid = spec["owner"]
    pad_x, pad_t, pad_b = m["pad_x"], m["pad_t"], m["pad_b"]
    inner_x = pad_x
    inner_w = w - 2 * pad_x
    inner_right = inner_x + inner_w
    cancelled_all = bool(spec["cancelled_all"])
    y = float(pad_t)

    # --- ряд 1: круг с номером, время, пилюля отмены, перемена справа
    diameter = m["badge"]
    accent = S_RED_D if cancelled_all else S_PURPLE
    accent_bg = S_RED_PILL if cancelled_all else S_PURPLE_L
    ops.append(_ellipse_op((inner_x, y, inner_x + diameter,
                            y + diameter), fill=accent_bg, kind="icon",
                           uid=f"{uid}:num:bg", owner=owner))
    ops.append(_text_op(_pen(m["roman"], "semibold"),
                        (inner_x, y, inner_x + diameter, y + diameter),
                        clean_text(spec.get("number")) or "—", accent,
                        align="center", valign="middle",
                        min_size=_type_floor("roman"), uid=f"{uid}:num",
                        owner=owner, role="roman"))

    meta_text, meta_icon = _meta_label_for(spec, has_next)
    # «ОТМЕНА» не дублируем: статус несёт пилюля «Занятие отменено».
    meta_tag = ""
    if not meta_text and spec.get("tag") in ("ИЗМЕНЕНО", "ДОБАВЛЕНО"):
        meta_tag = spec["tag"]
    meta_w = 0.0
    if meta_text:
        meta_pen = _pen(m["small"], _ROLE_WEIGHT["small"])
        meta_w = meta_pen.width(meta_text)
        if meta_icon in _KNOWN_ICONS:
            meta_w += m["icon"] * 0.78 + m["gap_icon"]
    elif meta_tag:
        tag_pen = _pen(m["tag"], "medium")
        meta_w = tag_pen.width(meta_tag) + 2 * m["pill_pad_x"]

    cancel_ops, cancel_w, cancel_h = ([], 0.0, 0.0)
    if cancelled_all or not spec["items"]:
        cancel_ops, cancel_w, cancel_h = _cancel_pill(
            m, f"{uid}:cancel", owner,
        )
    time_x = inner_x + diameter + m["gap_badge"]
    reserved = meta_w + (cancel_w + m["gap_icon"] if cancel_w else 0)
    time_max = max(120.0, inner_w - diameter - 2 * m["gap_badge"] - reserved
                   - 8)
    time_ops, time_w, time_h = _time_ops(
        m, clean_text(spec.get("start")) or _first_clock(spec.get("time")),
        _second_clock(spec.get("time")), time_x, 0.0, time_max,
        cancelled=False, uid=f"{uid}:time", owner=owner,
    )
    row_h = max(diameter, time_h, cancel_h)
    time_y = y + (row_h - time_h) / 2
    ops += _shift_ops(time_ops, 0, time_y)
    badge_inline = False
    if cancel_ops:
        badge_x = time_x + time_w + m["gap_icon"]
        meta_left = inner_right - meta_w - (8 if meta_w else 0)
        if badge_x + cancel_w <= meta_left + 0.5:
            ops += _shift_ops(cancel_ops, badge_x,
                              y + (row_h - cancel_h) / 2)
            badge_inline = True
    if meta_text:
        meta_ops, _meta_used = _meta_slot_ops(
            m, inner_right, y + row_h / 2, meta_text, meta_icon,
            uid=f"{uid}:meta", owner=owner,
        )
        ops += meta_ops
    elif meta_tag:
        tag_ops, tag_w, tag_h = _pill_ops(
            m, 0.0, 0.0, meta_tag, role="tag", fill=spec["tag_bg"],
            text_fill=spec["tag_fill"], weight="medium", uid=f"{uid}:tag",
            owner=owner,
        )
        ops += _shift_ops(tag_ops, inner_right - tag_w,
                          y + (row_h - tag_h) / 2)
    y += row_h
    if cancel_ops and not badge_inline:
        y += m["gap_subject"]
        ops += _shift_ops(cancel_ops, time_x, y)
        y += cancel_h

    items = spec["items"]
    multi = len(items) > 1 or any(item["subgroup"] for item in items)
    if items:
        y += m["gap_row"]
        for position, item in enumerate(items):
            if position:
                y += m["gap_items"]
            label = _subgroup_label(item["subgroup"]) or ""
            if multi and label:
                caption_ops, caption_h = _subgroup_caption(
                    m, inner_x, y, inner_w, label,
                    uid=f"{uid}:i{position}:caption", owner=owner,
                )
                ops += caption_ops
                y += caption_h + max(2, m["gap_subject"] * 0.35)
            # Частичная отмена: пилюля у этого занятия, карточка остаётся белой.
            if item["cancelled"] and not cancelled_all:
                pill_ops, _pill_w, pill_h = _cancel_pill(
                    m, f"{uid}:i{position}:cancel", owner,
                )
                ops += _shift_ops(pill_ops, inner_x, y)
                y += pill_h + m["gap_subject"]
            block_ops, block_h = layout_item_body(m, item, inner_x, inner_w)
            if block_h:
                ops += _shift_ops(block_ops, 0, y)
                y += block_h
    elif not cancel_ops:
        y += m["gap_row"]
        pill_ops, _pill_w, pill_h = _cancel_pill(m, f"{uid}:cancel", owner)
        ops += _shift_ops(pill_ops, inner_x, y)
        y += pill_h

    height = y + pad_b
    if cancelled_all:
        # Тонкая красная метка слева — заметно, но не блок.
        inset = m["radius"] * 0.42
        bar = 3.0
        if height - 2 * inset > 12:
            ops.append(_rect_op((0, inset, bar, height - inset), 1.5,
                                fill=S_RED, kind="accent",
                                uid=f"{uid}:accent", owner=owner))
    return ops, height


def _first_clock(value) -> str:
    """«08:30» из «08:30 — 09:50» (страховка для данных без start/end)."""
    parts = re.split(r"\s*[-—]\s*", clean_text(value))
    return parts[0] if parts and parts[0] else ""


def _second_clock(value) -> str:
    parts = re.split(r"\s*[-—]\s*", clean_text(value))
    return parts[1] if len(parts) > 1 else ""


# ------------------------------------------------------------
# LAYOUT: пустой день
# ------------------------------------------------------------

def layout_empty_card(m: dict, w: float, h: float) -> tuple:
    """Пустой день: одна спокойная карточка по центру области."""
    ops: list = [_shadow_op((0, 0, w, h), m["radius"], uid="empty:shadow")]
    ops.append(_rect_op((0, 0, w, h), m["radius"], fill=S_CARD,
                        outline=S_BORDER, width=1.1, kind="card",
                        uid="empty:bg"))
    icon_size = max(28, int(round(34 * m["scale"])))
    tile = icon_size + 20
    title_pen = _pen(m["subject"], "semibold")
    note_pen = _pen(m["small"], "regular")
    block = tile + 16 + title_pen.line_h() + 8 + note_pen.line_h()
    top = max(24.0, (h - block) / 2)
    tile_x = (w - tile) / 2
    ops.append(_rect_op((tile_x, top, tile_x + tile, top + tile),
                        tile * 0.28, fill=S_PURPLE_L, kind="icon",
                        uid="empty:icon:bg"))
    glyph = icon_size * 0.72
    ops += _icon_ops("calendar", tile_x + (tile - glyph) / 2,
                     top + (tile - glyph) / 2, glyph, S_PURPLE,
                     uid="empty:icon")
    cursor = top + tile + 16
    for index, (text, pen, fill, role) in enumerate((
            ("Занятий нет", title_pen, S_INK, "subject"),
            ("Расписание на этот день не опубликовано", note_pen, S_MUTED,
             "small"),
    )):
        line_h = pen.line_h()
        ops.append(_text_op(pen, (24, cursor, w - 24, cursor + line_h), text,
                            fill, align="center", valign="middle",
                            max_lines=1, min_size=_type_floor(role),
                            uid=f"empty:text:{index}"))
        cursor += line_h + 12
    return ops, h


# ------------------------------------------------------------
# LAYOUT: шапка и подвал
# ------------------------------------------------------------

def _day_caption(schedule) -> str:
    """«Четверг, 24 сентября 2026» — единственный день на картинке."""
    day = schedule.date
    return (f"{WEEKDAYS[day.weekday()]}, {day.day} "
            f"{MONTHS_GEN[day.month]} {day.year}")


def layout_header(m: dict, schedule, ctx: dict, title: Optional[str],
                  W: int) -> tuple:
    """Шапка: иконка + метка + группа/ФИО + дата | счётчик + прогресс.

    Возвращает (ops, y низа шапки).
    """
    ops: list = []
    is_staff = schedule.schedule_type == "staff"
    x = float(SAFE_AREA)
    y = float(SAFE_AREA)
    right_edge = float(W - SAFE_AREA)
    icon_size = float(max(40, int(round(48 * m["scale"]))))

    # --- правая колонка: счётчик занятий, шкала прогресса, «Изучено»
    lesson_count = count_lessons(schedule.lessons)
    count_word = _plural(lesson_count, "занятие", "занятия", "занятий")
    num_pen = _pen(m["header_pill"], "bold")
    word_pen = _pen(m["header_pill_word"], "regular")
    num_txt = str(lesson_count)
    pill_h = num_pen.line_h() + 2 * max(6, int(m["pill_pad_y"]))
    pill_w = float(int(m["pill_pad_x"] * 2 + num_pen.width(num_txt) + 7
                      + word_pen.width(count_word)))
    forecast = ctx.get("forecast")
    progress_on = bool(ctx.get("is_group") and forecast is not None
                       and forecast.studied_minutes > 0)
    progress_txt = study_badge_text(forecast) if progress_on else ""
    prog_pen = _pen(m["header_progress"], "regular")
    progress_w = prog_pen.width(progress_txt) if progress_txt else 0.0
    bar_w = max(pill_w, min(progress_w or pill_w, 280.0))
    bar_h = float(max(5, int(round(6 * m["scale"]))))
    right_w = max(pill_w, progress_w, bar_w)
    right_x = right_edge - right_w

    text_x = x + icon_size + m["gap_badge"]
    left_w = max(200.0, right_x - 30 - text_x)

    # --- иконка календаря (Lucide) на лавандовом скруглении
    ops.append(_rect_op((x, y, x + icon_size, y + icon_size),
                        icon_size * 0.28, fill=S_PURPLE, kind="icon",
                        uid="header:icon:bg", owner="header"))
    glyph = icon_size * 0.52
    ops += _icon_ops("calendar", x + (icon_size - glyph) / 2,
                     y + (icon_size - glyph) / 2, glyph, "#FFFFFF",
                     uid="header:icon", owner="header")

    # --- метка, название группы/ФИО, дата
    label_pen = _pen(m["header_label"], "semibold")
    label = clean_text(title or "РАСПИСАНИЕ").upper()
    spacing = max(0.8, m["header_label"] * 0.08)
    if label_pen.spaced_width(label, spacing) > left_w:
        label = label_pen.shorten(label, left_w)
    label_y = y + 1
    ops.append(_text_op(label_pen, (text_x, label_y, text_x + left_w,
                                    label_y + label_pen.line_h()), label,
                        S_PURPLE, valign="middle", spacing=spacing,
                        min_size=_type_floor("header_label"),
                        uid="header:label", owner="header"))
    label_bottom = label_y + label_pen.line_h()

    title_pen = _pen(m["header_title"], "bold")
    big_title = sanitize_text(schedule.staff_name or schedule.group) if is_staff \
        else sanitize_text(schedule.group or GROUP_NAME)
    title_lines = 2 if is_staff else 1
    title_res = title_pen.fit(big_title, left_w,
                              _type_floor("header_title"), title_lines)
    title_y = label_bottom + 12
    title_h = title_res["line_h"] * len(title_res["lines"])
    ops.append(_text_op(_pen(title_res["size"], "bold"),
                        (text_x, title_y, text_x + left_w, title_y + title_h),
                        big_title, S_INK, valign="top",
                        max_lines=title_lines, min_size=title_res["size"],
                        uid="header:title", owner="header",
                        line_gap=m["line_gap"]))
    title_bottom = title_y + title_h

    date_pen = _pen(m["header_date"], "regular")
    date_y = title_bottom + 4
    date_h = date_pen.line_h()
    date_op = _text_op(date_pen, (text_x, date_y, text_x + left_w,
                                  date_y + date_h), _day_caption(schedule),
                       S_MUTED, valign="middle",
                       min_size=_type_floor("header_date"),
                       uid="header:date", owner="header")
    ops.append(date_op)
    date_bottom = date_y + date_h
    day_label = day_label_for(schedule.date)
    if day_label in ("Сегодня", "Завтра"):
        day_ops, day_w, day_h = _pill_ops(
            m, 0.0, 0.0, day_label, role="tag", fill=S_PURPLE_L,
            text_fill=S_PURPLE_D, weight="medium", uid="header:day",
            owner="header",
        )
        day_x = date_op["bbox"][2] + m["gap_icon"] * 1.8
        if day_x + day_w <= text_x + left_w:
            ops += _shift_ops(day_ops, day_x,
                              date_y + (date_h - day_h) / 2)
            date_bottom = max(date_bottom, date_y + date_h)

    # --- счётчик занятий, шкала прогресса, «Изучено»
    pill_y = y + 4
    ops.append(_rect_op((right_x, pill_y, right_x + pill_w, pill_y + pill_h),
                        pill_h / 2.0, fill=S_CARD, outline=S_BORDER, width=1.0,
                        kind="chip", uid="header:pill:bg", owner="header"))
    cursor = right_x + m["pill_pad_x"]
    ops.append(_text_op(num_pen, (cursor, pill_y, cursor + num_pen.width(num_txt) + 1,
                                  pill_y + pill_h), num_txt, S_PURPLE,
                        valign="middle", max_lines=1,
                        min_size=_type_floor("header_pill"),
                        uid="header:pill:num", owner="header"))
    cursor += num_pen.width(num_txt) + 7
    ops.append(_text_op(word_pen, (cursor, pill_y, right_x + pill_w,
                                   pill_y + pill_h), count_word, S_MUTED,
                        valign="middle", max_lines=1,
                        min_size=_type_floor("header_pill_word"),
                        uid="header:pill:word", owner="header"))
    right_bottom = pill_y + pill_h
    if progress_txt:
        bar_y = right_bottom + 8
        ops.append(_rect_op((right_edge - bar_w, bar_y, right_edge,
                             bar_y + bar_h), bar_h / 2.0, fill=S_TRACK,
                            kind="shape", uid="header:bar:bg", owner="header"))
        share = 0.0
        if forecast.total_minutes > 0:
            share = min(1.0, forecast.studied_minutes / forecast.total_minutes)
        if share > 0:
            fill_w = max(bar_h * 1.6, bar_w * share)
            ops.append(_rect_op((right_edge - bar_w, bar_y,
                                 right_edge - bar_w + fill_w, bar_y + bar_h),
                                bar_h / 2.0, fill=S_PURPLE, kind="shape",
                                uid="header:bar", owner="header"))
        prog_y = bar_y + bar_h + m["gap_icon"] + 4
        ops.append(_text_op(prog_pen, (right_edge - max(bar_w, progress_w),
                                       prog_y, right_edge,
                                       prog_y + prog_pen.line_h()),
                            progress_txt, S_MUTED, align="right",
                            valign="middle", max_lines=1,
                            min_size=_type_floor("header_progress"),
                            uid="header:progress", owner="header"))
        right_bottom = prog_y + prog_pen.line_h()

    return ops, max(label_bottom, date_bottom, right_bottom) + m["gap_header"]


def _split_note(raw: str) -> tuple:
    """Сноска -> (заголовок со двоеточием, значение)."""
    text = clean_text(raw)
    head, _, tail = text.partition(": ")
    return head + (":" if tail else ""), tail


def _note_pen_size(m: dict, note_lines: list, share: float) -> int:
    """Кегль сносок: настолько крупный, чтобы колонки встали по ширине.

    Заголовок сноски должен остаться в одну строку, а значения переносятся
    по словам — поэтому колонки получают ширину по самому длинному
    фрагменту. Если места мало, кегль аккуратно уменьшается (не ниже пола).
    """
    floor = _type_floor("note")
    size = int(m["note"])
    while size > floor:
        pen = _pen(size, _ROLE_WEIGHT["note"])
        needed = 0.0
        for raw in note_lines[:2]:
            head, tail = _split_note(raw)
            longest_word = max((pen.width(word) for word in tail.split()), default=0.0)
            needed += max(pen.width(head), longest_word)
        if needed <= share:
            break
        size -= 1
    return size


def _note_columns(pen: Pen, note_lines: list, widths: list) -> list:
    """Сноски подвала, разложенные в колонки по заданным ширинам."""
    columns: list = []
    for index, raw in enumerate(note_lines[:len(widths)]):
        col_w = widths[index]
        head, tail = _split_note(raw)
        lines = [pen.shorten(head, col_w)]
        if tail:
            lines += [pen.shorten(line, col_w)
                      for line in pen.wrap(tail, col_w)]
        columns.append(lines)
    return columns


def _note_natural_widths(m: dict, note_lines: list, size: int) -> list:
    """Естественные ширины колонок подвала (по самому длинному фрагменту)."""
    pen = _pen(size, _ROLE_WEIGHT["note"])
    widths: list = []
    for raw in note_lines[:2]:
        head, tail = _split_note(raw)
        longest_word = max((pen.width(word) for word in tail.split()), default=0.0)
        widths.append(max(pen.width(head), longest_word))
    return widths


def layout_footer(m: dict, W: int, H: int, note_lines: list) -> tuple:
    """Подвал: светлая плашка, иконка, колонки со сносками.

    Возвращает (ops, высота подвала). Если сноски нет — подвала нет.
    """
    if not note_lines:
        return [], 0.0
    scale = m["scale"]
    pad_x = float(max(18, int(round(36 * scale))))
    pad_y = float(max(12, int(round(20 * scale))))
    inset = float(max(18, int(round(62 * scale))))
    panel_x0 = SAFE_AREA + inset
    panel_x1 = W - SAFE_AREA - inset
    icon_size = float(max(26, int(round(38 * scale))))
    gap = float(max(14, int(round(30 * scale))))
    text_x0 = panel_x0 + pad_x + icon_size + m["gap_icon"] * 2
    text_x1 = panel_x1 - pad_x
    share = max(120.0, text_x1 - text_x0 - gap * 2)
    note_size = _note_pen_size(m, note_lines, share)
    pen = _pen(note_size, _ROLE_WEIGHT["note"])
    naturale = _note_natural_widths(m, note_lines, note_size)
    if len(naturale) > 1 and sum(naturale) > 0:
        widths = [share * value / sum(naturale) for value in naturale]
    elif naturale:
        widths = [share]
    else:
        widths = []
    columns = _note_columns(pen, note_lines, widths)
    line_h = pen.line_h()
    row_gap = max(2, int(round(m["panel_line_gap"] * 0.7)))
    rows = max(len(column) for column in columns)
    content_h = rows * line_h + (rows - 1) * row_gap
    height = content_h + 2 * pad_y
    top = H - SAFE_AREA - height
    ops: list = [_rect_op((panel_x0, top, panel_x1, top + height),
                          m["row_radius"], fill=_fade(S_PURPLE_L, 0.55),
                          kind="panel", uid="footer:panel")]
    icon_y = top + (height - icon_size) / 2
    ops += _icon_ops("cap", panel_x0 + pad_x, icon_y, icon_size, S_PURPLE,
                     uid="footer:icon", owner="footer")
    note_index = 0
    if len(columns) > 1:
        divider_x = text_x0 + widths[0] + gap
        ops.append(_line_op(divider_x, top + pad_y * 0.7, divider_x,
                            top + height - pad_y * 0.7, S_DIVIDER, 1.0,
                            uid="footer:divider", owner="footer"))
    for column_index, lines in enumerate(columns):
        col_x = text_x0 + sum(widths[:column_index]) + gap * 2 * column_index
        block = len(lines) * line_h + (len(lines) - 1) * row_gap
        cursor = top + (height - block) / 2
        for line in lines:
            ops.append(_text_op(pen, (col_x, cursor, col_x + widths[column_index],
                                      cursor + line_h), line, S_MUTED,
                                valign="middle", max_lines=1,
                                min_size=_type_floor("note"),
                                uid=f"footer:note:{note_index}",
                                owner="footer"))
            cursor += line_h + row_gap
            note_index += 1
    return ops, height


# ------------------------------------------------------------
# ДАННЫЕ -> SPECS (что показать; presentation-слой не трогает модели)
# ------------------------------------------------------------

def _progress_text_for(lesson, ctx: dict) -> str:
    """Второстепенная строка прогресса: «Изучено: 8 акад. ч».

    Только основная группа: у преподавателя своей истории учёбы нет.
    """
    if not ctx.get("is_group"):
        return ""
    if is_placeholder_subject(lesson.subject):
        return ""
    normalized = normalize_subject_name(lesson.subject)
    if not normalized:
        return ""
    minutes = ctx.get("subject_totals", {}).get(normalized, 0)
    if minutes > 0:
        return f"Изучено: {format_academic_hours(minutes)}"
    return ""


def _subgroup_shared_teacher(items: list) -> list:
    """Преподаватели подгрупп одной пары (без отменённых строк)."""
    teachers = set()
    for entry in items:
        lesson = entry["lesson"]
        if entry.get("kind") == "removed":
            continue
        if is_placeholder_subject(lesson.subject):
            continue
        teacher = clean_text(lesson.teacher)
        if teacher and teacher != "—":
            teachers.add(teacher)
    return sorted(teachers)


def _change_lines(details: list) -> list:
    """Короткие строки изменений для плашки ВНУТРИ карточки."""
    lines: list = []
    for detail in details or []:
        field = clean_text(detail.get("field"))
        label = clean_text(detail.get("label") or FIELD_LABELS.get(field, "")) \
            or "Изменение"
        old_val = clean_text(detail.get("old"))
        new_val = clean_text(detail.get("new"))
        if field == "subject" or label == "Предмет":
            old_val = display_subject_text(old_val) if old_val else old_val
            new_val = display_subject_text(new_val) if new_val else new_val
        lines.append(f"{label}: {old_val or '—'} → {new_val or '—'}")
    return lines


def _item_dict(lesson, kind: str, details: list, ctx: dict, owner: str,
               uid: str) -> dict:
    room = sanitize_text(getattr(lesson, "room", ""))
    teacher = sanitize_text(getattr(lesson, "teacher", ""))
    groups = clean_text(getattr(lesson, "groups", ""))
    subject_raw = getattr(lesson, "subject", "")
    placeholder = is_placeholder_subject(subject_raw)
    cancelled = placeholder or kind == "removed"
    progress = "" if cancelled else _progress_text_for(lesson, ctx)
    hours = ""
    if progress.startswith("Изучено: "):
        hours = progress[len("Изучено: "):]
    return {
        "lesson": lesson, "kind": kind, "details": details or [],
        "uid": uid, "owner": owner,
        "subgroup": clean_text(getattr(lesson, "subgroup", "")),
        "subject": sanitize_text(display_subject_text(subject_raw)),
        "room": "" if room in ("", "—", "-") else room,
        "teacher": "" if teacher in ("", "—", "-") else teacher,
        "groups": "" if groups in ("", "—", "-") else groups,
        "placeholder": placeholder, "cancelled": cancelled,
        "progress": progress, "hours": hours,
        "change_lines": _change_lines(details),
        "time": _time_range(lesson),
    }


def _time_range(source) -> str:
    start = clean_text(getattr(source, "start", "") or
                      (source.get("start") if isinstance(source, dict) else ""))
    end = clean_text(getattr(source, "end", "") or
                    (source.get("end") if isinstance(source, dict) else ""))
    if start and end:
        return f"{start} — {end}"
    whole = clean_text(getattr(source, "time", "") or
                      (source.get("time") if isinstance(source, dict) else ""))
    return whole.replace("-", "—") if whole else "—"


def build_specs(schedule, changes: list, ctx: dict) -> list:
    """Список карточек дня: пары из расписания + отменённые целиком пары."""
    specs: list = []
    by_number: dict = {}
    for index, pair in enumerate(schedule.pairs):
        number = clean_text(pair.number).upper() or f"#{index + 1}"
        raw_items = _render_items_for_pair(pair, ctx["change_by_key"],
                                          ctx["removed_by_pair"])
        owner = f"card:{index}"
        items = [
            _item_dict(entry["lesson"], entry["kind"], entry.get("details"),
                      ctx, owner, f"{owner}:i{i}")
            for i, entry in enumerate(raw_items)
        ]
        spec = {
            "index": index, "owner": owner, "number": number,
            "time": _time_range(pair),
            "start": clean_text(getattr(pair, "start", "")),
            "end": clean_text(getattr(pair, "end", "")),
            "break": clean_text(getattr(pair, "break_duration", "")),
            "items": items, "raw": raw_items, "synthetic": False,
        }
        by_number[number] = spec
        specs.append(spec)

    extra = len(specs)
    for change in changes or []:
        if getattr(change, "kind", "") != "removed":
            continue
        number = clean_text(change.pair).upper()
        spec = by_number.get(number)
        old = change.old or {}
        if spec is None:
            spec = {
                "index": extra, "owner": f"card:{extra}",
                "number": number or "—",
                "time": _time_range(old),
                "start": clean_text(old.get("start")),
                "end": clean_text(old.get("end")),
                "break": "", "items": [],
                "raw": [], "synthetic": True,
            }
            by_number[number] = spec
            specs.append(spec)
            extra += 1
        lesson = _lesson_from_normalized_item(old)
        spec["items"].append(_item_dict(lesson, "removed", [], ctx,
                                       spec["owner"],
                                       f"{spec['owner']}:i{len(spec['items'])}"))
        spec["raw"].append({"lesson": lesson, "kind": "removed",
                            "details": []})

    specs.sort(key=lambda item: (ROMAN_PAIRS.get(item["number"], 99),
                                 item["index"]))
    for index, spec in enumerate(specs):
        spec["index"] = index
        spec["owner"] = f"card:{index}"
        for pos, item in enumerate(spec["items"]):
            item["owner"] = spec["owner"]
            item["uid"] = f"{spec['owner']}:i{pos}"
        cancelled = [item for item in spec["items"] if item["cancelled"]]
        spec["cancelled_all"] = bool(spec["items"]) and len(cancelled) == len(
            spec["items"])
        kinds = {item["kind"] for item in spec["items"]}
        tag, tag_fill, tag_bg = "", S_MUTED, S_PURPLE_L
        if "removed" in kinds:
            tag, tag_fill, tag_bg = "ОТМЕНА", S_RED_D, S_RED_L
        elif "added" in kinds:
            tag, tag_fill, tag_bg = "ДОБАВЛЕНО", S_GREEN_D, S_GREEN_L
        elif "changed" in kinds:
            tag, tag_fill, tag_bg = "ИЗМЕНЕНО", S_PURPLE_D, S_PURPLE_L
        spec["tag"], spec["tag_fill"], spec["tag_bg"] = tag, tag_fill, tag_bg
        spec["teachers"] = _subgroup_shared_teacher(spec["raw"])
        spec["multi"] = (len(spec["items"]) > 1
                         or any(i["subgroup"] for i in spec["items"]))
    return specs


def _breath_metrics(m: dict, extra: float) -> dict:
    """Раздвинуть внутренние отступы карточки, НЕ трогая кегли.

    Используется, когда занятий мало и холст большой: пустоту закрывает
    воздух внутри карточек, а не раздутый шрифт.
    """
    if extra <= 0:
        return m
    m2 = dict(m)
    pad_add = extra * 0.30
    m2["pad_t"] = m["pad_t"] + pad_add
    m2["pad_b"] = m["pad_b"] + pad_add
    gap_add = extra * 0.13
    for key in ("gap_row", "gap_subject", "gap_items"):
        m2[key] = m[key] + gap_add
    # Отступы плашек не «дышат»: иначе компактный блок отмены рос бы
    # быстрее обычной карточки и переставал быть компактным.
    return m2


def build_plan(schedule, changes: list, title: Optional[str], scale: float,
               H: int, ctx: dict, specs: Optional[list] = None) -> dict:
    """Чистый расчёт layout: всё измеряется, ничего не рисуется."""
    m = _metrics_for(scale)
    W = SCHEDULE_WIDTH
    if specs is None:
        specs = build_specs(schedule, changes, ctx)
    ops: list = list(_decor_ops(W, H))
    owners: dict = {}

    header_ops, content_top = layout_header(m, schedule, ctx, title, W)
    ops += header_ops

    note_lines = list(ctx.get("note_lines") or [])
    footer_ops, footer_h = layout_footer(m, W, H, note_lines)
    ops += footer_ops
    footer_top = float(H) - SAFE_AREA - footer_h

    inner_x = float(SAFE_AREA)
    inner_w = float(W - 2 * SAFE_AREA)
    available = footer_top - content_top

    def measure(metrics: dict) -> tuple:
        """Один проход измерения: стек блоков + их суммарная высота."""
        stack = []
        for index, spec in enumerate(specs):
            card_ops, card_h = layout_card(metrics, spec, inner_w,
                                          has_next=index + 1 < len(specs),
                                          index=index)
            stack.append({"kind": "card", "ops": card_ops, "height": card_h,
                          "spec": spec})
        gap = metrics["card_gap"]
        total = (sum(item["height"] for item in stack)
                 + gap * max(0, len(stack) - 1))
        return stack, total, gap

    # Карточки не растягиваем под высоту холста: 1–2 занятия остаются
    # компактными, лишний воздух — под списком, подвал прижат к низу.
    stack, total, gap = measure(m)
    fits = total <= available
    shift_top = 0.0

    # --- расстановка ---
    cards: list = []
    y = float(content_top) + shift_top
    if not stack:
        empty_h = max(220.0, min(420.0, available))
        y = content_top + max(0.0, (available - empty_h) / 2)
        empty_ops, _ = layout_empty_card(m, inner_w, empty_h)
        ops += _shift_ops(empty_ops, inner_x, y)
        owners["empty"] = (inner_x, y, inner_x + inner_w, y + empty_h)
    else:
        for entry in stack:
            spec = entry["spec"]
            height = entry["height"]
            box = (inner_x, y, inner_x + inner_w, y + height)
            owners[spec["owner"]] = box
            ops.append(_shadow_op(box, m["radius"], uid=f"{spec['owner']}:shadow",
                                  blur=m["shadow"], dy=m["shadow_y"],
                                  alpha=m["shadow_alpha"]))
            ops.append(_rect_op(box, m["radius"], fill=S_CARD,
                                outline=S_BORDER, width=1.1, kind="card",
                                uid=f"{spec['owner']}:bg"))
            ops += _shift_ops(entry["ops"], inner_x, y)
            cards.append({"owner": spec["owner"], "box": box, "spec": spec})
            y += height + gap

    owners["header"] = (0.0, 0.0, float(W), float(content_top + shift_top))
    owners["footer"] = (0.0, float(footer_top), float(W),
                        float(footer_top + footer_h))

    plan = {
        "W": W, "H": H, "metrics": m, "scale": m["scale"],
        "ops": ops, "owners": owners, "cards": cards, "specs": specs,
        "content_top": content_top + shift_top,
        "content_bottom": y - gap if stack else y,
        "footer_top": footer_top, "footer_h": footer_h,
        "note_lines": note_lines,
        "fits": fits, "needed": total + content_top + footer_h + 2 * SAFE_AREA,
        "title": title, "elements": ops, "left": (inner_x, inner_w),
        "empty": not stack,
    }
    return plan


def fit_scale(schedule, changes, title, ctx, specs, H: int) -> dict:
    """Наибольший читаемый масштаб для данной высоты холста.

    Дискретный бинарный поиск по сетке S_SCALE_STEP: размеры монотонны по
    масштабу, поэтому «наибольший влезший» — это и есть оптимум.
    """
    lo, hi = S_SCALE_MIN, S_SCALE_MAX
    steps = int(round((hi - lo) / S_SCALE_STEP))
    best = build_plan(schedule, changes, title, lo, H, ctx, specs)
    low, high = 0, steps
    while low <= high:
        mid = (low + high) // 2
        scale = round(lo + mid * S_SCALE_STEP, 3)
        if scale == best["scale"]:
            break
        plan = build_plan(schedule, changes, title, scale, H, ctx, specs)
        if plan["fits"]:
            best = plan
            low = mid + 1
        else:
            high = mid - 1
    return best


def choose_plan(schedule, changes, title, ctx, specs) -> dict:
    """Выбор формата: 2:3, при нехватке воздуха — выше по лестнице.

    Холст растёт только если иначе текст упёрся бы в пол; вертикального
    кропа не бывает ни при каких обстоятельствах.
    """
    fallback = None
    for H, min_scale in SCHEDULE_RATIO_LADDER:
        plan = fit_scale(schedule, changes, title, ctx, specs, H)
        if plan["fits"] and plan["scale"] >= min_scale:
            return plan
        fallback = plan
    needed = int((fallback or {}).get("needed") or SCHEDULE_HEIGHT_MAX)
    H = max(SCHEDULE_RATIO_LADDER[-1][0], min(needed, SCHEDULE_HEIGHT_MAX))
    plan = fit_scale(schedule, changes, title, ctx, specs, H)
    for _ in range(3):
        if plan["fits"]:
            break
        target = max(H, int(plan["needed"]) + 2 * SAFE_AREA)
        if target <= H:
            break
        H = target
        plan = fit_scale(schedule, changes, title, ctx, specs, H)
    return plan


# ------------------------------------------------------------
# RENDER: ops -> изображение
# ------------------------------------------------------------

class SupersampleCanvas:
    """Холст с супер-сэмплингом: рисуем в SS× больше, отдаём в 1x."""

    __slots__ = ("W", "H", "ss", "image", "draw")

    def __init__(self, W: int, H: int, bg: str, ss: int = SUPER_SAMPLE):
        self.W, self.H, self.ss = int(W), int(H), int(ss)
        self.image = Image.new("RGB", (self.W * self.ss, self.H * self.ss), bg)
        self.draw = ImageDraw.Draw(self.image)

    def finish(self) -> Image.Image:
        """Даунскейл LANCZOS: текст остаётся резким, полутона — гладкие."""
        if self.ss == 1:
            return self.image
        return self.image.resize((self.W, self.H), Image.LANCZOS)


def _draw_shadow(canvas: SupersampleCanvas, op: dict) -> None:
    """Мягкая тень карточки: размытый прямоугольник под ней.

    Размытие считается в масштабе финальной картинки (1x) — это вчетверо
    дешевле по пикселям и после даунскейла неотличимо от 2x.
    """
    W, H, ss = canvas.W, canvas.H, canvas.ss
    x0, y0, x1, y1 = op["box"]
    blur = float(op.get("blur") or 16)
    dy = float(op.get("dy") or 0)
    inset = max(1.0, blur * 0.18)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (x0 + inset, y0 + dy + inset * 0.4, x1 - inset, y1 + dy - inset * 0.2),
        radius=max(1.0, op["radius"] - inset * 0.4),
        fill=int(op.get("alpha") or 70),
    )
    mask = mask.filter(ImageFilter.GaussianBlur(blur))
    if ss != 1:
        mask = mask.resize((W * ss, H * ss), Image.BILINEAR)
    canvas.image.paste(Image.new("RGB", canvas.image.size,
                                 op.get("fill") or S_SHADOW), (0, 0), mask)


def _draw_svg_icon(canvas: SupersampleCanvas, op: dict) -> None:
    """Вклеить Lucide-иконку. Размер — в пикселях суперсэмпла, чтобы штрих был резким."""
    x0, y0, x1, y1 = op["box"]
    ss = canvas.ss
    side = max(1, int(round(max(x1 - x0, y1 - y0) * ss)))
    glyph = schedule_icons.icon_image(op["name"], side, op["color"])
    canvas.image.paste(
        glyph, (int(round(x0 * ss)), int(round(y0 * ss))), glyph,
    )


def draw_ops(canvas: SupersampleCanvas, ops: list) -> None:
    """Единственное место, которое что-то рисует: только исполняет ops."""
    draw = canvas.draw
    ss = canvas.ss
    for op in ops:
        kind = op["op"]
        if kind == "rect":
            x0, y0, x1, y1 = (op["box"][i] * ss for i in range(4))
            w, h = x1 - x0, y1 - y0
            if w < 1 or h < 1:
                continue
            radius = min(float(op.get("radius") or 0), min(w, h) / 2 - 0.5)
            radius = max(0.0, radius)
            outline = op.get("outline")
            width = int(round((op.get("width") or 0) * ss))
            draw.rounded_rectangle((x0, y0, x1, y1), radius=radius,
                                   fill=op.get("fill"),
                                   outline=outline if width else None,
                                   width=width if outline else 0)
        elif kind == "ellipse":
            x0, y0, x1, y1 = (op["box"][i] * ss for i in range(4))
            if x1 - x0 < 1 or y1 - y0 < 1:
                continue
            outline = op.get("outline")
            width = int(round((op.get("width") or 0) * ss))
            draw.ellipse((x0, y0, x1, y1), fill=op.get("fill"),
                         outline=outline if width else None,
                         width=width if outline else 0)
        elif kind == "text":
            font = _font_for(int(op["size"] * ss), op["weight"])
            spacing = (op.get("spacing") or 0.0) * ss
            fill = op["fill"]
            for (ox, oy), line in zip(op["origins"], op["lines"]):
                if not line:
                    continue
                if spacing:
                    cursor = ox * ss
                    for ch in line:
                        draw.text((cursor, oy * ss), ch, font=font, fill=fill)
                        cursor += font.getlength(ch) + spacing
                else:
                    draw.text((ox * ss, oy * ss), line, font=font, fill=fill)
        elif kind == "icon":
            _draw_svg_icon(canvas, op)
        elif kind == "poly":
            draw.polygon([(px * ss, py * ss) for px, py in op["points"]],
                         fill=op["fill"])
        elif kind == "line":
            x0, y0, x1, y1 = (op["points"][i] * ss for i in range(4))
            width = max(1, int(round(op.get("width") or 1) * ss))
            # Хайрлайн должен попадать в целые пиксели ДИЗАЙНА: после
            # даунскейла LANCZOS линия на полупикселе растворяется в фоне.
            if abs(y1 - y0) < 0.5:
                y0 = y1 = _math.floor(y0 / ss) * ss + ss / 2.0
            elif abs(x1 - x0) < 0.5:
                x0 = x1 = _math.floor(x0 / ss) * ss + ss / 2.0
            draw.line((x0, y0, x1, y1), fill=op["color"], width=width)
        elif kind == "shadow":
            _draw_shadow(canvas, op)


# ------------------------------------------------------------
# ВАЛИДАЦИЯ LAYOUT (перед сохранением)
# ------------------------------------------------------------

def _intersect_area(a, b) -> float:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def validate_layout(plan: dict) -> list:
    """Проверка layout ДО сохранения PNG.

    Guarantees, что пользователь увидит картинку целиком:
    - у каждого элемента есть зона и bounding box;
    - ink-бокс текста внутри своей зоны (переснимается Pen'ом, а не
      берётся на веру);
    - зона внутри owner-прямоугольника (карточки/шапки/подвала);
    - контент не выходит за canvas и не прижат к краю (safe area);
    - текст аудитории не мельче ROOM_FONT_MIN;
    - текстовые блоки не перекрываются;
    - карточки не наезжают друг на друга и на подвал.

    Возвращает список проблем (пустой — layout корректен).
    """
    problems: list = []
    eps = 1.6
    W, H = plan["W"], plan["H"]
    owners = plan.get("owners") or {}
    ops = plan.get("ops") or plan.get("elements") or []
    seen_ids: dict = {}

    for op in ops:
        uid = op.get("id") or "?"
        box = op.get("box")
        if box is None:
            if op["op"] != "poly":
                problems.append(f"«{uid}»: элемент без зоны (box)")
            continue
        x0, y0, x1, y1 = box
        if x1 + eps < x0 or y1 + eps < y0:
            problems.append(f"«{uid}»: вырожденная зона")
            continue
        # 1) холст
        if x0 < -eps or y0 < -eps or x1 > W + eps or y1 > H + eps:
            problems.append(f"«{uid}»: вышел за холст")
        # 2) safe area — только для значимого контента
        if op.get("kind") in ("text", "card", "panel", "chip", "band"):
            if (x0 < SAFE_AREA - eps or y0 < SAFE_AREA - eps
                    or x1 > W - SAFE_AREA + eps or y1 > H - SAFE_AREA + eps):
                problems.append(f"«{uid}»: вышел за safe area")
        # 3) owner
        owner = op.get("owner")
        if owner is not None:
            zone = owners.get(owner)
            if zone is None:
                problems.append(f"«{uid}»: неизвестный owner «{owner}»")
            elif (x0 < zone[0] - eps or y0 < zone[1] - eps
                  or x1 > zone[2] + eps or y1 > zone[3] + eps):
                problems.append(f"«{uid}»: зона вышла за {owner}")
        # 4) текст: независимый пересчёт метрик
        if op["op"] == "text":
            lines = op.get("lines")
            bbox = op.get("bbox")
            if not lines or bbox is None:
                problems.append(f"«{uid}»: текст без данных")
                continue
            pen = _pen(op["size"], op["weight"])
            for line in lines:
                if line and pen.width(line) > (x1 - x0) + eps:
                    problems.append(f"«{uid}»: строка шире зоны")
                    break
            if (bbox[0] < x0 - eps or bbox[1] < y0 - eps
                    or bbox[2] > x1 + eps or bbox[3] > y1 + eps):
                problems.append(f"«{uid}»: текст вышел за зону")
            if op.get("role") == "room" and op["size"] < ROOM_FONT_MIN - 0.5:
                problems.append(
                    f"«{uid}»: аудитория мельче пола {ROOM_FONT_MIN}px"
                )
            seen_ids.setdefault(uid, []).append(op)

    # 5) пересечения текстовых блоков внутри одного блока-владельца
    texts = [op for op in ops if op["op"] == "text" and op.get("bbox")]
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            if a.get("owner") != b.get("owner"):
                continue
            if _intersect_area(a["bbox"], b["bbox"]) > 4:
                problems.append(
                    f"пересечение: «{a.get('id')}» × «{b.get('id')}»"
                )

    # 6) карточки: не перекрываются, не наезжают на подвал
    cards = plan.get("cards") or []
    for i in range(len(cards)):
        box_a = cards[i]["box"]
        if box_a[3] > plan["footer_top"] + eps:
            problems.append(f"«{cards[i]['owner']}»: карточка наехала на подвал")
        for j in range(i + 1, len(cards)):
            box_b = cards[j]["box"]
            if _intersect_area(box_a, box_b) > 4:
                problems.append(
                    f"карточки перекрываются: {cards[i]['owner']} × "
                    f"{cards[j]['owner']}"
                )
    if not plan.get("fits") and not plan.get("empty"):
        problems.append("контент не влезает в выбранный холст")
    return problems


# ------------------------------------------------------------
# ТОЧКА ВХОДА: render_schedule_image
# ------------------------------------------------------------

# Последний layout-отчёт — для диагностики и тестов.
_LAST_RENDER: dict = {}


def render_schedule_image(
    schedule: Schedule,
    changes=None,
    title: Optional[str] = None,
) -> Path:
    """PNG расписания ОДНОГО ДНЯ, заточенный под превью Telegram.

    - Формат вертикальный: 1080×1620 (2:3), при плотном дне — выше по
      лестнице форматов; кропа не бывает никогда.
    - Одна картинка = одна дата. Никаких «сегодня + завтра» вместе.
    - Карточка пары: номер, время, предмет, преподаватель и аудитория —
      лавандовая колонка с крупным кодом комнаты, не зелёная плашка.
    - Отдельной колонки «Изменения» нет: ИЗМЕНЕНО/ДОБАВЛЕНО и пилюля
      «Занятие отменено» показываются внутри карточки соответствующей пары.
    - `changes=None` -> обычный день; `changes=[...]` -> карточки с
      инлайн-изменениями. `title` -> метка в шапке.
    - Перед сохранением layout проходит validate_layout(): ни один
      bounding box не выходит за canvas/safe area/карточку; текст
      аудитории не ниже ROOM_FONT_MIN.

    Бизнес-логика (история учёбы, прогнозы, подписки) не меняется —
    переписан только presentation/rendering слой.
    """
    try:
        changes = list(changes or [])
        is_group = schedule.schedule_type == "group"

        subject_totals = {}
        if is_group:
            register_subjects_from_schedule(schedule)
            subject_totals = load_subject_totals(schedule.group)
        forecast = get_study_forecast(schedule.group) if is_group else None
        note_lines = study_note_lines(forecast) if forecast is not None else []

        ctx = {
            "is_group": is_group,
            "subject_totals": subject_totals,
            "forecast": forecast,
            "note_lines": note_lines,
            "change_by_key": {
                (clean_text(c.pair).upper(), clean_text(c.subgroup) or None): c
                for c in changes
            },
            "removed_by_pair": {},
        }
        for c in changes:
            if c.kind == "removed":
                ctx["removed_by_pair"].setdefault(
                    clean_text(c.pair).upper(), []
                ).append(c)

        specs = build_specs(schedule, changes, ctx)
        plan = choose_plan(schedule, changes, title, ctx, specs)
        problems = validate_layout(plan)
        if problems:
            # Паникуем не сразу: пробуем самый компактный читаемый
            # вариант на максимальном холсте — он гарантированно
            # ничего не режет.
            backup = build_plan(schedule, changes, title, S_SCALE_MIN,
                                max(plan["H"], SCHEDULE_HEIGHT_MAX), ctx, specs)
            backup_problems = validate_layout(backup)
            if len(backup_problems) < len(problems):
                plan, problems = backup, backup_problems

        canvas = SupersampleCanvas(plan["W"], plan["H"], S_BG)
        draw_ops(canvas, plan["ops"])
        image = canvas.finish()

        kind = "staff" if schedule.schedule_type == "staff" else "group"
        staff_part = f"_{schedule.staff_id}" if schedule.staff_id else ""
        suffix = "_changed" if changes else ""
        owner = str(schedule.staff_id) if schedule.staff_id else schedule.group
        owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:10]
        content_hash = hashlib.sha256(
            (schedule_signature(schedule) + repr([(c.kind, c.pair, c.subgroup) for c in changes]))
            .encode("utf-8")
        ).hexdigest()[:12]
        filename = (
            f"schedule_{kind}{staff_part}_{schedule.date.isoformat()}_"
            f"{owner_hash}_{content_hash}{suffix}.png"
        )
        path = IMAGE_DIR / filename
        # PNG: максимум сжатия — картинка уходит в Telegram без перекодирования.
        image.save(path, "PNG", optimize=True, compress_level=9)

        truncated = sorted({op["id"] for op in plan["ops"]
                           if op["op"] == "text" and op.get("truncated")})
        _LAST_RENDER.update({
            "path": str(path),
            "size": (plan["W"], plan["H"]),
            "scale": plan["scale"],
            "metrics": dict(plan["metrics"]),
            "plan": plan,
            "problems": problems,
            "truncated": truncated,
            "elements": len(plan["ops"]),
            "cards": len(plan["cards"]),
        })
        logger.info(
            "Изображение сохранено: %s (%sx%s, соотношение %.2f, масштаб %s, "
            "карточек %s, проблем layout: %s, эллипсис: %s)",
            path, plan["W"], plan["H"], plan["H"] / plan["W"], plan["scale"],
            len(plan["cards"]),
            len(problems), ", ".join(truncated) if truncated else "нет",
        )
        if problems:
            logger.error("Layout-валидация: %s", problems)
        return path

    except Exception:
        logger.exception("Ошибка генерации изображения")
        raise
# ============================================================
# ПОСТОЯННЫЙ КЭШ РАСПИСАНИЙ И PNG
# ============================================================


def schedule_target_key(schedule: Schedule) -> str:
    if schedule.schedule_type == "staff":
        return f"staff:{schedule.staff_id}"
    group_id = next((key for key, value in GROUPS.items() if value.name == schedule.group), GROUP_ID)
    return f"group:{group_id}"


def _cache_lock(key: str) -> asyncio.Lock:
    # Создаётся только в event loop; отдельный lock на группу/дату.
    return _CACHE_LOCKS.setdefault(key, asyncio.Lock())


def load_cached_schedule(target_key: str, day: date) -> Optional[Schedule]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM schedule_cache WHERE target_key = ? AND date = ?",
            (target_key, day.isoformat()),
        ).fetchone()
    if not row:
        return None
    try:
        kind, owner_id = target_key.split(":", 1)
        data = json.loads(row["data"])
        if not data:
            return None
        if kind == "staff":
            member = STAFF_BY_ID.get(int(owner_id))
            if not member:
                return None
            schedule = schedule_from_storage(data, day, group=member.full_name)
            schedule.schedule_type = "staff"
            schedule.staff_id = member.staff_id
            schedule.staff_name = member.full_name
            return schedule
        group = get_group(int(owner_id))
        return schedule_from_storage(data, day, group=group.name)
    except (TypeError, ValueError, KeyError):
        return None


def schedule_image_context(schedule: Schedule) -> str:
    if schedule.schedule_type != "group":
        return "staff:v2"
    forecast = get_study_forecast(schedule.group)
    payload = {
        "studied": forecast.studied_minutes,
        "remaining": forecast.remaining_minutes,
        "total": forecast.total_minutes,
        "subjects": load_subject_totals(schedule.group),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_schedule_cache(schedule: Schedule, image_path: Optional[Path] = None,
                        image_context: Optional[str] = None) -> str:
    target_key = schedule_target_key(schedule)
    signature = schedule_signature(schedule)
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as conn:
        old = conn.execute(
            "SELECT hash, image_path, image_context FROM schedule_cache WHERE target_key = ? AND date = ?",
            (target_key, schedule.date.isoformat()),
        ).fetchone()
        old_path = old["image_path"] if old else ""
        unchanged = bool(old and old["hash"] == signature)
        path = str(image_path) if image_path else (old_path if unchanged else "")
        context = image_context if image_context is not None else (old["image_context"] if old and unchanged else "")
        if old_path and old_path != path:
            Path(old_path).unlink(missing_ok=True)
        conn.execute("""
            INSERT INTO schedule_cache
                (target_key, date, hash, data, image_path, image_context, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_key, date) DO UPDATE SET
                hash = excluded.hash, data = excluded.data,
                image_path = excluded.image_path, image_context = excluded.image_context,
                updated_at = excluded.updated_at
        """, (target_key, schedule.date.isoformat(), signature,
              json.dumps(normalize_schedule(schedule), ensure_ascii=False), path, context, stamp))
    return signature


def invalidate_group_image_cache(group_name: str) -> None:
    group_id = next((key for key, value in GROUPS.items() if value.name == group_name), GROUP_ID)
    target_key = f"group:{group_id}"
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT image_path FROM schedule_cache WHERE target_key = ? AND image_path != ''",
            (target_key,),
        ).fetchall()
        conn.execute(
            "UPDATE schedule_cache SET image_path = '', image_context = '' WHERE target_key = ?",
            (target_key,),
        )
    for row in rows:
        Path(row["image_path"]).unlink(missing_ok=True)


def cache_has_image(target_key: str, day: date) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT image_path FROM schedule_cache WHERE target_key = ? AND date = ?",
            (target_key, day.isoformat()),
        ).fetchone()
    return bool(row and row["image_path"] and Path(row["image_path"]).is_file())


def cached_image_path(schedule: Schedule) -> Optional[Path]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT hash, image_path, image_context FROM schedule_cache WHERE target_key = ? AND date = ?",
            (schedule_target_key(schedule), schedule.date.isoformat()),
        ).fetchone()
    if (not row or row["hash"] != schedule_signature(schedule)
            or row["image_context"] != schedule_image_context(schedule)
            or not row["image_path"]):
        return None
    path = Path(row["image_path"])
    return path if path.is_file() else None


async def get_cached_image(schedule: Schedule) -> Path:
    key = f"image:{schedule_target_key(schedule)}:{schedule.date.isoformat()}:{schedule_signature(schedule)}"
    async with _cache_lock(key):
        existing = cached_image_path(schedule)
        if existing:
            return existing
        context = schedule_image_context(schedule)
        path = render_schedule_image(schedule)
        save_schedule_cache(schedule, path, context)
        return path


async def get_schedule_cached(day: date, group_id: Optional[int] = None,
                              force_refresh: bool = False) -> Schedule:
    group = get_group(group_id)
    target_key = f"group:{group.id}"
    if not force_refresh:
        cached = load_cached_schedule(target_key, day)
        if cached is not None:
            return cached
    async with _cache_lock(f"schedule:{target_key}:{day.isoformat()}"):
        if not force_refresh:
            cached = load_cached_schedule(target_key, day)
            if cached is not None:
                return cached
        schedule = await get_schedule(day, group.id)
        if schedule.lessons:
            save_schedule_cache(schedule)
        return schedule


async def get_staff_schedule_cached(staff_id: int, day: date,
                                    force_refresh: bool = False) -> Schedule:
    target_key = f"staff:{int(staff_id)}"
    if not force_refresh:
        cached = load_cached_schedule(target_key, day)
        if cached is not None:
            return cached
    async with _cache_lock(f"schedule:{target_key}:{day.isoformat()}"):
        if not force_refresh:
            cached = load_cached_schedule(target_key, day)
            if cached is not None:
                return cached
        schedule = await get_staff_schedule(staff_id, day)
        if schedule.lessons:
            save_schedule_cache(schedule)
        return schedule


# ============================================================
# КАРТИНКА СОСТОЯНИЯ БОТА (/status)
# ============================================================

# Старт процесса и момент последней проверки расписания монитором.
_PROCESS_STARTED_MONOTONIC = time.monotonic()
_LAST_SCHEDULE_CHECK: dict = {"at": None}


def format_uptime(seconds: float) -> str:
    """Аптайм процесса: «2 д 4 ч», «5 ч 12 мин», «48 мин», «35 с»."""
    seconds = max(0, int(seconds or 0))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин"
    return f"{secs} с"


def _read_proc_status() -> dict:
    """Поля /proc/self/status (Linux): текущее потребление процесса."""
    info: dict = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
    except OSError:
        return {}
    return info


def _process_cpu_seconds() -> Optional[float]:
    """Процессорное время процесса (utime+stime) в секундах."""
    try:
        with open("/proc/self/stat", "r", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
        # Поле comm может содержать пробелы и скобки — режем по последней «)».
        end = stat.rfind(")")
        fields = stat[end + 1:].split()
        utime, stime = int(fields[11]), int(fields[12])
        try:
            clk = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            clk = 100
        if clk <= 0:
            clk = 100
        return (utime + stime) / clk
    except (OSError, ValueError, IndexError):
        # Запасной путь для Unix-систем без /proc.
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            return usage.ru_utime + usage.ru_stime
        except Exception:
            return None


def get_runtime_stats() -> dict:
    """Текущее потребление ресурсов процессом — только факт, без лимитов."""
    uptime = max(0.0, time.monotonic() - _PROCESS_STARTED_MONOTONIC)
    status = _read_proc_status()

    rss_kb: Optional[int] = None
    match = re.search(r"(\d+)\s*kB", status.get("VmRSS", ""))
    if match:
        rss_kb = int(match.group(1))

    threads: Optional[int] = None
    if status.get("Threads", "").isdigit():
        threads = int(status["Threads"])
    if threads is None:
        threads = threading.active_count()

    cpu_seconds = _process_cpu_seconds()
    cpu_percent: Optional[float] = None
    # Средняя загрузка ЦП с момента запуска — устойчивая характеристика
    # того, сколько бот потребляет сейчас; на старте процесса проценты
    # нестабильны, поэтому первые секунды показываем только время ЦП.
    if cpu_seconds is not None and uptime >= 10:
        cpu_percent = min(999.0, cpu_seconds / uptime * 100)

    return {
        "uptime_seconds": uptime,
        "cpu_seconds": cpu_seconds,
        "cpu_percent": cpu_percent,
        "rss_kb": rss_kb,
        "threads": threads,
    }


def format_size(size_bytes: int) -> str:
    """«132 КБ» / «1,2 МБ»."""
    size = max(0, int(size_bytes or 0))
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}".replace(".", ",") + " МБ"
    if size >= 1024:
        return f"{round(size / 1024)} КБ"
    return f"{size} Б"


def _database_stats() -> dict:
    """Размер БД (с WAL) и количество накопленных записей."""
    total = 0
    try:
        if DB_PATH.exists():
            total = DB_PATH.stat().st_size
        for suffix in ("-wal", "-shm"):
            extra = DB_PATH.parent / (DB_PATH.name + suffix)
            try:
                total += extra.stat().st_size
            except OSError:
                pass
    except OSError:
        total = 0

    lesson_rows = subjects = 0
    try:
        with db_connect() as conn:
            lesson_rows = int(
                conn.execute("SELECT COUNT(*) FROM lesson_history").fetchone()[0]
            )
            subjects = int(
                conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]
            )
    except Exception:
        logger.exception("Ошибка статистики БД")
    return {"size_bytes": total, "lesson_rows": lesson_rows, "subjects": subjects}


def _plural(count: int, one: str, few: str, many: str) -> str:
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def render_status_image(chat_id: int) -> Path:
    """PNG «Состояние бота» в дизайне картинки расписания.

    Внутри — подписки (функции прежнего текстового /status), прогресс
    учёбы с прогнозом и текущее потребление ресурсов процессом.
    Максимумы/лимиты ресурсов намеренно не показываются — только то,
    что бот использует сейчас.
    """
    try:
        settings = get_user_settings(chat_id)
        group_id = settings.get("group_id")
        group = get_group(group_id) if group_id is not None else None
        forecast = get_study_forecast(group.name) if group is not None else None
        runtime = get_runtime_stats()
        db_stats = _database_stats()
        created = subscriber_info(chat_id)
        subscribers_total = len(load_subscribers(group.id)) if group is not None else None

        # Шрифты
        font_label = get_font(24, bold=True)
        font_group = get_font(72, bold=True)
        font_date = get_font(30)
        font_title = get_font(26, bold=True)
        font_big = get_font(40, bold=True)
        font_key = get_font(26)
        font_value = get_font(26, bold=True)
        font_small = get_font(24)

        # Геометрия — та же сетка, что у картинки расписания.
        W = IMAGE_WIDTH
        MARGIN = 58
        x1, x2 = MARGIN, W - MARGIN
        inner = 48
        left = x1 + inner
        right = x2 - inner
        HEADER_H = 250
        card_gap = 28
        row_h = 44
        row_gap = 12
        title_gap = 26
        card_top = 34
        card_bottom = 38
        bar_h = 22
        bar_gap_top = 16
        bar_gap_bottom = 24

        now = now_local()

        # ---------- карточка «Учёба» ----------
        if forecast is None:
            study_headline = "Группа не выбрана"
            study_note = "Выберите группу, чтобы видеть прогресс учёбы"
            study_rows = []
        else:
            study_headline = study_badge_text(forecast)
            if forecast.studied_minutes <= 0:
                study_note = "Прогноз появится после первых завершённых занятий"
            elif not forecast.remaining_minutes:
                study_note = "Учебный год завершён"
            else:
                study_note = ""

            study_rows = []
            if forecast.remaining_minutes:
                study_rows.append(
                    ("Прогноз на учебный год",
                     f"≈ {format_academic_hours(forecast.total_minutes)}")
                )
                study_rows.append(
                    ("Осталось при текущем темпе",
                     f"≈ {format_academic_hours(forecast.remaining_minutes)}")
                )
                if forecast.pace_minutes_per_day:
                    study_rows.append(
                        ("Темп",
                         f"≈ {format_academic_hours(int(round(forecast.pace_minutes_per_day)))}"
                         " в учебный день")
                    )
            study_rows.append(
                ("Учебные дни (с 1 сентября по 30 июня)",
                 f"пройдено {forecast.elapsed_study_days}"
                 f" · осталось {forecast.remaining_study_days}")
            )
            if forecast.elapsed_study_days:
                study_rows.append(
                    ("Дней с занятиями",
                     f"{forecast.active_days} из {forecast.elapsed_study_days}")
                )

        # ---------- карточка «Ресурсы» ----------
        res_rows = [("Аптайм", format_uptime(runtime["uptime_seconds"]))]
        if runtime["cpu_percent"] is not None:
            res_rows.append(
                ("ЦП (в среднем с запуска)",
                 f"{runtime['cpu_percent']:.1f}".replace(".", ",") + " %")
            )
        elif runtime["cpu_seconds"] is not None:
            res_rows.append(("ЦП (накоплено)", f"{runtime['cpu_seconds']:.1f} с"))
        if runtime["rss_kb"] is not None:
            res_rows.append(
                ("Память (RSS)", f"{round(runtime['rss_kb'] / 1024)} МБ")
            )
        if runtime["threads"] is not None:
            res_rows.append(("Потоки", str(runtime["threads"])))
        res_rows.append(("База данных", format_size(db_stats["size_bytes"])))
        res_rows.append(
            ("История занятий",
             f"{db_stats['lesson_rows']} "
             f"{_plural(db_stats['lesson_rows'], 'запись', 'записи', 'записей')}"
             f" · {db_stats['subjects']} "
             f"{_plural(db_stats['subjects'], 'предмет', 'предмета', 'предметов')}")
        )

        # ---------- карточка «Подписки» ----------
        sub_rows = []
        if created and group is not None:
            sub_rows.append(("Этот чат", f"подписан · с {created}"))
        else:
            sub_rows.append(("Этот чат", "не подписан"))
        sub_rows.append(
            ("Всего подписок", str(subscribers_total) if subscribers_total is not None else "—")
        )
        sub_rows.append(
            ("Проверка изменений", f"каждые {CHECK_INTERVAL // 60} мин")
        )
        last_check = _LAST_SCHEDULE_CHECK.get("at")
        sub_rows.append(
            ("Последняя проверка",
             last_check.strftime("%d.%m %H:%M") if last_check else "—")
        )
        sub_rows.append(("Часовой пояс", f"{TIMEZONE} (UTC+5)"))

        # ---------- размеры карточек ----------
        def card_height(title: str, rows: list, *, big: str = "",
                        note: str = "", bar: bool = False) -> int:
            height = card_top + _text_h(font_title) + title_gap
            if big:
                height += _text_h(font_big) + 18
            if bar:
                height += bar_gap_top + bar_h + bar_gap_bottom
            if note:
                height += _text_h(font_small) + 10
            height += len(rows) * row_h + max(0, len(rows) - 1) * row_gap
            return height + card_bottom

        show_bar = bool(forecast and forecast.total_minutes > 0 and forecast.studied_minutes > 0)
        cards = [
            ("УЧЁБА", study_rows,
             {"big": study_headline, "note": study_note, "bar": show_bar}),
            ("РЕСУРСЫ · СЕЙЧАС", res_rows, {}),
            ("ПОДПИСКИ И УВЕДОМЛЕНИЯ", sub_rows, {}),
        ]
        heights = [card_height(t, r, **kw) for t, r, kw in cards]
        content_top = HEADER_H + 36
        content_bottom = content_top + sum(heights) \
            + max(0, len(cards) - 1) * card_gap + 8

        # Сноска про академический час — та же, что на картинке
        # расписания: единая точка формирования study_note_lines().
        note_lines = study_note_lines(forecast) if forecast is not None else []
        font_note = get_font(21)
        note_gap = 34
        note_line_gap = 8
        note_line_h = _text_h(font_note)
        note_h = (
            len(note_lines) * note_line_h
            + max(0, len(note_lines) - 1) * note_line_gap
            if note_lines else 0
        )
        note_y = content_bottom + note_gap if note_lines else None

        # Последний нарисованный элемент — сноска про академический час,
        # а если её нет — последняя карточка. Снизу остаётся только
        # нижний padding.
        footer_pad_bottom = 40
        last_drawn_y = note_y + note_h if note_lines else content_bottom
        H = int(last_drawn_y + footer_pad_bottom)

        image = Image.new("RGB", (W, H), COL_BG)
        draw = ImageDraw.Draw(image)

        # ---------- шапка (как у расписания) ----------
        draw.rectangle((0, 0, W, HEADER_H), fill=COL_WHITE)
        draw.rectangle((0, 0, 14, HEADER_H), fill=COL_ACCENT)
        draw.text((MARGIN + 20, 40), "СОСТОЯНИЕ БОТА",
                  font=font_label, fill=COL_ACCENT)

        big_title = group.name if group is not None else "Группа не выбрана"
        big_font = font_group
        chip_text = "РАБОТАЕТ"
        chip_pad_x = 26
        chip_w = font_label.getlength(chip_text) + chip_pad_x * 2
        chip_h = 54
        chip_x = W - MARGIN - chip_w
        title_max_w = (chip_x - 24) - (MARGIN + 20)
        if big_font.getlength(big_title) > title_max_w:
            for size in (64, 56, 48, 44, 40, 36, 32, 28, 24):
                candidate = get_font(size, bold=True)
                if candidate.getlength(big_title) <= title_max_w:
                    big_font = candidate
                    break
        draw.text((MARGIN + 20, 84), big_title, font=big_font, fill=COL_INK)
        draw.text(
            (MARGIN + 22, 186),
            f"{format_date_header(now.date())} · {now.strftime('%H:%M')}",
            font=font_date,
            fill=COL_MUTED,
        )

        # Зелёный бейдж «РАБОТАЕТ» в правом верхнем углу.
        draw.rounded_rectangle(
            (chip_x, 48, chip_x + chip_w, 48 + chip_h),
            radius=chip_h / 2,
            fill=COL_GREEN_LIGHT,
        )
        draw.text(
            (chip_x + chip_pad_x, 48 + (chip_h - _text_h(font_label)) // 2),
            chip_text,
            font=font_label,
            fill=COL_GREEN,
        )

        # ---------- карточки ----------
        y = content_top
        for (title, rows, kwargs), height in zip(cards, heights):
            # тень + карточка — в стилистике пар расписания
            draw.rounded_rectangle(
                (x1 + 6, y + 8, x2 + 6, y + height + 8),
                radius=30,
                fill="#E6EAF3",
            )
            draw.rounded_rectangle(
                (x1, y, x2, y + height),
                radius=30,
                fill=COL_WHITE,
                outline=COL_BORDER,
                width=2,
            )

            inner_y = y + card_top
            draw.text((left, inner_y), title, font=font_title, fill=COL_ACCENT)
            inner_y += _text_h(font_title) + title_gap

            if kwargs.get("big"):
                draw.text((left, inner_y), kwargs["big"],
                          font=font_big, fill=COL_INK)
                inner_y += _text_h(font_big) + 18

            if kwargs.get("bar"):
                inner_y += bar_gap_top
                track_w = right - left
                draw.rounded_rectangle(
                    (left, inner_y, right, inner_y + bar_h),
                    radius=bar_h / 2,
                    fill=COL_ACCENT_LIGHT,
                )
                share = min(
                    1.0, forecast.studied_minutes / forecast.total_minutes
                )
                fill_w = max(6, int(track_w * share))
                draw.rounded_rectangle(
                    (left, inner_y, left + fill_w, inner_y + bar_h),
                    radius=bar_h / 2,
                    fill=COL_ACCENT,
                )
                inner_y += bar_h + bar_gap_bottom

            if kwargs.get("note"):
                draw.text((left, inner_y), kwargs["note"],
                          font=font_small, fill=COL_MUTED)
                inner_y += _text_h(font_small) + 10

            for key_text, value_text in rows:
                draw.text((left, inner_y), key_text,
                          font=font_key, fill=COL_MUTED)
                value_w = draw.textlength(value_text, font=font_value)
                draw.text((right - value_w, inner_y), value_text,
                          font=font_value, fill=COL_INK)
                inner_y += row_h + row_gap

            y += height + card_gap

        # ---------- сноска про академический час ----------
        if note_lines:
            ny = note_y
            for note_line in note_lines:
                line_w = _italic_text_width(note_line, font_note)
                _draw_italic_text(
                    image, ((W - line_w) / 2, ny), note_line,
                    font_note, COL_MUTED,
                )
                ny += note_line_h + note_line_gap

        filename = (
            f"status_{now.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.png"
        )
        path = IMAGE_DIR / filename
        image.save(path, "PNG", optimize=True)
        logger.info("Изображение статуса сохранено: %s (%sx%s)", path, W, H)
        return path

    except Exception:
        logger.exception("Ошибка генерации изображения статуса")
        raise


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
                    text="← Предыдущий день",
                    callback_data=f"staff:{int(staff_id)}:{(target_day - timedelta(days=1)).isoformat()}",
                ),
                InlineKeyboardButton(
                    text="Сегодня",
                    callback_data=f"staff:{int(staff_id)}:{get_today().isoformat()}",
                ),
                InlineKeyboardButton(
                    text="Следующий день →",
                    callback_data=f"staff:{int(staff_id)}:{(target_day + timedelta(days=1)).isoformat()}",
                ),
            ],
            [InlineKeyboardButton(text="Уведомления об изменениях",
                                  callback_data=f"teacher:notify:{int(staff_id)}:1")],
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


def main_keyboard(is_subscribed: bool, has_group: bool = True) -> InlineKeyboardMarkup:
    group_button_text = "Сменить группу" if has_group else "Выбрать группу"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="today"),
         InlineKeyboardButton(text="Завтра", callback_data="schedule")],
        [InlineKeyboardButton(text="По дате", callback_data="date"),
         InlineKeyboardButton(text="О группе", callback_data="groupinfo")],
        [InlineKeyboardButton(text=group_button_text, callback_data="groups"),
         InlineKeyboardButton(text="Преподаватели", callback_data="teacher")],
        [InlineKeyboardButton(
            text="Уведомления: вкл." if is_subscribed else "Уведомления: выкл.",
            callback_data="notifications")],
        [InlineKeyboardButton(text="Всему колледжу · 50 ₽", callback_data="broadcast")],
        [InlineKeyboardButton(text="Статус", callback_data="status"),
         InlineKeyboardButton(text="Помощь", callback_data="help")],
    ])


def _spec_token(specialty: str) -> str:
    candidate = f"groups:spec:99:{specialty}:999"
    if ":" not in specialty and not specialty.startswith("#") and len(candidate.encode("utf-8")) <= 64:
        return specialty
    return "#" + hashlib.sha1(specialty.encode("utf-8")).hexdigest()[:10]


def _resolve_specialty_token(course: int, token: str) -> Optional[str]:
    specs = available_specialties(GROUPS, course=course)
    if token.startswith("#"):
        digest = token[1:]
        for spec in specs:
            if hashlib.sha1(spec.encode("utf-8")).hexdigest()[:10] == digest:
                return spec
        return None
    for spec in specs:
        if spec.casefold() == token.casefold():
            return spec
    return None


def group_course_keyboard(include_menu: bool = True) -> InlineKeyboardMarkup:
    courses = available_courses(GROUPS)
    rows = []
    for offset in range(0, len(courses), 2):
        chunk = courses[offset:offset + 2]
        rows.append([
            InlineKeyboardButton(
                text=f"{course} курс",
                callback_data=f"groups:course:{course}",
            )
            for course in chunk
        ])
    footer = [InlineKeyboardButton(text="Преподаватели", callback_data="teacher")]
    if include_menu:
        footer.append(InlineKeyboardButton(text="Главное меню", callback_data="menu"))
    rows.append(footer)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_specialty_keyboard(course: int) -> InlineKeyboardMarkup:
    specs = available_specialties(GROUPS, course=course)
    per_row = 2 if any(len(spec) > 8 for spec in specs) else 3
    rows = []
    for offset in range(0, len(specs), per_row):
        chunk = specs[offset:offset + per_row]
        rows.append([
            InlineKeyboardButton(
                text=spec,
                callback_data=f"groups:spec:{course}:{_spec_token(spec)}:0",
            )
            for spec in chunk
        ])
    rows.append([InlineKeyboardButton(text="← Назад к курсам", callback_data="groups:courses")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_list_keyboard(course: int, specialty: str, page: int = 0) -> InlineKeyboardMarkup:
    values = sorted_groups(GROUPS, course=course, specialty=specialty)
    per_page = 8
    pages = max(1, (len(values) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    spec_token = _spec_token(specialty)
    rows = []
    for offset in range(page * per_page, min(len(values), (page + 1) * per_page), 2):
        chunk = values[offset:offset + 2]
        rows.append([
            InlineKeyboardButton(text=item.name, callback_data=f"group:set:{item.id}")
            for item in chunk
        ])
    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="←", callback_data=f"groups:spec:{course}:{spec_token}:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="groups:noop"),
            InlineKeyboardButton(text="→", callback_data=f"groups:spec:{course}:{spec_token}:{min(pages - 1, page + 1)}"),
        ])
    rows.append([InlineKeyboardButton(text="← Назад к специальностям", callback_data=f"groups:course:{course}")])
    rows.append([InlineKeyboardButton(text="← Назад к курсам", callback_data="groups:courses")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _legacy_group_catalog_keyboard(course: Optional[int] = None, form: str = "all",
                                   page: int = 0) -> InlineKeyboardMarkup:
    correspondence = None if form == "all" else form == "correspondence"
    values = sorted_groups(GROUPS, course=course, correspondence=correspondence)
    per_page = 8
    pages = max(1, (len(values) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    rows = []
    for offset in range(page * per_page, min(len(values), (page + 1) * per_page), 2):
        chunk = values[offset:offset + 2]
        rows.append([InlineKeyboardButton(text=item.name, callback_data=f"group:set:{item.id}")
                     for item in chunk])
    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="←", callback_data=f"groups:list:{course or 0}:{form}:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="groups:noop"),
            InlineKeyboardButton(text="→", callback_data=f"groups:list:{course or 0}:{form}:{min(pages - 1, page + 1)}"),
        ])
    rows.append([InlineKeyboardButton(text="← Назад к курсам", callback_data="groups:courses")])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_catalog_keyboard(course: Optional[int] = None, form: str = "all",
                           page: int = 0, specialty: Optional[str] = None) -> InlineKeyboardMarkup:
    if form != "all":
        return _legacy_group_catalog_keyboard(course=course, form=form, page=page)
    if course is None:
        return group_course_keyboard()
    if specialty is None:
        return group_specialty_keyboard(course)
    return group_list_keyboard(course, specialty, page=page)


def notification_keyboard(enabled: bool, staff_id: Optional[int] = None) -> InlineKeyboardMarkup:
    if staff_id:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Отключить уведомления" if enabled else "Включить уведомления",
            callback_data=f"teacher:notify:{staff_id}:{0 if enabled else 1}")]])
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Отключить уведомления" if enabled else "Включить уведомления",
        callback_data="unsubscribe" if enabled else "subscribe")]])


def premium_title(text: str) -> str:
    """Premium custom emoji используется только при заданном ID из пака бота."""
    if PREMIUM_EMOJI_ID:
        return f'<tg-emoji emoji-id="{PREMIUM_EMOJI_ID}">◆</tg-emoji> <b>{text}</b>'
    return f"<b>{text}</b>"


def render_group_info_image(group: GroupInfo) -> Path:
    """Инфо-карточка группы в палитре и типографике расписания."""
    width, height = 1080, 1350
    scale = 2
    image = Image.new("RGB", (width * scale, height * scale), S_BG)
    draw = ImageDraw.Draw(image)

    def rect(box, radius, fill, outline=None, line=1):
        draw.rounded_rectangle(tuple(int(v * scale) for v in box), radius=radius * scale,
                               fill=fill, outline=outline, width=line * scale)

    def text(x, y, value, size, color=S_INK, bold=False):
        draw.text((x * scale, y * scale), sanitize_text(value),
                  font=get_font(size * scale, bold=bold), fill=color)

    rect((52, 52, 1028, 1298), 42, S_CARD)
    text(104, 112, "ИНК · ГРУППА", 25, S_MUTED, True)
    text(104, 175, group.name, 72, S_INK, True)
    text(104, 280, "Карточка группы", 32, S_MUTED)
    rect((104, 352, 976, 355), 1, S_BORDER)
    fields = [
        ("ФОРМА", group.form),
        ("ОСНОВА", group.study),
        ("БАЗА", f"{group.education} классов"),
        ("КУРС", str(group.course)),
        ("СЕМЕСТР", str(group.semester)),
        ("ID ГРУППЫ", str(group.id)),
    ]
    for index, (label, value) in enumerate(fields):
        col, row = index % 2, index // 2
        x, y = 104 + col * 446, 410 + row * 190
        rect((x, y, x + 416, y + 154), 26, S_PURPLE_L)
        text(x + 28, y + 25, label, 21, S_MUTED, True)
        text(x + 28, y + 72, value, 34, S_INK, True)
    text(104, 1010, "УЧЕБНЫЙ ГОД", 21, S_MUTED, True)
    text(104, 1055, f"1 семестр: {_info_date(group.start_1)} — {_info_date(group.end_1)}", 28)
    text(104, 1110, f"2 семестр: {_info_date(group.start_2)} — {_info_date(group.end_2)}", 28)
    status = "Расписание публикуется" if group.has_schedule else "Расписание пока не опубликовано"
    text(104, 1215, status, 24, S_MUTED)
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    path = IMAGE_DIR / f"group_info_{group.id}_v1.png"
    image.save(path, "PNG", optimize=True, compress_level=9)
    return path


def _info_date(value: str) -> str:
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "—"


# ============================================================
# ОТПРАВКА РАСПИСАНИЯ
# ============================================================

def _photo_caption(schedule: Schedule) -> str:
    if schedule.schedule_type == "staff":
        lines = [
            premium_title("Преподаватель"),
            f"<b>{html.escape(schedule.staff_name or schedule.group)}</b>",
            format_date_full(schedule.date),
        ]
    else:
        lines = [
            premium_title(html.escape(schedule.group)),
            format_date_full(schedule.date),
        ]
    if schedule.lessons:
        lines.append(f"Занятий: {count_lessons(schedule)}")
    return "\n".join(lines)


async def _send_photo(destination, schedule: Schedule, reply_markup=None) -> bool:
    """Отправляет постоянный PNG из кэша; генерация выполняется один раз на версию."""
    path = await get_cached_image(schedule)
    try:
        caption = _photo_caption(schedule)
        kwargs = {"caption": caption}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if _is_message_destination(destination):
            await destination.answer_photo(FSInputFile(path), **kwargs)
        else:
            await destination.message.answer_photo(FSInputFile(path), **kwargs)
        chat_id = _destination_chat_id(destination)
        if chat_id is not None and schedule.lessons:
            record_schedule_notification(
                chat_id,
                schedule.date.isoformat(),
                schedule_signature(schedule),
                schedule_target_key(schedule),
            )
        return True
    except Exception:
        logger.exception("Ошибка отправки изображения")
        return False


async def _send_text(destination, text: str) -> None:
    try:
        if _is_message_destination(destination):
            await destination.answer(text)
        else:
            await destination.message.answer(text)
    except Exception:
        logger.exception("Ошибка отправки текста")


def _destination_chat_id(destination) -> Optional[int]:
    chat = getattr(destination, "chat", None)
    if chat is not None and getattr(chat, "id", None) is not None:
        return int(chat.id)
    message = getattr(destination, "message", None)
    msg_chat = getattr(message, "chat", None)
    if msg_chat is not None and getattr(msg_chat, "id", None) is not None:
        return int(msg_chat.id)
    return None


def _selected_group_id(destination) -> Optional[int]:
    chat_id = _destination_chat_id(destination)
    if chat_id is None:
        # Не-Telegram объект без chat оставлен для legacy-интеграционных тестов.
        return GROUP_ID
    group_id = get_user_settings(chat_id).get("group_id")
    return int(group_id) if group_id is not None else None


async def _prompt_group_selection(
    destination,
    prefix: str = "Сначала выберите группу.",
) -> None:
    text = f"<b>{html.escape(prefix)}</b>\n\n<b>Шаг 1 из 3 · Выберите курс:</b>"
    markup = group_course_keyboard()
    if _is_message_destination(destination):
        await destination.answer(text, reply_markup=markup)
    else:
        await destination.message.answer(text, reply_markup=markup)


async def _edit_or_answer(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    msg = callback.message
    if getattr(msg, "text", None) and hasattr(msg, "edit_text"):
        try:
            await msg.edit_text(text, reply_markup=reply_markup)
            return
        except Exception:
            pass
    await msg.answer(text, reply_markup=reply_markup)


async def _schedule_for_destination(destination, day: date, group_id: int) -> Schedule:
    # Не-Telegram объекты без chat_id оставлены для обратной совместимости legacy-тестов.
    if isinstance(destination, (Message, CallbackQuery)) or _destination_chat_id(destination) is not None:
        return await get_schedule_cached(day, group_id)
    return await (get_schedule(day) if group_id == GROUP_ID else get_schedule(day, group_id))


async def _handle_today(destination):
    """Команда /today: расписание ТОЛЬКО на сегодняшний день.

    Никакого fallback на завтра/другую дату. Если расписания нет —
    сообщаем об этом.
    """
    selected_group_id = _selected_group_id(destination)
    if selected_group_id is None:
        await _prompt_group_selection(destination)
        return
    try:
        today = get_today()
        group = get_group(selected_group_id)
        schedule = await _schedule_for_destination(destination, today, group.id)
        register_subjects_from_schedule(schedule)
        record_completed_lessons(schedule)

        if not schedule.lessons:
            await _send_text(
                destination,
                f"{premium_title('Расписание на сегодня')}\n\n"
                f"Группа: <b>{html.escape(group.name)}</b>\n"
                f"{format_date_header(today)}\n\n"
                "Занятий нет или расписание ещё не опубликовано.",
            )
            return

        ok = await _send_photo(destination, schedule)
        if not ok:
            await _send_text(destination, "Не удалось отправить расписание.")
    except ScheduleUnavailable:
        await _send_text(
            destination,
            f"Не удалось получить расписание на "
            f"{format_date_full(get_today())}. Сайт недоступен, попробуй позже.",
        )
    except Exception:
        logger.exception("Ошибка /today")


async def _handle_schedule(destination):
    """Команда /schedule: расписание ТОЛЬКО на завтрашний день.

    Никакого fallback на сегодня/другую дату. Если расписания нет —
    сообщаем об этом.
    """
    selected_group_id = _selected_group_id(destination)
    if selected_group_id is None:
        await _prompt_group_selection(destination)
        return
    try:
        tomorrow = get_tomorrow()
        group = get_group(selected_group_id)
        schedule = await _schedule_for_destination(destination, tomorrow, group.id)
        register_subjects_from_schedule(schedule)
        record_completed_lessons(schedule)

        if not schedule.lessons:
            await _send_text(
                destination,
                f"{premium_title('Расписание на завтра')}\n\n"
                f"Группа: <b>{html.escape(group.name)}</b>\n"
                f"{format_date_header(tomorrow)}\n\n"
                "Занятий нет или расписание ещё не опубликовано.",
            )
            return

        ok = await _send_photo(destination, schedule)
        if not ok:
            await _send_text(destination, "Не удалось отправить расписание.")
    except ScheduleUnavailable:
        await _send_text(
            destination,
            f"Не удалось получить расписание на "
            f"{format_date_full(get_tomorrow())}. Сайт недоступен, попробуй позже.",
        )
    except Exception:
        logger.exception("Ошибка /schedule")


async def _handle_date(destination, target: date):
    """Показывает расписание на конкретную дату (без подстановки «сегодня»)."""
    selected_group_id = _selected_group_id(destination)
    if selected_group_id is None:
        await _prompt_group_selection(destination)
        return
    group = get_group(selected_group_id)
    try:
        if is_day_off(target):
            await _send_text(
                destination,
                f"{premium_title(format_date_header(target))}\n\n"
                f"Группа: <b>{html.escape(group.name)}</b>\n"
                f"{format_date_full(target)}\n\n"
                "Воскресенье — занятий нет.",
            )
            return

        schedule = await _schedule_for_destination(destination, target, group.id)
        register_subjects_from_schedule(schedule)
        record_completed_lessons(schedule)

        if not schedule.lessons:
            await _send_text(
                destination,
                f"{premium_title(format_date_header(target))}\n\n"
                f"Группа: <b>{html.escape(group.name)}</b>\n"
                f"{format_date_full(target)}\n\n"
                "Занятий нет или расписание ещё не опубликовано.",
            )
            return

        ok = await _send_photo(destination, schedule)
        if not ok:
            await _send_text(destination, "Не удалось отправить расписание.")
    except ScheduleUnavailable:
        await _send_text(
            destination,
            "Не удалось получить расписание. Сайт недоступен, попробуй позже.",
        )
    except Exception:
        logger.exception("Ошибка /date")


def _is_message_destination(destination) -> bool:
    if isinstance(destination, Message):
        return True
    if isinstance(destination, CallbackQuery) or hasattr(destination, "message"):
        return False
    return hasattr(destination, "answer")


async def _send_staff_schedule(
    destination,
    member: StaffMember,
    target_day: date,
    *,
    edit_existing: bool = False,
) -> bool:
    """Получает и отправляет staff-расписание с навигацией."""
    try:
        schedule = await get_staff_schedule_cached(member.staff_id, target_day)
        keyboard = staff_keyboard(member.staff_id, target_day)
        if not schedule.lessons:
            text = (
                "<b>Расписание преподавателя</b>\n"
                f"<b>{member.full_name}</b>\n\n"
                f"{format_date_full(target_day)}\n\n"
                "Занятий нет или расписание ещё не опубликовано."
            )
            if _is_message_destination(destination):
                await destination.answer(text, reply_markup=keyboard)
            else:
                await destination.message.answer(text, reply_markup=keyboard)
            return True

        path = await get_cached_image(schedule)
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
            chat_id = _destination_chat_id(destination)
            if chat_id is not None and schedule.lessons:
                record_schedule_notification(
                    chat_id,
                    target_day.isoformat(),
                    schedule_signature(schedule),
                    schedule_target_key(schedule),
                )
            return True
        except Exception:
            logger.exception("Ошибка отправки staff PNG")
            return False
    except ScheduleUnavailable:
        text = (
            "Не удалось получить расписание преподавателя. "
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
            f"Преподаватель «{clean_text(request.staff_query)}» не найден.\n"
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
    chat_id = _destination_chat_id(destination)
    if chat_id is not None:
        set_user_staff(chat_id, matches[0].staff_id)
    await _send_staff_schedule(destination, matches[0], target_day)


async def _status_text(chat_id: int) -> str:
    """Текстовый fallback картинки статуса (если PNG не сгенерировался)."""
    created = subscriber_info(chat_id)
    settings = get_user_settings(chat_id)
    group_id = settings.get("group_id")
    if group_id is None:
        lines = [
            "<b>Группа не выбрана</b>",
            "—" * 18,
            "Выберите группу: /groups",
            f"Проверка изменений каждые {CHECK_INTERVAL // 60} мин.",
            f"Часовой пояс: <b>{TIMEZONE}</b> (UTC+5)",
            f"Аптайм: {format_uptime(time.monotonic() - _PROCESS_STARTED_MONOTONIC)}",
        ]
        return "\n".join(lines)

    group = get_group(group_id)
    total = len(load_subscribers(group.id))
    forecast = get_study_forecast(group.name)
    lines = [
        f"<b>{html.escape(group.name)}</b>",
        "—" * 18,
    ]
    if created:
        lines.append("Уведомления включены.")
        lines.append(f"Подписка оформлена: <i>{created}</i>")
    else:
        lines.append("Уведомления отключены.")
    lines.append(f"Всего подписок: <b>{total}</b>")
    lines.append(f"Проверка изменений каждые {CHECK_INTERVAL // 60} мин.")
    lines.append(f"Часовой пояс: <b>{TIMEZONE}</b> (UTC+5)")
    if forecast.studied_minutes > 0:
        lines.append(study_badge_text(forecast))
        lines.append(f"По обыкновенному времени: {regular_study_time_text(forecast)}")
        if forecast.remaining_minutes:
            lines.append(
                "Осталось при текущем темпе: "
                f"<b>≈ {format_academic_hours(forecast.remaining_minutes)}</b>"
            )
        lines.append(f"<i>{STUDY_NOTE_EXPLANATION}</i>")
    lines.append(
        f"Аптайм: {format_uptime(time.monotonic() - _PROCESS_STARTED_MONOTONIC)}"
    )
    return "\n".join(lines)


def _status_caption(chat_id: Optional[int] = None) -> str:
    """Короткая подпись к картинке статуса."""
    group_id = get_user_settings(chat_id).get("group_id") if chat_id is not None else GROUP_ID
    if group_id is None:
        lines = ["<b>Статус бота</b> · Группа не выбрана"]
    else:
        group = get_group(group_id)
        forecast = get_study_forecast(group.name)
        lines = [f"<b>Статус бота</b> · <b>{html.escape(group.name)}</b>"]
        if forecast.studied_minutes > 0:
            lines.append(study_badge_text(forecast))
    lines.append(
        "Аптайм: "
        f"{format_uptime(time.monotonic() - _PROCESS_STARTED_MONOTONIC)}"
    )
    return "\n".join(lines)


async def _send_status(destination) -> None:
    """Отправляет картинку состояния бота; при сбое — текстовый fallback."""
    # Message-подобный объект имеет .chat, CallbackQuery — только .message.
    chat = getattr(destination, "chat", None)
    if chat is not None:
        chat_id = chat.id
    else:
        chat_id = destination.message.chat.id
    caption = _status_caption(chat_id)
    path = None
    try:
        path = render_status_image(chat_id)
    except Exception:
        logger.exception("Не удалось сгенерировать картинку статуса")

    if path is not None:
        try:
            if chat is not None:
                await destination.answer_photo(FSInputFile(path),
                                               caption=caption)
            else:
                await destination.message.answer_photo(FSInputFile(path),
                                                       caption=caption)
            return
        except Exception:
            logger.exception("Не удалось отправить картинку статуса")
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    await _send_text(destination, await _status_text(chat_id))


# ============================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================

dp = Dispatcher()


class BotCommandFilter(Command):
    """Команды с основным и дополнительными premium usernames бота."""

    async def validate_mention(self, bot: Bot, command) -> None:
        if not command.mention or self.ignore_mention:
            return
        me = await bot.me()
        allowed = set(BOT_USERNAME_ALIASES)
        if me.username:
            allowed.add(me.username.casefold())
        if command.mention.casefold() not in allowed:
            raise CommandException("Mention did not match")


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
        decision = check_rate_limit(user_id)
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


async def _callback_can_manage(callback: CallbackQuery) -> bool:
    chat = callback.message.chat
    if chat.type not in ("group", "supergroup"):
        return True
    try:
        member = await callback.bot.get_chat_member(chat.id, callback.from_user.id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


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


@dp.message(BotCommandFilter("start", "help"))
async def cmd_start(message: Message):
    chat_id = message.chat.id
    save_user_profile(message.from_user, chat_id)
    set_pending_action(chat_id, "")
    selected_group_id = get_user_settings(chat_id).get("group_id")
    is_help = (message.text or "").lstrip().lower().startswith("/help")
    if is_help:
        hint = (
            "Группа ещё не выбрана — нажмите «Выбрать группу» или /groups.\n\n"
            if selected_group_id is None
            else ""
        )
        await message.answer(
            f"{premium_title(BOT_NAME)}\n\n"
            f"{hint}"
            "Основные действия доступны кнопками главного меню.\n"
            "«Выбрать группу» — пошаговый выбор: курс → специальность → группа.\n"
            "«Преподаватели» — поиск по фамилии и отдельные уведомления.\n\n"
            "Команды: /today, /schedule, /date, /groups, /teacher, /subscribe, /status.",
            reply_markup=main_keyboard(
                _is_subscribed(chat_id), has_group=selected_group_id is not None
            ),
        )
        return

    if selected_group_id is None:
        text = (
            f"{premium_title(BOT_NAME)}\n\n"
            "Добро пожаловать! Для начала выберите вашу группу.\n\n"
            "<b>Шаг 1 из 3 · Выберите курс:</b>"
        )
        await message.answer(text, reply_markup=group_course_keyboard(include_menu=False))
        return

    group = get_group(selected_group_id)
    text = (
        f"{premium_title(BOT_NAME)}\n\n"
        f"Группа: <b>{html.escape(group.name)}</b>\n"
        f"{group.form} · {group.study} · {group.course} курс\n\n"
        "Выберите действие кнопками ниже.\n"
        "По дате: <code>/date 10.11.2026</code>\n"
        "Текстом: «расписание на завтра»."
    )
    await message.answer(text, reply_markup=main_keyboard(_is_subscribed(chat_id), has_group=True))


@dp.message(BotCommandFilter("groups"))
async def cmd_groups(message: Message):
    save_user_profile(message.from_user, message.chat.id)
    has_group = get_user_settings(message.chat.id).get("group_id") is not None
    await message.answer(
        "<b>Выбор группы</b>\nШаг 1 из 3 · Выберите курс:",
        reply_markup=group_course_keyboard(include_menu=has_group),
    )


@dp.message(BotCommandFilter("teacher"))
async def cmd_teacher(message: Message):
    save_user_profile(message.from_user, message.chat.id)
    set_pending_action(message.chat.id, "teacher_search")
    await message.answer("<b>Преподаватель</b>\nОтправьте фамилию или ФИО.")


@dp.message(BotCommandFilter("today"))
async def cmd_today(message: Message):
    """Показывает расписание только на сегодня."""
    await _handle_today(message)


@dp.message(BotCommandFilter("schedule"))
async def cmd_schedule(message: Message):
    await _handle_schedule(message)


@dp.message(BotCommandFilter("date"))
async def cmd_date(message: Message):
    """Поиск расписания по дате: /date 04.09.2026"""
    if _selected_group_id(message) is None:
        await _prompt_group_selection(message)
        return

    parts = (message.text or "").split(maxsplit=1)
    argument = parts[1] if len(parts) > 1 else ""

    if not argument.strip():
        await message.answer(
            "<b>Поиск расписания по дате</b>\n\n"
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
            "Дата не распознана.\n\n"
            "Попробуй так: <code>/date 04.09.2026</code>, "
            "<code>/date 4 сентября</code> или <code>/date завтра</code>."
        )
        return

    await _handle_date(message, target)


@dp.message(BotCommandFilter("subscribe"))
async def cmd_subscribe(message: Message):
    chat = message.chat
    is_group = chat.type in ("group", "supergroup")

    if is_group and not await _is_group_admin(message):
        await message.answer(
            "Изменять подписку группы может только администратор."
        )
        return

    selected_group_id = _selected_group_id(message)
    if selected_group_id is None:
        await _prompt_group_selection(message)
        return

    title = chat.title or chat.full_name or ""

    selected_group = get_group(selected_group_id)
    if subscribe_user(chat.id, chat_type=chat.type, title=title, group_id=selected_group.id):
        where = "Этот чат" if is_group else "Вы"
        await message.answer(
            f"<b>Уведомления включены</b>\n\n"
            f"{where} будет получать обновлённое расписание "
            f"при его изменении.\n\n"
            f"Отключить: /unsubscribe"
        )
    else:
        await message.answer(
            "Уведомления уже включены."
            if is_group
            else "Уведомления уже включены."
        )


@dp.message(BotCommandFilter("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    chat = message.chat
    is_group = chat.type in ("group", "supergroup")

    if is_group and not await _is_group_admin(message):
        await message.answer(
            "Изменять подписку группы может только администратор."
        )
        return

    if unsubscribe_user(chat.id):
        await message.answer("<b>Уведомления отключены</b>")
    else:
        await message.answer(
            "Этот чат не был подписан на уведомления."
            if is_group
            else "Вы не были подписаны на уведомления."
        )


@dp.message(BotCommandFilter("status"))
async def cmd_status(message: Message):
    await _send_status(message)


# ============================================================
# ПЛАТНАЯ РАССЫЛКА: МОДЕРАЦИЯ -> ОПЛАТА -> ДОСТАВКА
# ============================================================

BROADCAST_RULES = (
    "<b>Сообщение всему колледжу</b>\n\n"
    "Стоимость: 50 ₽. Реклама: 150 ₽.\n"
    "Публикация не анонимна: получатели увидят ваше имя и username.\n\n"
    "Запрещены: 18+, насилие, сливы и персональные данные, травля, "
    "дискриминация, мошенничество, ссылки на вредоносные ресурсы, спам, "
    "продажа запрещённых товаров и незаконная реклама.\n\n"
    "Сначала модератор проверяет сообщение, затем вы оплачиваете рассылку. "
    "Оплата взимается только после одобрения."
)


def create_broadcast(message: Message, is_ad: bool = False) -> int:
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    user = message.from_user
    with db_connect() as conn:
        cursor = conn.execute("""
            INSERT INTO broadcasts
                (user_id, author_name, username, original_chat_id, original_message_id,
                 is_ad, amount, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
        """, (user.id, user.full_name, user.username or "", message.chat.id,
              message.message_id, int(is_ad), BROADCAST_AD_PRICE if is_ad else BROADCAST_PRICE,
              stamp, stamp))
        return int(cursor.lastrowid)


def get_broadcast(broadcast_id: int) -> Optional[dict]:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
    return dict(row) if row else None


def update_broadcast(broadcast_id: int, **fields) -> None:
    allowed = {"is_ad", "amount", "status", "payment_charge_id", "moderator_id", "moderation_note"}
    values = {key: value for key, value in fields.items() if key in allowed}
    values["updated_at"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
    assignments = ", ".join(f"{key} = ?" for key in values)
    with db_connect() as conn:
        conn.execute(
            f"UPDATE broadcasts SET {assignments} WHERE id = ?",
            (*values.values(), broadcast_id),
        )


def broadcast_recipients() -> list[int]:
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT DISTINCT chat_id FROM (
                SELECT chat_id FROM user_settings WHERE chat_id > 0
                UNION SELECT user_id AS chat_id FROM subscribers WHERE user_id > 0
            ) ORDER BY chat_id
        """).fetchall()
    return [int(row["chat_id"]) for row in rows]


async def send_broadcast_invoice(bot: Bot, item: dict) -> None:
    if not PAYMENT_PROVIDER_TOKEN:
        raise RuntimeError("PAYMENT_PROVIDER_TOKEN не настроен")
    amount = int(item["amount"])
    title = "Реклама всему колледжу" if item["is_ad"] else "Сообщение всему колледжу"
    await bot.send_invoice(
        chat_id=item["user_id"], title=title,
        description=f"Модерация пройдена. Публикация не анонимна. Стоимость: {amount} ₽.",
        payload=f"broadcast:{item['id']}:{item['user_id']}",
        provider_token=PAYMENT_PROVIDER_TOKEN, currency="RUB",
        prices=[LabeledPrice(label=title, amount=amount * 100)],
    )


@dp.callback_query(F.data == "broadcast")
async def cb_broadcast(callback: CallbackQuery):
    if not ADMIN_IDS or not PAYMENT_PROVIDER_TOKEN:
        await callback.answer("Рассылка временно недоступна", show_alert=True)
        return
    save_user_profile(callback.from_user, callback.message.chat.id)
    set_pending_action(callback.message.chat.id, "broadcast_compose")
    await callback.answer()
    await callback.message.answer(BROADCAST_RULES + "\n\nОтправьте одно сообщение: текст, фото, видео или документ.")


@dp.callback_query(F.data.startswith("broadcast:type:"))
async def cb_broadcast_type(callback: CallbackQuery):
    try:
        _, _, raw_id, raw_ad = callback.data.split(":")
        broadcast_id, is_ad = int(raw_id), bool(int(raw_ad))
    except (ValueError, AttributeError):
        await callback.answer("Некорректный запрос", show_alert=True)
        return
    item = get_broadcast(broadcast_id)
    if not item or item["user_id"] != callback.from_user.id or item["status"] != "draft":
        await callback.answer("Заявка недоступна", show_alert=True)
        return
    update_broadcast(broadcast_id, is_ad=int(is_ad),
                     amount=BROADCAST_AD_PRICE if is_ad else BROADCAST_PRICE,
                     status="moderation")
    set_pending_action(callback.message.chat.id, "")
    await callback.answer("Отправлено на модерацию")
    await callback.message.answer(
        f"Заявка №{broadcast_id} отправлена на модерацию. "
        f"После одобрения появится счёт на {BROADCAST_AD_PRICE if is_ad else BROADCAST_PRICE} ₽."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Одобрить", callback_data=f"moderation:approve:{broadcast_id}"),
         InlineKeyboardButton(text="Отклонить", callback_data=f"moderation:reject:{broadcast_id}")],
    ])
    for admin_id in ADMIN_IDS:
        try:
            await callback.bot.send_message(
                admin_id,
                f"<b>Заявка №{broadcast_id}</b>\nАвтор: {html.escape(item['author_name'])}"
                f" (@{html.escape(item['username'])})\nТип: {'реклама, 150 ₽' if is_ad else 'сообщение, 50 ₽'}",
            )
            await callback.bot.copy_message(admin_id, item["original_chat_id"], item["original_message_id"])
            await callback.bot.send_message(admin_id, "Решение по заявке:", reply_markup=keyboard)
        except Exception:
            logger.exception("Не удалось отправить заявку модератору %s", admin_id)


@dp.callback_query(F.data.startswith("moderation:"))
async def cb_moderation(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    try:
        _, decision, raw_id = callback.data.split(":")
        broadcast_id = int(raw_id)
    except (ValueError, AttributeError):
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    item = get_broadcast(broadcast_id)
    if not item or item["status"] != "moderation":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    if decision == "reject":
        update_broadcast(broadcast_id, status="rejected", moderator_id=callback.from_user.id)
        await callback.answer("Отклонено")
        await callback.bot.send_message(item["user_id"], f"Заявка №{broadcast_id} отклонена. Оплата не взималась.")
        return
    if decision != "approve":
        await callback.answer("Некорректное решение", show_alert=True)
        return
    update_broadcast(broadcast_id, status="approved", moderator_id=callback.from_user.id)
    item = get_broadcast(broadcast_id)
    await callback.answer("Одобрено")
    await send_broadcast_invoice(callback.bot, item)


async def deliver_broadcast(bot: Bot, item: dict) -> None:
    update_broadcast(item["id"], status="sending")
    author = html.escape(item["author_name"])
    username = f" (@{html.escape(item['username'])})" if item["username"] else ""
    prefix = f"<b>Сообщение от {author}{username}</b>"
    if item["is_ad"]:
        prefix += "\nРеклама"
    delivered = 0
    for chat_id in broadcast_recipients():
        try:
            await bot.send_message(chat_id, prefix)
            await bot.copy_message(chat_id, item["original_chat_id"], item["original_message_id"])
            delivered += 1
            await asyncio.sleep(0.05)
        except Exception as error:
            logger.warning("Рассылка №%s не доставлена %s: %s", item["id"], chat_id, error)
    update_broadcast(item["id"], status="sent")
    await bot.send_message(item["user_id"], f"Рассылка №{item['id']} завершена. Доставлено: {delivered}.")


@dp.pre_checkout_query()
async def on_pre_checkout(query):
    try:
        _, raw_id, raw_user = query.invoice_payload.split(":")
        item = get_broadcast(int(raw_id))
        valid = item and item["status"] == "approved" and item["user_id"] == int(raw_user) == query.from_user.id
    except (ValueError, AttributeError):
        valid = False
    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Счёт недействителен. Запросите новую модерацию.")


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payment = message.successful_payment
    try:
        _, raw_id, raw_user = payment.invoice_payload.split(":")
        item = get_broadcast(int(raw_id))
        if not item or item["user_id"] != int(raw_user) or int(raw_user) != message.from_user.id:
            raise ValueError
        if item["status"] in ("paid", "sending", "sent"):
            return
        if item["status"] != "approved":
            raise ValueError
        expected = int(item["amount"]) * 100
        if payment.currency != "RUB" or payment.total_amount != expected:
            raise ValueError
    except (ValueError, AttributeError):
        logger.error("Неизвестный платёж: %s", payment.invoice_payload)
        await message.answer("Платёж сохранён, но заявка требует ручной проверки администратором.")
        for admin_id in ADMIN_IDS:
            await message.bot.send_message(admin_id, f"Требуется проверка платежа: {payment.telegram_payment_charge_id}")
        return
    update_broadcast(item["id"], status="paid", payment_charge_id=payment.telegram_payment_charge_id)
    await message.answer("Оплата принята. Начата рассылка.")
    await deliver_broadcast(message.bot, get_broadcast(item["id"]))


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


async def _accept_broadcast_content(message: Message) -> None:
    broadcast_id = create_broadcast(message)
    await message.answer(
        f"Заявка №{broadcast_id}. Выберите тип публикации.\n"
        "После модерации будет выставлен счёт. Авторство сохранится.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Сообщение · 50 ₽", callback_data=f"broadcast:type:{broadcast_id}:0")],
            [InlineKeyboardButton(text="Реклама · 150 ₽", callback_data=f"broadcast:type:{broadcast_id}:1")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"broadcast:cancel:{broadcast_id}")],
        ]),
    )


@dp.callback_query(F.data.startswith("broadcast:cancel:"))
async def cb_broadcast_cancel(callback: CallbackQuery):
    try:
        broadcast_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    item = get_broadcast(broadcast_id)
    if item and item["user_id"] == callback.from_user.id and item["status"] == "draft":
        update_broadcast(broadcast_id, status="cancelled")
    set_pending_action(callback.message.chat.id, "")
    await callback.answer("Заявка отменена")


@dp.message(F.chat.type == "private", ~F.text)
async def on_broadcast_media(message: Message):
    if get_user_settings(message.chat.id).get("pending_action") == "broadcast_compose":
        await _accept_broadcast_content(message)


@dp.message(F.text)
async def cmd_text_schedule(message: Message):
    """«расписание» / «расписание на <дата>» — без слэша.

    «расписание» -> завтра; дата разбирается parse_schedule_text и
    передаётся в ту же строгую функцию _handle_date/get_schedule,
    что и у команд с «/». Никакого fallback на другую дату.
    """
    settings = get_user_settings(message.chat.id) if hasattr(message, "chat") else {}
    pending = settings.get("pending_action", "")
    if pending == "broadcast_compose":
        await _accept_broadcast_content(message)
        return
    if pending == "date":
        set_pending_action(message.chat.id, "")
        target = parse_user_date(message.text or "")
        if target is None:
            await message.answer("Дата не распознана. Пример: 10.11.2026 или «завтра».")
            return
        await _handle_date(message, target)
        return
    if pending == "teacher_search":
        set_pending_action(message.chat.id, "")
        matches = search_staff(message.text or "")
        if not matches:
            await message.answer("Преподаватель не найден. Проверьте фамилию.")
            return
        if len(matches) > 1:
            await message.answer("Выберите преподавателя:", reply_markup=staff_choice_keyboard(matches, get_tomorrow()))
            return
        set_user_staff(message.chat.id, matches[0].staff_id)
        await _send_staff_schedule(message, matches[0], get_tomorrow())
        return

    request = parse_schedule_text(message.text or "")

    if not request.matched:
        # Сообщение не про расписание — молчим.
        return

    if request.schedule_type == "staff":
        await _handle_staff_request(message, request)
        return

    if _selected_group_id(message) is None:
        await _prompt_group_selection(message)
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
            f"<b>{BOT_NAME}</b>\n"
            "Расписание всех групп и преподавателей колледжа.\n\n"
            "/groups — выбрать группу\n"
            "/today и /schedule — расписание\n"
            "/subscribe — уведомления об изменениях\n"
            "/teacher — расписание преподавателя\n\n"
            "Настройки группового чата изменяет администратор.",
        )
    elif new_status in ("left", "kicked"):
        unsubscribe_user(chat.id)
        logger.info("Бот удалён из чата %s, подписка снята.", chat.id)


# ============================================================
# INLINE-КНОПКИ
# ============================================================


async def _send_group_info(destination, group: GroupInfo) -> None:
    path = render_group_info_image(group)
    caption = (
        f"{premium_title(html.escape(group.name))}\n"
        f"{group.form} · {group.study}\n"
        f"{group.base} · {group.course} курс · {group.semester} семестр"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Расписание на сегодня", callback_data="today"),
         InlineKeyboardButton(text="Сменить группу", callback_data="groups")],
        [InlineKeyboardButton(text="Главное меню", callback_data="menu")],
    ])
    if _is_message_destination(destination):
        await destination.answer_photo(FSInputFile(path), caption=caption, reply_markup=keyboard)
    else:
        await destination.message.answer_photo(FSInputFile(path), caption=caption, reply_markup=keyboard)


@dp.callback_query(F.data == "groups:noop")
async def cb_groups_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "groups")
async def cb_groups(callback: CallbackQuery):
    await callback.answer()
    has_group = _selected_group_id(callback) is not None
    await callback.message.answer(
        "<b>Выбор группы</b>\nШаг 1 из 3 · Выберите курс:",
        reply_markup=group_course_keyboard(include_menu=has_group),
    )


@dp.callback_query(F.data == "groups:courses")
async def cb_groups_courses(callback: CallbackQuery):
    await callback.answer()
    has_group = _selected_group_id(callback) is not None
    await _edit_or_answer(
        callback,
        "<b>Выбор группы</b>\nШаг 1 из 3 · Выберите курс:",
        group_course_keyboard(include_menu=has_group),
    )


@dp.callback_query(F.data.startswith("groups:course:"))
async def cb_groups_course(callback: CallbackQuery):
    try:
        course = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Курс не найден", show_alert=True)
        return
    if not available_specialties(GROUPS, course=course):
        await callback.answer("Группы этого курса не найдены", show_alert=True)
        return
    await callback.answer()
    await _edit_or_answer(
        callback,
        f"<b>Выбор группы · {course} курс</b>\nШаг 2 из 3 · Выберите специальность:",
        group_specialty_keyboard(course),
    )


@dp.callback_query(F.data.startswith("groups:spec:"))
async def cb_groups_spec(callback: CallbackQuery):
    try:
        _, _, raw_course, spec_token, raw_page = callback.data.split(":", 4)
        course, page = int(raw_course), int(raw_page)
        specialty = _resolve_specialty_token(course, spec_token)
        if not specialty:
            raise ValueError
    except (ValueError, AttributeError):
        await callback.answer("Специальность не найдена", show_alert=True)
        return
    await callback.answer()
    await _edit_or_answer(
        callback,
        f"<b>Выбор группы · {course} курс · {html.escape(specialty)}</b>\n"
        "Шаг 3 из 3 · Выберите группу:",
        group_list_keyboard(course, specialty, page=page),
    )


@dp.callback_query(F.data.startswith("groups:list:"))
async def cb_groups_list(callback: CallbackQuery):
    try:
        _, _, raw_course, form, raw_page = callback.data.split(":")
        course, page = int(raw_course) or None, int(raw_page)
        if form not in ("all", "fulltime", "correspondence"):
            raise ValueError
    except (ValueError, AttributeError):
        await callback.answer("Некорректный фильтр", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "<b>Выбор группы</b>\nФильтры: форма обучения и курс.",
        reply_markup=group_catalog_keyboard(course, form, page),
    )


@dp.callback_query(F.data.startswith("group:set:"))
async def cb_group_set(callback: CallbackQuery):
    if not await _callback_can_manage(callback):
        await callback.answer("Настройки изменяет администратор", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    if group_id not in GROUPS:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    chat_id = callback.message.chat.id
    save_user_profile(callback.from_user, chat_id)
    set_user_group(chat_id, group_id)
    group = get_group(group_id)
    first_calculation = ensure_study_history(group_id)
    await callback.answer(f"Выбрана {group.name}")
    text = (
        f"{premium_title(html.escape(group.name))}\n"
        f"{group.form} · {group.study} · {group.course} курс"
    )
    if first_calculation:
        text += (
            "\n\nЗапущен первичный подсчёт изученных часов. "
            "Он выполняется один раз для этой группы. "
            "Дальше данные обновляются автоматически, запросы будут примерно в 10 раз быстрее."
        )
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="today"),
         InlineKeyboardButton(text="Завтра", callback_data="schedule")],
        [InlineKeyboardButton(text="О группе", callback_data="groupinfo"),
         InlineKeyboardButton(text="Главное меню", callback_data="menu")],
    ]))


@dp.callback_query(F.data == "groupinfo")
async def cb_group_info(callback: CallbackQuery):
    await callback.answer()
    group_id = _selected_group_id(callback)
    if group_id is None:
        await _prompt_group_selection(callback)
        return
    await _send_group_info(callback, get_group(group_id))


@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    group_id = _selected_group_id(callback)
    if group_id is None:
        await callback.message.answer(
            f"{premium_title(BOT_NAME)}\nГруппа: <i>не выбрана</i>\n\n"
            "Для просмотра расписания выберите группу или найдите преподавателя.",
            reply_markup=main_keyboard(_is_subscribed(chat_id), has_group=False),
        )
        return
    group = get_group(group_id)
    await callback.message.answer(
        f"{premium_title(BOT_NAME)}\nГруппа: <b>{html.escape(group.name)}</b>",
        reply_markup=main_keyboard(_is_subscribed(chat_id), has_group=True),
    )


@dp.callback_query(F.data == "notifications")
async def cb_notifications(callback: CallbackQuery):
    settings = get_user_settings(callback.message.chat.id)
    staff_id = settings.get("staff_id") if settings.get("role") == "teacher" else None
    if staff_id:
        enabled = teacher_subscription(callback.message.chat.id, staff_id)
        member = STAFF_BY_ID.get(staff_id)
        text = f"<b>Уведомления</b>\nПреподаватель: {member.full_name if member else staff_id}"
        await callback.answer()
        await callback.message.answer(text, reply_markup=notification_keyboard(enabled, staff_id))
        return

    group_id = settings.get("group_id")
    if group_id is None:
        await callback.answer()
        await _prompt_group_selection(
            callback,
            prefix="Сначала выберите группу для уведомлений.",
        )
        return

    enabled = _is_subscribed(callback.message.chat.id)
    text = f"<b>Уведомления</b>\nГруппа: {get_group(group_id).name}\nНовое и изменённое расписание."
    await callback.answer()
    await callback.message.answer(text, reply_markup=notification_keyboard(enabled, None))


@dp.callback_query(F.data == "teacher")
async def cb_teacher(callback: CallbackQuery):
    save_user_profile(callback.from_user, callback.message.chat.id)
    set_pending_action(callback.message.chat.id, "teacher_search")
    await callback.answer()
    await callback.message.answer(
        "<b>Расписание преподавателя</b>\nОтправьте фамилию или ФИО."
    )


@dp.callback_query(F.data.startswith("teacher:notify:"))
async def cb_teacher_notify(callback: CallbackQuery):
    try:
        _, _, raw_id, raw_enabled = callback.data.split(":")
        staff_id, enabled = int(raw_id), bool(int(raw_enabled))
        member = STAFF_BY_ID[staff_id]
    except (ValueError, KeyError, AttributeError):
        await callback.answer("Преподаватель не найден", show_alert=True)
        return
    set_user_staff(callback.message.chat.id, staff_id)
    subscribe_teacher(callback.message.chat.id, staff_id, enabled)
    await callback.answer("Уведомления включены" if enabled else "Уведомления отключены")
    await callback.message.answer(
        f"<b>{member.full_name}</b>\n" +
        ("Новое и изменённое расписание будет приходить в этот чат." if enabled
         else "Уведомления отключены."),
        reply_markup=notification_keyboard(enabled, staff_id),
    )


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
    save_user_profile(callback.from_user, callback.message.chat.id)
    set_user_staff(callback.message.chat.id, member.staff_id)
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
    if not await _callback_can_manage(callback):
        await callback.answer("Настройки изменяет администратор", show_alert=True)
        return
    await callback.answer()
    chat = callback.message.chat
    group_id = _selected_group_id(callback)
    if group_id is None:
        await _prompt_group_selection(
            callback,
            prefix="Сначала выберите группу для уведомлений.",
        )
        return
    if subscribe_user(
        chat.id, chat_type=chat.type, title=chat.title or "", group_id=group_id
    ):
        await callback.message.answer(f"<b>Уведомления включены</b>\nГруппа: {get_group(group_id).name}")
    else:
        await callback.message.answer(f"Уведомления уже включены\nГруппа: {get_group(group_id).name}")


@dp.callback_query(F.data == "unsubscribe")
async def cb_unsubscribe(callback: CallbackQuery):
    if not await _callback_can_manage(callback):
        await callback.answer("Настройки изменяет администратор", show_alert=True)
        return
    await callback.answer()
    if unsubscribe_user(callback.message.chat.id):
        await callback.message.answer("<b>Уведомления отключены</b>")
    else:
        await callback.message.answer("Этот чат не был подписан.")


@dp.callback_query(F.data == "status")
async def cb_status(callback: CallbackQuery):
    await callback.answer()
    await _send_status(callback.message)


@dp.callback_query(F.data == "date")
async def cb_date(callback: CallbackQuery):
    save_user_profile(callback.from_user, callback.message.chat.id)
    if _selected_group_id(callback) is None:
        await callback.answer()
        await _prompt_group_selection(callback)
        return
    set_pending_action(callback.message.chat.id, "date")
    await callback.answer()
    await callback.message.answer(
        "<b>Расписание по дате</b>\nОтправьте дату: 10.11.2026, 4 сентября или «завтра»."
    )


@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "<b>Помощь</b>\n\n"
        "Основные действия доступны кнопками главного меню.\n"
        "«Выбрать / Сменить группу» — пошаговый выбор: курс → специальность → группа.\n"
        "«Преподаватели» — поиск по фамилии и отдельные уведомления.\n\n"
        "Команды: /today, /schedule, /date, /groups, /teacher, /subscribe, /status."
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
# ANTI-SPAM / RATE LIMIT
# ============================================================

@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    warning: bool = False


def check_rate_limit(
    user_id: int,
    current: Optional[datetime] = None,
) -> RateLimitDecision:
    """Атомарный sliding-window limiter.

    Решение и обновление счётчиков происходят в одной BEGIN IMMEDIATE
    транзакции, поэтому параллельные update не видят устаревшее состояние.
    При превышении лимита — только предупреждение, без банов.
    """
    user_id = int(user_id)
    timestamp = (
        _local_aware(current).timestamp() if current is not None else time.time()
    )
    cutoff = timestamp - max(1, RATE_LIMIT_WINDOW_SECONDS)
    try:
        with db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

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

            should_warn = (
                last_warning <= 0
                or timestamp - last_warning >= RATE_LIMIT_WARNING_COOLDOWN
            )
            warnings += 1
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
        extra = (
            f", гр. {clean_text(new.get('groups'))}"
            if clean_text(new.get("groups")) else ""
        )
        return (
            f"🟢 <b>Добавлено:</b> {label}\n"
            f"{display_subject_text(new.get('subject'))}"
            + (
                f", {clean_text(new.get('room') or '—')}"
                if new.get("room")
                else ""
            )
            + extra
        )
    if change.kind == "removed":
        old = change.old or {}
        extra = (
            f", гр. {clean_text(old.get('groups'))}"
            if clean_text(old.get("groups")) else ""
        )
        return (
            f"🔴 <b>Удалено:</b> {label}\n"
            f"{display_subject_text(old.get('subject'))}"
            + (
                f", {clean_text(old.get('room') or '—')}"
                if old.get("room")
                else ""
            )
            + extra
        )

    lines = [f"🟡 <b>Изменено:</b> {label}"]
    for detail in change.details[:8]:
        field_label = detail.get("label") or detail.get("field", "")
        old_val = clean_text(detail.get("old", ""))
        new_val = clean_text(detail.get("new", ""))
        if detail.get("field") == "subject" or field_label == "Предмет":
            # Отмена занятия показывается словами, а не точками сайта.
            old_val = display_subject_text(old_val) if old_val else old_val
            new_val = display_subject_text(new_val) if new_val else new_val
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
        action = "<b>Расписание опубликовано</b>"
    else:
        action = "<b>Расписание изменилось</b>"

    lines = [
        action,
        "",
        day_str,
        (
            f"Преподаватель: <b>{html.escape(schedule.staff_name or schedule.group)}</b>"
            if getattr(schedule, "schedule_type", "group") == "staff"
            else f"Группа: <b>{html.escape(schedule.group)}</b>"
        ),
        f"Занятий: {count_lessons(schedule)}",
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
    target_key: Optional[str] = None,
    subscribers: Optional[list] = None,
) -> int:
    """Отправляет актуальное расписание ТЕМ, кто его ещё не получил.

    Возвращает (доставлено, не_доставлено).
    У каждой пары «подписчик + дата» хранится последний доставленный
    hash, поэтому повторных уведомлений не будет, а сбой у одного
    получателя не считается доставкой.
    """
    target_key = target_key or schedule_target_key(schedule)
    subscribers = load_subscribers() if subscribers is None else subscribers
    if not subscribers:
        return 0, 0

    date_key = day.isoformat()
    notifications = load_schedule_notifications(target_key).get(date_key, {})
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
                record_schedule_notification(user_id, date_key, signature, target_key)
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
                    if target_key.startswith("staff:"):
                        subscribe_teacher(user_id, int(target_key.split(":", 1)[1]), False)
                    else:
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


async def _check_date(
    bot: Bot,
    day: date,
    group_id: Optional[int] = None,
    *,
    record_history: bool = True,
) -> None:
    """Проверка расписания на КОНКРЕТНУЮ дату.

    Сценарии:
    - расписания нет (пусто)     -> состояние не меняем, ничего не шлём;
    - ошибка источника           -> состояние не меняем, ничего не шлём;
    - расписание появилось впервые -> уведомление «опубликовано» только тем,
                                    кто ещё не видел эту версию;
    - расписание изменилось      -> уведомление «изменилось»;
    - совпадает с последним      -> ничего не шлём;
    - подписчиков нет            -> кэш и baseline обновляем (новым подписчикам
                                    уже опубликованное не уйдёт как «новое»);
    - доставка не удалась        -> состояние не обновляется,
                                    повторим в следующем цикле.
    """
    date_key = day.isoformat()
    group = get_group(group_id)
    target_key = f"group:{group.id}"
    state_key = date_key if group.id == GROUP_ID else f"{target_key}:{date_key}"

    if is_day_off(day):
        logger.info("Проверка %s: воскресенье, пропускаем.", date_key)
        return

    subscribers = load_subscribers(group.id)
    should_prewarm = bool(subscribers) or cache_has_image(target_key, day)
    try:
        schedule = (await get_schedule(day) if group.id == GROUP_ID
                    else await get_schedule(day, group.id))
        if schedule.lessons:
            save_schedule_cache(schedule)
    except ScheduleUnavailable:
        # Ошибка загрузки/парсинга — не считается изменением и также не
        # завершает backfill/историю.
        logger.warning(
            "Проверка %s: источник недоступен — изменением не считаем.",
            date_key,
        )
        return

    # Накопление истории запускается для активных/выбранных групп. Предметы
    # регистрируются при первом появлении, завершённые пары — после окончания.
    if schedule.schedule_type == "group" and record_history:
        register_subjects_from_schedule(schedule)
        record_completed_lessons(schedule)

    if not schedule.lessons:
        # Отсутствие расписания — тоже не изменение.
        logger.info(
            "Проверка %s: расписание отсутствует — состояние не меняем.",
            date_key,
        )
        return

    signature = schedule_signature(schedule)
    logger.info("Hash расписания %s: %s", date_key, signature)

    state = load_state()
    old_state = state.get(state_key)
    old_hash = old_state.get("hash") if old_state else None

    if old_hash == signature:
        logger.info("Изменений нет: %s", date_key)
        return

    if not subscribers:
        # Сохраняем baseline и без подписчиков, чтобы при первом появлении
        # подписчика бот не отправлял ему уже опубликованное расписание как
        # «новое», а дальнейшие изменения корректно сравнивались со старым.
        state[state_key] = {
            "hash": signature,
            "data": normalize_schedule(schedule),
        }
        save_state(state)
        logger.info(
            "Проверка %s: подписчиков нет, baseline сохранён (%s).",
            date_key,
            group.name,
        )
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
            old_schedule = schedule_from_storage(old_data, day, group=group.name)
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

    if should_prewarm:
        await get_cached_image(schedule)

    delivered, still_pending = await _notify_changed(
        bot,
        schedule,
        day,
        signature,
        first_time,
        changes=changes,
        target_key=target_key,
        subscribers=subscribers,
    )

    # Состояние фиксируем только когда расписание реально ушло всем
    # получателям (недоступные чаты снимаются с подписки и не считаются).
    # Если хоть один получатель не получил уведомление — состояние не
    # обновляется, и в следующем цикле отправка повторится только ему.
    if still_pending == 0:
        state[state_key] = {
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


def load_monitored_staff_ids() -> list[int]:
    with db_connect() as conn:
        rows = conn.execute("SELECT DISTINCT staff_id FROM teacher_subscribers ORDER BY staff_id").fetchall()
    return [int(row["staff_id"]) for row in rows]


async def _check_staff_date(bot: Bot, staff_id: int, day: date) -> None:
    if is_day_off(day):
        return
    subscribers = load_teacher_subscribers(staff_id)
    if not subscribers:
        return
    target_key = f"staff:{staff_id}"
    state_key = f"{target_key}:{day.isoformat()}"
    try:
        schedule = await get_staff_schedule(staff_id, day)
        if schedule.lessons:
            save_schedule_cache(schedule)
    except ScheduleUnavailable:
        return
    if not schedule.lessons:
        return
    signature = schedule_signature(schedule)
    state = load_state()
    old_state = state.get(state_key)
    if old_state and old_state.get("hash") == signature:
        return
    first_time = old_state is None
    changes = []
    if old_state and old_state.get("data") is not None:
        old = schedule_from_storage(old_state["data"], day, group=schedule.group)
        changes = compare_schedules(old, schedule)
    await get_cached_image(schedule)
    delivered, pending = await _notify_changed(
        bot, schedule, day, signature, first_time, changes,
        target_key=target_key, subscribers=subscribers,
    )
    if pending == 0:
        state[state_key] = {"hash": signature, "data": normalize_schedule(schedule)}
        save_state(state)


# ============================================================
# МОНИТОРИНГ ИЗМЕНЕНИЙ
# ============================================================

async def run_schedule_monitor_cycle(
    bot: Bot,
    birthday_checked_dates: Optional[set[str]] = None,
) -> None:
    """Один цикл фонового парсинга и проверки расписаний для всех групп."""
    active_groups = active_group_ids()
    active_group_set = set(active_groups)
    # Разовый пересчёт изученного времени после миграции истории на
    # запись по подгруппам (срабатывает один раз, дальше — пустой флаг).
    if GROUP_ID in active_group_set:
        try:
            await recalculate_study_history()
        except Exception:
            logger.exception("Ошибка разового пересчёта истории занятий")

    # История запускается только для реально выбранных / подписанных групп.
    # Уже обработанные даты пропускаются по БД.
    for gid in active_groups:
        try:
            await backfill_lesson_history(group_id=gid)
        except Exception:
            logger.exception("Ошибка backfill истории занятий (%s)", gid)

    # Каждый цикл даты вычисляются заново через Asia/Yekaterinburg.
    today = get_today()
    days = (today, get_tomorrow())
    monitor_groups = sorted(set(GROUPS.keys()) | active_group_set)
    semaphore = asyncio.Semaphore(MONITOR_CONCURRENCY)

    async def guarded(check, *args, **kwargs):
        async with semaphore:
            try:
                await check(*args, **kwargs)
            except Exception:
                logger.exception("Ошибка фоновой проверки: %s", args)

    jobs = []
    for day in days:
        for group_id in monitor_groups:
            jobs.append(
                guarded(
                    _check_date,
                    bot,
                    day,
                    group_id,
                    record_history=(group_id in active_group_set),
                )
            )
        for staff_id in load_monitored_staff_ids():
            jobs.append(guarded(_check_staff_date, bot, staff_id, day))
    if jobs:
        await asyncio.gather(*jobs)

    checked_dates = birthday_checked_dates if birthday_checked_dates is not None else set()
    if today.isoformat() not in checked_dates:
        try:
            birthday_result = await check_birthdays(bot, today)
            if birthday_result is not None:
                checked_dates.add(today.isoformat())
        except Exception:
            logger.exception("Ошибка скрытой ежедневной проверки")

    # Момент завершения цикла — для «Последняя проверка» в /status.
    _LAST_SCHEDULE_CHECK["at"] = now_local()


async def schedule_monitor(
    bot: Bot,
    initial_done: Optional[asyncio.Event] = None,
) -> None:
    """Фоновая задача. Не блокирует polling, одна на процесс.

    Со старта сразу парсит расписания (сегодня/завтра) для всех групп и
    каждые 5 минут перепроверяет обновления.
    """
    logger.info(
        "Мониторинг запущен (групп в каталоге: %s). Интервал: %s сек (%s мин).",
        len(GROUPS),
        CHECK_INTERVAL,
        CHECK_INTERVAL // 60,
    )

    birthday_checked_dates: set[str] = set()
    catalog_refreshed_at = time.monotonic()
    while True:
        try:
            if time.monotonic() - catalog_refreshed_at >= GROUP_CATALOG_TTL:
                await refresh_group_catalog()
                catalog_refreshed_at = time.monotonic()
            await run_schedule_monitor_cycle(bot, birthday_checked_dates)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка цикла мониторинга расписания")
        finally:
            if initial_done is not None and not initial_done.is_set():
                initial_done.set()

        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================
# MAIN
# ============================================================

COMMANDS = [
    BotCommand(command="today", description="Расписание на сегодня"),
    BotCommand(command="schedule", description="Расписание на завтра"),
    BotCommand(command="date", description="Расписание по дате"),
    BotCommand(command="groups", description="Выбрать группу"),
    BotCommand(command="teacher", description="Расписание преподавателя"),
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
    logger.info("Глобальный бот расписания: %s групп", len(GROUPS))
    logger.info("Legacy Group ID: %s", GROUP_ID)
    logger.info("URL: %s", BASE_URL)
    logger.info("Check interval: %s сек (%s мин)", CHECK_INTERVAL, CHECK_INTERVAL // 60)
    logger.info("Timezone: %s (UTC+5)", TIMEZONE)

    if not BOT_TOKEN:
        logger.error(
            "BOT_TOKEN не задан. Добавь переменную BOT_TOKEN в .env "
            "или окружение."
        )
        return

    await refresh_group_catalog()

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
    # не создаёт второй scheduler. Сразу при старте парсим расписания для
    # всех групп (не дольше 20 с ожидания до старта polling).
    initial_cycle_done = asyncio.Event()
    monitor_task = getattr(main, "_monitor_task", None)
    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(
            schedule_monitor(bot, initial_done=initial_cycle_done)
        )
        main._monitor_task = monitor_task
        try:
            await asyncio.wait_for(initial_cycle_done.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Первичный парсинг всех групп продолжается в фоне; запускаем polling."
            )

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

