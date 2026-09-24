# -*- coding: utf-8 -*-
"""Проверки логики бота.

Запуск:  python -m unittest discover -s tests -v
(из корня репозитория, с установленными зависимостями)
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from PIL import Image

# Изолируем БД до импорта модуля
_TEST_DATA = Path(tempfile.mkdtemp(prefix="sched_bot_test_"))
os.environ["DATA_DIR"] = str(_TEST_DATA)
os.environ["CHECK_INTERVAL"] = "300"

import bot  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_schedule(day: date, subject: str) -> bot.Schedule:
    return bot.Schedule(
        date=day,
        group=bot.GROUP_NAME,
        lessons=[
            bot.Lesson(
                pair="I",
                time="08:30 - 09:50",
                subject=subject,
                teacher="Иванов И.И.",
                room="УК107",
                start="08:30",
                end="09:50",
            )
        ],
    )


def empty_schedule(day: date) -> bot.Schedule:
    return bot.Schedule(date=day, group=bot.GROUP_NAME, lessons=[])


# --- Хелперы проверок рендера (дизайн «один день = одна картинка») ---

def hex_rgb(value: str) -> tuple:
    text = str(value).lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def plan_pad(plan) -> float:
    """Внутренний горизонтальный отступ карточки из метрик плана."""
    return float(plan["metrics"]["pad_x"])


def rendered_text() -> str:
    """Весь текст, реально нарисованный на последней картинке."""
    plan = bot._LAST_RENDER["plan"]
    return "\n".join(
        " ".join(op["lines"]) for op in plan["ops"] if op["op"] == "text"
    )


def ops_with(role: str) -> list:
    """Отрисованные блоки с указанной ролью (room — аудитория)."""
    plan = bot._LAST_RENDER["plan"]
    return [op for op in plan["ops"] if op.get("role") == role]


def text_sizes(role: str) -> list:
    return [op["size"] for op in ops_with(role)]


def close(color, target, tol=8) -> bool:
    return all(abs(color[i] - target[i]) <= tol for i in range(3))


def op_box(op: dict) -> tuple:
    """Габарит блока: ink-бокс у текста, прямоугольник у shapes/chip."""
    return op.get("bbox") or op["box"]


def plan_small_ops():
    """Второстепенные строки карточки: сноски и статусные метки."""
    plan = bot._LAST_RENDER["plan"]
    return [op for op in plan["ops"]
            if op["op"] == "text" and op.get("role") in ("small", "tag")]


def outside_safe_area(img, margin=None) -> list:
    """Пиксели контента (белый/насыщенный), прижатые ближе margin к краю.

    Тени — оттенки фона — специально не считаются: они декоративные.
    """
    margin = margin or bot.SAFE_AREA
    px = img.convert("RGB").load()
    w, h = img.size
    bad = []
    for y in range(h):
        for x in range(w):
            edge = min(x, y, w - 1 - x, h - 1 - y)
            if edge >= margin:
                continue
            r, g, b = px[x, y]
            # тёмный текст, белая карточка или цветной бейдж у края
            if (r < 120 and g < 120 and b < 120) or (r > 250 and g > 250 and b > 250):
                bad.append((x, y))
            elif abs(r - 27) < 40 and abs(g - 156) < 40 and abs(b - 98) < 40:
                bad.append((x, y))
    return bad


class FakeBot:
    """Минимальный Bot для тестов: записывает отправки."""

    def __init__(self, fail_users=()):
        self.sent = []
        self.fail_users = set(fail_users)

    async def send_photo(self, user_id, photo, caption):
        if user_id in self.fail_users:
            raise RuntimeError("boom")
        self.sent.append(("photo", user_id, caption))

    async def send_message(self, user_id, text):
        if user_id in self.fail_users:
            raise RuntimeError("boom")
        self.sent.append(("text", user_id, text))


class FakeMessage:
    """Минимальное Message для текстового обработчика."""

    def __init__(self, text):
        self.text = text
        self.answers = []

    async def answer(self, text, *args, **kwargs):
        self.answers.append(text)


class DBTestCase(unittest.TestCase):
    def setUp(self):
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM schedule_state")
            conn.execute("DELETE FROM schedule_notifications")
            conn.execute("DELETE FROM subscribers")

    def insert_user(self, user_id, created_at):
        with bot.db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO subscribers"
                " (user_id, created_at, chat_type, title)"
                " VALUES (?, ?, 'private', '')",
                (user_id, created_at),
            )


class TestTimezone(DBTestCase):
    def test_fixed_timezone(self):
        self.assertEqual(str(bot.TZ), "Asia/Yekaterinburg")
        self.assertEqual(bot.TIMEZONE, "Asia/Yekaterinburg")

    def test_today_uses_timezone(self):
        self.assertEqual(bot.get_today(), bot.now_local().date())
        self.assertEqual(bot.get_today(), bot.get_today())

    def test_tomorrow_is_strict_calendar_day(self):
        # Пятница -> суббота
        with mock.patch.object(
            bot, "now_local", return_value=datetime(2026, 9, 4, 12, 0, 0)
        ):
            self.assertEqual(bot.get_today(), date(2026, 9, 4))
            self.assertEqual(bot.get_tomorrow(), date(2026, 9, 5))
        # Суббота -> воскресенье (НЕ понедельник!)
        with mock.patch.object(
            bot, "now_local", return_value=datetime(2026, 9, 5, 12, 0, 0)
        ):
            self.assertEqual(bot.get_today(), date(2026, 9, 5))
            self.assertEqual(bot.get_tomorrow(), date(2026, 9, 6))
        # Воскресенье -> понедельник
        with mock.patch.object(
            bot, "now_local", return_value=datetime(2026, 9, 6, 12, 0, 0)
        ):
            self.assertEqual(bot.get_today(), date(2026, 9, 6))
            self.assertEqual(bot.get_tomorrow(), date(2026, 9, 7))

    def test_midnight_recompute(self):
        # До полуночи
        with mock.patch.object(
            bot, "now_local", return_value=datetime(2026, 9, 6, 23, 59, 59)
        ):
            today_a, tomorrow_a = bot.get_today(), bot.get_tomorrow()
        # После полуночи
        with mock.patch.object(
            bot, "now_local", return_value=datetime(2026, 9, 7, 0, 0, 1)
        ):
            today_b, tomorrow_b = bot.get_today(), bot.get_tomorrow()
        self.assertEqual((today_a, tomorrow_a), (date(2026, 9, 6), date(2026, 9, 7)))
        self.assertEqual((today_b, tomorrow_b), (date(2026, 9, 7), date(2026, 9, 8)))

    def test_day_label(self):
        today = bot.get_today()
        self.assertEqual(bot.day_label_for(today), "Сегодня")
        self.assertEqual(
            bot.day_label_for(today.replace(day=today.day + 1) if today.day < 31 else bot.get_tomorrow()),
            "Завтра",
        )




class TestScheduleTextParsing(unittest.TestCase):
    """Парсер текстовой команды «расписание…» (пункты 24–31, 34)."""

    def setUp(self):
        # Фиксируем «сейчас»: 2026-09-06 (воскресенье, Yekaterinburg).
        self.today = date(2026, 9, 6)
        patcher = mock.patch.object(
            bot, "now_local",
            return_value=datetime(2026, 9, 6, 14, 0, 0),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def parse(self, text):
        return bot.parse_schedule_text(text)

    def test_plain_schedule_means_tomorrow(self):
        for text in ("расписание", "Расписание", "РАСПИСАНИЕ",
                     "  расписание  "):
            request = self.parse(text)
            self.assertTrue(request.matched)
            self.assertFalse(request.error)
            self.assertIsNone(request.date)

    def test_relative_dates(self):
        self.assertEqual(
            self.parse("расписание на сегодня").date, self.today
        )
        self.assertEqual(
            self.parse("расписание на завтра").date,
            self.today + timedelta(days=1),
        )
        self.assertEqual(
            self.parse("расписание на послезавтра").date,
            self.today + timedelta(days=2),
        )

    def test_ru_month_forms(self):
        self.assertEqual(
            self.parse("расписание на 4 сентября").date,
            date(2026, 9, 4),
        )
        # Формы без окончания тоже понимаются
        self.assertEqual(
            self.parse("расписание на 4 сентябрь").date,
            date(2026, 9, 4),
        )
        self.assertEqual(
            self.parse("расписание на 1 января").date,
            date(2026, 1, 1),
        )
        self.assertEqual(
            self.parse("расписание на 31 декабрь").date,
            date(2026, 12, 31),
        )

    def test_month_always_current_year(self):
        # В сентябре 2026 «4 января» -> 2026 (текущий год)
        self.assertEqual(
            self.parse("расписание на 4 января").date,
            date(2026, 1, 4),
        )

    def test_numeric_formats(self):
        self.assertEqual(
            self.parse("расписание на 04.09.2026").date,
            date(2026, 9, 4),
        )
        self.assertEqual(
            self.parse("расписание на 4.9.2026").date,
            date(2026, 9, 4),
        )
        self.assertEqual(
            self.parse("расписание на 04/09/2026").date,
            date(2026, 9, 4),
        )

    def test_two_digit_year_rule(self):
        # 26 -> 2026 (однозначно, без угадывания века)
        self.assertEqual(
            self.parse("расписание на 04.09.26").date,
            date(2026, 9, 4),
        )
        # «4 сентября 26» тоже
        self.assertEqual(
            self.parse("расписание на 4 сентября 26").date,
            date(2026, 9, 4),
        )

    def test_full_year_and_goda_variants(self):
        self.assertEqual(
            self.parse("расписание на 4 сентября 2026").date,
            date(2026, 9, 4),
        )
        self.assertEqual(
            self.parse("расписание на 4 сентября 2026 года").date,
            date(2026, 9, 4),
        )
        self.assertEqual(
            self.parse("расписание на 4 сентября 2026г").date,
            date(2026, 9, 4),
        )

    def test_spaces_and_case(self):
        self.assertEqual(
            self.parse("РАСПИСАНИЕ   НА   ЗАВТРА").date,
            self.today + timedelta(days=1),
        )
        self.assertEqual(
            self.parse("  Расписание  на  4  сентября  ").date,
            date(2026, 9, 4),
        )

    def test_invalid_date_error(self):
        for text in (
            "расписание на 35 сентября",
            "расписание на 99.99.2026",
            "расписание на абвг",
            "расписание на 2026",
            "расписание на",
        ):
            request = self.parse(text)
            self.assertTrue(request.matched, text)
            self.assertTrue(request.error, text)

    def test_not_a_schedule_message(self):
        for text in ("привет", "расписаниеx", "распи", "на завтра"):
            request = self.parse(text)
            self.assertFalse(request.matched)

    def test_new_year_wrap(self):
        # 31.12.2026 23:59 -> послезавтра 2 января 2027
        with mock.patch.object(
            bot, "now_local",
            return_value=datetime(2026, 12, 31, 23, 59, 0),
        ):
            parser_date = bot.parse_schedule_text("расписание на послезавтра").date
            self.assertEqual(parser_date, date(2027, 1, 2))


class TestScheduleTextHandler(DBTestCase):
    """Полный путь: текст -> parse_schedule_text -> _handle_date."""

    def setUp(self):
        super().setUp()
        self.requests = []
        self.photos = []
        self.texts = []

        async def fake_get_schedule(day):
            self.requests.append(day)
            return make_schedule(day, "Предмет")

        async def fake_send_photo(dest, schedule):
            self.photos.append(schedule)
            return True

        async def fake_send_text(dest, text):
            self.texts.append(text)

        patcher = mock.patch.object(
            bot, "now_local",
            return_value=datetime(2026, 9, 7, 14, 0, 0),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for patch in (
            mock.patch.object(bot, "get_schedule", fake_get_schedule),
            mock.patch.object(bot, "_send_photo", fake_send_photo),
            mock.patch.object(bot, "_send_text", fake_send_text),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_plain_text_uses_tomorrow(self):
        message = FakeMessage("расписание")
        run(bot.cmd_text_schedule(message))
        self.assertEqual(self.requests, [date(2026, 9, 8)])
        self.assertEqual([s.date for s in self.photos], [date(2026, 9, 8)])

    def test_text_for_relative_date(self):
        message = FakeMessage("расписание на сегодня")
        run(bot.cmd_text_schedule(message))
        self.assertEqual(self.requests, [date(2026, 9, 7)])

    def test_text_for_date_uses_exact_date(self):
        target = date(2026, 9, 12)
        message = FakeMessage("расписание на 12 сентября 2026")
        run(bot.cmd_text_schedule(message))
        self.assertEqual(self.requests, [target])
        self.assertEqual([s.date for s in self.photos], [target])

    def test_invalid_date_shows_hint_no_request(self):
        message = FakeMessage("расписание на абвг")
        run(bot.cmd_text_schedule(message))
        self.assertEqual(self.requests, [])
        self.assertEqual(len(message.answers), 1)
        self.assertIn("Не удалось определить дату", message.answers[0])
        self.assertIn("расписание на завтра", message.answers[0])

    def test_not_schedule_text_ignored(self):
        message = FakeMessage("привет")
        run(bot.cmd_text_schedule(message))
        self.assertEqual(self.requests, [])
        self.assertEqual(message.answers, [])

    def test_text_no_fallback_on_missing(self):
        """Если на запрошенной дате расписания нет — не показываем другое."""
        target = date(2026, 9, 10)
        requested = []

        async def fake_get_schedule(day):
            requested.append(day)
            if day == target:
                return empty_schedule(day)
            return make_schedule(day, "Другая дата")

        message = FakeMessage("расписание на 10 сентября 2026")
        with mock.patch.object(bot, "get_schedule", fake_get_schedule):
            run(bot.cmd_text_schedule(message))
        self.assertEqual(requested, [target])
        self.assertTrue(self.texts)
        self.assertIn(bot.format_date_header(target), self.texts[0])
        for text in self.texts:
            self.assertNotIn(bot.format_date_header(date(2026, 9, 6)), text)
            self.assertNotIn(bot.format_date_header(date(2026, 9, 7)), text)


class TestDateParsingEdge(unittest.TestCase):
    """Пункт 28: parse_user_date + отдельный парсер, TZ Yekaterinburg."""

    def test_parse_user_date_two_digit(self):
        self.assertEqual(
            bot.parse_user_date("04.09.26"), date(2026, 9, 4)
        )
        self.assertEqual(
            bot.parse_user_date("4.9.26"), date(2026, 9, 4)
        )

    def test_parse_user_date_month_now(self):
        with mock.patch.object(
            bot, "now_local",
            return_value=datetime(2026, 9, 6, 14, 0, 0),
        ):
            self.assertEqual(
                bot.parse_user_date("4 сентября"), date(2026, 9, 4)
            )
            self.assertEqual(
                bot.parse_user_date("4 сентября 2026 года"),
                date(2026, 9, 4),
            )

    def test_parse_user_date_rejects_invalid(self):
        for raw in ("35 сентября", "99.99.2026", "абвг", "2026"):
            self.assertIsNone(bot.parse_user_date(raw))


class TestParseAndGetSchedule(unittest.TestCase):
    SAMPLE_HTML = """
    <html><body>
      <div class="card myCard">
        <div class="card-header">
          <span class="h3">I</span>
          <span class="h4">08<sup>30</sup> — 09<sup>50</sup></span>
        </div>
        <div class="d-md-none text-center text-truncate">Математика</div>
        <div class="d-none d-md-block"><b>Доп. лекция</b></div>
        <span class="Staff">Иванов И.И.</span>
        <span>ауд. <span class="h5">УК107</span></span>
      </div>
      <div class="card myCard">
        <div class="card-header">
          <span class="h3">II</span>
          <span class="h4">10<sup>00</sup> — 11<sup>20</sup></span>
        </div>
        <div class="d-md-none text-center text-truncate">Физика</div>
        <span class="Staff">Петров П.П.</span>
        <span>ауд. <span class="h5">УК208</span></span>
      </div>
    </body></html>
    """

    def test_parser(self):
        day = date(2026, 9, 7)
        schedule = bot.parse_schedule(self.SAMPLE_HTML, day)
        self.assertEqual(schedule.date, day)
        self.assertEqual(len(schedule.lessons), 2)
        self.assertEqual(schedule.lessons[0].pair, "I")
        self.assertEqual(schedule.lessons[0].time, "08:30 - 09:50")
        self.assertEqual(schedule.lessons[0].subject, "Математика")
        self.assertEqual(schedule.lessons[0].room, "УК107")
        self.assertEqual(schedule.lessons[1].subject, "Физика")

    def test_signature_stable(self):
        day = date(2026, 9, 7)
        a = make_schedule(day, "Математика")
        b = make_schedule(day, "Математика")
        c = make_schedule(day, "Физика")
        self.assertEqual(bot.schedule_signature(a), bot.schedule_signature(b))
        self.assertNotEqual(bot.schedule_signature(a), bot.schedule_signature(c))

    def test_get_schedule_no_fallback_on_empty_or_error(self):
        day = date(2026, 9, 10)
        # Пустая страница -> пустое расписание, но НЕ чужая дата
        async def fake_fetch(d):
            return "<html><body></body></html>"

        async def scenario():
            with mock.patch.object(bot, "fetch_html", fake_fetch):
                schedule = await bot.get_schedule(day)
            self.assertEqual(schedule.date, day)
            self.assertEqual(schedule.lessons, [])
            self.assertFalse(hasattr(bot, "get_schedule_with_fallback"))

        run(scenario())

    def test_schedule_unavailable_propagates(self):
        async def fake_fetch(d):
            return None

        async def scenario():
            with mock.patch.object(bot, "fetch_html", fake_fetch):
                with self.assertRaises(bot.ScheduleUnavailable):
                    await bot.get_schedule(date(2026, 9, 10))

        run(scenario())


class TestTodayAndScheduleHandlers(DBTestCase):
    def test_today_empty_no_fallback_to_tomorrow(self):
        today = bot.get_today()
        tomorrow = bot.get_tomorrow()
        calls = []
        sent_texts = []
        sent_photos = []

        async def fake_get_schedule(day):
            calls.append(day)
            # Сегодня пусто, а вот завтра было бы ПОЛНОЕ расписание —
            # если бы был fallback, тест бы это поймал.
            if day == today:
                return empty_schedule(today)
            return make_schedule(day, "Опасный завтрашний предмет")

        async def fake_send_photo(dest, schedule):
            sent_photos.append(schedule)
            return True

        async def fake_send_text(dest, text):
            sent_texts.append(text)

        async def scenario():
            with mock.patch.object(bot, "get_schedule", fake_get_schedule), \
                 mock.patch.object(bot, "_send_photo", fake_send_photo), \
                 mock.patch.object(bot, "_send_text", fake_send_text):
                await bot._handle_today(object())

        run(scenario())

        self.assertEqual(calls, [today])
        self.assertEqual(sent_photos, [])
        text = "\n".join(sent_texts)
        self.assertIn("на сегодня", text)
        self.assertIn(bot.format_date_header(today), text)
        self.assertNotIn(bot.format_date_header(bot.get_tomorrow()), text)
        self.assertEqual(len(sent_texts), 1)

    def test_schedule_empty_no_fallback_to_today(self):
        today = bot.get_today()
        tomorrow = bot.get_tomorrow()
        calls = []
        sent_texts = []
        sent_photos = []

        async def fake_get_schedule(day):
            calls.append(day)
            if day == tomorrow:
                return empty_schedule(tomorrow)
            return make_schedule(day, "Сегодняшний предмет")

        async def fake_send_photo(dest, schedule):
            sent_photos.append(schedule)
            return True

        async def fake_send_text(dest, text):
            sent_texts.append(text)

        async def scenario():
            with mock.patch.object(bot, "get_schedule", fake_get_schedule), \
                 mock.patch.object(bot, "_send_photo", fake_send_photo), \
                 mock.patch.object(bot, "_send_text", fake_send_text):
                await bot._handle_schedule(object())

        run(scenario())

        # Ровно один запрос — на завтра
        self.assertEqual(calls, [tomorrow])
        self.assertEqual(sent_photos, [])
        text = "\n".join(sent_texts)
        self.assertIn("на завтра", text)
        self.assertIn(bot.format_date_header(tomorrow), text)
        self.assertNotIn(bot.format_date_header(today), text)
        self.assertEqual(len(sent_texts), 1)

    def test_today_rich_sends_photo_with_today(self):
        today = bot.get_today()
        sent = []

        async def fake_get_schedule(day):
            return make_schedule(day, "Предмет")

        async def fake_send_photo(dest, schedule):
            sent.append(schedule.date)
            return True

        async def fake_send_text(dest, text):
            self.fail("text send не должен вызываться")

        async def scenario():
            with mock.patch.object(bot, "get_schedule", fake_get_schedule), \
                 mock.patch.object(bot, "_send_photo", fake_send_photo), \
                 mock.patch.object(bot, "_send_text", fake_send_text):
                await bot._handle_today(object())

        run(scenario())
        self.assertEqual(sent, [today])

    def test_schedule_rich_sends_photo_with_tomorrow(self):
        tomorrow = bot.get_tomorrow()
        sent = []

        async def fake_get_schedule(day):
            return make_schedule(day, "Предмет")

        async def fake_send_photo(dest, schedule):
            sent.append(schedule.date)
            return True

        async def fake_send_text(dest, text):
            self.fail("text send не должен вызываться")

        async def scenario():
            with mock.patch.object(bot, "get_schedule", fake_get_schedule), \
                 mock.patch.object(bot, "_send_photo", fake_send_photo), \
                 mock.patch.object(bot, "_send_text", fake_send_text):
                await bot._handle_schedule(object())

        run(scenario())
        self.assertEqual(sent, [tomorrow])


class TestMonitoring(DBTestCase):
    def setUp(self):
        super().setUp()
        self.bot = FakeBot()
        self.day = date(2026, 9, 10)
        self.current_schedule = [make_schedule(self.day, "Математика")]

        async def fake_get_schedule(day):
            if day != self.day:
                return empty_schedule(day)
            return self.current_schedule[0]

        self.get_schedule_patch = mock.patch.object(
            bot, "get_schedule", fake_get_schedule
        )
        self.render_patch = mock.patch.object(
            bot, "render_schedule_image",
            return_value=Path("/tmp/nonexistent_test.png"),
        )
        self.get_schedule_patch.start()
        self.render_patch.start()
        self.addCleanup(self.get_schedule_patch.stop)
        self.addCleanup(self.render_patch.stop)

    def test_first_appearance_sends_and_baseline_saved(self):
        bot.subscribe_user(1001)
        run(bot._check_date(self.bot, self.day))
        self.assertEqual(len(self.bot.sent), 1)
        kind, user_id, caption = self.bot.sent[0]
        self.assertEqual(kind, "photo")
        self.assertEqual(user_id, 1001)
        self.assertIn("Расписание опубликовано", caption)
        # Дата в подписи однозначно указана
        self.assertIn(bot.format_date_header(self.day), caption)
        self.assertNotIn("Сегодня,", caption)  # дата не сегодня/завтра
        self.assertIn(self.day.isoformat(), bot.load_state())

    def test_no_repeat_when_unchanged(self):
        bot.subscribe_user(1001)
        run(bot._check_date(self.bot, self.day))
        run(bot._check_date(self.bot, self.day))
        run(bot._check_date(self.bot, self.day))
        self.assertEqual(len(self.bot.sent), 1)

    def test_change_notifies_today_only_label(self):
        bot.subscribe_user(1001)
        run(bot._check_date(self.bot, self.day))
        self.current_schedule[0] = make_schedule(self.day, "Физика")
        run(bot._check_date(self.bot, self.day))
        self.assertEqual(len(self.bot.sent), 2)
        self.assertIn("Расписание изменилось", self.bot.sent[1][2])
        # Не изменилось -> снова ничего
        run(bot._check_date(self.bot, self.day))
        self.assertEqual(len(self.bot.sent), 2)

    def test_empty_and_error_do_not_change_state(self):
        bot.subscribe_user(1001)
        run(bot._check_date(self.bot, self.day))
        saved = bot.load_state()[self.day.isoformat()]
        # Пустое расписание
        self.current_schedule[0] = empty_schedule(self.day)
        run(bot._check_date(self.bot, self.day))
        self.assertEqual(bot.load_state()[self.day.isoformat()], saved)
        self.assertEqual(len(self.bot.sent), 1)
        # Ошибка источника
        async def fake_get_schedule(day):
            raise bot.ScheduleUnavailable("нет сети")

        with mock.patch.object(bot, "get_schedule", fake_get_schedule):
            run(bot._check_date(self.bot, self.day))
        self.assertEqual(bot.load_state()[self.day.isoformat()], saved)
        self.assertEqual(len(self.bot.sent), 1)

    def test_tomorrow_checked_independently(self):
        bot.subscribe_user(1001)
        # Фиксируем «сейчас»: тест не должен зависеть от перехода через
        # полночь между получением tomorrow и проверкой даты монитором.
        with mock.patch.object(
            bot, "now_local",
            return_value=datetime(2026, 9, 11, 15, 0, 0),
        ):
            tomorrow = bot.get_tomorrow()

            async def fake_get_schedule(day):
                return make_schedule(day, "Завтрашний предмет")

            with mock.patch.object(bot, "get_schedule", fake_get_schedule):
                run(bot._check_date(self.bot, tomorrow))
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("Завтра", self.bot.sent[0][2])
        self.assertIn(bot.format_date_full(tomorrow), self.bot.sent[0][2])

    def test_partial_delivery_retries_only_pending(self):
        bot.subscribe_user(1001)
        bot.subscribe_user(1002)
        fail_bot = FakeBot(fail_users={1002})
        run(bot._check_date(fail_bot, self.day))
        # Доставлено только 1001, состояние НЕ обновлено
        self.assertEqual(len(fail_bot.sent), 1)
        saved = bot.load_state()
        self.assertNotIn(self.day.isoformat(), saved)
        # Второй цикл: тот же контент — досылаем только 1002
        ok_bot = FakeBot()
        run(bot._check_date(ok_bot, self.day))
        self.assertEqual(len(ok_bot.sent), 1)
        self.assertEqual(ok_bot.sent[0][1], 1002)
        self.assertEqual(len(bot.load_schedule_notifications()[self.day.isoformat()]), 2)
        self.assertIn(self.day.isoformat(), bot.load_state())

    def test_no_subscribers_no_crash(self):
        run(bot._check_date(self.bot, self.day))
        self.assertEqual(self.bot.sent, [])
        self.assertNotIn(self.day.isoformat(), bot.load_state())

    def test_recovery_when_state_missing_but_delivered(self):
        # Эмуляция сбоя: записи о доставке есть, а baseline потерян
        # (например, процесс упал между записью и сохранением состояния).
        bot.subscribe_user(1001)
        run(bot._check_date(self.bot, self.day))
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM schedule_state")
        run(bot._check_date(self.bot, self.day))
        # Повторно НЕ отправляем, состояние восстанавливаем
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn(self.day.isoformat(), bot.load_state())

    def test_interval_is_5_minutes(self):
        self.assertEqual(bot.CHECK_INTERVAL, 300)

    def test_monitor_full_cycle_both_dates(self):
        """Один цикл монитора: сегодня и завтра без дублей."""
        bot.subscribe_user(1001)
        fake_bot = FakeBot()

        class StopLoop(Exception):
            pass

        sleep_calls = {"n": 0}

        async def fake_sleep(sec):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise StopLoop()

        async def fake_get_schedule(day):
            return make_schedule(day, "Предмет")

        # Фиксируем «сейчас»: даты внутри монитора не должны отличаться
        # от дат в проверках, даже если тест запущен у полуночи.
        with mock.patch.object(
            bot, "now_local", return_value=datetime(2026, 9, 11, 15, 0, 0)
        ), mock.patch.object(bot, "get_schedule", fake_get_schedule), \
             mock.patch.object(bot, "render_schedule_image",
                               return_value=Path("/tmp/x.png")), \
             mock.patch("asyncio.sleep", fake_sleep):
            today = bot.get_today()
            tomorrow = bot.get_tomorrow()
            with self.assertRaises(StopLoop):
                run(bot.schedule_monitor(fake_bot))

            # Сегодня (2026-09-11, пятница) и завтра -> оба уведомления.
            self.assertFalse(bot.is_day_off(today))
            self.assertGreaterEqual(len(fake_bot.sent), 1)
            self.assertIn("Завтра", fake_bot.sent[-1][2])
            self.assertIn(tomorrow.isoformat(), bot.load_state())

    def test_sunday_skipped(self):
        # Воскресенье 2026-09-06
        sunday = date(2026, 9, 6)
        run(bot._check_date(self.bot, sunday))
        self.assertEqual(self.bot.sent, [])
        self.assertNotIn(sunday.isoformat(), bot.load_state())

    def test_state_saves_normalized_data_json(self):
        day = date(2026, 9, 7)
        schedule = bot.Schedule(
            date=day,
            group=bot.GROUP_NAME,
            lessons=[
                lesson("II", "1"),
                lesson("II", "2", room="ПК303"),
            ],
        )
        state = {
            day.isoformat(): {
                "hash": bot.schedule_signature(schedule),
                "data": bot.normalize_schedule(schedule),
            }
        }
        bot.save_state(state)
        loaded = bot.load_state()[day.isoformat()]
        self.assertEqual(loaded["hash"], state[day.isoformat()]["hash"])
        self.assertEqual(len(loaded["data"]), 2)
        restored = bot.schedule_from_storage(loaded["data"], day)
        self.assertEqual(
            len(bot.compare_schedules(restored, schedule)), 0
        )


PROVIDED_HTML = """
<html><body>
  <div class="card myCard">
    <div class="card-header">
      <span class="h3">I</span> пара
      <span class="pl-2 h4">08<sup>30</sup> - 09<sup>50</sup></span>
      <span class="pl-1">перемена 15 мин</span>
    </div>
    <div class="card-body p-0">
      <div class="d-md-none text-center text-truncate">Химия Н и Г</div>
      <span>ауд.<span class="h5">УК307</span></span>
      <span class="Staff">Арнаутова А.В.</span>
    </div>
  </div>

  <div class="card myCard">
    <div class="card-header">
      <span class="h3">II</span> пара
      <span class="pl-2 h4">10<sup>00</sup> - 11<sup>20</sup></span>
      <span class="pl-1">перемена 15 мин</span>
    </div>
    <div class="card-body p-0">
      <div class="d-flex flex-column subGroup1">
        <span>1</span> п/гр.
        <span>ауд.<span class="h5">ПК103</span></span>
        <span class="Staff">Мурзабулатова Ф.Ф.</span>
        <span class="d-md-none text-center text-truncate">Ин.яз.</span>
      </div>
      <div class="d-flex flex-column subGroup2">
        <span>2</span> п/гр.
        <span>ауд.<span class="h5 font-weight-bold">ПК303</span></span>
        <span class="Staff">Амирханова Г.А.</span>
        <span class="d-md-none text-center text-truncate">Ин.яз.</span>
      </div>
    </div>
  </div>

  <div class="card myCard">
    <div class="card-header">
      <span class="h3">III</span> пара
      <span class="pl-2 h4">11<sup>40</sup> - 13<sup>00</sup></span>
    </div>
    <div class="card-body p-0">
      <div class="d-md-none text-center text-truncate">Экспл Н/Г мест</div>
      <span>ауд.<span class="h5">ПК217</span></span>
      <span class="Staff">Степанов С.В.</span>
    </div>
  </div>

  <div class="card myCard">
    <div class="card-header">
      <span class="h3">IV</span> пара
      <span class="pl-2 h4">13<sup>20</sup> - 14<sup>40</sup></span>
    </div>
    <div class="card-body p-0">
      <div class="d-md-none text-center text-truncate">Пром безопас</div>
      <span>ауд.<span class="h5">ПК201</span></span>
      <span class="Staff">Гайзуллин И.Т.</span>
    </div>
  </div>
