# -*- coding: utf-8 -*-
"""Проверки логики бота.

Запуск:  python -m unittest discover -s tests -v
(из корня репозитория, с установленными зависимостями)
"""

import asyncio
import os
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
        today = bot.get_today()
        tomorrow = bot.get_tomorrow()

        class StopLoop(Exception):
            pass

        sleep_calls = {"n": 0}

        async def fake_sleep(sec):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise StopLoop()

        async def fake_get_schedule(day):
            return make_schedule(day, "Предмет")

        with mock.patch.object(bot, "get_schedule", fake_get_schedule), \
             mock.patch.object(bot, "render_schedule_image",
                               return_value=Path("/tmp/x.png")), \
             mock.patch("asyncio.sleep", fake_sleep):
            with self.assertRaises(StopLoop):
                run(bot.schedule_monitor(fake_bot))

        # Сегодня воскресенье (если так) -> пропуск; завтра -> уведомление.
        if bot.is_day_off(today):
            self.assertEqual(len(fake_bot.sent), 1)
            self.assertIn("Завтра", fake_bot.sent[0][2])
            self.assertIn(tomorrow.isoformat(), bot.load_state())
        else:
            self.assertGreaterEqual(len(fake_bot.sent), 1)
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
    def test_render_subgroups_and_changes_dynamic_height(self):
        schedule = bot.Schedule(
            date=date(2026, 9, 7),
            group=bot.GROUP_NAME,
            lessons=[
                lesson("I", None, "Химия Н и Г", "УК307", "Арнаутова А.В."),
                lesson("II", "1"),
                lesson("II", "2", room="ПК305"),
                lesson(
                    "III", None, "Очень длинное название предмета для проверки "
                    "переноса текста в несколько строк внутри Pillow",
                    "ПК217", "Степанов Сергей Владимирович Иванов Пётр Петрович",
                    "11:40", "13:00",
                ),
            ],
        )
        changes = bot.compare_schedules(
            bot.Schedule(
                date=date(2026, 9, 7),
                group=bot.GROUP_NAME,
                lessons=[
                    lesson("I", None, "Химия Н и Г", "УК307", "Арнаутова А.В."),
                    lesson("II", "1"),
                    lesson("II", "2", room="ПК303"),
                    lesson(
                        "III", None, "Очень длинное название предмета для проверки "
                        "переноса текста в несколько строк внутри Pillow",
                        "ПК217", "Степанов Сергей Владимирович Иванов Пётр Петрович",
                        "11:40", "13:00",
                    ),
                ],
            ),
            schedule,
        )
        self.assertEqual(len(changes), 1)
        normal = bot.render_schedule_image(schedule)
        changed = bot.render_schedule_image(schedule, changes=changes)
        with Image.open(normal) as img_n, Image.open(changed) as img_c:
            self.assertGreater(img_n.size[0], 500)
            self.assertGreater(img_n.size[1], 300)
            # У изменённой картинки есть блок «Что изменилось» => выше.
            self.assertGreater(img_c.size[1], img_n.size[1])

    def test_render_added_and_removed_subgroups(self):
        old = bot.Schedule(date=date(2026, 9, 8), group=bot.GROUP_NAME,
                           lessons=[lesson("II", "1")])
        new = bot.Schedule(date=date(2026, 9, 8), group=bot.GROUP_NAME,
                           lessons=[
                               lesson("II", "1"),
                               lesson("II", "2", room="ПК303",
                                      teacher="Амирханова Г.А."),
                           ])
        changes = bot.compare_schedules(old, new)
        self.assertEqual(changes[0].kind, "added")
        path = bot.render_schedule_image(new, changes=changes)
        self.assertTrue(path.exists())

    def test_render_many_changes_doesnt_crash(self):
        lessons = []
        changes = []
        for i in range(1, 12):
            lessons.append(lesson("II", str(i), subject=f"Предмет {i}"))
            changes.append(bot.ScheduleChange(
                kind="added",
                pair="II",
                subgroup=str(i),
                old=None,
                new={"subject": f"Предмет {i}", "room": "ПК100", "teacher": "—"},
                details=[],
            ))
        schedule = bot.Schedule(date=date(2026, 9, 8), group=bot.GROUP_NAME,
                                lessons=lessons)
        path = bot.render_schedule_image(schedule, changes=changes)
        self.assertTrue(path.exists())


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


