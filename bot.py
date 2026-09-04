# -*- coding: utf-8 -*-
"""
Telegram-бот расписания группы ЭС7-24 (Институт нефти и газа, ishnk.ru).

- Получает расписание напрямую по HTTP (aiohttp), БЕЗ браузера.
- Разбирает HTML через BeautifulSoup (только карточки div.card.myCard с .card-header).
- Игнорирует недельную таблицу.
- Генерирует современную PNG-картинку через Pillow.
- Поддержка подписок (SQLite), фоновый мониторинг изменений.
- Работает в Docker, кодировка UTF-8, московское время.
"""

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
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
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
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

# Период автоматической проверки (секунды)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))

# Таймаут HTTP-запроса
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))

# Время, из которого берётся «сегодня» / «завтра»
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
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
    """Одно занятие."""

    pair: str          # римский номер, например "I"
    time: str          # "08:30 - 09:50"
    subject: str
    teacher: str
    room: str
    start: str = ""    # "08:30"
    end: str = ""      # "09:50"


@dataclass
class Schedule:
    """Расписание на конкретный день."""

    date: date
    group: str
    lessons: list
    fallback: bool = False


class ScheduleUnavailable(Exception):
    """Сайт недоступен / сеть не работает."""


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("schedule_bot")


# ============================================================
# КАТАЛОГИ / ИНИЦИАЛИЗАЦИЯ
# ============================================================

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# ДАТА (Московское время)
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


def get_today() -> date:
    return datetime.now(TZ).date()


def get_tomorrow() -> date:
    return get_today() + timedelta(days=1)


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

def _find_room(card) -> str:
    """
    Аудитория в элементе с текстом «ауд.»:

        <span>ауд.<span class="h5">УК107</span></span>

    Возвращает только номер (УК107). Если нет — "".
    """
    # Способ 1: элемент .h5 рядом с текстом «ауд.»
    for text_node in card.find_all(string=True):
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
    card_text = clean_text(card.get_text(" ", strip=True))
    match = re.search(
        r"ауд\.\s*([A-Za-zА-Яа-я0-9№.\-()/]+)",
        card_text,
        re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1))

    return ""


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

        # Предмет: основной селектор (мобильная вёрстка)
        subject = ""
        subject_node = card.select_one(".d-md-none.text-center.text-truncate")

        # fallback: .d-none.d-md-block b
        if subject_node is None:
            bold_node = card.select_one(".d-none.d-md-block b")
            if bold_node is not None:
                subject_node = bold_node

        if subject_node is not None:
            subject = clean_text(subject_node.get_text(" ", strip=True))

        if not subject:
            fallback = card.select_one(".d-none.d-md-block")
            if fallback is not None:
                subject = clean_text(fallback.get_text(" ", strip=True))

        # Преподаватель: .Staff (видимый текст; иначе атрибут title)
        teacher = ""
        staff = card.select_one(".Staff")
        if staff is not None:
            teacher = clean_text(staff.get_text(" ", strip=True))
            if not teacher and staff.get("title"):
                teacher = clean_text(staff.get("title"))

        if not teacher and not subject:
            # если тело карточки вообще пустое — это не занятие
            continue

        room = _find_room(card) or "—"

        lessons.append(
            Lesson(
                pair=pair,
                time=time_str,
                start=start,
                end=end,
                subject=subject or "Предмет не указан",
                teacher=teacher or "—",
                room=room,
            )
        )

    logger.info("Найдено подходящих карточек: %s", len(lessons))

    # Убираем дубликаты и сортируем по номеру пары
    unique: list = []
    seen = set()

    for lesson in lessons:
        key = (lesson.pair, lesson.time, lesson.subject,
               lesson.teacher, lesson.room)
        if key in seen:
            continue
        seen.add(key)
        unique.append(lesson)

    unique.sort(key=lambda x: ROMAN_PAIRS.get(x.pair, 99))

    logger.info("Найдено занятий: %s", len(unique))

    for lesson in unique:
        logger.info(
            "Пара %s | %s | %s | %s | %s",
            lesson.pair, lesson.time,
            lesson.room, lesson.teacher, lesson.subject,
        )

    return Schedule(
        date=day,
        group=GROUP_NAME,
        lessons=unique,
    )