</body></html>
"""


def lesson(
    pair="II",
    subgroup=None,
    subject="Ин.яз.",
    room="ПК103",
    teacher="Мурзабулатова Ф.Ф.",
    start="10:00",
    end="11:20",
):
    return bot.Lesson(
        pair=pair,
        time=f"{start} - {end}",
        subject=subject,
        teacher=teacher,
        room=room,
        start=start,
        end=end,
        subgroup=subgroup,
    )


def study_day_schedule(day, pairs):
    """Schedule из спецификации пар: [(pair, start, end, [lessons])].

    lessons — список bot.Lesson (обычно 1 занятие или 2-3 подгруппы).
    """
    lessons = []
    for pair_number, start, end, pair_lessons in pairs:
        lessons.extend(pair_lessons)
    return bot.Schedule(date=day, group=bot.GROUP_NAME, lessons=lessons)


def site_september_2026():
    """Реальное расписание ЭС7-24 на 1-11 сентября 2026 (данные сайта).

    Итог по минутам: 180 + 320 + 400 + 320 + 320 + 240 + 240 + 320 + 320
    = 2660 минут (44 ч 20 мин). Пара II 7 сентября — две подгруппы
    «Ин.яз.»; пара IV 11 сентября — подгруппа 1 «Ин.яз в проф», а у
    подгруппы 2 пара ОТМЕНЕНА («~..............»: подгруппа уходит
    домой, занятие не проводится и в историю не пишется).
    """
    def one(pair, start, end, subject, room, teacher):
        return lesson(pair, None, subject, room, teacher, start, end)

    return {
        date(2026, 9, 1): [
            ("III", "10:50", "11:50",
             [one("III", "10:50", "11:50", "кл.час", "УК202", "Мукалляпова А.И.")]),
            ("IV", "12:00", "13:00",
             [one("IV", "12:00", "13:00", "НГПО", "ПК217", "Степанов С.В.")]),
            ("V", "13:15", "14:15",
             [one("V", "13:15", "14:15", "Разраб н/г мест", "УК105", "Дроздов А.П.")]),
        ],
        date(2026, 9, 2): [
            ("I", "08:30", "09:50",
             [one("I", "08:30", "09:50", "Основы экономик", "УК303", "Кильдиярова Г.Р.")]),
            ("II", "10:00", "11:20",
             [one("II", "10:00", "11:20", "Основы экономик", "УК303", "Кильдиярова Г.Р.")]),
            ("III", "11:35", "12:55",
             [one("III", "11:35", "12:55", "Экспл Н/Г мест", "УК103", "Дроздов А.П.")]),
            ("IV", "13:25", "14:45",
             [one("IV", "13:25", "14:45", "Экспл Н/Г мест", "УК103", "Дроздов А.П.")]),
        ],
        date(2026, 9, 3): [
            ("I", "08:30", "09:50",
             [one("I", "08:30", "09:50", "Экспл Н/Г мест", "УК107", "Дроздов А.П.")]),
            ("II", "10:00", "11:20",
             [one("II", "10:00", "11:20", "Тек (под) рем", "ПК107", "Зубайдуллин З.Ш.")]),
            ("III", "11:35", "12:55",
             [one("III", "11:35", "12:55", "Основы экономик", "УК303", "Кильдиярова Г.Р.")]),
            ("IV", "13:25", "14:45",
             [one("IV", "13:25", "14:45", "Экспл Н/Г мест", "УК107", "Дроздов А.П.")]),
            ("V", "14:55", "16:15",
             [one("V", "14:55", "16:15", "НГПО", "ПК217", "Степанов С.В.")]),
        ],
        date(2026, 9, 4): [
            ("I", "08:30", "09:50",
             [one("I", "08:30", "09:50", "Пром безопас", "ПК108", "Гайзуллин И.Т.")]),
            ("II", "10:00", "11:20",
             [one("II", "10:00", "11:20", "Пожарная безоп", "ПК206", "Фахретдинов Р.Ф.")]),
            ("III", "11:35", "12:55",
             [one("III", "11:35", "12:55", "Экспл Н/Г мест", "ПК102", "Дроздов А.П.")]),
            ("IV", "13:25", "14:45",
             [one("IV", "13:25", "14:45", "Экспл Н/Г мест", "ПК102", "Дроздов А.П.")]),
        ],
        date(2026, 9, 7): [
            ("I", "08:30", "09:50",
             [one("I", "08:30", "09:50", "Химия Н и Г", "УК307", "Арнаутова А.В.")]),
            ("II", "10:00", "11:20", [
                lesson("II", "1", "Ин.яз.", "ПК103", "Мурзабулатова Ф.Ф.",
                       "10:00", "11:20"),
                lesson("II", "2", "Ин.яз.", "ПК303", "Амирханова Г.А.",
                       "10:00", "11:20"),
            ]),
            ("III", "11:35", "12:55",
             [one("III", "11:35", "12:55", "НГПО", "ПК217", "Степанов С.В.")]),
            ("IV", "13:25", "14:45",
             [one("IV", "13:25", "14:45", "Пром безопас", "ПК201", "Гайзуллин И.Т.")]),
        ],
        date(2026, 9, 8): [
            ("I", "08:30", "09:50",
             [one("I", "08:30", "09:50", "НГПО", "ПК217", "Степанов С.В.")]),
            ("II", "10:00", "11:20",
             [one("II", "10:00", "11:20", "НГПО", "ПК217", "Степанов С.В.")]),
            ("III", "11:35", "12:55",
             [one("III", "11:35", "12:55", "Физ-ра", "бол зал 2", "Кинзябаев А.И.")]),
        ],
        date(2026, 9, 9): [
            ("I", "08:30", "09:50",
             [one("I", "08:30", "09:50", "Основы экономик", "УК303", "Кильдиярова Г.Р.")]),
            ("II", "10:00", "11:20",
             [one("II", "10:00", "11:20", "Основы экономик", "УК303", "Кильдиярова Г.Р.")]),
            ("III", "11:35", "12:55",
             [one("III", "11:35", "12:55", "Тек (под) рем", "ПК218", "Зубайдуллин З.Ш.")]),
        ],
        date(2026, 9, 10): [
            ("I", "08:30", "09:50",
             [one("I", "08:30", "09:50", "НГПО", "ПК217", "Степанов С.В.")]),
            ("II", "10:00", "11:20",
             [one("II", "10:00", "11:20", "НГПО", "ПК217", "Степанов С.В.")]),
            ("III", "11:35", "12:55",
             [one("III", "11:35", "12:55", "Основы экономик", "УК303", "Кильдиярова Г.Р.")]),
            ("IV", "13:25", "14:45",
             [one("IV", "13:25", "14:45", "Тек (под) рем", "ПК218", "Зубайдуллин З.Ш.")]),
        ],
        date(2026, 9, 11): [
            ("I", "08:30", "09:50",
             [one("I", "08:30", "09:50", "Пожарная безоп", "ПК102", "Фахретдинов Р.Ф.")]),
            ("II", "10:00", "11:20",
             [one("II", "10:00", "11:20", "Химия Н и Г", "УК303", "Арнаутова А.В.")]),
            ("III", "11:35", "12:55",
             [one("III", "11:35", "12:55", "Химия Н и Г", "УК303", "Арнаутова А.В.")]),
            ("IV", "13:25", "14:45", [
                lesson("IV", "1", "Ин.яз в проф", "ПК103",
                       "Мурзабулатова Ф.Ф.", "13:25", "14:45"),
                lesson("IV", "2", "~..............", "—", "—",
                       "13:25", "14:45"),
            ]),
        ],
    }


class TestSubgroupParsing(unittest.TestCase):
    def test_provided_html_has_both_subgroups(self):
        day = date(2026, 9, 7)
        schedule = bot.parse_schedule(PROVIDED_HTML, day)
        self.assertEqual(len(schedule.lessons), 5)

        pair_i = [x for x in schedule.lessons if x.pair == "I"]
        pair_ii = [x for x in schedule.lessons if x.pair == "II"]

        self.assertEqual(len(pair_i), 1)
        self.assertIsNone(pair_i[0].subgroup)
        self.assertEqual(pair_i[0].subject, "Химия Н и Г")
        self.assertEqual(pair_i[0].room, "УК307")
        self.assertEqual(pair_i[0].teacher, "Арнаутова А.В.")

        # Главное требование: вторая подгруппа не потеряна.
        self.assertEqual(len(pair_ii), 2)
        by_sub = {bot.clean_text(x.subgroup): x for x in pair_ii}
        self.assertEqual(set(by_sub), {"1", "2"})
        self.assertEqual(by_sub["1"].subject, "Ин.яз.")
        self.assertEqual(by_sub["1"].room, "ПК103")
        self.assertEqual(by_sub["1"].teacher, "Мурзабулатова Ф.Ф.")
        self.assertEqual(by_sub["2"].subject, "Ин.яз.")
        self.assertEqual(by_sub["2"].room, "ПК303")
        self.assertEqual(by_sub["2"].teacher, "Амирханова Г.А.")

        # Пары: II содержит два занятия, III/IV по одному.
        pairs = {p.number: p for p in schedule.pairs}
        self.assertEqual(len(pairs["II"].lessons), 2)
        self.assertEqual(len(pairs["III"].lessons), 1)
        self.assertEqual(pairs["III"].lessons[0].room, "ПК217")
        self.assertEqual(len(pairs["IV"].lessons), 1)
        self.assertEqual(pairs["IV"].lessons[0].room, "ПК201")

    def test_plain_text_without_known_classes_still_parses_subgroups(self):
        html = """
        <div class="card myCard">
          <div class="card-header"><span class="h3">II</span> пара
            <span class="h4">10<sup>00</sup> - 11<sup>20</sup></span></div>
          <div class="card-body">
            <div class="d-flex flex-column subGroup1">
              <span>1</span> п/гр.
              <span>ауд.<span class="h5">ПК103</span></span>
              Мурзабулатова Ф.Ф.
              Ин.яз.
            </div>
            <div class="d-flex flex-column subGroup2">
              <span>2</span> п/гр.
              <span>ауд.<span class="h5">ПК303</span></span>
              Амирханова Г.А.
              Ин.яз.
            </div>
          </div>
        </div>
        """
        schedule = bot.parse_schedule(html, date(2026, 9, 7))
        self.assertEqual(len(schedule.lessons), 2)
        self.assertEqual(schedule.lessons[0].subgroup, "1")
        self.assertEqual(schedule.lessons[0].subject, "Ин.яз.")
        self.assertEqual(schedule.lessons[0].teacher, "Мурзабулатова Ф.Ф.")
        self.assertEqual(schedule.lessons[1].subgroup, "2")
        self.assertEqual(schedule.lessons[1].subject, "Ин.яз.")
        self.assertEqual(schedule.lessons[1].teacher, "Амирханова Г.А.")

    def test_ordinary_pair_has_no_fake_subgroup(self):
        schedule = bot.parse_schedule(PROVIDED_HTML, date(2026, 9, 7))
        ordinary = [x for x in schedule.lessons if x.pair == "I"][0]
        self.assertIsNone(ordinary.subgroup)
        self.assertEqual(ordinary.subject, "Химия Н и Г")


class TestScheduleComparison(unittest.TestCase):
    def make(self, days=None, lessons=None):
        return bot.Schedule(
            date=days or date(2026, 9, 7),
            group=bot.GROUP_NAME,
            lessons=lessons or [],
        )

    def test_room_change_only_target_subgroup(self):
        old = self.make(lessons=[
            lesson("II", "1", room="ПК103"),
            lesson("II", "2", room="ПК303"),
        ])
        new = self.make(lessons=[
            lesson("II", "1", room="ПК103"),
            lesson("II", "2", room="ПК305"),
        ])
        changes = bot.compare_schedules(old, new)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "changed")
        self.assertEqual(changes[0].subgroup, "2")
        self.assertEqual(len(changes[0].details), 1)
        self.assertEqual(changes[0].details[0]["field"], "room")
        self.assertEqual(changes[0].details[0]["old"], "ПК303")
        self.assertEqual(changes[0].details[0]["new"], "ПК305")

    def test_teacher_subject_and_time_changes(self):
        old = self.make(lessons=[lesson(subgroup=None, subject="Ин.яз.")])
        new = self.make(lessons=[
            lesson(subgroup=None, subject="Математика", teacher="Иванова А.А.",
                   start="10:00", end="11:30")
        ])
        changes = bot.compare_schedules(old, new)
        self.assertEqual(len(changes), 1)
        fields = {d["field"] for d in changes[0].details}
        self.assertIn("subject", fields)
        self.assertIn("teacher", fields)
        self.assertIn("time", fields)

    def test_added_subgroup_detected(self):
        old = self.make(lessons=[lesson("II", "1")])
        new = self.make(lessons=[
            lesson("II", "1"),
            lesson("II", "2", room="ПК303", teacher="Амирханова Г.А."),
        ])
        changes = bot.compare_schedules(old, new)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "added")
        self.assertEqual(changes[0].subgroup, "2")

    def test_removed_subgroup_detected(self):
        old = self.make(lessons=[
            lesson("II", "1"),
            lesson("II", "2", room="ПК303", teacher="Амирханова Г.А."),
        ])
        new = self.make(lessons=[lesson("II", "1")])
        changes = bot.compare_schedules(old, new)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "removed")
        self.assertEqual(changes[0].subgroup, "2")

    def test_added_and_removed_pair(self):
        old = self.make(lessons=[lesson("I", subject="Химия")])
        new = self.make(lessons=[
            lesson("I", subject="Химия"),
            lesson("IV", subject="Пром безопас", room="ПК201",
                   teacher="Гайзуллин И.Т.", start="13:20", end="14:40"),
        ])
        changes = bot.compare_schedules(old, new)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "added")
        self.assertEqual(changes[0].pair, "IV")

        old2 = self.make(lessons=[
            lesson("I"),
            lesson("II", "2"),
        ])
        new2 = self.make(lessons=[lesson("I")])
        changes2 = bot.compare_schedules(old2, new2)
        self.assertEqual(len(changes2), 1)
        self.assertEqual(changes2[0].kind, "removed")
        self.assertEqual(changes2[0].pair, "II")

    def test_no_changes(self):
        old = self.make(lessons=[lesson("II", "1"), lesson("II", "2")])
        new = self.make(lessons=[lesson("II", "1"), lesson("II", "2")])
        self.assertEqual(bot.compare_schedules(old, new), [])

    def test_whitespace_and_case_are_invisible_to_hash(self):
        old = bot.Schedule(date=date(2026, 9, 7), group=bot.GROUP_NAME,
                           lessons=[lesson(room=" ПК103 ",
                                           subject=" ин.яз. ")])
        new = bot.Schedule(date=date(2026, 9, 7), group=bot.GROUP_NAME,
                           lessons=[lesson(room="ПК103", subject="Ин.яз.")])
        self.assertEqual(bot.schedule_signature(old), bot.schedule_signature(new))


class TestPillowRendering(unittest.TestCase):
    """Рендер дня: вертикальный формат, аудитория — акцент, инлайн-изменения."""

    def four_pairs(self, day=date(2026, 9, 24)):
        return bot.Schedule(date=day, group=bot.GROUP_NAME, lessons=[
            lesson("I", None, "Пожарная безопасность", "ПК108",
                   "Фахретдинов Р. Ф.", "08:30", "09:50"),
            lesson("II", None, "Ин.яз.", "УК103", "Мурзабулатова Ф.Ф.",
                   "10:00", "11:20"),
            lesson("III", None, "Химия Н и Г", "УК307", "Арнаутова А.В.",
                   "11:35", "12:55"),
            lesson("V", None, "Физ-ра", "бол зал 2", "Кинзябаев А.И.",
                   "14:55", "16:15"),
        ])

    # --- формат ---
    def test_image_is_portrait_2_to_3(self):
        """Один день = одна вертикальная картинка 2:3 (1080×1620)."""
        self.assertEqual((bot.SCHEDULE_WIDTH, bot.SCHEDULE_HEIGHT),
                         (1080, 1620))
        path = bot.render_schedule_image(self.four_pairs())
        with Image.open(path) as img:
            w, h = img.size
        self.assertEqual((w, h), (1080, 1620))
        self.assertGreater(h, w, "картинка должна быть вертикальной")
        self.assertEqual(round(h / w, 2), 1.5, "соотношение ~2:3")
        # длинная сторона в разумных пределах: без «чрезмерно высокого» холста
        self.assertLessEqual(h, 1980)

    def test_text_is_supersampled_and_sharp(self):
        """Рендер идёт в 2x и даунскейлится — иначе текст в превью мыльный."""
        self.assertGreaterEqual(bot.SUPER_SAMPLE, 2)
        path = bot.render_schedule_image(self.four_pairs())
        with Image.open(path) as img:
            # финал всегда в 1x: холст не «случайно» в два раза больше
            self.assertEqual(img.size, (bot.SCHEDULE_WIDTH, bot.SCHEDULE_HEIGHT))
            self.assertEqual(img.format, "PNG")

    def test_file_is_small_enough_for_telegram(self):
        path = bot.render_schedule_image(self.four_pairs())
        size = Path(path).stat().st_size
        self.assertLess(size, 1.5 * 1024 * 1024,
                        "PNG для Telegram должен оставаться лёгким")

    def test_nothing_sits_in_the_unsafe_border(self):
        """Все важные элементы — в safe area: превью Telegram скругляет края."""
        path = bot.render_schedule_image(self.four_pairs())
        with Image.open(path) as img:
            self.assertEqual(outside_safe_area(img, bot.SAFE_AREA - 6), [],
                             "контент прижат к краю холста")

    def plan_problems(self):
        return bot._LAST_RENDER["problems"]

    def test_layout_report_has_no_problems(self):
        for day in (date(2026, 9, 24), date(2026, 9, 25)):
            bot.render_schedule_image(self.four_pairs(day))
            self.assertEqual(bot._LAST_RENDER["problems"], [])
            self.assertGreater(bot._LAST_RENDER["elements"], 10)

    # --- аудитория: зелёная плашка, аккуратная, но заметная ---
    def test_room_is_rendered_as_a_green_panel(self):
        path = bot.render_schedule_image(self.four_pairs())
        plan = bot._LAST_RENDER["plan"]
        panels = [op for op in plan["ops"] if op.get("kind") == "panel"
                  and op.get("fill") == bot.S_GREEN_L]
        self.assertEqual(len(panels), 4, "нет зелёных плашек аудиторий")
        rooms = ops_with("room")
        room_sizes = text_sizes("room")
        self.assertEqual(len(room_sizes), 4)
        for op in rooms:
            self.assertIn("Ауд.", op["lines"][0])
            box = op["bbox"]
            self.assertTrue(
                any(p["box"][0] <= box[0] + 1 and p["box"][2] >= box[2] - 1
                    and p["box"][1] <= box[1] + 1 and p["box"][3] >= box[3] - 1
                    for p in panels),
                "аудитория нарисована вне зелёной плашки",
            )
        # аудитория заметнее преподавателя…
        self.assertGreaterEqual(min(room_sizes), max(text_sizes("teacher")))
        if ops_with("hours"):
            self.assertGreater(min(text_sizes("hours")),
                               max(text_sizes("teacher")))
        # …но не «кричат»: кегль в одном диапазоне с предметом
        self.assertLessEqual(max(room_sizes), max(text_sizes("subject")) + 1)
        self.assertGreaterEqual(min(room_sizes), bot.ROOM_FONT_MIN)
        for op in ops_with("hours"):
            self.assertEqual(op["fill"], bot.S_GREEN_D)
        with Image.open(path) as img:
            px = img.convert("RGB").load()
            # зелёная плашка аудитории реально нарисована
            green = hex_rgb(bot.S_GREEN_L)
            self.assertTrue(any(
                close(px[x, y], green, 5)
                for y in range(0, img.height, 3) for x in range(0, img.width, 3)
            ), "зелёная плашка аудитории не найдена")

    def test_room_font_never_drops_below_floor_with_many_lessons(self):
        """Много занятий — компактнее всё, КРОМЕ аудитории и её часов."""
        lessons = [lesson(chr(ord("I") + i), None, f"Предмет {i}",
                          f"ПК{i:03d}", "Иванова А.А.",
                          f"{8 + i:02d}:00", f"{9 + i:02d}:00")
                   for i in range(9)]
        path = bot.render_schedule_image(
            bot.Schedule(date=date(2026, 9, 24), group=bot.GROUP_NAME,
                         lessons=lessons))
        plan = bot._LAST_RENDER["plan"]
        self.assertEqual(bot._LAST_RENDER["problems"], [])
        self.assertLessEqual(plan["scale"], 0.80,
                             "плотный день должен уплотниться")
        for op in ops_with("room"):
            self.assertGreaterEqual(op["size"], bot.ROOM_FONT_MIN)
        # второстепенные строки не крупнее аудитории — но и не «в пол»
        self.assertLessEqual(max(text_sizes("teacher")),
                             min(text_sizes("room")))
        with Image.open(path) as img:
            self.assertEqual(img.size[0], bot.SCHEDULE_WIDTH)
            self.assertGreaterEqual(img.size[1], bot.SCHEDULE_HEIGHT,
                                    "если не влезает — растёт холст, а не кроп")
            self.assertEqual(outside_safe_area(img, bot.SAFE_AREA - 6), [])

    # --- изменения внутри карточки, без правой колонки ---
    def test_changes_render_inline_without_right_column(self):
        schedule = bot.Schedule(date=date(2026, 9, 24), group=bot.GROUP_NAME,
                                lessons=[
                                    lesson("II", None, "Ин.яз.", "УК103",
                                           "Мурзабулатова Ф.Ф."),
                                ])
        changes = [bot.ScheduleChange(
            kind="changed", pair="II", subgroup=None,
            old={"room": "УК105"}, new={"room": "УК103"},
            details=[{"field": "room", "label": "Аудитория",
                      "old": "УК105", "new": "УК103"}],
        )]
        path = bot.render_schedule_image(schedule, changes=changes,
                                       title="РАСПИСАНИЕ ИЗМЕНИЛОСЬ")
        text = rendered_text()
        # и старая, и новая аудитория — внутри карточки
        self.assertIn("Аудитория: УК105 → УК103", text)
        self.assertIn("Ауд. УК103", text)
        self.assertIn("ИЗМЕНЕНО", text)
        # колонки «Изменения» больше нет ни в layout, ни на картинке
        plan = bot._LAST_RENDER["plan"]
        self.assertNotIn("panel", plan["owners"])
        self.assertFalse([op for op in plan["ops"]
                         if str(op.get("id")).startswith("panel")])
        self.assertNotIn("right", plan)
        with Image.open(path) as img:
            w, h = img.size
            self.assertEqual((w, h), (bot.SCHEDULE_WIDTH, bot.SCHEDULE_HEIGHT))
            # правая половина не зарезервирована под колонку: карточка
            # занимает всю ширину safe area
            box = plan["cards"][0]["box"]
            self.assertAlmostEqual(box[0], bot.SAFE_AREA, delta=1)
            self.assertAlmostEqual(box[2], w - bot.SAFE_AREA, delta=1)

    def test_removed_pair_gets_its_own_cancelled_card(self):
        """Раньше удалённая пара жила в правой колонке — теперь это карточка."""
        schedule = bot.Schedule(date=date(2026, 9, 24), group=bot.GROUP_NAME,
                                lessons=[lesson("I", None, "Математика",
                                                 "УК201", "Иванова А.А.",
                                                 "08:30", "09:50")])
        changes = [bot.ScheduleChange(
            kind="removed", pair="III", subgroup=None,
            old={"pair": "III", "start": "11:35", "end": "12:55",
                 "subject": "Химия", "room": "УК307", "teacher": "—",
                 "subgroup": None},
            new=None, details=[],
        )]
        bot.render_schedule_image(schedule, changes=changes)
        text = rendered_text()
        self.assertIn("Занятие отменено", text)
        self.assertIn("Ауд. УК307", text)   # аудиторию не выбрасываем
        plan = bot._LAST_RENDER["plan"]
        self.assertEqual(len(plan["cards"]), 2)
        cancelled = plan["specs"][1]
        self.assertTrue(cancelled["cancelled_all"])
        self.assertEqual(bot._LAST_RENDER["problems"], [])

    def test_many_changes_do_not_crash_or_crop(self):
        items, changes = [], []
        for i in range(1, 12):
            items.append(lesson("II", str(i), subject=f"Предмет {i}",
                                room=f"ПК{i:03d}"))
            changes.append(bot.ScheduleChange(
                kind="added", pair="II", subgroup=str(i), old=None,
                new={"subject": f"Предмет {i}", "room": f"ПК{i:03d}",
                     "teacher": "—"}, details=[],
            ))
        schedule = bot.Schedule(date=date(2026, 9, 24), group=bot.GROUP_NAME,
                               lessons=items)
        path = bot.render_schedule_image(schedule, changes=changes)
        self.assertTrue(Path(path).exists())
        self.assertEqual(bot._LAST_RENDER["problems"], [])
        with Image.open(path) as img:
            self.assertGreater(img.size[1], img.size[0])
            self.assertLessEqual(img.size[1], bot.SCHEDULE_HEIGHT_MAX,
                                 "11 подгрупп не должны ломать формат холста")

    def test_added_subgroup_and_removed_subgroup_render(self):
        schedule = bot.Schedule(date=date(2026, 9, 24), group=bot.GROUP_NAME,
                                lessons=[
                                    lesson("II", "1", "Ин.яз.", "ПК103",
                                           "Мурзабулатова Ф.Ф."),
                                    lesson("II", "2", "Ин.яз.", "ПК303",
                                           "Амирханова Г.А."),
                                ])
        changes = [bot.ScheduleChange(
            kind="removed", pair="II", subgroup="3",
            old={"pair": "II", "start": "10:00", "end": "11:20",
                 "subject": "Химия", "room": "—", "teacher": "—",
                 "subgroup": "3"},
            new=None, details=[],
        )]
        path = bot.render_schedule_image(schedule, changes=changes)
        text = rendered_text()
        self.assertIn("1 П/ГР.", text)
        self.assertIn("2 П/ГР.", text)
        self.assertTrue(Path(path).exists())
        self.assertEqual(bot._LAST_RENDER["problems"], [])

    def test_long_subject_wraps_and_never_overflows(self):
        schedule = bot.Schedule(date=date(2026, 9, 24), group=bot.GROUP_NAME,
                                lessons=[lesson(
                                    "I", None,
                                    "Очень длинное название предмета, которое "
                                    "точно не помещается в одну строку карточки "
                                    "целиком даже после уменьшения размера "
                                    "шрифта внутри всех доступных переносов",
                                    "УК103", "Степанов Сергей Владимирович",
                                    "08:30", "09:50")])
        path = bot.render_schedule_image(schedule)
        self.assertEqual(bot._LAST_RENDER["problems"], [])
        subject = [op for op in bot._LAST_RENDER["plan"]["ops"]
                   if op["op"] == "text" and "длинное" in " ".join(op["lines"])]
        self.assertTrue(subject)
        self.assertGreater(len(subject[0]["lines"]), 1,
                           "длинное название должно переноситься, а не резаться")
        plan = bot._LAST_RENDER["plan"]
        card = plan["cards"][0]["box"]
        for op in subject:
            self.assertLessEqual(op["bbox"][0], card[2])
            self.assertLessEqual(op["bbox"][3], card[3])
        with Image.open(path) as img:
            self.assertEqual(outside_safe_area(img, bot.SAFE_AREA - 6), [])

    def test_empty_day_renders_single_portrait_card(self):
        path = bot.render_schedule_image(empty_schedule(date(2026, 9, 8)))
        text = rendered_text()
        self.assertIn("Занятий нет", text)
        self.assertEqual(bot._LAST_RENDER["problems"], [])
        with Image.open(path) as img:
            self.assertEqual(img.size, (bot.SCHEDULE_WIDTH,
                                        bot.SCHEDULE_HEIGHT))

    def test_two_days_never_share_one_image(self):
        """Сегодня и завтра — две отдельные картинки, каждая про свой день."""
        today = bot.render_schedule_image(self.four_pairs(date(2026, 9, 24)))
        tomorrow = bot.render_schedule_image(self.four_pairs(date(2026, 9, 25)))
        self.assertNotEqual(Path(today).name, Path(tomorrow).name)
        for path, day in ((today, date(2026, 9, 24)), (tomorrow, date(2026, 9, 25))):
            bot.render_schedule_image(self.four_pairs(day))
            text = rendered_text()
            caption = bot._day_caption(bot.Schedule(
                date=day, group=bot.GROUP_NAME, lessons=[]))
            other = date(2026, 9, 25) if day == date(2026, 9, 24) else date(2026, 9, 24)
            other_caption = bot._day_caption(bot.Schedule(
                date=other, group=bot.GROUP_NAME, lessons=[]))
            self.assertIn(caption, text)
            self.assertNotIn(other_caption, text)
            self.assertEqual(text.count("2026"), 1)


class TestLessonCount(unittest.TestCase):
    """COUNT = количество уникальных пар / временных слотов."""

    def sched(self, lessons):
        return bot.Schedule(date=date(2026, 9, 7), group=bot.GROUP_NAME,
                            lessons=lessons)

    def four_pairs(self):
        return [
            lesson("I", None, "Химия Н и Г", "УК307", "Арнаутова А.В.",
                   "08:30", "09:50"),
            lesson("II", None, "Ин.яз."),
            lesson("III", None, "НГПО", "ПК217", "Степанов С.В.",
                   "11:35", "12:55"),
            lesson("IV", None, "Пром безопас", "ПК201", "Гайзуллин И.Т.",
                   "13:25", "14:45"),
        ]

    def test_a_four_pairs_without_subgroups(self):
        self.assertEqual(bot.count_lessons(self.sched(self.four_pairs())), 4)

    def test_b_four_pairs_one_with_two_subgroups(self):
        lessons = self.four_pairs()
        lessons[1] = lesson("II", "1")
        lessons.insert(2, lesson("II", "2", room="ПК303",
                                 teacher="Амирханова Г.А."))
        schedule = self.sched(lessons)
        self.assertEqual(len(schedule.lessons), 5)
        self.assertEqual(bot.count_lessons(schedule), 4)

    def test_c_four_pairs_one_with_three_subgroups(self):
        lessons = self.four_pairs()
        lessons[1] = lesson("II", "1")
        lessons.insert(2, lesson("II", "2", room="ПК303"))
        lessons.insert(3, lesson("II", "3", room="ПК305"))
        schedule = self.sched(lessons)
        self.assertEqual(len(schedule.lessons), 6)
        self.assertEqual(bot.count_lessons(schedule), 4)

    def test_d_same_subject_in_two_pairs_counts_twice(self):
        schedule = self.sched([
            lesson("I", None, "Ин.яз.", start="08:30", end="09:50"),
            lesson("II", None, "Ин.яз."),
        ])
        self.assertEqual(bot.count_lessons(schedule), 2)

    def test_e_different_rooms_and_teachers_still_one_lesson(self):
        schedule = self.sched([
            lesson("II", "1", room="ПК103", teacher="Мурзабулатова Ф.Ф."),
            lesson("II", "2", room="ПК303", teacher="Амирханова Г.А."),
        ])
        self.assertEqual(bot.count_lessons(schedule), 1)

    def test_f_pair_without_subgroups(self):
        self.assertEqual(bot.count_lessons(self.sched([lesson("II")])), 1)

    def test_empty_schedule(self):
        self.assertEqual(bot.count_lessons(self.sched([])), 0)

    def test_count_matches_number_of_rendered_pairs(self):
        schedule = bot.parse_schedule(PROVIDED_HTML, date(2026, 9, 7))
        self.assertEqual(len(schedule.lessons), 5)      # записи с подгруппами
        self.assertEqual(bot.count_lessons(schedule), 4)  # занятия
        self.assertEqual(len(schedule.pairs), 4)

    def test_accepts_lists_and_dicts(self):
        schedule = self.sched([lesson("II", "1"), lesson("II", "2")])
        self.assertEqual(bot.count_lessons(schedule.lessons), 1)
        self.assertEqual(bot.count_lessons(bot.normalize_schedule(schedule)), 1)

    def test_time_only_lessons_grouped_by_slot(self):
        without_pair = [
            bot.Lesson(pair="", time="10:00 - 11:20", subject="Ин.яз.",
                       teacher="—", room="ПК103", subgroup="1"),
            bot.Lesson(pair="", time="10:00 - 11:20", subject="Ин.яз.",
                       teacher="—", room="ПК303", subgroup="2"),
            bot.Lesson(pair="", time="11:35 - 12:55", subject="НГПО",
                       teacher="—", room="ПК217"),
        ]
        self.assertEqual(bot.count_lessons(without_pair), 2)

    def test_captions_use_lesson_count(self):
        schedule = bot.parse_schedule(PROVIDED_HTML, date(2026, 9, 7))
        self.assertIn("Занятий: 4", bot._photo_caption(schedule))
        caption = bot._notification_caption(
            schedule, date(2026, 9, 7), first_time=False, changes=[]
        )
        self.assertIn("Занятий: 4", caption)
        self.assertNotIn("Занятий: 5", caption)


class TestCancelledLessonRendering(unittest.TestCase):
    """Отмена занятия («~..............») на картинке расписания.

    Отменённая пара — обычная карточка в той же колонке: мягкая розовая
    плашка внутри карточки, пилюля «Занятие отменено», красный бейдж
    «ОТМЕНА»; аудитория остаётся, если она есть в данных.
    """

    RED = hex_rgb(bot.S_RED)          # бейдж «ОТМЕНА»
    RED_BG = hex_rgb(bot.S_RED_L)     # фон отменённой карточки
    GREEN_TEXT = hex_rgb(bot.S_GREEN_D)
    GREEN_MUTED = hex_rgb(bot.S_GREEN_M)
    GREEN_BG = hex_rgb(bot.S_GREEN_L)

    def cancelled_schedule(self, room="—"):
        return bot.Schedule(
            date=date(2026, 9, 11),
            group=bot.GROUP_NAME,
            lessons=[
                lesson("IV", "2", "~..............", room, "—",
                       "13:25", "14:45"),
            ],
        )

    def test_cancelled_card_is_red_and_has_no_right_panel(self):
        path = bot.render_schedule_image(self.cancelled_schedule())
        plan = bot._LAST_RENDER["plan"]
        # мягкая розовая плашка отмены внутри карточки
        pink = [op for op in plan["ops"] if op.get("kind") == "panel"
                and op.get("fill") == bot.S_RED_L]
        self.assertTrue(pink, "нет розовой плашки отмены")
        with Image.open(path) as img:
            box = tuple(int(v) for v in plan["cards"][0]["box"])
            panel_box = tuple(int(v) for v in pink[0]["box"])
            px = img.convert("RGB").load()
            self.assertTrue(any(
                close(px[x, panel_box[1] + 3], self.RED_BG, 6)
                for x in range(panel_box[0] + 20, panel_box[2] - 20, 7)
            ), "плашка отмены не красно-розовая")
            self.assertTrue(panel_box[0] >= box[0] and panel_box[2] <= box[2],
                            "плашка отмены вышла за карточку")
            # пилюля «Занятие отменено» — внутри карточки, красным текстом
            pill = [op for op in plan["ops"] if op["op"] == "text"
                    and "Занятие отменено" in " ".join(op["lines"])]
            self.assertEqual(len(pill), 1)
            self.assertEqual(pill[0]["fill"], bot.S_RED_D)
            self.assertTrue(box[0] <= pill[0]["bbox"][0]
                            and pill[0]["bbox"][2] <= box[2],
                            "пилюля отмены вышла за карточку")
            # красный бейдж «ОТМЕНА» справа
            badge = [op for op in plan["ops"] if op.get("kind") == "chip"
                     and op.get("fill") == bot.S_RED]
            self.assertTrue(badge, "нет красного бейджа ОТМЕНА")
            badge_op = badge[0]
            self.assertTrue(any(
                close(px[x, int((badge_op["box"][1] + badge_op["box"][3]) / 2)],
                      self.RED, 8)
                for x in range(int(badge_op["box"][0]),
                               int(badge_op["box"][2]), 5)
            ), "бейдж ОТМЕНА не нарисован красным")
        self.assertNotIn("panel", plan["owners"])

    def test_cancelled_card_keeps_room_when_data_has_a_room(self):
        """Отменённое занятие без аудитории ≠ отмена с аудиторией: плашка нужна."""
        path = bot.render_schedule_image(self.cancelled_schedule(room="УК307"))
        text = rendered_text()
        self.assertIn("Занятие отменено", text)
        self.assertIn("Ауд. УК307", text)
        rooms = ops_with("room")
        self.assertEqual(len(rooms), 1)
        self.assertGreaterEqual(rooms[0]["size"], bot.ROOM_FONT_MIN)
        # статус отмены всё равно заметнее второстепенных строк
        small = [op for op in plan_small_ops()]
        if small:
            self.assertGreater(rooms[0]["size"], max(op["size"] for op in small))
        with Image.open(path) as img:
            px = img.convert("RGB").load()
            # под аудиторию нарисована зелёная плашка (а не «серая строка»)
            chip_y = int(rooms[0]["bbox"][3]) + 2
            self.assertTrue(any(
                close(px[x, chip_y], self.GREEN_BG, 6)
                for x in range(0, img.width, 3)
            ), "аудитория отменённой пары не подсвечена зелёным")

    def test_cancelled_card_without_room_says_so_quietly(self):
        bot.render_schedule_image(self.cancelled_schedule())
        text = rendered_text()
        self.assertIn("Занятие отменено", text)
        self.assertNotIn("Ауд.", text)
        self.assertEqual(bot._LAST_RENDER["problems"], [])

    def test_cancellation_is_shorter_than_a_full_card(self):
        """Отменённая карточка компактнее обычной: меньше строк — меньше блок."""
        bot.render_schedule_image(bot.Schedule(
            date=date(2026, 9, 11), group=bot.GROUP_NAME,
            lessons=[lesson("IV", None, "~..............", "—", "—",
                            "13:25", "14:45")],
        ))
        cancelled_box = bot._LAST_RENDER["plan"]["cards"][0]["box"]
        cancelled_size = bot._LAST_RENDER["size"]
        bot.render_schedule_image(bot.Schedule(
            date=date(2026, 9, 10), group=bot.GROUP_NAME,
            lessons=[lesson("IV", None, "Ин.яз в проф", "ПК103",
                            "Мурзабулатова Ф.Ф.", "13:25", "14:45")],
        ))
        normal_box = bot._LAST_RENDER["plan"]["cards"][0]["box"]
        self.assertEqual(cancelled_size, bot._LAST_RENDER["size"],
                         "холст не должен зависеть от содержимого карточки")
        self.assertLess(cancelled_box[3] - cancelled_box[1],
                        normal_box[3] - normal_box[1],
                        "отменённая карточка не компактнее обычной")
    def test_cancellation_in_change_notification_text(self):
        """Уведомление об отмене — словами, без «~..............»."""
        old = bot.Schedule(date=date(2026, 9, 11), group=bot.GROUP_NAME,
                           lessons=[lesson("IV", "1", "Ин.яз в проф")])
        new = bot.Schedule(date=date(2026, 9, 11), group=bot.GROUP_NAME,
                           lessons=[lesson("IV", "1", "~..............")])
        changes = bot.compare_schedules(old, new)
        self.assertTrue(changes)
        text = bot._format_change_text(changes[0])
        self.assertIn("Занятие отменено", text)
        self.assertNotIn("~", text)
        summary = "\n".join(bot._change_summary_lines(changes))
        self.assertIn("Занятие отменено", summary)
        self.assertNotIn("~", summary)


class TestImageLayoutFixes(unittest.TestCase):
    """Разметка: перерыв между карточками, низ картинки, подгруппы."""

    BG = hex_rgb(bot.S_BG)
    MUTED = hex_rgb(bot.S_MUTED)

    def make(self, break_duration="", day=None):
        item = lesson("I", None, "Химия Н и Г", "УК307", "Арнаутова А.В.",
                      "08:30", "09:50")
        item.break_duration = break_duration
        return bot.Schedule(date=day or date(2026, 9, 9),
                            group=bot.GROUP_NAME, lessons=[item])

    DECOR = (hex_rgb(bot.S_BG_LIGHT), hex_rgb(bot.S_BG_DEEP))
    SHADOW = hex_rgb(bot.S_SHADOW)

    def is_filler(self, pixel) -> bool:
        """Фон, подложка или мягкая тень карточки — не нарисованный контент."""
        bases = (self.BG,) + self.DECOR
        for base in bases:
            if close(pixel, base, 2):
                return True
            # тень — смесь подложки и S_SHADOW: пиксель лежит на отрезке
            vec = [self.SHADOW[i] - base[i] for i in range(3)]
            norm = sum(v * v for v in vec) or 1
            t = sum((pixel[i] - base[i]) * vec[i] for i in range(3)) / norm
            if not (0.0 <= t <= 1.05):
                continue
            if sum((pixel[i] - (base[i] + t * vec[i])) ** 2
                   for i in range(3)) <= 144:
                return True
        return False

    def drawn_rows(self, path):
        """Строки с нарисованным контентом (без фона, подложек и теней)."""
        with Image.open(path) as img:
            im = img.convert("RGB")
            px = im.load()
            return [
                y for y in range(im.height)
                if any(not self.is_filler(px[x, y])
                       for x in range(20, im.width - 20, 4))
            ]

    @staticmethod
    def footer_note_rows(plan):
        """Строки сноски подвала из плана."""
        return [op for op in plan["ops"]
                if str(op.get("id")).startswith("footer:note")]

    def rows_of(self, path, color, tol=6):
        with Image.open(path) as img:
            px = img.convert("RGB").load()
            return [
                y for y in range(img.height)
                if any(close(px[x, y], color, tol)
                       for x in range(60, img.width - 60, 2))
            ]

    def test_break_label_sits_in_the_time_row_of_its_card(self):
        """«перемена N мин» — тихая пометка справа в строке времени пары."""
        first = lesson("I", None, "Химия", "УК307", "Арнаутова А.В.",
                       "08:30", "09:50")
        first.break_duration = "30 мин"
        schedule = bot.Schedule(date=date(2026, 9, 9), group=bot.GROUP_NAME,
                                lessons=[
                                    first,
                                    lesson("II", None, "Ин.яз.", "ПК103",
                                           "Мурзабулатова Ф.Ф.", "10:00",
                                           "11:20"),
                                ])
        bot.render_schedule_image(schedule)
        plan = bot._LAST_RENDER["plan"]
        breaks = [op for op in plan["ops"] if op["op"] == "text"
                  and "перемена" in " ".join(op["lines"]).lower()]
        self.assertEqual(len(breaks), 1)
        label = breaks[0]
        first_card = plan["cards"][0]["box"]
        # пометка — внутри СВОЕЙ карточки, в строке времени
        self.assertGreaterEqual(label["bbox"][1], first_card[1])
        self.assertLessEqual(label["bbox"][3], first_card[3])
        self.assertEqual(label["fill"], bot.S_MUTED)
        # тише аудитории и времени, но заметнее «шума»
        self.assertLess(label["size"], min(text_sizes("room")))
        self.assertGreaterEqual(label["size"], bot.S_TYPE_MIN["small"])
        # у пары с паузой есть иконка часов рядом с пометкой
        clock = [op for op in plan["ops"]
                 if ":meta:icon" in str(op.get("id"))]
        self.assertTrue(clock, "нет иконки часов у пометки про перемену")
        self.assertEqual(bot._LAST_RENDER["problems"], [])
        # тихая пометка выровнена по правому краю карточки
        self.assertAlmostEqual(label["bbox"][2], first_card[2] - plan_pad(plan),
                               delta=plan_pad(plan) + 2)

    def test_break_line_does_not_move_room_or_subject(self):
        """Наличие перемены не сдвигает левый край контента карточки."""
        with_break = bot.render_schedule_image(
            self.make("30 мин", day=date(2026, 9, 9)))
        without_break = bot.render_schedule_image(
            self.make("", day=date(2026, 9, 10)))

        def room_left(path):
            bot.render_schedule_image(
                self.make("30 мин" if path == with_break else "",
                          day=date(2026, 9, 9) if path == with_break
                          else date(2026, 9, 10)))
            return min(op["bbox"][0] for op in ops_with("room"))

        left_with_break = room_left(with_break)
        left_without = room_left(without_break)
        self.assertAlmostEqual(left_with_break, left_without, delta=1.0)

    def test_nothing_below_last_card_except_footer(self):
        """Под последней карточкой — только плашка подвала со сноской."""
        schedule = bot.parse_schedule(PROVIDED_HTML, date(2026, 9, 7))
        path = bot.render_schedule_image(schedule)
        rows = self.drawn_rows(path)
        with Image.open(path) as img:
            plan = bot._LAST_RENDER["plan"]
            height = img.height
            last_card_bottom = int(max(c["box"][3] for c in plan["cards"]))
        notes = self.footer_note_rows(plan)
        panels = [op for op in plan["ops"] if op.get("id") == "footer:panel"]
        below = [y for y in rows if y > last_card_bottom + 20]
        if notes:
            self.assertTrue(panels, "нет плашки подвала со сносками")
            panel_box = panels[0]["box"]
            self.assertGreaterEqual(min(below), int(panel_box[1]) - 2)
            self.assertLessEqual(max(below), int(panel_box[3]) + 1)
        else:
            self.assertEqual(below, [], "под карточкой есть лишние элементы")
        # нижний контент не прижат к краю холста (safe area минус
        # антиалиасинг нижних выносных элементов)
        self.assertGreaterEqual(height - 1 - max(rows), bot.SAFE_AREA - 20)
        self.assertGreaterEqual(height - 1 - last_card_bottom,
                                bot.SAFE_AREA - 20)
    def test_footer_is_bottom_anchored(self):
        """Подвал прижат к низу, контент не залезает в него и в поля."""
        plans = []
        for schedule in (
            empty_schedule(date(2026, 9, 8)),
            bot.Schedule(date=date(2026, 9, 9), group=bot.GROUP_NAME,
                         lessons=[lesson("I", None, "Химия", "УК307",
                                         "Арнаутова А.В.", "08:30", "09:50")]),
        ):
            bot.render_schedule_image(schedule)
            plans.append(bot._LAST_RENDER["plan"])
        for plan in plans:
            self.assertLessEqual(plan["content_bottom"],
                                 plan["H"] - bot.SAFE_AREA + 1)
            self.assertLess(plan["content_bottom"], plan["footer_top"] + 1)
            if plan["footer_h"]:
                # подвал прижат к низу холста, а не висит под карточками
                self.assertGreaterEqual(
                    plan["footer_top"],
                    plan["H"] - bot.SAFE_AREA - plan["footer_h"] - 1)
                self.assertLessEqual(plan["owners"]["footer"][3],
                                     plan["H"] - bot.SAFE_AREA + 1)
            else:
                self.assertEqual(plan["footer_top"],
                                 plan["H"] - bot.SAFE_AREA)
        self.assertEqual(plans[0]["H"], plans[1]["H"],
                         "формат дня фиксирован: пустой день не короче")
    def test_subgroups_make_a_taller_card_not_a_second_image(self):
        schedule = bot.parse_schedule(PROVIDED_HTML, date(2026, 9, 7))
        pairs = {p.number: p for p in schedule.pairs}
        self.assertEqual(len(pairs["II"].lessons), 2)
        two = bot.Schedule(date=date(2026, 9, 7), group=bot.GROUP_NAME,
                           lessons=[x for x in schedule.lessons
                                    if x.pair == "II"])
        one = bot.Schedule(date=date(2026, 9, 8), group=bot.GROUP_NAME,
                           lessons=[x for x in schedule.lessons
                                    if x.pair == "IV"])
        path_two = bot.render_schedule_image(two)
        bot.render_schedule_image(one)
        box_one = bot._LAST_RENDER["plan"]["cards"][0]["box"]
        bot.render_schedule_image(two)
        box_two = bot._LAST_RENDER["plan"]["cards"][0]["box"]
        with Image.open(path_two) as img_two:
            self.assertEqual(img_two.size,
                             (bot.SCHEDULE_WIDTH, bot.SCHEDULE_HEIGHT))
        # подгруппы — строки ВНУТРИ одной карточки, карточка выше
        self.assertGreater(len(bot._LAST_RENDER["plan"]["specs"][0]["items"]),
                           1)
        self.assertGreater(box_two[3] - box_two[1], box_one[3] - box_one[1])
        text = rendered_text()
        self.assertIn("1 П/ГР.", text)
        self.assertIn("2 П/ГР.", text)
        # у каждой подгруппы своя аудитория
        self.assertEqual(len(ops_with("room")), 2)


class TestScheduleCardDesign(unittest.TestCase):
    """Дизайн карточки: вертикальный формат, зоны, валидация layout."""

    def schedule_with_subgroups(self, day=None):
        return bot.Schedule(
            date=day or date(2026, 9, 24), group=bot.GROUP_NAME,
            lessons=[
                lesson("I", None, "Пром безопас", "СК201",
                       "Гайзуллин И.Т.", "08:30", "09:50"),
                lesson("II", "1", "Ин.яз в проф", "ПК103",
                       "Мурзабулатова Ф.Ф.", "10:00", "11:20"),
                lesson("II", "2", "~..............", "—", "—",
                       "10:00", "11:20"),
                lesson("III", None, "Химия Н и Г", "УК307",
                       "Арнаутова А.В.", "11:35", "12:55"),
            ],
        )

    def test_format_is_vertical_and_ratio_ladder_is_consistent(self):
        self.assertEqual((bot.SCHEDULE_WIDTH, bot.SCHEDULE_HEIGHT),
                         (1080, 1620))
        heights = [h for h, _ in bot.SCHEDULE_RATIO_LADDER]
        self.assertEqual(heights[0], bot.SCHEDULE_HEIGHT)
        self.assertEqual(heights, sorted(heights), "лестница форматов по росту")
        self.assertTrue(all(h > bot.SCHEDULE_WIDTH for h in heights),
                        "все форматы вертикальные")
        self.assertLessEqual(max(heights), 1980)
        path = bot.render_schedule_image(self.schedule_with_subgroups())
        with Image.open(path) as img:
            self.assertEqual(img.size, (bot.SCHEDULE_WIDTH, bot.SCHEDULE_HEIGHT))

    def plan_problems(self):
        return bot._LAST_RENDER["problems"]

    def test_layout_validation_clean(self):
        """validate_layout: нет выходов за bounds и пересечений."""
        bot.render_schedule_image(self.schedule_with_subgroups())
        self.assertEqual(bot._LAST_RENDER["problems"], [])
        self.assertGreater(bot._LAST_RENDER["elements"], 10)
        self.assertTrue(bot._LAST_RENDER["truncated"] == [])

    def test_validate_layout_reports_broken_plan_instead_of_raising(self):
        """План с выбитым за карточку блоком -> список проблем, не исключение."""
        bot.render_schedule_image(self.schedule_with_subgroups())
        plan = dict(bot._LAST_RENDER["plan"])
        ops = [dict(op) for op in plan["ops"]]
        room = next(i for i, op in enumerate(ops)
                    if op.get("role") == "room")
        box = plan["cards"][0]["box"]
        # уводим чип аудитории за правый край карточки
        x0, y0, x1, y1 = ops[room]["bbox"]
        shift = (box[2] - x0) + 30
        ops[room]["bbox"] = (x0 + shift, y0, x1 + shift, y1)
        plan["ops"] = ops
        problems = bot.validate_layout(plan)
        self.assertTrue(problems, "валидатор обязан заметить выход за карточку")
        self.assertTrue(any("card:0" in p for p in problems))

    def test_every_card_is_a_separate_zone_and_cards_do_not_overlap(self):
        bot.render_schedule_image(self.schedule_with_subgroups())
        plan = bot._LAST_RENDER["plan"]
        boxes = [c["box"] for c in plan["cards"]]
        self.assertEqual(len(boxes), 3)     # I, II, III (отмена — внутри II)
        for (ax0, ay0, ax1, ay1) in boxes:
            self.assertGreaterEqual(ax0, bot.SAFE_AREA - 1)
            self.assertLessEqual(ax1, plan["W"] - bot.SAFE_AREA + 1)
            self.assertGreater(ay1, ay0)
        for i in range(len(boxes) - 1):
            self.assertLessEqual(boxes[i][3], boxes[i + 1][1],
                                 "карточки пересеклись по вертикали")
        # каждый блок карточки лежит внутри её зоны
        for card in plan["cards"]:
            owner = card["owner"]
            zone = plan["owners"][owner]
            for op in plan["ops"]:
                if op.get("owner") != owner:
                    continue
                bbox = op_box(op)
                self.assertGreaterEqual(bbox[0], zone[0] - 1)
                self.assertGreaterEqual(bbox[1], zone[1] - 1)
                self.assertLessEqual(bbox[2], zone[2] + 1)
                self.assertLessEqual(bbox[3], zone[3] + 1)

    def test_hierarchy_time_subject_teacher_room(self):
        """Иерархия: время → предмет → преподаватель, аудитория — справа."""
        bot.render_schedule_image(self.schedule_with_subgroups())
        plan = bot._LAST_RENDER["plan"]
        card = plan["cards"][0]["box"]

        def first_in_card(role):
            ops = [op for op in plan["ops"] if op.get("role") == role
                   and op["bbox"][1] >= card[1] - 1
                   and op["bbox"][3] <= card[3] + 1]
            self.assertTrue(ops, f"блок «{role}» не найден в карточке")
            return min(ops, key=lambda op: op["bbox"][1])

        time_op = first_in_card("time")
        subject_op = first_in_card("subject")
        teacher_op = first_in_card("teacher")
        room_op = first_in_card("room")
        self.assertLess(time_op["bbox"][1], subject_op["bbox"][1])
        self.assertLess(subject_op["bbox"][1], teacher_op["bbox"][1])
        # кегли: время крупнее предмета, предмет — крупнее мелочей
        self.assertGreater(time_op["size"], subject_op["size"])
        self.assertGreater(subject_op["size"], teacher_op["size"])
        self.assertGreaterEqual(room_op["size"], bot.ROOM_FONT_MIN)
        self.assertGreater(room_op["size"], teacher_op["size"])
        # аудитория — в зелёной плашке справа, а не мелкий текст в углу
        panels = [op for op in plan["ops"] if op.get("kind") == "panel"
                  and op.get("fill") == bot.S_GREEN_L]
        self.assertTrue(panels, "нет зелёной плашки аудитории")
        self.assertTrue(any(p["box"][0] <= room_op["bbox"][0] + 1
                            and p["box"][2] >= room_op["bbox"][2] - 1
                            for p in panels),
                        "аудитория нарисована вне плашки")
        self.assertGreater(room_op["bbox"][0], subject_op["bbox"][0],
                           "плашка аудитории должна быть справа от текста")

    def test_background_is_lavender_with_soft_diagonals(self):
        """Фон холста — светлая лаванда с едва заметными подложками."""
        path = bot.render_schedule_image(self.schedule_with_subgroups())
        plan = bot._LAST_RENDER["plan"]
        decor = [op for op in plan["ops"]
                 if str(op.get("id")).startswith("deco:bg")]
        self.assertTrue(decor, "нет декоративных подложек фона")
        with Image.open(path) as img:
            px = img.convert("RGB").load()
            bg = hex_rgb(bot.S_BG)
            self.assertTrue(close(px[2, 2], bg, 6), "фон холста не лавандовый")
            self.assertTrue(close(px[img.width - 3, img.height - 3], bg, 6)
                            or close(px[img.width - 3, img.height - 3],
                                     hex_rgb(bot.S_BG_LIGHT), 6))
        # подложки отличаются от фона совсем немного — это фон, не контент
        for color in (bot.S_BG_LIGHT, bot.S_BG_DEEP):
            diff = sum(abs(a - b) for a, b in zip(hex_rgb(color), bg))
            self.assertLessEqual(diff, 30, "подложка слишком контрастная")
        self.assertEqual(bot._LAST_RENDER["problems"], [])

    @staticmethod
    def drawn_rows(path):
        with Image.open(path) as img:
            im = img.convert("RGB")
            px = im.load()
            return [y for y in range(im.height)
                    if any(px[x, y] != hex_rgb(bot.S_BG)
                           for x in range(20, im.width - 20, 4))]

    @staticmethod
    def rows_of(path, color, tol=6):
        with Image.open(path) as img:
            px = img.convert("RGB").load()
            return [y for y in range(img.height)
                    if any(close(px[x, y], color, tol)
                           for x in range(60, img.width - 60, 2))]


class TestTotalStudyBadge(DBTestCase):
    """Прогресс: «Изучено: X / Y акад. ч» в шапке и в карточках занятий."""

    PURPLE = hex_rgb(bot.S_PURPLE)          # шкала прогресса в шапке
    GREEN_TEXT = hex_rgb(bot.S_GREEN_D)     # строка «Изучено: N акад. ч»
    MUTED = hex_rgb(bot.S_MUTED)

    def setUp(self):
        super().setUp()
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
            conn.execute("DELETE FROM subjects")

    def tearDown(self):
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
            conn.execute("DELETE FROM subjects")

    @staticmethod
    def ops_by_id(*ids):
        plan = bot._LAST_RENDER["plan"]
        return [op for op in plan["ops"] if str(op.get("id")) in ids]

    def header_progress_ops(self):
        return self.ops_by_id("header:progress", "header:bar", "header:bar:bg")

    def card_rows_of(self, color, tol=6):
        """Строки с этим цветом в зоне карточек (ниже шапки)."""
        path = bot._LAST_RENDER["path"]
        plan = bot._LAST_RENDER["plan"]
        top = int(max(plan["owners"]["header"][3], plan["content_top"])) - 10
        with Image.open(path) as img:
            px = img.convert("RGB").load()
            return [
                y for y in range(top, img.height)
                if any(close(px[x, y], color, tol)
                       for x in range(80, img.width - 80, 2))
            ]

    def color_clusters(self, rows):
        clusters = []
        for row in rows:
            if not clusters or row > clusters[-1][-1] + 2:
                clusters.append([row])
            else:
                clusters[-1].append(row)
        return clusters

    def progress_lines(self):
        """Изученные часы внутри плашки аудитории (роль hours)."""
        plan = bot._LAST_RENDER["plan"]
        return [op for op in plan["ops"] if op["op"] == "text"
                and op.get("role") == "hours"]

    # --- прогресс в карточках ---
    def test_subject_progress_shown_on_group_image(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 1), "I", "08:30", "09:50",
            "Математика", duration=160,
        )
        schedule = bot.Schedule(
            date=date(2026, 9, 7), group=bot.GROUP_NAME,
            lessons=[lesson("I", None, "Математика", "УК307",
                            "Иванова А.А.", "08:30", "09:50")],
        )
        bot.render_schedule_image(schedule)
        lines = self.progress_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("4 акад. ч", " ".join(lines[0]["lines"]))
        # зелёным, и внутри карточки, а не где-то сбоку
        self.assertEqual(lines[0]["fill"], bot.S_GREEN_D)
        card = bot._LAST_RENDER["plan"]["cards"][0]["box"]
        self.assertLessEqual(lines[0]["bbox"][0], card[2])
        self.assertLessEqual(lines[0]["bbox"][2], card[2])
        # аудитория и часы заметнее преподавателя, но мельче времени
        self.assertGreater(min(text_sizes("room")), max(text_sizes("teacher")))
        self.assertGreater(lines[0]["size"], max(text_sizes("teacher")))
        self.assertLessEqual(lines[0]["size"], max(text_sizes("subject")) + 2)
        self.assertTrue(self.card_rows_of(self.GREEN_TEXT),
                        "зелёная строка прогресса не нарисована")

    def test_subject_progress_without_history_is_not_shown(self):
        """Нет истории — нет и шумной строки: карточка остаётся спокойной."""
        schedule = bot.Schedule(
            date=date(2026, 9, 7), group=bot.GROUP_NAME,
            lessons=[lesson("I", None, "Новый предмет", "УК307",
                            "Иванова А.А.", "08:30", "09:50")],
        )
        path = bot.render_schedule_image(schedule)
        self.assertEqual(self.progress_lines(), [])
        self.assertNotIn("Первое занятие по предмету", rendered_text())
        self.assertEqual(bot.get_subject_progress("Новый предмет"),
                         "Первое занятие по предмету")
        self.assertEqual(bot._LAST_RENDER["problems"], [])
        with Image.open(path) as img:
            self.assertEqual(img.size,
                             (bot.SCHEDULE_WIDTH, bot.SCHEDULE_HEIGHT))

    def test_subject_progress_is_absent_on_staff_image(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 1), "I", "08:30", "09:50",
            "Математика", duration=80,
        )
        staff = bot.Schedule(
            date=date(2026, 9, 8), group="Степанов Сергей Владимирович",
            schedule_type="staff", staff_id=321,
            staff_name="Степанов Сергей Владимирович",
            lessons=[lesson("I", None, "Математика", "УК307",
                            "Степанов С.В.", "08:30", "09:50")],
        )
        bot.render_schedule_image(staff)
        self.assertEqual(self.progress_lines(), [])
        self.assertEqual(self.header_progress_ops(), [])

    def test_progress_does_not_shrink_the_room(self):
        """Строка прогресса не съедает место аудитории: чип не меньше пола."""
        def one(day):
            return bot.Schedule(
                date=day, group=bot.GROUP_NAME,
                lessons=[lesson("I", None, "Математика", "УК307",
                                "Иванова Анна Сергеевна", "08:30", "09:50")])

        bot.render_schedule_image(one(date(2026, 9, 7)))
        plan_without = bot._LAST_RENDER["plan"]
        self.assertEqual(self.progress_lines(), [])
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 1), "I", "08:30", "09:50",
            "Математика", duration=80,
        )
        bot.render_schedule_image(one(date(2026, 9, 8)))
        plan_with = bot._LAST_RENDER["plan"]
        self.assertTrue(self.progress_lines())
        for plan in (plan_without, plan_with):
            for op in plan["ops"]:
                if op.get("role") == "room":
                    self.assertGreaterEqual(op["size"], bot.ROOM_FONT_MIN)
            self.assertEqual(bot.validate_layout(plan), [])
        # карточка с прогрессом не стала теснее
        self.assertGreaterEqual(
            plan_with["cards"][0]["box"][3] - plan_with["cards"][0]["box"][1],
            plan_without["cards"][0]["box"][3] -
            plan_without["cards"][0]["box"][1] - 1)

    def test_progress_is_rendered_for_each_subgroup_and_pair(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 1), "I", "08:30", "09:50",
            "Информатика", duration=80,
        )
        schedule = bot.Schedule(
            date=date(2026, 9, 7), group=bot.GROUP_NAME,
            lessons=[
                lesson("I", "1", "Информатика", "УК307", "Иванова А.А.",
                       "08:30", "09:50"),
                lesson("I", "2", "Информатика", "ПК303", "Петров П.П.",
                       "08:30", "09:50"),
                lesson("II", None, "Информатика", "УК307", "Иванова А.А.",
                       "10:00", "11:20"),
            ],
        )
        bot.render_schedule_image(schedule)
        # У каждого из трёх блоков своя строка прогресса и своя аудитория
        self.assertGreaterEqual(len(self.progress_lines()), 2)
        self.assertEqual(len(ops_with("room")), 3)
        self.assertEqual(bot._LAST_RENDER["problems"], [])

    def test_total_minutes_sums_only_main_group(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 1), "I", "08:30", "09:50",
            "Математика", duration=80,
        )
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 2), "II", "10:00", "11:20",
            "Физика", duration=95,
        )
        # Чужая группа в сумму не попадает.
        bot.record_completed_lesson(
            "Другая-группа", date(2026, 9, 1), "I", "08:30", "09:50",
            "Химия", duration=999,
        )
        self.assertEqual(bot.load_total_study_minutes(), 175)
        self.assertEqual(bot.format_duration(175), "2 ч 55 мин")

    # --- бейдж шапки ---
    def test_badge_shown_on_group_image(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 1), "I", "08:30", "09:50",
            "Математика", duration=80,
        )
        schedule = bot.Schedule(
            date=date(2026, 9, 7), group=bot.GROUP_NAME,
            lessons=[lesson("I", None, "Химия Н и Г", "УК307",
                            "Арнаутова А.В.", "08:30", "09:50")],
        )
        path = bot.render_schedule_image(schedule)
        ops = self.header_progress_ops()
        self.assertIn("header:progress", [str(op["id"]) for op in ops])
        self.assertIn("header:bar", [str(op["id"]) for op in ops])
        plan = bot._LAST_RENDER["plan"]
        badge = next(op for op in ops if op["id"] == "header:progress")
        self.assertIn("Изучено", " ".join(badge["lines"]))
        zone = plan["owners"]["header"]
        for op in ops:
            box = op_box(op)
            self.assertGreaterEqual(box[0], zone[0])
            self.assertLessEqual(box[2], zone[2])
            self.assertLessEqual(box[3], zone[3])
            # в safe area
            self.assertLessEqual(box[2], plan["W"] - bot.SAFE_AREA + 1)
        # бейдж в шапке, а не наехал на карточки
        self.assertLess(badge["bbox"][3], plan["content_top"])
        with Image.open(path) as img:
            bar = next(op for op in ops if op["id"] == "header:bar")
            y = int((op_box(bar)[1] + op_box(bar)[3]) / 2)
            x = int((op_box(bar)[0] + op_box(bar)[2]) / 2)
            px = img.convert("RGB").load()
            self.assertTrue(close(px[x, y], self.PURPLE, 24),
                            "шкала прогресса не нарисована")

    def test_no_badge_without_history(self):
        schedule = bot.Schedule(
            date=date(2026, 9, 7), group=bot.GROUP_NAME,
            lessons=[lesson("I", None, "Химия Н и Г", "УК307",
                            "Арнаутова А.В.", "08:30", "09:50")],
        )
        bot.render_schedule_image(schedule)
        self.assertEqual(self.header_progress_ops(), [])
        # счётчик занятий при этом остаётся
        self.assertIn("1", rendered_text())

    def test_no_badge_on_staff_image(self):
        # История копится только для основной группы; на staff-картинке
        # бейдж не нужен даже при наличии записей в БД.
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 1), "I", "08:30", "09:50",
            "Математика", duration=80,
        )
        staff = bot.Schedule(
            date=date(2026, 9, 8), group="Степанов Сергей Владимирович",
            schedule_type="staff", staff_id=321,
            staff_name="Степанов Сергей Владимирович",
            lessons=[lesson("I", None, "Математика", "УК307", "—")],
        )
        bot.render_schedule_image(staff)
        self.assertEqual(self.header_progress_ops(), [])
        self.assertEqual(bot._LAST_RENDER["problems"], [])


class StudyDBTestCase(DBTestCase):
    """База тестов истории учёбы: чистые lesson_*-таблицы и bot_meta."""

    def setUp(self):
        super().setUp()
        self._clean_study_tables()

    def tearDown(self):
        self._clean_study_tables()
        super().tearDown()

    def _clean_study_tables(self):
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
            conn.execute("DELETE FROM subjects")
            conn.execute("DELETE FROM lesson_backfill_days")
            conn.execute("DELETE FROM bot_meta")
            conn.execute(
                "INSERT INTO bot_meta (key, value) VALUES ('schema_version', ?)",
                (str(bot.SCHEMA_VERSION),),
            )


class TestSubgroupStudyTime(StudyDBTestCase):
    """Время подгрупп: строка на подгруппу, общая сумма — пара один раз."""

    def record(self, schedule, current=None):
        with mock.patch.object(
            bot, "get_academic_year_start", return_value=date(2026, 9, 1)
        ):
            return bot.record_completed_lessons(
                schedule, current or datetime(2026, 9, 12, 8, 0)
            )

    def history_rows(self, pair=None, day=None):
        query = "SELECT * FROM lesson_history"
        conditions, params = [], []
        if day is not None:
            conditions.append("date = ?")
            params.append(day.isoformat())
        if pair is not None:
            conditions.append("pair_number = ?")
            params.append(pair)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY subgroup_key"
        with bot.db_connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def test_placeholder_subject_detection(self):
        self.assertTrue(bot.is_placeholder_subject("~.............."))
        self.assertTrue(bot.is_placeholder_subject("..........."))
        self.assertTrue(bot.is_placeholder_subject("---"))
        self.assertTrue(bot.is_placeholder_subject(""))
        self.assertTrue(bot.is_placeholder_subject(None))
        self.assertFalse(bot.is_placeholder_subject("НГПО"))
        self.assertFalse(bot.is_placeholder_subject("Ин.яз."))
        self.assertFalse(bot.is_placeholder_subject("Тек (под) рем"))

    def test_display_subject_text(self):
        # Отмена на сайте показывается словами, а не точками.
        self.assertEqual(
            bot.display_subject_text("~.............."), "Занятие отменено"
        )
        self.assertEqual(bot.display_subject_text("..."), "Занятие отменено")
        self.assertEqual(
            bot.display_subject_text(""), "Предмет не указан"
        )
        self.assertEqual(
            bot.display_subject_text(None), "Предмет не указан"
        )
        self.assertEqual(bot.display_subject_text("НГПО"), "НГПО")

    def test_fully_cancelled_pair_records_nothing(self):
        """Отмена у ВСЕХ подгрупп пары — пара не даёт времени группе."""
        schedule = study_day_schedule(date(2026, 9, 7), [
            ("II", "10:00", "11:20", [
                lesson("II", "1", "~..............", "—", "—"),
                lesson("II", "2", "~..............", "—", "—"),
            ]),
        ])
        self.record(schedule)
        self.assertEqual(self.history_rows(), [])
        self.assertEqual(bot.load_total_study_minutes(), 0)
        self.assertEqual(bot.load_subject_totals(), {})

    def test_cancelled_whole_pair_without_subgroups(self):
        """Карточка без подгрупп, но с отменой — тоже не пишется."""
        schedule = study_day_schedule(date(2026, 9, 7), [
            ("II", "10:00", "11:20",
             [lesson("II", None, "~..............", "—", "—")]),
        ])
        self.record(schedule)
        self.assertEqual(self.history_rows(), [])
        self.assertEqual(bot.load_total_study_minutes(), 0)

    def test_two_subgroups_same_subject_one_slot(self):
        schedule = study_day_schedule(date(2026, 9, 7), [
            ("II", "10:00", "11:20", [
                lesson("II", "1", "Ин.яз.", "ПК103", "Мурзабулатова Ф.Ф."),
                lesson("II", "2", "Ин.яз.", "ПК303", "Амирханова Г.А."),
            ]),
        ])
        inserted = self.record(schedule)
        self.assertEqual(inserted, 2)  # по строке на каждую подгруппу

        rows = self.history_rows(pair="II")
        self.assertEqual([r["subgroup_key"] for r in rows], ["1", "2"])
        self.assertEqual(
            [r["teacher"] for r in rows],
            ["Мурзабулатова Ф.Ф.", "Амирханова Г.А."],
        )
        self.assertEqual([r["room"] for r in rows], ["ПК103", "ПК303"])

        # Общая сумма группы: пара считается ОДИН раз (80, а не 160).
        self.assertEqual(bot.load_total_study_minutes(), 80)
        # Предмет тоже не удваивается от двух подгрупп.
        self.assertEqual(bot.load_subject_totals(), {"ин.яз": 80})

    def test_different_subjects_get_own_time(self):
        schedule = study_day_schedule(date(2026, 9, 7), [
            ("II", "10:00", "11:20", [
                lesson("II", "1", "Ин.яз.", "ПК103", "Мурзабулатова Ф.Ф."),
                lesson("II", "2", "Нем.яз.", "ПК303", "Амирханова Г.А."),
            ]),
        ])
        self.record(schedule)
        # Общая сумма — по-прежнему один слот пары.
        self.assertEqual(bot.load_total_study_minutes(), 80)
        # Но каждая подгруппа засчитала время своему предмету.
        totals = bot.load_subject_totals()
        self.assertEqual(totals.get("ин.яз"), 80)
        self.assertEqual(totals.get("нем.яз"), 80)

    def test_placeholder_subgroup_not_recorded(self):
        schedule = study_day_schedule(date(2026, 9, 11), [
            ("IV", "13:25", "14:45", [
                lesson("IV", "1", "Ин.яз в проф", "ПК103",
                       "Мурзабулатова Ф.Ф."),
                lesson("IV", "2", "~..............", "—", "—"),
            ]),
        ])
        self.record(schedule)
        rows = self.history_rows(pair="IV")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subgroup_key"], "1")
        self.assertEqual(rows[0]["subject"], "Ин.яз в проф")
        # Время пары не потерялось и мусорный «предмет» не накопился.
        self.assertEqual(bot.load_total_study_minutes(), 80)
        self.assertNotIn("~..............", bot.load_subject_totals())
        self.assertEqual(
            bot.get_subject_progress("~.............."), ""
        )

    def test_record_is_idempotent(self):
        schedule = study_day_schedule(date(2026, 9, 7), [
            ("II", "10:00", "11:20", [
                lesson("II", "1", "Ин.яз.", "ПК103"),
                lesson("II", "2", "Ин.яз.", "ПК303"),
            ]),
        ])
        self.assertEqual(self.record(schedule), 2)
        self.assertEqual(self.record(schedule), 0)  # повтор — без дублей
        self.assertEqual(len(self.history_rows()), 2)
        self.assertEqual(bot.load_total_study_minutes(), 80)

    def test_staff_schedule_not_recorded(self):
        staff = bot.Schedule(
            date=date(2026, 9, 7),
            group="Степанов Сергей Владимирович",
            schedule_type="staff",
            staff_id=321,
            lessons=[lesson("II", "1", "НГПО")],
        )
        self.assertEqual(self.record(staff), 0)
        self.assertEqual(self.history_rows(), [])

    def test_realistic_placeholder_html_from_site(self):
        """Реальная структура 11.09.2026: у 2-й подгруппы пара ОТМЕНЕНА.

        Сайт рисует «~..............» — занятия у подгруппы нет, она
        свободна. В историю такая подгруппа не попадает, но время пары
        для остальной группы не теряется.
        """
        html = """
        <html><body>
          <div class="card myCard">
            <div class="card-header">
              <span class="h3">IV</span> пара
              <span class="pl-2 h4">13<sup>25</sup> - 14<sup>45</sup></span>
              <span class="pl-1">перемена 10 мин</span>
            </div>
            <div class="card-body p-0">
              <div class="d-flex flex-column subGroup1">
                <span>1</span> п/гр.
                <span>ауд.<span class="h5">ПК103</span></span>
                <span class="Staff">Мурзабулатова Ф.Ф.</span>
                <div class="d-md-none text-center text-truncate">Ин.яз в проф</div>
                <div class="d-none d-md-block"><b>Ин.яз в проф</b>
                  Иностранный язык в профессиональной деятельности</div>
              </div>
              <div class="d-flex flex-column subGroup2">
                <span>2</span> п/гр.
                <span>ауд.</span>
                <span class="Staff"></span>
                <div class="d-md-none text-center text-truncate">~..............</div>
                <div class="d-none d-md-block">...................................</div>
              </div>
            </div>
          </div>
        </body></html>
        """
        schedule = bot.parse_schedule(html, date(2026, 9, 11))
        pair_iv = [p for p in schedule.pairs if p.number == "IV"]
        self.assertEqual(len(pair_iv), 1)
        self.assertEqual(len(pair_iv[0].lessons), 2)
        by_sub = {
            bot.clean_text(x.subgroup): x for x in pair_iv[0].lessons
        }
        self.assertEqual(by_sub["1"].subject, "Ин.яз в проф")
        self.assertTrue(bot.is_placeholder_subject(by_sub["2"].subject))

        self.record(schedule)
        rows = self.history_rows(pair="IV")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Ин.яз в проф")
        self.assertEqual(bot.load_total_study_minutes(), 80)

    def test_real_september_totals(self):
        """Итог 1-11 сентября по реальным данным сайта — 44 ч 20 мин."""
        for day, pairs in site_september_2026().items():
            self.record(study_day_schedule(day, pairs))

        self.assertEqual(bot.load_total_study_minutes(), 2660)
        self.assertEqual(bot.format_duration(2660), "44 ч 20 мин")
        # 9 дней с занятиями (суббота 5 сентября — без пар).
        self.assertEqual(
            bot.count_active_study_days(
                bot.GROUP_NAME, date(2026, 9, 1), date(2026, 9, 11)
            ),
            9,
        )
        # 35 строк: 33 обычных пары + 2 подгруппы пары II 7 сентября
        # (заглушка 11 сентября не пишется).
        self.assertEqual(len(self.history_rows()), 35)
        # Химия Н и Г: 7.09 (80) + 11.09 (80 + 80) = 240 минут.
        totals = bot.load_subject_totals()
        self.assertEqual(totals["химия н и г"], 240)


class TestStudyForecast(StudyDBTestCase):
    """Прогноз «сколько осталось учиться» с 1 сентября по 30 июня."""

    def test_academic_year_bounds(self):
        self.assertEqual(
            bot.get_academic_year_start(date(2026, 9, 1)), date(2026, 9, 1)
        )
        self.assertEqual(
            bot.get_academic_year_end(date(2026, 9, 11)), date(2027, 6, 30)
        )
        self.assertEqual(
            bot.get_academic_year_end(date(2027, 2, 10)), date(2027, 6, 30)
        )
        self.assertEqual(
            bot.get_academic_year_start(date(2027, 7, 1)), date(2026, 9, 1)
        )

    def test_count_study_days(self):
        # 7-13 сентября 2026: 7 дней минус воскресенье 13-го.
        self.assertEqual(
            bot.count_study_days(date(2026, 9, 7), date(2026, 9, 13)), 6
        )
        self.assertEqual(
            bot.count_study_days(date(2026, 9, 13), date(2026, 9, 13)), 0
        )
        self.assertEqual(
            bot.count_study_days(date(2026, 9, 14), date(2026, 9, 13)), 0
        )

    def test_forecast_math_and_badge(self):
        minutes_by_day = {
            date(2026, 9, 1): 180, date(2026, 9, 2): 320,
            date(2026, 9, 3): 400, date(2026, 9, 4): 320,
            date(2026, 9, 7): 320, date(2026, 9, 8): 240,
            date(2026, 9, 9): 240, date(2026, 9, 10): 320,
            date(2026, 9, 11): 320,
        }
        for day, minutes in minutes_by_day.items():
            bot.record_completed_lesson(
                bot.GROUP_NAME, day, "I", "08:30", "09:50", "НГПО",
                duration=minutes,
            )

        forecast = bot.get_study_forecast(today=date(2026, 9, 11))
        self.assertEqual(forecast.studied_minutes, 2660)
        self.assertEqual(forecast.elapsed_study_days, 10)   # без воскресенья
        self.assertEqual(forecast.active_days, 9)           # суббота пустая

        expected_remaining_days = sum(
            1
            for i in range((date(2027, 6, 30) - date(2026, 9, 12)).days + 1)
            if (date(2026, 9, 12) + timedelta(days=i)).weekday() != 6
        )
        self.assertEqual(forecast.remaining_study_days, expected_remaining_days)

        # R = X * Dr / De, Y = X + R.
        expected_remaining = round(2660 * expected_remaining_days / 10)
        self.assertEqual(forecast.remaining_minutes, expected_remaining)
        self.assertEqual(forecast.total_minutes, 2660 + expected_remaining)
        self.assertAlmostEqual(
            forecast.pace_minutes_per_day, 266.0, places=6
        )

        # Бейдж «Изучено: X / Y акад. ч» (академический час = 40 мин):
        # 2660 мин = 66,5 акад. ч; Y = (2660 + R) / 40.
        self.assertEqual(
            bot.study_badge_text(forecast),
            f"Изучено: 66,5 / "
            f"{bot._format_academic_units((2660 + expected_remaining) / 40)}"
            " акад. ч",
        )

    def test_no_history_no_forecast(self):
        forecast = bot.get_study_forecast(today=date(2026, 9, 11))
        self.assertEqual(forecast.studied_minutes, 0)
        self.assertIsNone(forecast.remaining_minutes)
        self.assertEqual(forecast.total_minutes, 0)
        self.assertEqual(bot.study_badge_text(forecast), "Изучено: 0 акад. ч")
        # Без истории сноска не формируется.
        self.assertEqual(bot.study_note_lines(forecast), [])

    def test_year_finished_nothing_remains(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2027, 6, 1), "I", "08:30", "09:50",
            "НГПО", duration=80,
        )
        forecast = bot.get_study_forecast(today=date(2027, 7, 5))
        self.assertEqual(forecast.remaining_study_days, 0)
        self.assertEqual(forecast.remaining_minutes, 0)
        self.assertEqual(forecast.total_minutes, forecast.studied_minutes)
        # Учебный год закончился — бейдж без прогнозной части.
        self.assertEqual(bot.study_badge_text(forecast), "Изучено: 2 акад. ч")
        # В сноске — только факт, без «/ Y».
        self.assertEqual(
            bot.study_note_lines(forecast),
            [
                "Время считается по академическому часу: 1 акад. ч = 40 мин.",
                "По обыкновенному времени: 1 ч 20 мин",
            ],
        )


class TestAcademicHours(StudyDBTestCase):
    """Академический час (40 минут) в бейдже, сноске и подписях предметов."""

    def test_synthetic_italic_slant(self):
        """Курсивная сноска: верх штрихов сдвинут вправо (~12°)."""
        font = bot.get_font(30)
        img = Image.new("RGB", (600, 100), (255, 255, 255))
        bot._draw_italic_text(
            img, (100, 30), "HHHHH", font, (100, 116, 139)
        )
        px = img.load()
        rows = {}
        for y in range(100):
            xs = [
                x for x in range(600)
                if px[x, y] == (100, 116, 139)
            ]
            if xs:
                rows[y] = (min(xs), max(xs))
        self.assertTrue(rows, "курсивный текст не нарисован")
        ys = sorted(rows)
        top_y, bottom_y = ys[0], ys[-1]
        height = bottom_y - top_y
        shift = rows[top_y][0] - rows[bottom_y][0]
        # Наклон = shear * высота: 0.21 * ~21px ≈ 4-5px.
        self.assertGreater(height, 10)
        self.assertGreater(shift, 0.21 * height * 0.5)
        self.assertLess(shift, 0.21 * height * 1.5)

    def test_format_academic_hours(self):
        cases = {
            0: "0 акад. ч",
            20: "0,5 акад. ч",
            30: "0,75 акад. ч",
            40: "1 акад. ч",
            60: "1,5 акад. ч",
            80: "2 акад. ч",
            90: "2,25 акад. ч",
            95: "2,38 акад. ч",
            160: "4 акад. ч",
            2660: "66,5 акад. ч",
            69160: "1729 акад. ч",
        }
        for minutes, expected in cases.items():
            self.assertEqual(
                bot.format_academic_hours(minutes), expected,
                f"неверный перевод {minutes} минут",
            )

    def test_academic_hour_constant(self):
        self.assertEqual(bot.ACADEMIC_HOUR_MINUTES, 40)

    def test_note_lines_with_regular_time(self):
        minutes_by_day = {date(2026, 9, 1): 180, date(2026, 9, 2): 320}
        for day, minutes in minutes_by_day.items():
            bot.record_completed_lesson(
                bot.GROUP_NAME, day, "I", "08:30", "09:50", "НГПО",
                duration=minutes,
            )
        forecast = bot.get_study_forecast(today=date(2026, 9, 2))
        self.assertEqual(forecast.studied_minutes, 500)

        lines = bot.study_note_lines(forecast)
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            lines[0],
            "Время считается по академическому часу: 1 акад. ч = 40 мин.",
        )
        # Обыкновенное время — в формате «X / Y» с прогнозом.
        self.assertTrue(lines[1].startswith("По обыкновенному времени: 8 ч 20 мин / "))
        self.assertEqual(
            bot.format_academic_hours(forecast.studied_minutes), "12,5 акад. ч"
        )

    def test_subject_progress_in_academic_hours(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 7), "I", "08:30", "09:50",
            "НГПО", duration=160,
        )
        self.assertEqual(
            bot.get_subject_progress("НГПО"), "Изучено: 4 акад. ч"
        )

    def footer_note_ops(self):
        """Строки сноски подвала из последнего рендера."""
        plan = bot._LAST_RENDER["plan"]
        return [op for op in plan["ops"]
                if str(op.get("id")).startswith("footer:note")]

    def test_note_rendered_on_schedule_image(self):
        """Серая сноска — последний элемент под карточками, по центру."""
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 7), "I", "08:30", "09:50",
            "НГПО", duration=320,
        )
        schedule = bot.Schedule(
            date=date(2026, 9, 7), group=bot.GROUP_NAME,
            lessons=[lesson("I", None, "НГПО", "ПК217", "Степанов С.В.",
                            "08:30", "09:50")],
        )
        path = bot.render_schedule_image(schedule)
        notes = self.footer_note_ops()
        self.assertGreaterEqual(len(notes), 2)
        self.assertIn("40 мин", " ".join(" ".join(op["lines"])
                                        for op in notes))
        plan = bot._LAST_RENDER["plan"]
        last_card = max(c["box"][3] for c in plan["cards"])
        for op in notes:
            self.assertGreater(op["bbox"][1], last_card,
                               "сноска наехалa на карточки")
            self.assertLessEqual(op["bbox"][3], plan["H"] - bot.SAFE_AREA + 1)
            self.assertGreaterEqual(op["bbox"][0], bot.SAFE_AREA - 1)
            self.assertLessEqual(op["bbox"][2], plan["W"] - bot.SAFE_AREA + 1)
            self.assertEqual(op["fill"], bot.S_MUTED)
        with Image.open(path) as img:
            px = img.convert("RGB").load()
            y = int(notes[0]["bbox"][1] + 1)
            self.assertTrue(any(close(px[x, y], hex_rgb(bot.S_MUTED), 6)
                               for x in range(60, img.width - 60, 2)),
                            "сноска про академический час не найдена")

    def test_note_appears_in_footer_with_history(self):
        """Сноска появляется в подвале только при наличии истории.

        Формат картинки фиксированный: история не меняет высоту холста,
        а добавляет сноску в подвал.
        """
        lessons = [
            lesson("I", None, "НГПО", "ПК217", "Степанов С.В.",
                   "08:30", "09:50"),
            lesson("II", None, "Физ-ра", "бол зал 2", "Кинзябаев А.И.",
                   "10:00", "11:20"),
        ]
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 7), "I", "08:30", "09:50",
            "НГПО", duration=320,
        )
        bot.render_schedule_image(bot.Schedule(date=date(2026, 9, 7),
                                              group=bot.GROUP_NAME,
                                              lessons=lessons))
        plan_history = bot._LAST_RENDER["plan"]
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
        bot.render_schedule_image(bot.Schedule(date=date(2026, 9, 8),
                                              group=bot.GROUP_NAME,
                                              lessons=lessons))
        plan_empty = bot._LAST_RENDER["plan"]

        self.assertEqual(plan_history["H"], plan_empty["H"])
        self.assertGreaterEqual(len(self.footer_note_ops()), 0)
        self.assertEqual(self.footer_note_ops(), [],
                        "сноска без истории не должна отображаться")
        self.assertEqual(len(plan_history["cards"]), 2)
        self.assertEqual(len(plan_empty["cards"]), 2)
        self.assertEqual(bot.validate_layout(plan_history), [])
        self.assertEqual(bot.validate_layout(plan_empty), [])
        # карточки — вертикальный список: вторая начинается ниже первой
        boxes = [c["box"] for c in plan_empty["cards"]]
        self.assertLessEqual(boxes[0][3], boxes[1][1])

    def test_note_absent_on_staff_image(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 7), "I", "08:30", "09:50",
            "НГПО", duration=320,
        )
        staff = bot.Schedule(
            date=date(2026, 9, 8), group="Степанов Сергей Владимирович",
            schedule_type="staff", staff_id=321,
            staff_name="Степанов Сергей Владимирович",
            lessons=[lesson("I", None, "НГПО", "ПК217", "—")],
        )
        bot.render_schedule_image(staff)
        self.assertEqual(self.footer_note_ops(), [])
        self.assertEqual(bot._LAST_RENDER["problems"], [])


class TestFooterSignatureRemoved(StudyDBTestCase):
    """Подписи «ИНК · расписание» нет ни на расписании, ни на /status.

    Сноска про академический час остаётся последним нарисованным
    элементом картинки, снизу — только нижний padding.
    """

    FOOTER_GRAY = (152, 161, 176)   # бывший COL_FOOTER («#98A1B0»)
    MUTED = hex_rgb(bot.S_MUTED)    # сноска в подвале
    BG = hex_rgb(bot.S_BG)          # фон картинки расписания
    STATUS_BG = (243, 245, 250)     # COL_BG (картинка /status)

    def setUp(self):
        super().setUp()
        # История нужна, чтобы на картинке была сноска про акад. час.
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 7), "I", "08:30", "09:50",
            "НГПО", duration=320,
        )

    def footer_pixels(self, path):
        """Координаты пикселей цвета подписи подвала."""
        with Image.open(path) as img:
            im = img.convert("RGB")
            px = im.load()
            return [
                (x, y)
                for y in range(im.height)
                for x in range(im.width)
                if px[x, y] == self.FOOTER_GRAY
            ]

    def note_rows(self, path, color=None):
        """Строки сноски (по умолчанию S_MUTED), точность ±6: глифы
        FreeType имеют пиксели частичного покрытия."""
        color = color or self.MUTED
        with Image.open(path) as img:
            im = img.convert("RGB")
            px = im.load()
            return [
                y for y in range(im.height)
                if any(abs(px[x, y][0] - color[0]) <= 6
                       and abs(px[x, y][1] - color[1]) <= 6
                       and abs(px[x, y][2] - color[2]) <= 6
                       for x in range(60, im.width - 60, 2))
            ]

    def drawn_rows(self, path, bg=None):
        """Строки, где есть хоть один пиксель не цвета фона."""
        bg = bg or self.BG
        with Image.open(path) as img:
            im = img.convert("RGB")
            px = im.load()
            return [
                y for y in range(im.height)
                if any(px[x, y] != bg
                       for x in range(20, im.width - 20, 4))
            ]

    def test_footer_color_is_gone_from_module(self):
        """Цвета подвала в палитре больше нет — рисовать его нечем."""
        self.assertFalse(hasattr(bot, "COL_FOOTER"))

    def test_schedule_image_has_no_footer_signature(self):
        schedule = bot.Schedule(
            date=date(2026, 9, 7), group=bot.GROUP_NAME,
            lessons=[lesson("I", None, "НГПО", "ПК217", "Степанов С.В.",
                            "08:30", "09:50")],
        )
        path = bot.render_schedule_image(schedule)
        self.assertEqual(
            self.footer_pixels(path), [],
            "на картинке расписания осталась подпись «ИНК · расписание»",
        )
        # Сноска про академический час на месте и стала последним
        # нарисованным элементом: снизу остаётся нижний padding.
        note = self.note_rows(path)
        self.assertTrue(note, "сноска про академический час исчезла")
        # ниже сноски — только нижний отступ светлой плашки подвала
        # (внутренний отступ плашки + safe area до края холста)
        self.assertLessEqual(max(self.drawn_rows(path)) - max(note),
                             bot.SAFE_AREA + 34,
                             "под сноской нарисовано что-то ещё")
        self.assertGreaterEqual(max(note) - min(note), 1)
        with Image.open(path) as img:
            height, width = img.height, img.width
        self.assertGreaterEqual(height - 1 - max(note), bot.SAFE_AREA - 10,
                                "сноска прижата к нижнему краю")
        self.assertLessEqual(height - 1 - max(note), bot.SAFE_AREA + 46)
        # сноски — в плашке подвала, внутри safe area
        plan = bot._LAST_RENDER["plan"]
        notes = [op for op in plan["ops"]
                 if str(op.get("id")).startswith("footer:note")]
        self.assertTrue(notes)
        panel = [op for op in plan["ops"] if op.get("id") == "footer:panel"]
        self.assertTrue(panel, "нет плашки подвала")
        panel_box = panel[0]["box"]
        self.assertGreaterEqual(panel_box[0], bot.SAFE_AREA - 1)
        self.assertLessEqual(panel_box[2], width - bot.SAFE_AREA + 1)
        for op in notes:
            self.assertGreaterEqual(op["bbox"][0], panel_box[0])
            self.assertLessEqual(op["bbox"][2], panel_box[2] + 1)
            self.assertGreater(op["bbox"][1], panel_box[1])
            self.assertLess(op["bbox"][3], panel_box[3])
        self.assertEqual(bot._LAST_RENDER["problems"], [])

    def test_status_image_has_no_footer_signature(self):
        path = bot.render_status_image(chat_id=987654)
        try:
            self.assertEqual(
                self.footer_pixels(path), [],
                "на картинке /status осталась подпись «ИНК · расписание»",
            )
            note = self.note_rows(path, color=(100, 116, 139))
            self.assertTrue(note, "сноска про академический час исчезла")
            # Сночка — последний элемент: хвосты скошенных глифов могут
            # выступать не более чем на пару пикселей.
            self.assertLessEqual(
                max(self.drawn_rows(path, bg=self.STATUS_BG)) - max(note), 2
            )
            with Image.open(path) as img:
                height = img.height
            self.assertGreaterEqual(height - 1 - max(note), 20)
            self.assertLessEqual(height - 1 - max(note), 60)
        finally:
            path.unlink(missing_ok=True)


class TestHistoryMigration(unittest.TestCase):
    """Миграция lesson_history v1 -> v2 и разовый пересчёт истории."""

    LESSON_HISTORY_V1 = """
        CREATE TABLE lesson_history (
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
    LESSON_HISTORY_V2 = """
        CREATE TABLE lesson_history (
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
    BACKFILL_DAYS = """
        CREATE TABLE lesson_backfill_days (
            group_name   TEXT NOT NULL,
            date         TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (group_name, date)
        )
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "old.db"

    def tearDown(self):
        self._tmp.cleanup()

    def connect(self, lesson_schema):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(lesson_schema)
        conn.execute(self.BACKFILL_DAYS)
        return conn

    def insert_row(self, conn, day, pair="I", subject="НГПО", minutes=80):
        conn.execute(
            "INSERT INTO lesson_history"
            " (group_name, date, pair_number, start_time, end_time, subject,"
            "  normalized_subject, duration_minutes, subgroup_info, teacher,"
            "  room, completed_at, created_at)"
            " VALUES (?, ?, ?, '08:30', '09:50', ?, ?, ?, '1, 2', '', '',"
            " '2026-09-01', '2026-09-01')",
            (bot.GROUP_NAME, day.isoformat(), pair, subject,
             bot.normalize_subject_name(subject), minutes),
        )

    def mark_processed(self, conn, day):
        conn.execute(
            "INSERT INTO lesson_backfill_days VALUES (?, ?, 'x')",
            (bot.GROUP_NAME, day.isoformat()),
        )

    def test_v1_migrates_and_schedules_recalc(self):
        start = bot.get_academic_year_start()
        prev_year_day = start - timedelta(days=1)

        conn = self.connect(self.LESSON_HISTORY_V1)
        self.insert_row(conn, prev_year_day, pair="I")   # прошлый год — остаётся
        self.insert_row(conn, start, pair="II")          # текущий — пересчёт
        self.mark_processed(conn, prev_year_day)
        self.mark_processed(conn, start)
        conn.commit()

        bot._migrate_lesson_history(conn)
        conn.commit()

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(lesson_history)")
        }
        self.assertIn("subgroup_key", columns)

        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT date, subgroup_key FROM lesson_history"
            )
        ]
        self.assertEqual([r["date"] for r in rows], [prev_year_day.isoformat()])
        self.assertEqual(rows[0]["subgroup_key"], "")

        markers = [
            row["date"]
            for row in conn.execute("SELECT date FROM lesson_backfill_days")
        ]
        self.assertEqual(markers, [prev_year_day.isoformat()])

        meta = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM bot_meta")
        }
        self.assertEqual(meta.get("schema_version"), str(bot.SCHEMA_VERSION))
        self.assertEqual(meta.get("study_recalc_from"), start.isoformat())
        conn.close()

        # Повторная миграция уже мигрированной базы — no-op: строки на месте,
        # флаг пересчёта не трогается (его снимает recalculate_study_history).
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        bot._migrate_lesson_history(conn)
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM lesson_history").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(
            bot._meta_get(conn, "study_recalc_from", ""),
            start.isoformat(),
        )
        conn.close()

    def test_fresh_v2_schema_sets_version_without_recalc(self):
        start = bot.get_academic_year_start()
        conn = self.connect(self.LESSON_HISTORY_V2)
        self.insert_row(conn, start, pair="I", subject="НГПО")
        self.mark_processed(conn, start)
        bot._ensure_meta_table(conn)
        bot._meta_set(conn, "schema_version", str(bot.SCHEMA_VERSION))
        conn.commit()

        bot._migrate_lesson_history(conn)

        count = conn.execute("SELECT COUNT(*) FROM lesson_history").fetchone()[0]
        self.assertEqual(count, 1)
        markers = conn.execute(
            "SELECT COUNT(*) FROM lesson_backfill_days"
        ).fetchone()[0]
        self.assertEqual(markers, 1)
        flag = bot._meta_get(conn, "study_recalc_from", "")
        self.assertEqual(flag, "")
        conn.close()


