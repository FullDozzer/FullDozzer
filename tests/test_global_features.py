# -*- coding: utf-8 -*-
"""Проверки глобального каталога, изоляции подписок и постоянного кэша."""

import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_group_selection import RecordingCallback, RecordingMessage
from test_schedule_bot import (
    DBTestCase,
    FakeBot,
    bot,
    empty_schedule,
    make_schedule,
    run,
)


class TestPremiumUsernameCommands(DBTestCase):
    def setUp(self):
        super().setUp()
        self.telegram = SimpleNamespace(
            me=mock.AsyncMock(return_value=SimpleNamespace(username="MainScheduleBot"))
        )

    def test_collectible_username_is_accepted(self):
        command = run(bot.BotCommandFilter("help").parse_command(
            "/help@mellowagent", self.telegram
        ))
        self.assertEqual(command.command, "help")
        self.assertEqual(command.mention, "mellowagent")

    def test_arguments_and_case_are_preserved(self):
        command = run(bot.BotCommandFilter("date").parse_command(
            "/date@MellowAgent 10.11.2026", self.telegram
        ))
        self.assertEqual(command.args, "10.11.2026")

    def test_main_username_still_works(self):
        command = run(bot.BotCommandFilter("today").parse_command(
            "/today@MainScheduleBot", self.telegram
        ))
        self.assertEqual(command.command, "today")

    def test_other_bot_is_not_accepted(self):
        with self.assertRaises(bot.CommandException):
            run(bot.BotCommandFilter("help").parse_command(
                "/help@another_bot", self.telegram
            ))


class TestGlobalCatalog(DBTestCase):
    def test_supplied_catalog_is_complete_and_typed(self):
        self.assertGreaterEqual(len(bot.FALLBACK_GROUPS), 70)
        self.assertEqual(bot.get_group(508).name, "ЭС7-24")
        self.assertEqual(bot.get_group(508).study, "Коммерческая")
        self.assertEqual(bot.get_group(512).form, "Заочная")
        self.assertEqual(bot.get_group(542).form, "Очная")
        self.assertEqual(bot.get_group(542).study, "Бюджетная")

    def test_api_payload_ignores_department_keys(self):
        groups = bot.parse_groups_payload({"groups": {
            "dep_0": [508],
            "508": {"Name": "ЭС7-24", "realCourse": 3, "Study": "Коммерческая"},
        }})
        self.assertEqual(list(groups), [508])

    def test_group_keyboard_filters(self):
        markup = bot.group_catalog_keyboard(course=1, form="correspondence")
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("ЭСз-26", labels)
        self.assertNotIn("ЭС1-26", labels)


class TestGlobalSubscriptions(DBTestCase):
    def setUp(self):
        super().setUp()
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM user_settings")
            conn.execute("DELETE FROM teacher_subscribers")

    def test_group_subscriptions_are_isolated(self):
        bot.subscribe_user(101, group_id=508)
        bot.subscribe_user(102, group_id=542)
        self.assertEqual(bot.load_subscribers(508), [101])
        self.assertEqual(bot.load_subscribers(542), [102])

    def test_teacher_subscription_is_independent(self):
        bot.subscribe_user(101, group_id=508)
        bot.subscribe_teacher(101, 321, True)
        self.assertEqual(bot.load_teacher_subscribers(321), [101])
        self.assertEqual(bot.load_subscribers(508), [101])
        bot.subscribe_teacher(101, 321, False)
        self.assertEqual(bot.load_teacher_subscribers(321), [])
        self.assertEqual(bot.load_subscribers(508), [101])


