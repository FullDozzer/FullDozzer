# -*- coding: utf-8 -*-
"""Проверки логики бота версии 2.1.4.

Запуск:  python -m unittest discover -s tests -v
(из корня репозитория, с установленными зависимостями)
"""

import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

# Изолируем БД до импорта модуля
_TEST_DATA = Path(tempfile.mkdtemp(prefix="sched_bot_test_"))
os.environ["DATA_DIR"] = str(_TEST_DATA)
os.environ["CHECK_INTERVAL"] = "300"
os.environ["CHANGELOG_2_1_4_RELEASED_AT"] = "2026-09-06 12:00:00"

import bot  # noqa: E402
from versioning import (  # noqa: E402
    BOT_VERSION,
    CHANGELOG,
    changelog_text,
    get_released_at,
    pending_versions,
    version_key,
)


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


class DBTestCase(unittest.TestCase):
    def setUp(self):
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM schedule_state")
            conn.execute("DELETE FROM schedule_notifications")
            conn.execute("DELETE FROM subscribers")

    def insert_user(self, user_id, created_at, last_notified_version=""):
        with bot.db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO subscribers"
                " (user_id, created_at, chat_type, title,"
                "  last_notified_version)"
                " VALUES (?, ?, 'private', '', ?)",
                (user_id, created_at, last_notified_version),
            )


class TestVersionSystem(unittest.TestCase):
    def test_bot_version(self):
        self.assertEqual(BOT_VERSION, "2.1.4")
        self.assertEqual(bot.BOT_VERSION, "2.1.4")

    def test_changelog_2_1_4_exists(self):
        self.assertIn("2.1.4", CHANGELOG)
        changes = CHANGELOG["2.1.4"]["changes"]
        self.assertGreaterEqual(len(changes), 6)
        joined = " ".join(changes).lower()
        self.assertIn("/today", joined)
        self.assertIn("5 минут", joined)
        self.assertIn("asia/yekaterinburg", joined)

    def test_version_key_and_pending(self):
        self.assertEqual(version_key("2.1.4"), (2, 1, 4))
        self.assertLess(version_key("2.1.4"), version_key("2.1.5"))
        self.assertEqual(pending_versions(""), ["2.1.4"])
        self.assertEqual(pending_versions("2.1.4"), [])
        # Пользователь после 2.1.4 (до 2.1.5) получит 2.1.5
        self.assertEqual(pending_versions("2.1.4"), [])

    def test_released_at_comes_from_config(self):
        released = get_released_at("2.1.4")
        self.assertIsNotNone(released)
        self.assertEqual(released, datetime(2026, 9, 6, 12, 0, 0))
        with mock.patch.dict(
            os.environ, {"CHANGELOG_2_1_4_RELEASED_AT": ""}
        ):
            self.assertIsNone(get_released_at("2.1.4"))

    def test_changelog_text(self):
        text = changelog_text("2.1.4")
        self.assertIn("2.1.4", text)
        self.assertIn("/today", text)
        self.assertIn("•", text)
        self.assertIn("Asia/Yekaterinburg", text)


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

    def test_parse_db_datetime(self):
        self.assertEqual(
            bot.parse_db_datetime("2026-09-01 08:30:00"),
            datetime(2026, 9, 1, 8, 30, 0),
        )
        self.assertIsNone(bot.parse_db_datetime(""))


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

    def test_monitor_full_cycle_both_dates_and_changelog(self):
        """Один цикл монитора: сегодня + завтра + changelog, без дублей."""
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


class TestChangelogDelivery(DBTestCase):
    def setUp(self):
        super().setUp()
        self.bot = FakeBot()

    def test_old_user_gets_21_4_new_user_does_not(self):
        self.insert_user(2001, "2026-09-01 00:00:00")   # до релиза
        self.insert_user(2002, "2026-09-07 00:00:00")   # после релиза
        run(bot._process_changelog(self.bot))
        sent_ids = [item[1] for item in self.bot.sent if item[0] == "text"]
        self.assertEqual(sent_ids, [2001])
        with bot.db_connect() as conn:
            rows = {
                r["user_id"]: r["last_notified_version"]
                for r in conn.execute(
                    "SELECT user_id, last_notified_version FROM subscribers"
                )
            }
        self.assertEqual(rows[2001], "2.1.4")
        self.assertEqual(rows[2002], "")

    def test_restart_does_not_resend(self):
        self.insert_user(2001, "2026-09-01 00:00:00")
        run(bot._process_changelog(self.bot))
        count_after_first = len(self.bot.sent)
        # «Перезапуск»: снова вызываем рассылку
        run(bot._process_changelog(self.bot))
        self.assertEqual(len(self.bot.sent), count_after_first)

    def test_failed_send_not_marked_then_retried(self):
        self.insert_user(3001, "2026-09-01 00:00:00")

        async def failing(b, user_id, version):
            return False

        with mock.patch.object(bot, "_deliver_changelog", failing):
            run(bot._process_changelog(self.bot))
        with bot.db_connect() as conn:
            row = conn.execute(
                "SELECT last_notified_version FROM subscribers"
                " WHERE user_id = 3001"
            ).fetchone()
        self.assertEqual(row["last_notified_version"], "")
        # Теперь доставка удалась
        run(bot._process_changelog(self.bot))
        with bot.db_connect() as conn:
            row = conn.execute(
                "SELECT last_notified_version FROM subscribers"
                " WHERE user_id = 3001"
            ).fetchone()
        self.assertEqual(row["last_notified_version"], "2.1.4")

    def test_unreleased_version_not_sent(self):
        self.insert_user(4001, "2026-09-01 00:00:00")
        with mock.patch.dict(
            os.environ, {"CHANGELOG_2_1_4_RELEASED_AT": ""}
        ):
            run(bot._process_changelog(self.bot))
        self.assertEqual(self.bot.sent, [])
        with bot.db_connect() as conn:
            row = conn.execute(
                "SELECT last_notified_version FROM subscribers"
                " WHERE user_id = 4001"
            ).fetchone()
        self.assertEqual(row["last_notified_version"], "")

    def test_legacy_user_without_created_at_is_old(self):
        self.insert_user(5001, "")
        run(bot._process_changelog(self.bot))
        sent_ids = [item[1] for item in self.bot.sent if item[0] == "text"]
        self.assertEqual(sent_ids, [5001])

    def test_future_version_planning(self):
        # Пользователь после 2.1.4 не получает 2.1.4 —
        # и так как CHANGELOG для 2.1.5 ещё нет, ничего не получает.
        self.insert_user(6001, "2026-09-10 00:00:00",
                         last_notified_version="2.1.4")
        run(bot._process_changelog(self.bot))
        self.assertEqual(self.bot.sent, [])


if __name__ == "__main__":
    unittest.main()
