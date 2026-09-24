# -*- coding: utf-8 -*-
"""
Telegram-бот расписания группы ЭС7-24 (Институт нефти и газа, ishnk.ru).

- Получает расписание напрямую по HTTP (aiohttp), БЕЗ браузера.
- Разбирает HTML через BeautifulSoup (только карточки div.card.myCard с .card-header).
- Игнорирует недельную таблицу.
- Генерирует современную PNG-картинку через Pillow: одна дата = одна
  вертикальная карточка (1080×1620, 2:3; при плотном дне холст растёт по
  лестнице форматов). Карточка пары: номер, время, предмет, преподаватель
  и аккуратная зелёная плашка аудитории; изменения и отмены показываются
  внутри карточки пары.
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
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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

            # Миграция истории занятий на схему v2 выполняется в самом
            # конце: ей нужны уже созданные lesson_backfill_days/bot_meta.
            _migrate_lesson_history(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lesson_history_subject
                ON lesson_history (group_name, normalized_subject, date)
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
    if schedule.group != GROUP_NAME:
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
    if schedule.schedule_type != "group" or schedule.group != GROUP_NAME:
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
            lines.append(
                f"Добавлено: {label} — {display_subject_text(new.get('subject'))}"
                + (f", {new.get('room') or '—'}" if new.get("room") else "")
            )
            continue
        if change.kind == "removed":
            old = change.old or {}
            lines.append(
                f"Удалено: {label} — {display_subject_text(old.get('subject'))}"
                + (f", {old.get('room') or '—'}" if old.get("room") else "")
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
# Дизайн (референс — «легкая лавандовая карточка дня»):
#   - фон — светлая лаванда с едва заметными диагональными подложками;
#   - шапка: иконка календаря, «РАСПИСАНИЕ», группа крупно, дата;
#     справа — счётчик занятий, шкала прогресса и «Изучено: … акад. ч»;
#   - карточка пары: круг с номером + время (начало тёмное, конец
#     приглушённое) + тихая пометка справа (перемена / подгруппы),
#     затем предмет, преподаватель и зелёная плашка аудитории;
#   - плашка аудитории: «Ауд. ПК108» и изученное время — зелёный
#     акцент, но сдержанного кегля: аудитория заметна цветом и
#     подложкой, а не «режимом для слабовидящих»;
#   - отмена: розовая плашка внутри карточки с пилюлей «Занятие
#     отменено» и красным бейджем «ОТМЕНА»;
#   - подвал: светлая плашка со сноской про академический час и
#     обыкновенным временем в две колонки;
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
# ПАЛИТРА: лавандовый холст, белые карточки, зелёные аудитории
# ------------------------------------------------------------

S_BG         = "#F2F1FA"   # фон холста
S_BG_LIGHT   = "#F7F6FC"   # диагональная подложка (светлее фона)
S_BG_DEEP    = "#ECEAF7"   # диагональная подложка (темнее фона)
S_CARD       = "#FFFFFF"   # карточка пары
S_INK        = "#191826"   # основной текст
S_MUTED      = "#8A88A0"   # второстепенный текст
S_FAINT      = "#A9A7BD"   # самый тихий текст
S_PURPLE     = "#6C5CE7"   # акцент: метка, номер пары, шкала прогресса
S_PURPLE_D   = "#5B49D6"   # текст изменений
S_PURPLE_L   = "#EFEDFD"   # светлая лаванда: круг номера, статусные пилюли
S_PURPLE_B   = "#E2DDFA"   # граница лавандовых плашек
S_GREEN      = "#17A15E"   # иконка аудитории
S_GREEN_D    = "#128A4E"   # «6 акад. ч»
S_GREEN_M    = "#5F9179"   # «Ауд. ПК108» — спокойный зелёно-серый
S_GREEN_L    = "#E9F7EF"   # плашка аудитории
S_GREEN_B    = "#D6EFE1"   # граница плашки аудитории
S_RED        = "#E8556D"   # бейдж «ОТМЕНА»
S_RED_D      = "#D9455F"   # текст «Занятие отменено»
S_RED_L      = "#FDECEF"   # розовая плашка отмены
S_RED_PILL   = "#FBE0E6"   # фон пилюли «Занятие отменено»
S_RED_B      = "#F8D6DD"   # граница розовой плашки
S_BORDER     = "#EEECF7"   # граница карточки
S_SHADOW     = "#B7AFDC"   # мягкая тень карточки (прозрачность — в маске)
S_TRACK      = "#E9E7F6"   # дорожка шкалы прогресса
S_DIVIDER    = "#E0DDF1"   # разделитель колонок подвала

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

# --- Текстовая шкала (дизайн-пиксели при масштабе 1.0) ---
S_TYPE = {
    "header_label": 20,     # «РАСПИСАНИЕ» с разрядкой
    "header_title": 58,     # группа / ФИО
    "header_date": 27,      # «Четверг, 24 сентября 2026»
    "header_pill": 30,      # число в счётчике занятий
    "header_pill_word": 23,  # «занятий» в счётчике
    "header_progress": 22,  # «Изучено: … акад. ч»
    "roman": 26,            # номер пары в круге
    "time": 34,             # время пары
    "subject": 33,          # предмет
    "teacher": 23,          # преподаватель
    "room": 24,             # «Ауд. ПК108»
    "hours": 27,            # «6 акад. ч»
    "small": 21,            # перемена / «2 подгруппы» / изменения
    "subgroup": 19,         # «1 П/ГР.»
    "cancel": 21,           # «Занятие отменено»
    "tag": 21,              # «ОТМЕНА»
    "note": 20,             # сноска подвала
}
# Полы: при плотном дне всё уплотняется, но читаемость не пробивается.
S_TYPE_MIN = {
    "header_label": 15,
    "header_title": 38,
    "header_date": 19,
    "header_pill": 22,
    "header_pill_word": 16,
    "header_progress": 16,
    "roman": 18,
    "time": 24,
    "subject": 23,
    "teacher": 16,
    "room": 17,
    "hours": 19,
    "small": 15,
    "subgroup": 14,
    "cancel": 15,
    "tag": 15,
    "note": 15,
}
ROOM_FONT_MIN = S_TYPE_MIN["room"]

# --- Отступы и зазоры: база и пол (умножаются на тот же масштаб) ---
S_SPACE = {
    "pad_x": 34, "pad_t": 28, "pad_b": 30,          # карточка
    "gap_badge": 26,                                # круг номера → время
    "gap_row": 22,                                  # строка времени → предмет
    "gap_subject": 14,                              # предмет → преподаватель
    "gap_items": 20,                                # между блоками подгрупп
    "gap_panel": 28,                                # левая колонка ↔ плашка
    "gap_icon": 9,                                  # иконка → текст
    "card_gap": 14,                                 # между карточками
    "gap_header": 34,                               # шапка → карточки
    "panel_pad_x": 20, "panel_pad_y": 14,           # плашка аудитории
    "panel_line_gap": 6,                            # строки в плашке
    "pill_pad_x": 16, "pill_pad_y": 9,              # пилюли (отмена, статусы)
    "band_pad_x": 14, "band_pad_y": 9,              # плашка изменений
    "badge": 66,                                    # диаметр круга с номером
    "icon": 22,                                     # размер иконок в строке
    "radius": 32, "row_radius": 22,                 # радиусы карточек и плашек
}
S_SPACE_MIN = {
    "pad_x": 22, "pad_t": 17, "pad_b": 18,
    "gap_badge": 15, "gap_row": 13, "gap_subject": 8, "gap_items": 11,
    "gap_panel": 16, "gap_icon": 5,
    "card_gap": 8, "gap_header": 20,
    "panel_pad_x": 12, "panel_pad_y": 8, "panel_line_gap": 3,
    "pill_pad_x": 9, "pill_pad_y": 5,
    "band_pad_x": 8, "band_pad_y": 5,
    "badge": 42, "icon": 15,
    "radius": 18, "row_radius": 13,
}
S_LINE_GAP_BASE = 4             # межстрочный зазор внутри блока текста
PILL_RADIUS_RATIO = 0.5         # пилюли — «капсулы»
CHANGE_LINES_MAX = 3            # сколько строк изменений показать в карточке

# Вес начертаний для ролей текста
_ROLE_WEIGHT = {
    "header_label": "bold", "header_title": "bold", "header_date": "regular",
    "header_pill": "bold", "header_pill_word": "regular",
    "header_progress": "regular",
    "roman": "bold", "time": "bold", "time_end": "medium",
    "subject": "semibold", "teacher": "regular", "room": "medium",
    "hours": "bold", "small": "regular", "subgroup": "bold",
    "cancel": "medium", "tag": "bold", "note": "regular",
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
    m["subject_lines"] = 3 if scale >= 0.96 else 2
    m["shadow"] = max(8, int(round(18 * scale)))          # радиус размытия
    m["shadow_y"] = max(3, int(round(8 * scale)))         # смещение вниз
    m["shadow_alpha"] = 70                                # плотность тени (0…255)
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
# ИКОНКИ: простые векторные глифы (без внешних ассетов)
# ------------------------------------------------------------

def _circle_points(cx: float, cy: float, r: float, n: int = 28) -> list:
    return [(cx + r * _math.cos(2 * _math.pi * i / n),
             cy + r * _math.sin(2 * _math.pi * i / n)) for i in range(n)]


def _arc_points(cx: float, cy: float, r: float, a0: float, a1: float,
                n: int = 20) -> list:
    """Точки дуги от угла a0 до a1 (радианы) — для «плеч» человечков."""
    return [(cx + r * _math.cos(a0 + (a1 - a0) * i / n),
             cy + r * _math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]


def _person_ops(x: float, y: float, s: float, color: str, *,
                uid: str, owner=None) -> list:
    """Человечек: голова + плечи (сплошные заливки)."""
    cx = x + s / 2
    head_r = s * 0.185
    head = _circle_points(cx, y + s * 0.22, head_r)
    body_r = s * 0.315
    base = y + s * 0.94
    body = (_arc_points(cx, base, body_r, _math.pi, 2 * _math.pi)
            + [(cx + body_r, base), (cx - body_r, base)])
    return [
        _poly_op(head, color, kind="icon", uid=uid + ":head", owner=owner),
        _poly_op(body, color, kind="icon", uid=uid + ":body", owner=owner),
    ]


# Иконки, которые умеет рисовать _icon_ops (всё остальное — без иконки).
_KNOWN_ICONS = frozenset({"person", "people", "clock", "cap", "calendar",
                          "cross"})


def _icon_ops(name: str, x: float, y: float, s: float, color: str, *,
              uid: str, owner=None, ink: str = S_CARD) -> list:
    """Иконка в квадрате (x, y, s, s). Возвращает список ops.

    ink — цвет деталей поверх плашки (иконки рисуются цветом карточки,
    когда стоят на насыщенном фоне).
    """
    if name == "person":
        return _person_ops(x, y + s * 0.02, s * 0.96, color, uid=uid,
                           owner=owner)
    if name == "people":
        # Двое: за спиной — приглушённый напарник, впереди — основной.
        back = _person_ops(x + s * 0.40, y - s * 0.02, s * 0.62,
                           _fade(color, 0.36), uid=uid + ":b", owner=owner)
        front = _person_ops(x - s * 0.04, y + s * 0.16, s * 0.70, color,
                            uid=uid + ":f", owner=owner)
        return back + front
    if name == "clock":
        w = max(1.2, s * 0.085)
        cx, cy = x + s / 2, y + s / 2
        r = s / 2 - w / 2
        return [
            _ellipse_op((cx - r, cy - r, cx + r, cy + r), outline=color,
                        width=w, kind="icon", uid=uid + ":ring", owner=owner),
            _line_op(cx, cy, cx, cy - r * 0.50, color, w, uid=uid + ":h1",
                     owner=owner),
            _line_op(cx, cy, cx + r * 0.40, cy, color, w, uid=uid + ":h2",
                     owner=owner),
        ]
    if name == "cap":
        top = [(x, y + s * 0.42), (x + s * 0.5, y + s * 0.12),
               (x + s, y + s * 0.42), (x + s * 0.5, y + s * 0.72)]
        base = [(x + s * 0.24, y + s * 0.50), (x + s * 0.76, y + s * 0.50),
                (x + s * 0.71, y + s * 0.88), (x + s * 0.29, y + s * 0.88)]
        return [
            _poly_op(top, color, kind="icon", uid=uid + ":top", owner=owner),
            _poly_op(base, _fade(color, 0.30), kind="icon", uid=uid + ":base",
                     owner=owner),
        ]
    if name == "calendar":
        # Плотная плитка календаря с «дырочками» — только заливки, без
        # тонких обводок (после даунскейла линии «звенят»).
        left, right = x + s * 0.22, x + s * 0.78
        top, bottom = y + s * 0.20, y + s * 0.84
        bar = y + s * 0.40
        dot = s * 0.06
        ops = [
            _rect_op((left, y + s * 0.10, left + s * 0.10, bar), s * 0.045,
                     fill=ink, kind="icon", uid=uid + ":t1", owner=owner),
            _rect_op((right - s * 0.10, y + s * 0.10, right, bar), s * 0.045,
                     fill=ink, kind="icon", uid=uid + ":t2", owner=owner),
            _rect_op((left, bar, right, bottom), s * 0.10, fill=ink,
                     kind="icon", uid=uid + ":body", owner=owner),
        ]
        for index, (dx, dy) in enumerate(((0.30, 0.52), (0.50, 0.52),
                                          (0.70, 0.52), (0.30, 0.70),
                                          (0.50, 0.70), (0.70, 0.70))):
            left_x = x + s * dx - dot / 2
            top_y = y + s * dy - dot / 2
            ops.append(_ellipse_op((left_x, top_y, left_x + dot, top_y + dot),
                                   fill=color, kind="icon",
                                   uid=f"{uid}:dot{index}", owner=owner))
        return ops
    if name == "cross":
        w = max(1.4, s * 0.14)
        pad = s * 0.26
        return [
            _line_op(x + pad, y + pad, x + s - pad, y + s - pad, color, w,
                     uid=uid + ":d1", owner=owner),
            _line_op(x + s - pad, y + pad, x + pad, y + s - pad, color, w,
                     uid=uid + ":d2", owner=owner),
        ]
    return []


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
    icon_size = m["icon"] * 0.62 if icon else 0.0
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
        p_end = _pen(size, "medium")
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
                        start_text, S_RED_D if cancelled else S_INK,
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
    """Подпись подгруппы «1 П/ГР.» с лёгкой разрядкой."""
    pen = _pen(m["subgroup"], "bold")
    spacing = max(0.6, m["subgroup"] * 0.06)
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
# LAYOUT: плашка аудитории (зелёный акцент карточки)
# ------------------------------------------------------------

def _room_panel_ops(m: dict, item: dict, right_x: float, y: float,
                    max_w: float, *, uid: str, owner=None) -> tuple:
    """Зелёная плашка: «👥 Ауд. ПК108» и ниже «6 акад. ч».

    Аудитория остаётся главным цветовым акцентом карточки, но кегль
    спокойный: «Ауд. …» — приглушённый зелёно-серый, изученные часы —
    зелёный полужирный. Плашка выравнивается по правому краю карточки.
    """
    room_text = f"Ауд. {item['room']}"
    hours = clean_text(item.get("hours") or "")
    pad_x, pad_y = m["panel_pad_x"], m["panel_pad_y"]
    room_pen = _pen(m["room"], _ROLE_WEIGHT["room"])
    hours_pen = _pen(m["hours"], _ROLE_WEIGHT["hours"])
    icon_size = m["icon"] * 0.66
    gap = m["gap_icon"]
    inner_max = max(48.0, max_w - 2 * pad_x)
    room_w_max = max(30.0, inner_max - icon_size - gap)
    if room_pen.width(room_text) > room_w_max:
        room_text = room_pen.shorten(room_text, room_w_max)
    room_w = icon_size + gap + room_pen.width(room_text)
    hours_w = hours_pen.width(hours) if hours else 0.0
    width = min(max_w, max(room_w, hours_w) + 2 * pad_x)
    line_gap = m["panel_line_gap"]
    content_h = room_pen.line_h() + (line_gap + hours_pen.line_h()
                                    if hours else 0)
    height = pad_y * 2 + content_h
    x = right_x - width
    ops: list = [_rect_op((x, y, x + width, y + height), m["row_radius"],
                          fill=S_GREEN_L, outline=S_GREEN_B, width=1.1,
                          kind="panel", uid=uid + ":bg", owner=owner)]
    cursor = y + pad_y
    room_line_h = room_pen.line_h()
    ops += _icon_ops("people", x + pad_x, cursor + (room_line_h - icon_size) / 2,
                     icon_size, S_GREEN, uid=uid + ":icon", owner=owner)
    ops.append(_text_op(room_pen,
                        (x + pad_x + icon_size + gap, cursor,
                         x + width - pad_x, cursor + room_line_h), room_text,
                        S_GREEN_M, valign="middle", max_lines=1,
                        min_size=ROOM_FONT_MIN, uid=uid + ":room", owner=owner,
                        role="room"))
    if hours:
        cursor += room_line_h + line_gap
        hours_line_h = hours_pen.line_h()
        ops.append(_text_op(hours_pen,
                            (x + pad_x, cursor, x + width - pad_x,
                             cursor + hours_line_h), hours, S_GREEN_D,
                            valign="middle", max_lines=1,
                            min_size=_type_floor("hours"), uid=uid + ":hours",
                            owner=owner, role="hours"))
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
            m, item, x + w, 0.0, min(w * 0.60, 470.0),
            uid=item["uid"] + ":panel", owner=item["owner"],
        )
    left_w = w if panel_w <= 0 else max(140.0, w - panel_w - m["gap_panel"])
    ops: list = []
    y = 0.0

    # 1. ПРЕДМЕТ (у «плейсхолдера»-отмены его заменяет пилюля отмены)
    if not (item["cancelled"] and item["placeholder"]):
        subj_ops, subj_h = _flow_text(
            m, x, y, left_w, item["subject"], "subject",
            S_RED_D if item["cancelled"] else S_INK,
            max_lines=m["subject_lines"], min_size=_type_floor("subject"),
            uid=item["uid"] + ":subject", owner=item["owner"],
        )
        ops += subj_ops
        y += subj_h

    # 2. ПРЕПОДАВАТЕЛЬ
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


def layout_card(m: dict, spec: dict, w: float, *, has_next: bool = False,
                index: int = 0) -> tuple:
    """Карточка одной пары: номер + время, затем занятия/подгруппы.

    Возвращает (ops, высота) в относительных координатах (0,0) — левый
    верх карточки. Рамку, фон и тень добавляет build_plan, когда знает
    итоговую высоту.
    """
    ops: list = []
    owner = spec["owner"]
    uid = spec["owner"]
    pad_x, pad_t, pad_b = m["pad_x"], m["pad_t"], m["pad_b"]
    inner_x = pad_x
    inner_w = w - 2 * pad_x
    cancelled_all = bool(spec["cancelled_all"])
    y = float(pad_t)

    # --- ряд 1: круг с номером пары, время, тихая пометка справа
    diameter = m["badge"]
    accent = S_RED_D if cancelled_all else S_PURPLE_D
    accent_bg = S_RED_L if cancelled_all else S_PURPLE_L
    ops.append(_ellipse_op((inner_x, y, inner_x + diameter,
                            y + diameter), fill=accent_bg, kind="icon",
                           uid=f"{uid}:num:bg", owner=owner))
    ops.append(_text_op(_pen(m["roman"], "bold"),
                        (inner_x, y, inner_x + diameter, y + diameter),
                        clean_text(spec.get("number")) or "—", accent,
                        align="center", valign="middle",
                        min_size=_type_floor("roman"), uid=f"{uid}:num",
                        owner=owner, role="roman"))

    meta_text, meta_icon = _meta_label_for(spec, has_next)
    meta_tag = ""
    if not meta_text and not cancelled_all \
            and spec.get("tag") in ("ИЗМЕНЕНО", "ДОБАВЛЕНО"):
        meta_tag = spec["tag"]
    meta_w = 0.0
    if meta_text:
        meta_pen = _pen(m["small"], _ROLE_WEIGHT["small"])
        meta_w = meta_pen.width(meta_text)
        if meta_icon in _KNOWN_ICONS:
            meta_w += m["icon"] * 0.62 + m["gap_icon"]
    elif meta_tag:
        tag_pen = _pen(m["tag"], "bold")
        meta_w = tag_pen.width(meta_tag) + 2 * m["pill_pad_x"]
    time_x = inner_x + diameter + m["gap_badge"]
    time_max = max(120.0, inner_w - diameter - 2 * m["gap_badge"] - meta_w
                   - m["gap_panel"])
    time_ops, time_w, time_h = _time_ops(
        m, clean_text(spec.get("start")) or _first_clock(spec.get("time")),
        _second_clock(spec.get("time")), time_x, 0.0, time_max,
        cancelled=cancelled_all, uid=f"{uid}:time", owner=owner,
    )
    row_h = max(diameter, time_h)
    time_y = y + (row_h - time_h) / 2
    ops += _shift_ops(time_ops, 0, time_y)
    if meta_text:
        meta_ops, _meta_used = _meta_slot_ops(
            m, inner_x + inner_w, y + row_h / 2, meta_text, meta_icon,
            uid=f"{uid}:meta", owner=owner,
        )
        ops += meta_ops
    elif meta_tag:
        tag_ops, tag_w, tag_h = _pill_ops(
            m, 0.0, 0.0, meta_tag, role="tag", fill=spec["tag_bg"],
            text_fill=spec["tag_fill"], weight="bold", uid=f"{uid}:tag",
            owner=owner,
        )
        ops += _shift_ops(tag_ops, inner_x + inner_w - tag_w,
                          y + (row_h - tag_h) / 2)
    y += row_h

    items = spec["items"]
    multi = len(items) > 1 or any(item["subgroup"] for item in items)
    if not items:
        # Синтетическая карточка удалённой пары без подгрупп: только плашка
        y += m["gap_row"]
        pill_ops, pill_w, pill_h = _pill_ops(
            m, inner_x, y, "Занятие отменено", role="cancel",
            fill=S_RED_PILL, text_fill=S_RED_D, uid=f"{uid}:cancel",
            owner=owner,
        )
        ops += pill_ops
        y += pill_h
    else:
        for position, item in enumerate(items):
            if position:
                y += m["gap_items"]
            label = _subgroup_label(item["subgroup"]) or ""
            if multi and label and not item["cancelled"]:
                caption_ops, caption_h = _subgroup_caption(
                    m, inner_x, y, inner_w, label,
                    uid=f"{uid}:i{position}:caption", owner=owner,
                )
                ops += caption_ops
                y += caption_h + max(2, m["gap_subject"] * 0.25)
            if item["cancelled"]:
                block_ops, block_h = layout_cancelled_item(
                    m, item, inner_x, inner_w, uid=f"{uid}:i{position}",
                    owner=owner,
                )
            else:
                block_ops, block_h = layout_item_body(
                    m, item, inner_x, inner_w,
                )
            ops += _shift_ops(block_ops, 0, y)
            y += block_h

    return ops, y + pad_b


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
    icon_size = max(30, int(round(44 * m["scale"])))
    title_pen = _pen(m["subject"], "semibold")
    note_pen = _pen(m["small"], "regular")
    block = icon_size + 20 + title_pen.line_h() + 12 + note_pen.line_h()
    top = max(24.0, (h - block) / 2)
    ops += _icon_ops("calendar", (w - icon_size) / 2, top, icon_size,
                     S_CARD, uid="empty:icon", ink=S_PURPLE)
    cursor = top + icon_size + 20
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
    icon_size = float(max(42, int(round(64 * m["scale"]))))

    # --- правая колонка: счётчик занятий, шкала прогресса, «Изучено»
    lesson_count = count_lessons(schedule.lessons)
    count_word = _plural(lesson_count, "занятие", "занятия", "занятий")
    num_pen = _pen(m["header_pill"], "bold")
    word_pen = _pen(m["header_pill_word"], "regular")
    num_txt = str(lesson_count)
    pill_h = max(int(num_pen.line_h() + 2 * m["pill_pad_y"]),
                 int(icon_size * 0.86))
    pill_w = float(int(m["pill_pad_x"] * 2 + num_pen.width(num_txt) + 7
                      + word_pen.width(count_word)))
    forecast = ctx.get("forecast")
    progress_on = bool(ctx.get("is_group") and forecast is not None
                       and forecast.studied_minutes > 0)
    progress_txt = study_badge_text(forecast) if progress_on else ""
    prog_pen = _pen(m["header_progress"], "regular")
    progress_w = prog_pen.width(progress_txt) if progress_txt else 0.0
    bar_w = max(210.0, min(progress_w, 330.0))
    bar_h = float(max(7, int(round(12 * m["scale"]))))
    right_w = max(pill_w, progress_w, bar_w)
    right_x = right_edge - right_w

    text_x = x + icon_size + m["gap_badge"]
    left_w = max(200.0, right_x - 30 - text_x)

    # --- иконка календаря на лавандовом квадрате
    ops.append(_rect_op((x, y, x + icon_size, y + icon_size),
                        icon_size * 0.30, fill=S_PURPLE, kind="icon",
                        uid="header:icon:bg", owner="header"))
    ops += _icon_ops("calendar", x, y, icon_size, S_PURPLE, uid="header:icon",
                     owner="header", ink=S_CARD)

    # --- метка, название группы/ФИО, дата
    label_pen = _pen(m["header_label"], "bold")
    label = clean_text(title or "РАСПИСАНИЕ").upper()
    spacing = max(1.6, m["header_label"] * 0.14)
    if label_pen.spaced_width(label, spacing) > left_w:
        label = label_pen.shorten(label, left_w)
    label_y = y + icon_size * 0.06
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
    date_y = title_bottom + 10
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
            text_fill=S_PURPLE_D, weight="bold", uid="header:day",
            owner="header",
        )
        day_x = date_op["bbox"][2] + m["gap_icon"] * 1.8
        if day_x + day_w <= text_x + left_w:
            ops += _shift_ops(day_ops, day_x,
                              date_y + (date_h - day_h) / 2)
            date_bottom = max(date_bottom, date_y + date_h)

    # --- счётчик занятий, шкала прогресса, «Изучено»
    pill_y = y + 4
    ops.append(_shadow_op((right_x, pill_y + 2, right_x + pill_w,
                           pill_y + pill_h + 2), pill_h / 2, uid="header:pill:shadow",
                          blur=12, dy=4, alpha=58))
    ops.append(_rect_op((right_x, pill_y, right_x + pill_w, pill_y + pill_h),
                        pill_h / 2.0, fill=S_CARD, kind="chip",
                        uid="header:pill:bg", owner="header"))
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
        bar_y = right_bottom + m["gap_row"] * 0.7
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
                          m["row_radius"], fill=_fade(S_PURPLE_L, 0.42),
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
        "groups": clean_text(getattr(lesson, "groups", "")),
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
        if widen_gaps and len(stack) > 1:
            gap += min(slack_for_gaps / max(1, len(stack) - 1), gap * 0.9)
        total = (sum(item["height"] for item in stack)
                 + gap * max(0, len(stack) - 1))
        return stack, total, gap

    slack_for_gaps = 0.0
    widen_gaps = False
    stack, total, gap = measure(m)
    fits = total <= available
    if fits and available - total > 4:
        # Остаток воздуха — в зазоры между карточками (не более +90%),
        # чтобы не было «карточки вверху, пустота внизу».
        slack_for_gaps = available - total
        widen_gaps = len(stack) > 1
        wide = measure(m)
        if wide[1] <= available:
            stack, total, gap = wide
            fits = True
        else:
            slack_for_gaps, widen_gaps = 0.0, False
    # Занятий мало: воздух добавляем ВНУТРЬ карточек (отступы и зазоры
    # блоков), кегли не трогаем.
    shift_top = 0.0
    if fits and stack:
        slack = available - total
        n_cards = sum(1 for item in stack if item["kind"] == "card")
        if slack > m["card_gap"] * 2 and n_cards:
            breath = min(220.0, slack * 0.85 / n_cards)
            while breath > 6:
                breathed = measure(_breath_metrics(m, breath))
                if breathed[1] <= available:
                    stack, total, gap = breathed
                    fits = True
                    break
                breath /= 2.0
            if available - total > m["card_gap"] * 2:
                shift_top = min(120.0, (available - total) * 0.40)

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
      мягкой зелёной плашкой, а не «кричащим» чипом.
    - Отдельной колонки «Изменения» нет: ИЗМЕНЕНО/ДОБАВЛЕНО/ОТМЕНА и
      сами правки показываются внутри карточки соответствующей пары.
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
            subject_totals = load_subject_totals()
        forecast = get_study_forecast() if is_group else None
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
        filename = (
            f"schedule_{kind}{staff_part}_{schedule.date.isoformat()}"
            f"{suffix}.png"
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
        forecast = get_study_forecast()
        runtime = get_runtime_stats()
        db_stats = _database_stats()
        created = subscriber_info(chat_id)
        subscribers_total = len(load_subscribers())

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
        if created:
            sub_rows.append(("Этот чат", f"подписан · с {created}"))
        else:
            sub_rows.append(("Этот чат", "не подписан"))
        sub_rows.append(("Всего подписок", str(subscribers_total)))
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

        show_bar = forecast.total_minutes > 0 and forecast.studied_minutes > 0
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
        note_lines = study_note_lines(forecast)
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

        big_title = GROUP_NAME
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
    """Текстовый fallback картинки статуса (если PNG не сгенерировался)."""
    created = subscriber_info(chat_id)
    total = len(load_subscribers())
    forecast = get_study_forecast()
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
    if forecast.studied_minutes > 0:
        lines.append(f"📖 {study_badge_text(forecast)}")
        lines.append(f"По обыкновенному времени: {regular_study_time_text(forecast)}")
        if forecast.remaining_minutes:
            lines.append(
                "Осталось при текущем темпе: "
                f"<b>≈ {format_academic_hours(forecast.remaining_minutes)}</b>"
            )
        lines.append(f"<i>{STUDY_NOTE_EXPLANATION}</i>")
    lines.append(
        f"⏱ Аптайм: {format_uptime(time.monotonic() - _PROCESS_STARTED_MONOTONIC)}"
    )
    return "\n".join(lines)


def _status_caption() -> str:
    """Короткая подпись к картинке статуса."""
    forecast = get_study_forecast()
    lines = [f"🤖 <b>Статус бота</b> · 📚 <b>{GROUP_NAME}</b>"]
    if forecast.studied_minutes > 0:
        lines.append(f"📖 {study_badge_text(forecast)}")
    lines.append(
        "⏱ Аптайм: "
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
    caption = _status_caption()
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
    await _send_status(message)


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
    await _send_status(callback.message)


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
        return (
            f"🟢 <b>Добавлено:</b> {label}\n"
            f"{display_subject_text(new.get('subject'))}"
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
            f"{display_subject_text(old.get('subject'))}"
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
        # Отсутствие расписания — тоже не изменение.
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
# МОНИТОРИНГ ИЗМЕНЕНИЙ
# ============================================================

async def schedule_monitor(bot: Bot) -> None:
    """Фоновая задача. Не блокирует polling, одна на процесс.

    Каждые 5 минут даты сегодня/завтра пересчитываются заново
    (переход через полночь обрабатывается автоматически), обе даты
    проверяются независимо.
    """
    logger.info(
        "Мониторинг запущен. Интервал: %s сек (%s мин).",
        CHECK_INTERVAL,
        CHECK_INTERVAL // 60,
    )

    birthday_checked_dates: set[str] = set()
    while True:
        # Разовый пересчёт изученного времени после миграции истории на
        # запись по подгруппам (срабатывает один раз, дальше — пустой флаг).
        try:
            await recalculate_study_history()
        except Exception:
            logger.exception("Ошибка разового пересчёта истории занятий")

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

        # Момент завершения цикла — для «Последняя проверка» в /status.
        _LAST_SCHEDULE_CHECK["at"] = now_local()

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
    logger.info("Бот расписания группы %s", GROUP_NAME)
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