class TestImageLayoutFixes(unittest.TestCase):
    """Разметка картинки: блок «перемена» и подвал."""

    BG = (243, 245, 250)

    def scan(self, path):
        """Возвращает (ширина, высота, строки карточек, строки подвала)."""
        with Image.open(path) as img:
            im = img.convert("RGB")
            width, height = im.size
            px = im.load()
            card_rows = [
                y for y in range(height)
                if any(px[x, y] == (255, 255, 255)
                       for x in range(200, width - 200, 8))
            ]
            drawn_rows = [
                y for y in range(height)
                if any(px[x, y] != self.BG
                       for x in range(20, width - 20, 4))
            ]
        last_card = max(card_rows)
        footer_rows = [y for y in drawn_rows if y > last_card + 12]
        return width, height, last_card, footer_rows

    def subject_left_x(self, path):
        """Левая граница текста предмета (единственный тёмный текст)."""
        with Image.open(path) as img:
            im = img.convert("RGB")
            width, height = im.size
            px = im.load()
            for x in range(width):
                for y in range(300, height):
                    r, g, b = px[x, y]
                    if r < 80 and g < 80 and b < 80:
                        return x
        return -1

    def make(self, break_duration=""):
        item = lesson("I", None, "Химия Н и Г", "УК307", "Арнаутова А.В.",
                      "08:30", "09:50")
        item.break_duration = break_duration
        return bot.Schedule(date=date(2026, 9, 9), group=bot.GROUP_NAME,
                            lessons=[item])

    def test_footer_is_inside_image_and_not_over_last_card(self):
        schedule = bot.parse_schedule(PROVIDED_HTML, date(2026, 9, 7))
        path = bot.render_schedule_image(schedule)
        width, height, last_card, footer_rows = self.scan(path)
        self.assertTrue(footer_rows, "подвал не найден под последней карточкой")
        # Подвал ниже последней карточки, с заметным отступом.
        self.assertGreaterEqual(min(footer_rows) - last_card, 20)
        # И не прижат к нижней границе / не обрезан.
        self.assertGreaterEqual(height - 1 - max(footer_rows), 20)

    def test_footer_included_in_height_with_summary_block(self):
        old = bot.Schedule(date=date(2026, 9, 8), group=bot.GROUP_NAME,
                           lessons=[lesson("II", "1")])
        new = bot.Schedule(date=date(2026, 9, 8), group=bot.GROUP_NAME,
                           lessons=[lesson("II", "1", room="ПК305")])
        changes = bot.compare_schedules(old, new)
        path = bot.render_schedule_image(new, changes=changes)
        width, height, last_card, footer_rows = self.scan(path)
        self.assertTrue(footer_rows)
        self.assertGreaterEqual(height - 1 - max(footer_rows), 20)

    def test_empty_schedule_footer_fits(self):
        path = bot.render_schedule_image(empty_schedule(date(2026, 9, 8)))
        width, height, last_card, footer_rows = self.scan(path)
        self.assertTrue(footer_rows)
        self.assertGreaterEqual(min(footer_rows) - last_card, 20)
        self.assertGreaterEqual(height - 1 - max(footer_rows), 20)

    def test_break_line_does_not_move_subject(self):
        with_break = bot.render_schedule_image(self.make("30 мин"))
        x_with = self.subject_left_x(with_break)
        without_break = bot.render_schedule_image(self.make(""))
        x_without = self.subject_left_x(without_break)
        self.assertGreater(x_with, 0)
        self.assertEqual(x_with, x_without)

    def test_break_line_reserves_vertical_space(self):
        with Image.open(bot.render_schedule_image(self.make("30 мин"))) as a, \
                Image.open(bot.render_schedule_image(self.make(""))) as b:
            # Строка перемены добавляет высоту, а не наезжает на предмет.
            self.assertGreater(a.size[1], b.size[1])

    def test_subgroups_are_still_rendered(self):
        schedule = bot.parse_schedule(PROVIDED_HTML, date(2026, 9, 7))
        # Пара II с двумя подгруппами выше пары IV без подгрупп.
        pairs = {p.number: p for p in schedule.pairs}
        self.assertEqual(len(pairs["II"].lessons), 2)
        two = bot.Schedule(date=date(2026, 9, 7), group=bot.GROUP_NAME,
                           lessons=[x for x in schedule.lessons
                                    if x.pair == "II"])
        one = bot.Schedule(date=date(2026, 9, 7), group=bot.GROUP_NAME,
                           lessons=[x for x in schedule.lessons
                                    if x.pair == "IV"])
        with Image.open(bot.render_schedule_image(two)) as img_two, \
                Image.open(bot.render_schedule_image(one)) as img_one:
            self.assertGreater(img_two.size[1], img_one.size[1])


if __name__ == "__main__":
    unittest.main()