class TestPersistentScheduleCache(DBTestCase):
    def setUp(self):
        super().setUp()
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM schedule_cache")

    def test_second_download_uses_sqlite_cache(self):
        day = date(2026, 11, 10)
        schedule = make_schedule(day, "Математика")
        calls = []

        async def fake_get(target_day, group_id=None):
            calls.append((target_day, group_id))
            return schedule

        async def scenario():
            with mock.patch.object(bot, "get_schedule", fake_get):
                first = await bot.get_schedule_cached(day, 508)
                second = await bot.get_schedule_cached(day, 508)
            return first, second

        first, second = run(scenario())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.group, second.group)
        self.assertEqual(bot.schedule_signature(first), bot.schedule_signature(second))

    def test_new_hash_removes_old_image_reference(self):
        day = date(2026, 11, 10)
        first = make_schedule(day, "Математика")
        second = make_schedule(day, "Физика")
        image = bot.IMAGE_DIR / "cache_invalidation_test.png"
        image.write_bytes(b"old")
        bot.save_schedule_cache(first, image)
        bot.save_schedule_cache(second)
        self.assertFalse(image.exists())
        self.assertIsNone(bot.cached_image_path(second))

    def test_png_is_rendered_once_per_version(self):
        day = date(2026, 11, 10)
        schedule = make_schedule(day, "Математика")
        image = bot.IMAGE_DIR / "persistent_test.png"
        image.write_bytes(b"png")
        bot.save_schedule_cache(schedule)

        async def scenario():
            with mock.patch.object(bot, "render_schedule_image", return_value=image) as renderer:
                first = await bot.get_cached_image(schedule)
                second = await bot.get_cached_image(schedule)
                return first, second, renderer.call_count

        first, second, calls = run(scenario())
        self.assertEqual(first, image)
        self.assertEqual(second, image)
        self.assertEqual(calls, 1)
        image.unlink(missing_ok=True)

    def test_study_history_accepts_non_default_group(self):
        day = bot.get_today()
        schedule = make_schedule(day, "Математика")
        schedule.group = "БС1-26"
        schedule.lessons[0].start = "00:00"
        schedule.lessons[0].end = "00:40"
        schedule.lessons[0].time = "00:00 - 00:40"
        default_before = bot.load_total_study_minutes("ЭС7-24")
        bot.record_completed_lessons(schedule)
        self.assertEqual(bot.load_total_study_minutes("БС1-26"), 40)
        self.assertEqual(bot.load_total_study_minutes("ЭС7-24"), default_before)