class TestDeployMigrationEndToEnd(unittest.TestCase):
    """Продакшен-сценарий обновления: v1-база -> init_db -> пересчёт.

    Именно это произойдёт на сервере при деплое новой версии: старая база
    с записями «представителем» пары мигрирует, дни текущего года
    сбрасываются и пересчитываются по подгруппам.
    """

    def test_v1_db_recalculates_after_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bot.db"
            start = bot.get_academic_year_start()

            # --- «старая» база: v1-схема, строки за 4 дня, метки backfill ---
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.execute(TestHistoryMigration.LESSON_HISTORY_V1)
            conn.execute(TestHistoryMigration.BACKFILL_DAYS)
            for offset, pairs in ((0, ("I", "II")), (1, ("I",)),
                                  (2, ()), (3, ("I",))):
                day = start + timedelta(days=offset)
                for pair in pairs:
                    conn.execute(
                        "INSERT INTO lesson_history"
                        " (group_name, date, pair_number, start_time,"
                        "  end_time, subject, normalized_subject,"
                        "  duration_minutes, subgroup_info, teacher, room,"
                        "  completed_at, created_at)"
                        " VALUES (?, ?, ?, '08:30', '09:50', 'Ин.яз.',"
                        " 'ин.яз', 80, '1, 2', '', '', 'x', 'x')",
                        (bot.GROUP_NAME, day.isoformat(), pair),
                    )
                if pairs:
                    conn.execute(
                        "INSERT INTO lesson_backfill_days VALUES (?, ?, 'x')",
                        (bot.GROUP_NAME, day.isoformat()),
                    )
            conn.commit()
            conn.close()

            # --- деплой: init_db мигрирует схему и ставит флаг пересчёта ---
            with mock.patch.object(bot, "DB_PATH", db_path):
                bot.init_db()
                self.assertEqual(
                    bot.get_meta_value("study_recalc_from"),
                    start.isoformat(),
                )

                # Новое расписание тех же дней: пара II 1-го дня теперь
                # честно разделена по подгруппам с разными предметами.
                def sub_pair(day, pair, s1, s2):
                    return study_day_schedule(day, [(
                        pair, "10:00", "11:20", [
                            lesson(pair, "1", s1, "ПК103"),
                            lesson(pair, "2", s2, "ПК303"),
                        ],
                    )])

                schedules = {
                    start: None, start + timedelta(days=1): None,
                    start + timedelta(days=2): None,
                    start + timedelta(days=3): None,
                    start + timedelta(days=4): None,
                }
                schedules[start] = study_day_schedule(start, [
                    ("I", "08:30", "09:50",
                     [lesson("I", None, "Химия Н и Г", "УК307")]),
                ])
                # Вторая пара первого дня — подгруппы с разными предметами.
                schedules[start] = bot.Schedule(
                    date=start,
                    group=bot.GROUP_NAME,
                    lessons=(
                        schedules[start].lessons
                        + sub_pair(start, "II", "Ин.яз.", "Нем.яз.").lessons
                    ),
                )
                schedules[start + timedelta(days=1)] = study_day_schedule(
                    start + timedelta(days=1),
                    [("I", "08:30", "09:50",
                      [lesson("I", None, "НГПО", "ПК217")])],
                )
                # день 2 — без занятий; день 3 — пара; день 4 (сегодня) — пара.
                schedules[start + timedelta(days=3)] = study_day_schedule(
                    start + timedelta(days=3),
                    [("I", "08:30", "09:50",
                      [lesson("I", None, "НГПО", "ПК217")])],
                )
                schedules[start + timedelta(days=4)] = study_day_schedule(
                    start + timedelta(days=4),
                    [("I", "08:30", "09:50",
                      [lesson("I", None, "НГПО", "ПК217")])],
                )

                async def fake_get_schedule(day):
                    return schedules.get(day) or bot.Schedule(
                        date=day, group=bot.GROUP_NAME, lessons=[]
                    )

                today = start + timedelta(days=4)
                with mock.patch.object(bot, "get_schedule",
                                       fake_get_schedule), \
                     mock.patch.object(bot, "get_today",
                                       return_value=today), \
                     mock.patch.object(bot, "now_local",
                                       return_value=datetime(
                                           today.year, today.month,
                                           today.day, 20, 0)), \
                     mock.patch.object(bot, "get_academic_year_start",
                                       return_value=start):
                    inserted = run(bot.recalculate_study_history())

                # 6 строк: пара I + 2 подгруппы пары II (день 0) и по паре
                # в дни 1, 3, 4.
                self.assertEqual(inserted, 6)
                self.assertEqual(
                    bot.get_meta_value("study_recalc_from"), ""
                )

                # Общая сумма: 5 слотов пар по 80 минут (подгруппы пары II
                # не удваивают время группы).
                self.assertEqual(bot.load_total_study_minutes(), 400)

                # Но каждый предмет подгруппы получил своё время.
                totals = bot.load_subject_totals()
                self.assertEqual(totals["ин.яз"], 80)
                self.assertEqual(totals["нем.яз"], 80)
                self.assertEqual(totals["химия н и г"], 80)
                self.assertEqual(totals["нгпо"], 240)

                # Все 5 дней отмечены обработанными.
                with bot.db_connect() as conn:
                    days = conn.execute(
                        "SELECT COUNT(*) AS n FROM lesson_backfill_days"
                    ).fetchone()["n"]
                self.assertEqual(days, 5)


