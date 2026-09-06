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
- Версия 2.1.4 — система версий и changelog: versioning.py.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, F
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
    Message,
)

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

# Группа
GROUP_NAME = os.getenv("GROUP_NAME", "ЭС7-24").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "508"))
BASE_URL = os.getenv(
    "BASE_URL", "http://www.ishnk.ru/2025/site/schedule/group/508"
).rstrip("/")

# Токен берётся только из переменной окружения / .env, не из кода.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

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
    """Одно занятие / одна подгруппа в рамках пары."""

    pair: str          # римский номер, например "I"
    time: str          # "08:30 - 09:50"
    subject: str
    teacher: str
    room: str
    start: str = ""    # "08:30"
    end: str = ""      # "09:50"
    subgroup: Optional[str] = None  # "1", "2" или None (обычное занятие)
    break_duration: str = ""        # например "15 мин"

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

    `lessons` остаётся плоским списком всех занятий/подгрупп (Это удобно
    для подписи/хэша и совместимости), а `pairs` собирает их в пары.
    """

    date: date
    group: str
    lessons: list
    fallback: bool = False

    @property
    def pairs(self) -> list:
        """Группировка flat-списка занятий в пары."""
        return group_into_pairs(self.lessons)


class ScheduleUnavailable(Exception):
    """Сайт недоступен / сеть не работает."""


def group_into_pairs(lessons: list) -> list:
    """Группирует flat-список занятий в пары.

    Порядок пар сохраняет порядок первого появления / порядок по номеру.
    Внутри пары занятия сортируются по подгруппе (None идёт первым).
    """
    groups = OrderedDict()
    for lesson in lessons:
        key = (
            clean_text(lesson.pair).upper(),
            clean_text(lesson.start) or "",
            clean_text(lesson.end) or "",
        )
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
    "ма": 5, "июн": 6, "июл": 7, "август": 8,
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


# ============================================================
# ТЕКСТОВАЯ КОМАНДА «РАСПИСАНИЕ»
# ============================================================

@dataclass
class ScheduleTextRequest:
    """Результат разбора текстовой команды «расписание…».

    - matched=False — сообщение не команда (бота не касается);
    - date=None, error=False — «расписание» без даты -> завтра;
    - date=<дата> — распознанная дата;
    - error=True — «расписание» есть, но дату определить не удалось.
    """

    matched: bool = False
    date: Optional[date] = None
    error: bool = False


_SCHEDULE_TEXT_RE = re.compile(r"^расписание(.*)$", re.IGNORECASE)


def parse_schedule_text(text: str) -> ScheduleTextRequest:
    """Разбирает «расписание» и «расписание на <дата>».

    Отдельная функция: обработка команды -> парсинг даты -> get_schedule.
    Устойчива к регистру и лишним пробелам. Относительные даты
    («сегодня», «завтра», «послезавтра») считаются в часовом поясе
    Asia/Yekaterinburg. Ошибок «угадывания» нет: непонятная дата
    возвращает error=True.
    """
    if not text:
        return ScheduleTextRequest()

    normalized = clean_text(text).lower()
    match = _SCHEDULE_TEXT_RE.match(normalized)
    if not match:
        return ScheduleTextRequest()

    rest = match.group(1).strip()
    if not rest:
        # Просто «расписание» -> расписание на завтра.
        return ScheduleTextRequest(matched=True)

    # Дальше допускается только «на <дата>».
    arg_match = re.fullmatch(r"на(?:\s+(.+))?", rest)
    if not arg_match:
        # «расписание чего-то» — это не наша команда.
        return ScheduleTextRequest()

    arg = clean_text(arg_match.group(1) or "")
    if not arg:
        # «расписание на» без даты — подсказка, не угадываем.
        return ScheduleTextRequest(matched=True, error=True)

    target = parse_user_date(arg)
    if target is None:
        return ScheduleTextRequest(matched=True, error=True)

    return ScheduleTextRequest(matched=True, date=target)


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


async def fetch_html(day: date):
    """
    Выполняет GET по HTTP и возвращает строку HTML (utf-8) либо None.

    - allow_redirects=False: не переходим по 3xx и не меняем протокол.
    - timeout ~20 секунд.
    """
    url = build_url(day)

    logger.info("Получение расписания: %s", day.isoformat())

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)

    try:
        async with aiohttp.ClientSession(
            headers=HEADERS,
            timeout=timeout,
        ) as session:

            async with session.get(
                url,
                allow_redirects=False,
            ) as response:

                status = response.status
                logger.info("HTTP статус: %s", status)

                # Редирект — не следуем (иначе могли бы уйти на другой протокол).
                if status in (300, 301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    logger.error(
                        "HTTP редирект %s на «%s» — не переходим по перенаправлению.",
                        status,
                        location,
                    )
                    return None

                if status != 200:
                    logger.error("HTTP ошибка: %s", status)
                    return None

                raw = await response.read()

                if not raw:
                    logger.error("Пустой HTML")
                    return None

                logger.info("Получено HTML: %s байт", len(raw))

                return raw.decode("utf-8", errors="replace")

    except asyncio.TimeoutError:
        logger.error("Таймаут при получении расписания: %s", url)
        return None
    except aiohttp.ClientError as error:
        logger.error("HTTP ошибка при получении расписания: %s", error)
        return None
    except Exception:
        logger.exception("Не удалось получить расписание")
        return None


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
        if text_node and "ауд." in text_node:
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
        r"ауд\.\s*([A-Za-zА-Яа-я0-9№.\-()/]+)",
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


def parse_schedule(html: str, day: date) -> Schedule:
    soup = BeautifulSoup(html, "html.parser")

    # Учитываем ТОЛЬКО карточки ежедневного расписания:
    #   div.card.myCard  +  .card-header  (+ .h3 и .h4 внутри)
    cards = soup.select("div.card.myCard")

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

    logger.info("Найдено занятий: %s", len(unique))

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

    return Schedule(
        date=day,
        group=GROUP_NAME,
        lessons=unique,
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


# ============================================================
# НОРМАЛИЗАЦИЯ, ХЭШ И СРАВНЕНИЕ РАСПИСАНИЙ
# ============================================================

LESSON_FIELDS = ("pair", "start", "end", "subgroup", "subject", "room", "teacher")
FIELD_LABELS = {
    "subject": "Предмет",
    "room": "Аудитория",
    "teacher": "Преподаватель",
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
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
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


def render_schedule_image(
    schedule: Schedule,
    changes=None,
    title: Optional[str] = None,
) -> Path:
    """
    Создаёт PNG-картинку расписания.

    - `changes=None`  -> обычная картинка без выделения изменений.
    - `changes=[...]` -> изменённые блоки выделяются цветом/рамкой,
      добавляется заголовок «РАСПИСАНИЕ ИЗМЕНИЛОСЬ» и блок
      «Что изменилось».
    - `title`         -> произвольный заголовок в шапке (например
      «РАСПИСАНИЕ ОПУБЛИКОВАНО»).
    """
    try:
        lessons = list(schedule.lessons)
        changes = list(changes or [])
        pairs = schedule.pairs

        # Шрифты
        font_label = get_font(24, bold=True)
        font_group = get_font(72, bold=True)
        font_date = get_font(30)
        font_count = get_font(24, bold=True)
        font_pair = get_font(28, bold=True)
        font_time = get_font(32, bold=True)
        font_break = get_font(22)
        font_subject = get_font(34, bold=True)
        font_info = get_font(26)
        font_subg = get_font(24, bold=True)
        font_status = get_font(22, bold=True)
        font_detail = get_font(23, bold=True)
        font_empty_title = get_font(44, bold=True)
        font_empty_sub = get_font(30)
        font_footer = get_font(24)
        font_summary = get_font(25, bold=True)
        font_summary_body = get_font(24)

        # Геометрия
        W = 1080
        MARGIN = 58
        HEADER_H = 250
        card_gap = 28
        x1 = MARGIN
        x2 = W - MARGIN
        inner = 48
        text_w = (x2 - x1) - 2 * inner
        circle_s = 68
        top_pad = 26
        pad_bottom = 24
        item_gap = 16
        line_h = int(font_subject.size * 1.35)
        chip_h = 44
        subg_h = 30
        status_h = 30
        detail_h = 30

        change_by_key = {
            (clean_text(c.pair).upper(), clean_text(c.subgroup) or None): c
            for c in changes
        }
        removed_by_pair = {}
        for c in changes:
            if c.kind == "removed":
                removed_by_pair.setdefault(
                    clean_text(c.pair).upper(), []
                ).append(c)

        def subject_lines_for(subject):
            return _wrap_lines(
                subject or "Предмет не указан", font_subject, text_w
            )

        def item_block_height(item) -> int:
            h = 8 + 10  # верхний/нижний отступ
            if _subgroup_label(item["lesson"].subgroup):
                h += subg_h
            if item["kind"] != "normal":
                h += status_h
            h += line_h * len(subject_lines_for(item["lesson"].subject))
            h += 14 + chip_h
            if item["kind"] == "changed":
                h += 8 + detail_h * len(item["details"])
            return h

        def pair_card_height(pair) -> int:
            items = _render_items_for_pair(
                pair, change_by_key, removed_by_pair
            )
            header_h = top_pad + circle_s + 24
            blocks_h = sum(item_block_height(i) for i in items)
            blocks_gap = max(0, len(items) - 1) * item_gap
            return header_h + blocks_h + blocks_gap + pad_bottom

        # Пустое расписание.
        if not lessons:
            empty_h = 250
            pairs = []
            cards_h = empty_h
        else:
            cards_h = (
                sum(pair_card_height(p) for p in pairs)
                + max(0, len(pairs) - 1) * card_gap
            )

        summary_lines = _change_summary_lines(changes) if changes else []
        if changes and not summary_lines:
            summary_lines = ["Что изменилось:"]
        summary_h = 0
        if summary_lines:
            summary_h = 50 + summary_lines.__len__() * 34 + 20

        # Если есть блок «Что изменилось», оставляем больше места внизу,
        # чтобы подвал не накладывался на последнюю строку изменений.
        H = (
            HEADER_H
            + 36
            + cards_h
            + summary_h
            + (140 if summary_lines else 80)
        )

        image = Image.new("RGB", (W, H), COL_BG)
        draw = ImageDraw.Draw(image)

        # ---------- шапка ----------
        draw.rectangle((0, 0, W, HEADER_H), fill=COL_WHITE)
        draw.rectangle((0, 0, 14, HEADER_H), fill=COL_ACCENT)

        header_label = title or (
            "РАСПИСАНИЕ ИЗМЕНИЛОСЬ" if changes else "РАСПИСАНИЕ"
        )
        draw.text((MARGIN + 20, 40), header_label,
                  font=font_label, fill=COL_ACCENT)
        draw.text((MARGIN + 20, 84), GROUP_NAME,
                  font=font_group, fill=COL_INK)
        draw.text((MARGIN + 22, 186),
                  format_date_header(schedule.date),
                  font=font_date, fill=COL_MUTED)

        # бейдж с количеством занятий
        if lessons:
            count_text = f"{len(lessons)} "
            count_text += "занятие" if len(lessons) == 1 \
                else "занятия" if len(lessons) < 5 else "занятий"
        else:
            count_text = "занятий нет"

        cw = draw.textlength(count_text, font=font_count)
        chip_pad_x = 26
        chip_w = cw + chip_pad_x * 2
        chip_h = 54
        chip_x = W - MARGIN - chip_w
        chip_y = 48
        draw.rounded_rectangle(
            (chip_x, chip_y, chip_x + chip_w, chip_y + chip_h),
            radius=chip_h / 2,
            fill=COL_ACCENT_LIGHT,
        )
        draw.text(
            (chip_x + chip_pad_x,
             chip_y + (chip_h - _text_h(font_count)) // 2),
            count_text,
            font=font_count,
            fill=COL_ACCENT,
        )

        # ---------- пустое расписание ----------
        if not lessons:
            by = HEADER_H + 36
            draw.rounded_rectangle(
                (x1, by, x2, by + empty_h),
                radius=30,
                fill=COL_WHITE,
                outline=COL_BORDER,
                width=2,
            )
            title_txt = "Занятий нет"
            tw = draw.textlength(title_txt, font=font_empty_title)
            draw.text(((W - tw) / 2, by + 62), title_txt,
                      font=font_empty_title, fill=COL_INK)
            sub = "Расписание на этот день не опубликовано."
            sw = draw.textlength(sub, font=font_empty_sub)
            draw.text(((W - sw) / 2, by + 140), sub,
                      font=font_empty_sub, fill=COL_MUTED)

        # ---------- карточки пар ----------
        else:
            y = HEADER_H + 36
            for pair in pairs:
                left = x1 + inner
                top = y
                ch = pair_card_height(pair)
                items = _render_items_for_pair(
                    pair, change_by_key, removed_by_pair
                )
                pair_has_change = any(
                    it["kind"] != "normal" for it in items
                )

                # тень
                draw.rounded_rectangle(
                    (x1 + 6, top + 8, x2 + 6, top + ch + 8),
                    radius=30,
                    fill="#E6EAF3",
                )
                # карточка
                draw.rounded_rectangle(
                    (x1, top, x2, top + ch),
                    radius=30,
                    fill=COL_WHITE,
                    outline=COL_ACCENT if pair_has_change else COL_BORDER,
                    width=3 if pair_has_change else 2,
                )

                # --- кружок пары ---
                cy_top = top + top_pad
                cx = left
                draw.ellipse(
                    (cx, cy_top, cx + circle_s, cy_top + circle_s),
                    fill=COL_ACCENT,
                )
                roman = clean_text(pair.number).upper()
                rw = draw.textlength(roman, font=font_pair)
                rh = _text_h(font_pair)
                draw.text(
                    (cx + (circle_s - rw) / 2,
                     cy_top + (circle_s - rh) / 2),
                    roman,
                    font=font_pair,
                    fill=COL_WHITE,
                )

                # --- время ---
                time_y = cy_top + (circle_s - _text_h(font_time)) / 2
                draw.text((left + circle_s + 34, time_y),
                          f"{pair.start} — {pair.end}",
                          font=font_time, fill=COL_ACCENT)
                if pair.break_duration:
                    break_text = f"перемена {pair.break_duration}"
                    draw.text(
                        (left + circle_s + 34,
                         cy_top + circle_s + 4),
                        break_text,
                        font=font_break,
                        fill=COL_MUTED,
                    )

                # --- блоки занятий/подгрупп ---
                by = top + top_pad + circle_s + 24
                for item in items:
                    lesson = item["lesson"]
                    kind = item["kind"]
                    h = item_block_height(item)

                    # Подсветка изменений.
                    if kind == "changed":
                        fill = COL_WARN_LIGHT
                        outline = COL_WARN
                    elif kind == "added":
                        fill = COL_GREEN_LIGHT
                        outline = COL_GREEN
                    elif kind == "removed":
                        fill = COL_RED_LIGHT
                        outline = COL_RED
                    else:
                        fill = None
                        outline = None

                    if fill is not None:
                        draw.rounded_rectangle(
                            (left - 6, by, x2 - inner + 6, by + h),
                            radius=14,
                            fill=fill,
                            outline=outline,
                            width=2,
                        )

                    inner_y = by + 8

                    # Подгруппа.
                    subgroup_txt = _subgroup_label(lesson.subgroup)
                    if subgroup_txt:
                        draw.rounded_rectangle(
                            (left, inner_y, left + draw.textlength(
                                subgroup_txt, font=font_subg
                            ) + 24, inner_y + subg_h),
                            radius=subg_h / 2,
                            fill=COL_ACCENT_LIGHT,
                        )
                        draw.text(
                            (left + 12,
                             inner_y + (subg_h - _text_h(font_subg)) / 2),
                            subgroup_txt,
                            font=font_subg,
                            fill=COL_ACCENT,
                        )
                        inner_y += subg_h + 6

                    # Статус изменения (без эмодзи: шрифт DejaVu их не рисует).
                    if kind == "changed":
                        status = "ИЗМЕНЕНО"
                        color = COL_WARN
                    elif kind == "added":
                        status = "ДОБАВЛЕНО"
                        color = COL_GREEN
                    elif kind == "removed":
                        status = "УДАЛЕНО"
                        color = COL_RED
                    else:
                        status = ""

                    if status:
                        sw2 = draw.textlength(status, font=font_status)
                        draw.text(
                            (x2 - inner - sw2, inner_y),
                            status,
                            font=font_status,
                            fill=color,
                        )
                        inner_y += status_h

                    # Предмет.
                    subject = clean_text(lesson.subject) or "Предмет не указан"
                    subj_lines = subject_lines_for(subject)
                    for idx, line in enumerate(subj_lines):
                        draw.text(
                            (left, inner_y + idx * line_h),
                            line,
                            font=font_subject,
                            fill=COL_INK,
                        )
                    inner_y += line_h * len(subj_lines)

                    # Аудитория + преподаватель.
                    inner_y += 14
                    meta_y = inner_y
                    room_text = (
                        f"ауд. {lesson.room}"
                        if clean_text(lesson.room) not in ("", "—")
                        else "ауд. —"
                    )
                    room_color = COL_RED if kind == "removed" else COL_GREEN
                    room_fill = (
                        COL_RED_LIGHT if kind == "removed" else COL_GREEN_LIGHT
                    )
                    room_w = draw.textlength(room_text, font=font_info)
                    room_chip_pad = 18
                    room_chip_w = room_w + room_chip_pad * 2
                    draw.rounded_rectangle(
                        (left, meta_y,
                         left + room_chip_w, meta_y + chip_h),
                        radius=chip_h / 2,
                        fill=room_fill,
                    )
                    draw.text(
                        (left + room_chip_pad,
                         meta_y + (chip_h - _text_h(font_info)) / 2),
                        room_text,
                        font=font_info,
                        fill=room_color,
                    )

                    teacher = (
                        clean_text(lesson.teacher)
                        if clean_text(lesson.teacher) not in ("", "—")
                        else "Преподаватель не указан"
                    )
                    teacher_x = left + room_chip_w + 24
                    teacher_max_w = (x2 - inner) - teacher_x
                    teacher = _truncate(teacher, font_info, teacher_max_w)
                    draw.text(
                        (teacher_x, meta_y + (chip_h - _text_h(font_info)) / 2),
                        teacher,
                        font=font_info,
                        fill=COL_MUTED,
                    )
                    inner_y += chip_h

                    # Было -> стало для изменённых полей.
                    if kind == "changed":
                        inner_y += 8
                        for detail in item["details"]:
                            label_name = detail.get("label") or detail.get(
                                "field", ""
                            )
                            old_val = clean_text(detail.get("old", ""))
                            new_val = clean_text(detail.get("new", ""))
                            line = (
                                f"{label_name}: "
                                f"{old_val or '—'} -> {new_val or '—'}"
                            )
                            draw.text(
                                (left, inner_y),
                                _truncate(line, font_detail, text_w),
                                font=font_detail,
                                fill=COL_WARN,
                            )
                            inner_y += detail_h

                    # --- подвал блока ---
                    by += h + item_gap

                y += ch + card_gap

        # ---------- блок «Что изменилось» ----------
        if summary_lines:
            sy = HEADER_H + 36 + cards_h + (36 if lessons else 0)
            summary_h_actual = 50 + summary_lines.__len__() * 34 + 20
            draw.rounded_rectangle(
                (x1, sy, x2, sy + summary_h_actual),
                radius=30,
                fill=COL_WHITE,
                outline=COL_WARN,
                width=2,
            )
            tys = sy + 24
            heading = "Что изменилось:"
            draw.text((x1 + inner, tys), heading,
                      font=font_summary, fill=COL_WARN)
            tys += 44
            for line in summary_lines:
                draw.text(
                    (x1 + inner + 8, tys),
                    _truncate(line, font_summary_body, text_w - 16),
                    font=font_summary_body,
                    fill=COL_INK,
                )
                tys += 34

        # ---------- подвал ----------
        footer = "ИНК · расписание"
        draw.text(
            (W - MARGIN - draw.textlength(footer, font=font_footer),
             H - 56),
            footer,
            font=font_footer,
            fill=COL_FOOTER,
        )

        # ---------- сохранение ----------
        suffix = "_changed" if changes else ""
        filename = (
            f"schedule_{schedule.date.isoformat()}_{len(lessons)}{suffix}.png"
        )
        path = IMAGE_DIR / filename
        image.save(path, "PNG", optimize=True)
        logger.info("Изображение сохранено: %s (%sx%s)", path, W, H)
        return path

    except Exception:
        logger.exception("Ошибка генерации изображения")
        raise


# ============================================================
# КЛАВИАТУРА
# ============================================================

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
    lines = [
        f"📚 <b>{schedule.group}</b>",
        f"📅 {format_date_full(schedule.date)}",
    ]
    if schedule.lessons:
        lines.append(f"🕐 Занятий: {len(schedule.lessons)}")
    return "\n".join(lines)


async def _send_photo(destination, schedule: Schedule) -> bool:
    """Генерирует PNG и отправляет его. Временный файл удаляется после отправки."""
    path = render_schedule_image(schedule)
    try:
        caption = _photo_caption(schedule)
        if isinstance(destination, Message):
            await destination.answer_photo(
                FSInputFile(path), caption=caption
            )
        else:
            await destination.message.answer_photo(
                FSInputFile(path), caption=caption
            )
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
        if isinstance(destination, Message):
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
        f"Я бот расписания группы <b>{GROUP_NAME}</b>.\n\n"
        f"Доступные действия:\n"
        f"📅 <b>Сегодня</b> — /today\n"
        f"📅 <b>Завтра</b> — /schedule\n"
        f"✍️ <b>Текстом</b> — просто напиши «расписание»\n"
        f"    или «расписание на 4 сентября»\n"
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
            f"👋 Привет! Я бот расписания группы <b>{GROUP_NAME}</b>.\n\n"
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
        day_str = f"{label}, {format_date_full(day)}"
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
        f"🕐 Занятий: {len(schedule.lessons)}",
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
                await asyncio.sleep(0.08)
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

    if not load_subscribers():
        # Некому отправлять — не ходим на сайт и не фиксируем baseline,
        # чтобы первый подписавшийся получил уведомление о расписании.
        logger.info("Проверка %s: подписчиков нет, пропускаем.", date_key)
        return

    try:
        schedule = await get_schedule(day)
    except ScheduleUnavailable:
        # Ошибка загрузки/парсинга — не считается изменением.
        logger.warning(
            "Проверка %s: источник недоступен — изменением не считаем.",
            date_key,
        )
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
        f"занятий: {len(schedule.lessons)}, изменений: {len(changes)}",
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
                await asyncio.sleep(0.08)
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

    while True:
        # Каждый цикл даты вычисляются заново через Asia/Yekaterinburg.
        for day, label in (
            (get_today(), "сегодня"),
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