async def get_schedule(day: date) -> Schedule:
    """Получает и разбирает расписание. Бросает ScheduleUnavailable при сетевой ошибке."""
    html = await fetch_html(day)

    if html is None:
        raise ScheduleUnavailable(f"Сайт недоступен для {day.isoformat()}")

    try:
        return parse_schedule(html, day)
    except Exception:
        logger.exception("Ошибка парсинга HTML")
        raise ScheduleUnavailable("Ошибка парсинга расписания")


async def get_schedule_with_fallback(target: date):
    """
    1. target_date.
    2. Есть занятия -> вернуть их (fallback=False).
    3. Нет занятий -> взять сегодняшнее; если есть — вернуть его (fallback=True).
    4. И сегодня пусто -> вернуть пустое расписание.
    """
    schedule = await get_schedule(target)

    if schedule.lessons:
        return schedule

    today_schedule = await get_schedule(get_today())
    today_schedule.fallback = True

    if today_schedule.lessons:
        return today_schedule

    return today_schedule


# ============================================================
# ХЭШ РАСПИСАНИЯ
# ============================================================

def schedule_signature(schedule: Schedule) -> str:
    """
    Стабильная подпись, зависящая от: даты, пары, времени, предмета,
    преподавателя и аудитории.
    """
    parts = [schedule.date.isoformat(), schedule.group]

    for lesson in schedule.lessons:
        parts.extend(
            [lesson.pair, lesson.time,
             lesson.subject, lesson.teacher, lesson.room]
        )

    data = "\n".join(parts).encode("utf-8")

    return hashlib.sha256(data).hexdigest()


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_state (
                    date       TEXT PRIMARY KEY,
                    hash       TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        logger.info("База данных готова: %s", DB_PATH)
    except Exception:
        logger.exception("Ошибка инициализации SQLite")


def subscribe_user(user_id: int) -> bool:
    """Добавляет подписчика. True если добавлен, False если уже был."""
    created = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO subscribers (user_id, created_at)"
                " VALUES (?, ?)",
                (user_id, created),
            )
            return cur.rowcount > 0
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
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT date, hash FROM schedule_state"
            ).fetchall()
            return {row["date"]: row["hash"] for row in rows}
    except Exception:
        logger.exception("Ошибка SQLite (load state)")
        return {}


def save_state(state: dict) -> None:
    try:
        now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        with db_connect() as conn:
            for date_key, digest in state.items():
                conn.execute(
                    "INSERT OR REPLACE INTO schedule_state (date, hash, updated_at)"
                    " VALUES (?, ?, ?)",
                    (date_key, digest, now),
                )
    except Exception:
        logger.exception("Ошибка SQLite (save state)")


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