class TestStudyRecalc(StudyDBTestCase):
    """Разовый пересчёт изученного времени 1-11 сентября."""

    def test_recalculate_study_history_from_scratch(self):
        # Имитируем состояние после миграции: флаг пересчёта с 1 сентября.
        bot.set_meta_value("study_recalc_from", "2026-09-01")

        schedules = {
            day: study_day_schedule(day, pairs)
            for day, pairs in site_september_2026().items()
        }

        async def fake_get_schedule(day):
            return schedules.get(
                day, bot.Schedule(date=day, group=bot.GROUP_NAME, lessons=[])
            )

        with mock.patch.object(bot, "get_schedule", fake_get_schedule), \
             mock.patch.object(bot, "get_today",
                               return_value=date(2026, 9, 11)), \
             mock.patch.object(bot, "now_local",
                               return_value=datetime(2026, 9, 11, 20, 0)), \
             mock.patch.object(bot, "get_academic_year_start",
                               return_value=date(2026, 9, 1)):
            inserted = run(bot.recalculate_study_history())

        # 35 записей (33 пары + 2 подгруппы; заглушка не пишется).
        self.assertEqual(inserted, 35)
        # Флаг снят.
        self.assertEqual(bot.get_meta_value("study_recalc_from"), "")

        # Итог по реальным данным сайта: 44 ч 20 мин.
        self.assertEqual(bot.load_total_study_minutes(), 2660)

        # Пара с подгруппами записана двумя строками.
        with bot.db_connect() as conn:
            keys = [
                row["subgroup_key"]
                for row in conn.execute(
                    "SELECT subgroup_key FROM lesson_history"
                    " WHERE date = '2026-09-07' AND pair_number = 'II'"
                )
            ]
            days = conn.execute(
                "SELECT COUNT(*) AS n FROM lesson_backfill_days"
            ).fetchone()["n"]
        self.assertEqual(sorted(keys), ["1", "2"])
        # Все 11 дней отмечены обработанными.
        self.assertEqual(days, 11)

        # Повторный запуск без флага не делает ничего.
        calls = {"backfill": 0}

        async def fake_backfill(*args, **kwargs):
            calls["backfill"] += 1
            return 0

        with mock.patch.object(bot, "backfill_lesson_history", fake_backfill):
            self.assertEqual(run(bot.recalculate_study_history()), 0)
        self.assertEqual(calls["backfill"], 0)

    def test_recalc_survives_source_error(self):
        """Ошибка источника не снимает день с пересчёта окончательно."""
        bot.set_meta_value("study_recalc_from", "2026-09-01")

        async def failing_get_schedule(day):
            raise bot.ScheduleUnavailable("сайт недоступен")

        with mock.patch.object(bot, "get_schedule", failing_get_schedule), \
             mock.patch.object(bot, "get_today",
                               return_value=date(2026, 9, 11)):
            inserted = run(bot.recalculate_study_history())

        self.assertEqual(inserted, 0)
        self.assertEqual(bot.get_meta_value("study_recalc_from"), "")
        with bot.db_connect() as conn:
            days = conn.execute(
                "SELECT COUNT(*) AS n FROM lesson_backfill_days"
            ).fetchone()["n"]
        self.assertEqual(days, 0)  # дни не отмечены — обычный backfill доберёт


