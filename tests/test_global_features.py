# -*- coding: utf-8 -*-
"""Проверки глобального каталога, изоляции подписок и постоянного кэша."""

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_schedule_bot import DBTestCase, bot, make_schedule, run


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