def render_schedule_image(schedule: Schedule) -> Path:
    """
    Современная минималистичная PNG-картинка. Высота зависит от числа занятий.
    """
    try:
        lessons = schedule.lessons

        # Шрифты
        font_label = get_font(24, bold=True)
        font_group = get_font(72, bold=True)
        font_date = get_font(30)
        font_count = get_font(24, bold=True)
        font_pair = get_font(28, bold=True)
        font_time = get_font(40, bold=True)
        font_subject = get_font(40, bold=True)
        font_info = get_font(28)
        font_empty_title = get_font(44, bold=True)
        font_empty_sub = get_font(30)
        font_footer = get_font(24)

        # Геометрия
        W = 1080
        MARGIN = 58
        HEADER_H = 250
        card_gap = 28
        x1 = MARGIN
        x2 = W - MARGIN
        inner = 48  # отступ внутри карточки

        # --- геометрия карточки (для высоты и для отрисовки) ---
        circle_s = 68
        top_pad = 26          # отступ сверху карточки (круг)
        subj_gap = 18         # зазор между кругом и предметом
        subj_top = top_pad + circle_s + subj_gap   # верх предмета (относительно карточки)
        line_h = int(font_subject.size * 1.35)     # межстрочный интервал предмета
        meta_gap = 14         # зазор между предметом и мета-строкой
        chip_h = 44           # высота бейджа аудитории
        pad_bottom = 22       # нижний отступ карточки

        def subject_lines_for(lesson):
            if lesson.subject:
                return _wrap_lines(
                    lesson.subject,
                    font_subject,
                    (x2 - x1) - 2 * inner,
                )
            return ["Предмет не указан"]

        def card_height_for(lesson) -> int:
            lines = subject_lines_for(lesson)
            subj_block_h = line_h * len(lines)
            meta_top = subj_top + subj_block_h + meta_gap
            return meta_top + chip_h + pad_bottom

        card_heights = [card_height_for(l) for l in lessons]

        total_cards_h = sum(card_heights) + max(0, len(lessons) - 1) * card_gap

        if not lessons:
            empty_h = 250
            total_cards_h = empty_h

        H = HEADER_H + 36 + total_cards_h + 80

        image = Image.new("RGB", (W, H), COL_BG)
        draw = ImageDraw.Draw(image)

        # ---------- шапка ----------
        draw.rectangle((0, 0, W, HEADER_H), fill=COL_WHITE)
        draw.rectangle((0, 0, 14, HEADER_H), fill=COL_ACCENT)

        # label
        draw.text((MARGIN + 20, 40), "РАСПИСАНИЕ",
                  font=font_label, fill=COL_ACCENT)
        # группа
        draw.text((MARGIN + 20, 84), GROUP_NAME,
                  font=font_group, fill=COL_INK)
        # дата
        draw.text((MARGIN + 22, 186),
                  format_date_header(schedule.date),
                  font=font_date, fill=COL_MUTED)

        # бейдж «N занятий»
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
            title = "Занятий нет"
            tw = draw.textlength(title, font=font_empty_title)
            draw.text(((W - tw) / 2, by + 62), title,
                      font=font_empty_title, fill=COL_INK)
            sub = "Расписание на этот день не опубликовано."
            sw = draw.textlength(sub, font=font_empty_sub)
            draw.text(((W - sw) / 2, by + 140), sub,
                      font=font_empty_sub, fill=COL_MUTED)

        # ---------- карточки занятий ----------
        else:
            y = HEADER_H + 36

            for lesson, ch in zip(lessons, card_heights):
                left = x1 + inner
                top = y

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
                    outline=COL_BORDER,
                    width=2,
                )

                # --- круг с номером пары ---
                cy_top = top + top_pad
                cx = left
                draw.ellipse(
                    (cx, cy_top, cx + circle_s, cy_top + circle_s),
                    fill=COL_ACCENT,
                )
                roman = lesson.pair
                rw = draw.textlength(roman, font=font_pair)
                rh = _text_h(font_pair)
                draw.text(
                    (cx + (circle_s - rw) / 2,
                     cy_top + (circle_s - rh) / 2),
                    roman,
                    font=font_pair,
                    fill=COL_WHITE,
                )

                # --- время рядом с кругом ---
                time_y = cy_top + (circle_s - _text_h(font_time)) / 2
                draw.text((left + circle_s + 34, time_y),
                          f"{lesson.start} — {lesson.end}",
                          font=font_time, fill=COL_ACCENT)

                # --- предмет (самый заметный текст карточки) ---
                subject_lines = subject_lines_for(lesson)
                sy = top + subj_top
                for i, line in enumerate(subject_lines):
                    draw.text((left, sy + i * line_h), line,
                              font=font_subject, fill=COL_INK)
                subj_block_h = line_h * len(subject_lines)

                # --- аудитория + преподаватель (вторичная информация) ---
                meta_top = top + subj_top + subj_block_h + meta_gap
                meta_h = _text_h(font_info)

                room_text = f"ауд. {lesson.room}" if lesson.room not in ("", "—") \
                    else "ауд. —"
                room_w = draw.textlength(room_text, font=font_info)
                room_chip_pad = 18
                room_chip_w = room_w + room_chip_pad * 2
                ry = meta_top
                draw.rounded_rectangle(
                    (left, ry, left + room_chip_w, ry + chip_h),
                    radius=chip_h / 2,
                    fill=COL_GREEN_LIGHT,
                )
                draw.text(
                    (left + room_chip_pad, ry + (chip_h - meta_h) / 2),
                    room_text,
                    font=font_info,
                    fill=COL_GREEN,
                )

                # преподаватель
                teacher = lesson.teacher if lesson.teacher not in ("", "—") \
                    else "Преподаватель не указан"
                teacher_x = left + room_chip_w + 24
                teacher_max_w = (x2 - inner) - teacher_x
                if draw.textlength(teacher, font=font_info) > teacher_max_w:
                    while (draw.textlength(teacher, font=font_info)
                           > teacher_max_w and len(teacher) > 1):
                        teacher = teacher[:-1]
                    teacher += "…"

                draw.text(
                    (teacher_x, ry + (chip_h - meta_h) / 2),
                    teacher,
                    font=font_info,
                    fill=COL_MUTED,
                )

                y += ch + card_gap

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
        filename = f"schedule_{schedule.date.isoformat()}_{len(lessons)}.png"
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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Сегодня", callback_data="today"),
                InlineKeyboardButton(text="➡️ Завтра", callback_data="tomorrow"),
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
    try:
        schedule = await get_schedule(get_today())
        heading = f"📅 <b>Расписание на сегодня</b>\n\n"
        if not schedule.lessons:
            await _send_text(
                destination,
                heading + f"Группа: <b>{GROUP_NAME}</b>\n"
                f"{format_date_full(get_today())}\n\n☕ Занятий нет.",
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
        logger.exception("Ошибка /schedule")


async def _handle_tomorrow(destination):
    try:
        target = get_tomorrow()
        schedule = await get_schedule_with_fallback(target)

        if schedule.fallback:
            await _send_text(
                destination,
                f"ℹ️ На завтра ({format_date_full(target)}) расписание "
                "не опубликовано — показываю расписание на сегодня.",
            )

        if not schedule.lessons:
            await _send_text(
                destination,
                f"➡️ <b>Расписание на завтра</b>\n\n"
                f"Группа: <b>{GROUP_NAME}</b>\n"
                f"{format_date_full(target)}\n\n☕ Занятий нет.",
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
        logger.exception("Ошибка /tomorrow")


async def _status_text(user_id: int) -> str:
    created = subscriber_info(user_id)
    total = len(load_subscribers())
    lines = [
        f"📚 <b>{GROUP_NAME}</b>",
        "—" * 18,
    ]
    if created:
        lines.append("🔔 Вы подписаны на уведомления.")
        lines.append(f"Подписка оформлена: <i>{created}</i>")
    else:
        lines.append("🔕 Вы не подписаны на уведомления.")
    lines.append(f"Всего подписчиков: <b>{total}</b>")
    lines.append(f"Проверка изменений каждые {CHECK_INTERVAL // 60} мин.")
    return "\n".join(lines)


# ============================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================

dp = Dispatcher()


def _is_subscribed(user_id: int) -> bool:
    return subscriber_info(user_id) is not None


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    text = (
        f"👋 Привет!\n\n"
        f"Я бот расписания группы <b>{GROUP_NAME}</b>.\n\n"
        f"Доступные действия:\n"
        f"📅 <b>Сегодня</b> — /schedule\n"
        f"➡️ <b>Завтра</b> — /tomorrow\n"
        f"🔔 <b>Уведомления</b> — /subscribe\n"
        f"🔕 <b>Отключить уведомления</b> — /unsubscribe\n"
        f"ℹ️ <b>Статус</b> — /status\n"
        f"🆘 <b>Помощь</b> — /help\n\n"
        f"🔔 Подписчики автоматически получают обновлённое "
        f"расписание при его изменении."
    )
    await message.answer(text, reply_markup=main_keyboard(_is_subscribed(user_id)))


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):
    await _handle_today(message)


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message):
    await _handle_tomorrow(message)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    user_id = message.from_user.id
    if subscribe_user(user_id):
        await message.answer(
            "🔔 <b>Уведомления включены.</b>\n\n"
            "Я буду автоматически проверять изменения расписания "
            f"и присылать новые картинки.\n\n"
            f"Отключить: /unsubscribe"
        )
    else:
        await message.answer("🔔 Вы уже подписаны на уведомления.")


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    user_id = message.from_user.id
    if unsubscribe_user(user_id):
        await message.answer("🔕 <b>Уведомления отключены.</b>")
    else:
        await message.answer("Вы не были подписаны на уведомления.")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(await _status_text(message.from_user.id))