class TestStartupPrefetchAndNewSubscriberSuppression(DBTestCase):
    def test_user_opens_schedules_before_monitor_then_subscribes_no_duplicate_published(self):
        today = date(2026, 11, 10)
        tomorrow = date(2026, 11, 11)
        group_id = 542
        group_name = bot.get_group(group_id).name
        versions = {
            today: make_schedule(today, "Математика"),
            tomorrow: make_schedule(tomorrow, "Физика"),
        }
        versions[today].group = group_name
        versions[tomorrow].group = group_name

        fetch_calls = []

        async def fake_get_schedule(day, gid=None):
            fetch_calls.append((day, gid))
            return versions.get(day, empty_schedule(day))

        chat_id = 700001
        bot.set_user_group(chat_id, group_id)
        img_path = bot.IMAGE_DIR / "user_first_test.png"
        img_path.write_bytes(b"png")

        with mock.patch.object(bot, "now_local", return_value=datetime(2026, 11, 10, 12, 0, 0)), \
             mock.patch.object(bot, "get_schedule", fake_get_schedule), \
             mock.patch.object(bot, "render_schedule_image", return_value=img_path), \
             mock.patch.object(bot, "FSInputFile", side_effect=lambda p: str(p)):
            # 1. Пользователь открывает /today и /schedule раньше монитора.
            msg_today = RecordingMessage(chat_id=chat_id, text="/today")
            msg_tomorrow = RecordingMessage(chat_id=chat_id, text="/schedule")
            run(bot.cmd_today(msg_today))
            run(bot.cmd_schedule(msg_tomorrow))
            self.assertEqual(len(msg_today.photos), 1)
            self.assertEqual(len(msg_tomorrow.photos), 1)

            # Оба расписания сразу попали в SQLite-кэш; повторное открытие идёт из кэша.
            self.assertEqual(len(fetch_calls), 2)
            self.assertIsNotNone(bot.load_cached_schedule(f"group:{group_id}", today))
            self.assertIsNotNone(bot.load_cached_schedule(f"group:{group_id}", tomorrow))
            run(bot.cmd_today(RecordingMessage(chat_id=chat_id, text="/today")))
            self.assertEqual(len(fetch_calls), 2)

            # 2. Пользователь включает уведомления и запускается монитор.
            sub_msg = RecordingMessage(chat_id=chat_id, text=".")
            run(bot.cb_subscribe(RecordingCallback("subscribe", sub_msg)))
            self.assertTrue(any("Уведомления включены" in t for t, _ in sub_msg.answers))

            fake_bot = FakeBot()
            run(bot._check_date(fake_bot, today, group_id))
            run(bot._check_date(fake_bot, tomorrow, group_id))
            self.assertEqual(fake_bot.sent, [])

            # 3. Реальное изменение завтрашнего расписания после подписки доставляется.
            updated = make_schedule(tomorrow, "Информатика")
            updated.group = group_name
            versions[tomorrow] = updated
            run(bot._check_date(fake_bot, tomorrow, group_id))
            self.assertEqual(len(fake_bot.sent), 1)
            self.assertEqual(fake_bot.sent[0][1], chat_id)
            self.assertIn("Расписание изменилось", fake_bot.sent[0][2])
        img_path.unlink(missing_ok=True)

    def test_already_subscribed_user_who_opened_today_gets_no_duplicate_while_other_gets_notified(self):
        today = date(2026, 11, 10)
        group_id = 542
        group_name = bot.get_group(group_id).name
        sched = make_schedule(today, "Алгебра")
        sched.group = group_name

        # Оба пользователя подписались ДО публикации расписания.
        user_opened = 700010
        user_waiting = 700011
        bot.subscribe_user(user_opened, group_id=group_id)
        bot.subscribe_user(user_waiting, group_id=group_id)

        img_path = bot.IMAGE_DIR / "user_opened_first.png"
        img_path.write_bytes(b"png")

        async def fake_get_schedule(day, gid=None):
            return sched

        with mock.patch.object(bot, "now_local", return_value=datetime(2026, 11, 10, 9, 0, 0)), \
             mock.patch.object(bot, "get_schedule", fake_get_schedule), \
             mock.patch.object(bot, "render_schedule_image", return_value=img_path), \
             mock.patch.object(bot, "FSInputFile", side_effect=lambda p: str(p)):
            # Первый пользователь открыл /today вручную раньше монитора.
            run(bot.cmd_today(RecordingMessage(chat_id=user_opened, text="/today")))
            fake_bot = FakeBot()
            run(bot._check_date(fake_bot, today, group_id))

        self.assertEqual([u for _, u, _ in fake_bot.sent], [user_waiting])
        self.assertIn("Расписание опубликовано", fake_bot.sent[0][2])
        img_path.unlink(missing_ok=True)

    def test_startup_cycle_parses_all_groups_and_new_subscriber_gets_no_old_published_spam(self):
        today = date(2026, 11, 10)
        tomorrow = date(2026, 11, 11)
        day_after = date(2026, 11, 12)

        published = {today}  # на старте опубликовано только сегодня; завтра появится позже

        async def fake_get_schedule(day, gid=None):
            if day not in published:
                return empty_schedule(day)
            sched = make_schedule(day, f"Предмет {day.isoformat()}")
            if gid is not None:
                sched.group = bot.get_group(gid).name
            return sched

        fake_bot = FakeBot()
        with mock.patch.object(bot, "now_local", return_value=datetime(2026, 11, 10, 10, 0, 0)), \
             mock.patch.object(bot, "get_schedule", fake_get_schedule), \
             mock.patch.object(bot, "check_birthdays", mock.AsyncMock(return_value=False)), \
             mock.patch.object(bot, "render_schedule_image", return_value=Path("/tmp/x.png")):
            # 1. Стартовый цикл парсит все группы без активных подписчиков.
            run(bot.run_schedule_monitor_cycle(fake_bot))
            self.assertEqual(fake_bot.sent, [])
            for gid in bot.GROUPS:
                self.assertIsNotNone(bot.load_cached_schedule(f"group:{gid}", today))
                self.assertIsNone(bot.load_cached_schedule(f"group:{gid}", tomorrow))

            # 2. Новый пользователь выбирает группу 542 и включает уведомления,
            # даже не открывая расписание.
            new_user = 700020
            bot.set_user_group(new_user, 542)
            sub_msg = RecordingMessage(chat_id=new_user, text=".")
            run(bot.cb_subscribe(RecordingCallback("subscribe", sub_msg)))

            # Следующий цикл монитора не шлёт «новое опубликованное» для уже известного today.
            run(bot.run_schedule_monitor_cycle(fake_bot))
            self.assertEqual(fake_bot.sent, [])

            # 3. Когда публикуется реально новое расписание на завтра — уведомление уходит.
            published.add(tomorrow)
            run(bot.run_schedule_monitor_cycle(fake_bot))
            self.assertEqual(len(fake_bot.sent), 1)
            self.assertEqual(fake_bot.sent[0][1], new_user)
            self.assertIn("Расписание опубликовано", fake_bot.sent[0][2])

            # 4. Второй новый пользователь, подписавшийся уже ПОСЛЕ публикации tomorrow,
            # не получает ни today, ни tomorrow как «новое опубликованное».
            second_user = 700021
            bot.set_user_group(second_user, 542)
            bot.subscribe_user(second_user, group_id=542)
            run(bot.run_schedule_monitor_cycle(fake_bot))
            self.assertEqual(len(fake_bot.sent), 1)

            # 5. Очередная новая дата (послезавтра) доставляется обоим подписчикам.
            published.add(day_after)
            run(bot._check_date(fake_bot, day_after, 542))
            recipients = {u for _, u, _ in fake_bot.sent[1:]}
            self.assertEqual(recipients, {new_user, second_user})

    def test_switching_groups_while_subscribed_suppresses_already_known_schedules(self):
        today = date(2026, 11, 10)
        sched_542 = make_schedule(today, "История")
        sched_542.group = bot.get_group(542).name
        bot.save_schedule_cache(sched_542)

        user_id = 700030
        bot.subscribe_user(user_id, group_id=508)
        # Пользователь переключается на группу 542 через интерфейс выбора группы.
        cb_msg = RecordingMessage(chat_id=user_id, text=".")
        with mock.patch.object(bot, "ensure_study_history", return_value=False):
            run(bot.cb_group_set(RecordingCallback("group:set:542", cb_msg)))

        fake_bot = FakeBot()

        async def fake_get_schedule(day, gid=None):
            return sched_542

        with mock.patch.object(bot, "get_schedule", fake_get_schedule), \
             mock.patch.object(bot, "render_schedule_image", return_value=Path("/tmp/x.png")):
            run(bot._check_date(fake_bot, today, 542))

        self.assertEqual(fake_bot.sent, [])

    def test_empty_schedule_is_not_poisoning_cache(self):
        day = date(2026, 11, 11)
        state = {"published": False}

        async def fake_get_schedule(target_day, gid=None):
            if not state["published"]:
                return empty_schedule(target_day)
            return make_schedule(target_day, "Физика")

        with mock.patch.object(bot, "get_schedule", fake_get_schedule):
            first = run(bot.get_schedule_cached(day, 508))
            self.assertEqual(first.lessons, [])
            self.assertIsNone(bot.load_cached_schedule("group:508", day))

            state["published"] = True
            second = run(bot.get_schedule_cached(day, 508))
            self.assertEqual(len(second.lessons), 1)
            self.assertIsNotNone(bot.load_cached_schedule("group:508", day))

    def test_teacher_schedule_opened_before_subscribe_does_not_re_notify(self):
        day = date(2026, 11, 10)
        staff_id = 69
        member = bot.STAFF_BY_ID[staff_id]
        staff_sched = make_schedule(day, "Базы данных")
        staff_sched.group = member.full_name
        staff_sched.schedule_type = "staff"
        staff_sched.staff_id = staff_id
        staff_sched.staff_name = member.full_name

        img_path = bot.IMAGE_DIR / "staff_first_test.png"
        img_path.write_bytes(b"png")
        chat_id = 700040

        async def fake_get_staff(sid, target_day):
            return staff_sched

        with mock.patch.object(bot, "get_staff_schedule", fake_get_staff), \
             mock.patch.object(bot, "render_schedule_image", return_value=img_path), \
             mock.patch.object(bot, "FSInputFile", side_effect=lambda p: str(p)):
            msg = RecordingMessage(chat_id=chat_id, text="расписание преподавателя Аглиуллиной на сегодня")
            with mock.patch.object(bot, "now_local", return_value=datetime(2026, 11, 10, 12, 0, 0)):
                run(bot.cmd_text_schedule(msg))
            self.assertEqual(len(msg.photos), 1)
            self.assertIsNotNone(bot.load_cached_schedule(f"staff:{staff_id}", day))

            run(bot.cb_teacher_notify(RecordingCallback(f"teacher:notify:{staff_id}:1", RecordingMessage(chat_id=chat_id))))
            fake_bot = FakeBot()
            run(bot._check_staff_date(fake_bot, staff_id, day))
            self.assertEqual(fake_bot.sent, [])
        img_path.unlink(missing_ok=True)

    def test_main_startup_warms_all_groups_before_polling(self):
        today = date(2026, 11, 10)
        tomorrow = date(2026, 11, 11)
        seen_before_polling = {}

        class DummyBot:
            def __init__(self, *args, **kwargs):
                self.session = SimpleNamespace(close=mock.AsyncMock())

        async def fake_get_schedule(day, gid=None):
            sched = make_schedule(day, "Стартовая пара")
            if gid is not None:
                sched.group = bot.get_group(gid).name
            return sched

        async def fake_start_polling(bot_obj):
            for gid in bot.GROUPS:
                seen_before_polling[gid] = (
                    bot.load_cached_schedule(f"group:{gid}", today) is not None,
                    bot.load_cached_schedule(f"group:{gid}", tomorrow) is not None,
                )

        if hasattr(bot.main, "_monitor_task"):
            delattr(bot.main, "_monitor_task")

        with mock.patch.object(bot, "BOT_TOKEN", "12345:test"), \
             mock.patch.object(bot, "Bot", DummyBot), \
             mock.patch.object(bot, "refresh_group_catalog", mock.AsyncMock(return_value=len(bot.GROUPS))), \
             mock.patch.object(bot, "_setup_commands", mock.AsyncMock()), \
             mock.patch.object(bot, "now_local", return_value=datetime(2026, 11, 10, 9, 0, 0)), \
             mock.patch.object(bot, "get_schedule", fake_get_schedule), \
             mock.patch.object(bot, "check_birthdays", mock.AsyncMock(return_value=False)), \
             mock.patch.object(bot.dp, "start_polling", fake_start_polling):
            run(bot.main())

        self.assertEqual(len(seen_before_polling), len(bot.GROUPS))
        self.assertTrue(all(t and tm for t, tm in seen_before_polling.values()))
        state = bot.load_state()
        self.assertIn(today.isoformat(), state)
        self.assertIn(f"group:542:{tomorrow.isoformat()}", state)