class TestStatusImage(DBTestCase):
    """Картинка /status в дизайне расписания + текстовый fallback."""

    ACCENT = (79, 70, 229)        # #4F46E5
    GREEN_LIGHT = (229, 246, 237)  # #E5F6ED

    def setUp(self):
        super().setUp()
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
            conn.execute("DELETE FROM subjects")

    def tearDown(self):
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
            conn.execute("DELETE FROM subjects")
        super().tearDown()

    class FakeStatusMessage:
        def __init__(self, chat_id=987654, fail_photo=False):
            self.chat = type("Chat", (), {"id": chat_id})()
            self.photos = []
            self.answers = []
            self.fail_photo = fail_photo

        async def answer_photo(self, photo, caption=None, **kwargs):
            if self.fail_photo:
                raise RuntimeError("фото не ушло")
            self.photos.append((photo, caption))

        async def answer(self, text, *args, **kwargs):
            self.answers.append(text)

    def test_format_uptime(self):
        self.assertEqual(bot.format_uptime(45), "45 с")
        self.assertEqual(bot.format_uptime(5 * 60), "5 мин")
        self.assertEqual(bot.format_uptime(3 * 3600 + 5 * 60), "3 ч 05 мин")
        self.assertEqual(bot.format_uptime(2 * 86400 + 4 * 3600), "2 д 4 ч")

    def test_renders_png_in_schedule_style(self):
        bot.record_completed_lesson(
            bot.GROUP_NAME, date(2026, 9, 7), "I", "08:30", "09:50",
            "НГПО", duration=320,
        )
        path = bot.render_status_image(chat_id=987654)
        try:
            self.assertTrue(path.exists())
            with Image.open(path) as img:
                im = img.convert("RGB")
                self.assertEqual(im.width, bot.IMAGE_WIDTH)
                self.assertGreater(im.height, 600)
                self.assertLess(im.height, 3500)
                px = im.load()
                # Белая шапка и акцентная полоса — как у расписания.
                self.assertEqual(px[600, 10], (255, 255, 255))
                self.assertEqual(px[5, 100], self.ACCENT)
                # Зелёный бейдж «РАБОТАЕТ» в правом верхнем углу.
                chip_rows = [
                    y for y in range(40, 120)
                    if any(px[x, y] == self.GREEN_LIGHT
                           for x in range(600, im.width - 40, 4))
                ]
                self.assertTrue(chip_rows, "бейдж РАБОТАЕТ не найден")
                # Карточки (белые блоки) ниже шапки.
                card_rows = [
                    y for y in range(300, im.height)
                    if any(px[x, y] == (255, 255, 255)
                           for x in range(200, im.width - 200, 8))
                ]
                self.assertTrue(card_rows, "карточки статуса не найдены")
        finally:
            path.unlink(missing_ok=True)

    def test_send_status_photo_and_fallbacks(self):
        # Обычная отправка: фото с подписью.
        message = self.FakeStatusMessage()
        run(bot._send_status(message))
        self.assertEqual(len(message.photos), 1)
        _, caption = message.photos[0]
        self.assertIn("Статус бота", caption)
        self.assertIn(bot.GROUP_NAME, caption)

        # Сбой генерации картинки -> текстовый fallback.
        message = self.FakeStatusMessage()
        with mock.patch.object(bot, "render_status_image",
                               side_effect=RuntimeError("boom")):
            run(bot._send_status(message))
        self.assertEqual(message.photos, [])
        self.assertEqual(len(message.answers), 1)
        self.assertIn(bot.GROUP_NAME, message.answers[0])
        self.assertIn("подписок", message.answers[0])

        # Сбой отправки фото -> тоже текстовый fallback.
        message = self.FakeStatusMessage(fail_photo=True)
        run(bot._send_status(message))
        self.assertEqual(len(message.answers), 1)
        self.assertIn(bot.GROUP_NAME, message.answers[0])

    def test_cmd_status_sends_photo(self):
        message = self.FakeStatusMessage(chat_id=42)
        run(bot.cmd_status(message))
        self.assertEqual(len(message.photos), 1)

    def test_status_text_includes_study_and_uptime(self):
        text = run(bot._status_text(chat_id=42))
        self.assertIn("подписок", text)
        self.assertIn("Аптайм", text)
        self.assertIn(bot.TIMEZONE, text)


if __name__ == "__main__":
    unittest.main()