# ============================================================
# INLINE-КНОПКИ
# ============================================================

@dp.callback_query(F.data == "today")
async def cb_today(callback: CallbackQuery):
    await callback.answer()
    await _handle_today(callback)


@dp.callback_query(F.data == "tomorrow")
async def cb_tomorrow(callback: CallbackQuery):
    await callback.answer()
    await _handle_tomorrow(callback)


@dp.callback_query(F.data == "subscribe")
async def cb_subscribe(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    if subscribe_user(user_id):
        await callback.message.answer(
            "🔔 <b>Уведомления включены.</b>"
        )
    else:
        await callback.message.answer("🔔 Вы уже подписаны.")


@dp.callback_query(F.data == "unsubscribe")
async def cb_unsubscribe(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    if unsubscribe_user(user_id):
        await callback.message.answer("🔕 <b>Уведомления отключены.</b>")
    else:
        await callback.message.answer("Вы не были подписаны.")


@dp.callback_query(F.data == "status")
async def cb_status(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(await _status_text(callback.from_user.id))


@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f"🆘 <b>Помощь</b>\n\n"
        f"Я показываю расписание группы <b>{GROUP_NAME}</b> "
        f"на сегодня и на завтра.\n\n"
        f"• /schedule — сегодня\n"
        f"• /tomorrow — завтра (если пусто — сегодня)\n"
        f"• /subscribe — уведомления об изменениях\n"
        f"• /unsubscribe — отключить уведомления\n"
        f"• /status — ваш статус"
    )


# ============================================================
# МОНИТОРИНГ ИЗМЕНЕНИЙ
# ============================================================

async def _notify_changed(bot: Bot, schedule: Schedule, day: date) -> None:
    """Отправляет обновлённое расписание всем подписчикам."""
    subscribers = load_subscribers()
    if not subscribers:
        return

    image_path = render_schedule_image(schedule)
    caption = (
        "🔔 <b>Расписание изменилось</b>\n\n"
        f"Группа: <b>{GROUP_NAME}</b>\n"
        f"Дата: {format_date_full(day)}"
    )

    try:
        for user_id in subscribers:
            try:
                await bot.send_photo(
                    user_id,
                    photo=FSInputFile(image_path),
                    caption=caption,
                )
                await asyncio.sleep(0.08)
            except Exception as error:
                logger.warning(
                    "Не удалось уведомить %s: %s", user_id, error
                )
    finally:
        try:
            image_path.unlink(missing_ok=True)
        except Exception:
            pass


async def _check_date(bot: Bot, day: date) -> None:
    date_key = day.isoformat()

    try:
        schedule = await get_schedule(day)
    except ScheduleUnavailable:
        # Отсутствие расписания / сеть — не ошибка для мониторинга.
        logger.warning("Проверка %s: сайт недоступен, пропускаем.", date_key)
        return

    signature = schedule_signature(schedule)
    logger.info("Hash расписания: %s", signature)

    state = load_state()
    old = state.get(date_key)

    # Первый запуск: фиксируем baseline и НЕ отправляем уведомление.
    if old is None:
        state[date_key] = signature
        save_state(state)
        logger.info(
            "Первичная фиксация расписания %s (занятий: %s)",
            date_key,
            len(schedule.lessons),
        )
        return

    if old == signature:
        logger.info("Изменений нет: %s", date_key)
        return

    # Изменение -> обновляем baseline и уведомляем подписчиков.
    logger.info("РАСПИСАНИЕ ИЗМЕНИЛОСЬ: %s", date_key)
    state[date_key] = signature
    save_state(state)

    await _notify_changed(bot, schedule, day)


async def schedule_monitor(bot: Bot) -> None:
    """Фоновая задача. Не блокирует polling."""
    logger.info(
        "Мониторинг запущен. Интервал: %s сек (%s мин).",
        CHECK_INTERVAL,
        CHECK_INTERVAL // 60,
    )

    while True:
        try:
            # Проверяем сегодня
            await _check_date(bot, get_today())
            # Проверяем завтра (появление нового расписания = изменение)
            await _check_date(bot, get_tomorrow())
        except Exception:
            logger.exception("Ошибка в цикле мониторинга")

        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================
# MAIN
# ============================================================

async def main() -> None:
    logger.info("=" * 60)
    logger.info("Бот расписания группы %s", GROUP_NAME)
    logger.info("Group ID: %s", GROUP_ID)
    logger.info("URL: %s", BASE_URL)
    logger.info("Check interval: %s сек", CHECK_INTERVAL)
    logger.info("Timezone: %s", TIMEZONE)

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

    monitor_task = asyncio.create_task(schedule_monitor(bot))

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
