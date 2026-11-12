# -*- coding: utf-8 -*-
"""Разовая доставка анонса: только существующие подписчики и ровно раз.

Ключевые обещания, которые здесь закреплены тестами:

  * получатели — снимок на момент запуска, новые подписчики не получают;
  * выключенный флаг ANNOUNCEMENT_BROADCAST означает «ничего не делать»;
  * повторный запуск (рестарт, падение, второй процесс) не отправляет дубль;
  * ручной send_announcement_card не попадает в учёт разовой рассылки.
"""

import os
from unittest import mock

from test_schedule_bot import DBTestCase, FakeBot, bot, run


class AnnouncementTestCase(DBTestCase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"ANNOUNCEMENT_BROADCAST": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM announcement_deliveries")
            conn.execute("DELETE FROM announcement_runs")
            conn.execute(
                "DELETE FROM bot_meta WHERE key = ?",
                (bot.ANNOUNCEMENT_SNAPSHOT_META,),
            )
        self.addCleanup(self._reset_meta)

    def _reset_meta(self):
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM announcement_deliveries")
            conn.execute("DELETE FROM announcement_runs")
            conn.execute(
                "DELETE FROM bot_meta WHERE key = ?",
                (bot.ANNOUNCEMENT_SNAPSHOT_META,),
            )

    def add_user_settings(self, chat_id):
        with bot.db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_settings (chat_id, group_id,"
                " updated_at) VALUES (?, 508, '2026-01-01 00:00:00')",
                (chat_id,),
            )


class TestAnnouncementAudience(AnnouncementTestCase):
    def test_only_existing_subscribers_get_the_announcement(self):
        self.insert_user(1001, "2026-01-01 00:00:00")
        self.insert_user(1002, "2026-02-01 00:00:00")
        self.add_user_settings(1003)

        bot.freeze_announcement_recipients()
        # Новый пользователь пришёл уже ПОСЛЕ снимка.
        self.insert_user(2001, "2026-11-12 12:00:00")
        self.add_user_settings(2002)

        fake = FakeBot()
        run(bot.run_announcement(fake, "mellowagent"))

        delivered = sorted(item[1] for item in fake.sent)
        self.assertEqual(delivered, [1001, 1002, 1003])
        self.assertNotIn(2001, delivered)
        self.assertNotIn(2002, delivered)

    def test_snapshot_is_frozen_once(self):
        self.insert_user(1001, "2026-01-01 00:00:00")
        self.assertEqual(bot.freeze_announcement_recipients(), 1)
        first_stamp = bot.announcement_snapshot_at()
        self.assertTrue(first_stamp)

        self.insert_user(1002, "2026-03-01 00:00:00")
        # Второй вызов ничего не добавляет: снимок уже есть.
        self.assertEqual(bot.freeze_announcement_recipients(), 1)
        self.assertEqual(bot.announcement_snapshot_at(), first_stamp)
        self.assertEqual(bot.announcement_snapshot_size(), 1)

    def test_group_chats_are_not_reached_by_new_users(self):
        self.insert_user(500, "2026-01-01 00:00:00")
        bot.freeze_announcement_recipients()
        self.insert_user(-100500, "2026-05-05 00:00:00")
        fake = FakeBot()
        run(bot.run_announcement(fake, "mellowagent"))
        self.assertEqual([item[1] for item in fake.sent], [500])


class TestAnnouncementOnce(AnnouncementTestCase):
    def test_second_run_sends_nothing(self):
        for chat_id in (101, 102, 103):
            self.insert_user(chat_id, "2026-01-01 00:00:00")

        fake = FakeBot()
        first = run(bot.run_announcement(fake, "mellowagent"))
        self.assertEqual(first["delivered"], 3)
        self.assertEqual(len(fake.sent), 3)

        second = run(bot.run_announcement(fake, "mellowagent"))
        self.assertEqual(second["delivered"], 0)
        self.assertEqual(len(fake.sent), 3)      # дублей нет
        self.assertEqual(bot.announcement_report()["sent"], 3)

    def test_restart_midway_does_not_resend(self):
        for chat_id in (201, 202):
            self.insert_user(chat_id, "2026-01-01 00:00:00")
        fake = FakeBot(fail_users={202})
        run(bot.run_announcement(fake, "mellowagent"))
        self.assertEqual(bot.announcement_report().get("failed"), 1)

        # «Рестарт» ничего не переотправляет — ни успешным, ни сбойным:
        # сам факт попытки уже учтён, дубль недопустим.
        healthy = FakeBot()
        run(bot.run_announcement(healthy, "mellowagent"))
        self.assertEqual(healthy.sent, [])

    def test_failed_can_be_released_manually_but_sent_never(self):
        for chat_id in (211, 212):
            self.insert_user(chat_id, "2026-01-01 00:00:00")
        run(bot.run_announcement(FakeBot(fail_users={212}), "mellowagent"))
        self.assertEqual(bot.release_failed_announcements(), 1)

        retry = FakeBot()
        run(bot.run_announcement(retry, "mellowagent"))
        # 211 уже 'sent' — второй раз не получает; 212 вернулся в очередь.
        self.assertEqual([item[1] for item in retry.sent], [212])
        self.assertEqual(bot.release_failed_announcements(), 0)

    def test_sent_marker_set_before_telegram_call(self):
        """Упавший посреди отправки чат не получает второе сообщение."""
        self.insert_user(301, "2026-01-01 00:00:00")

        class CrashingBot(FakeBot):
            async def send_photo(self, user_id, photo, caption):
                # Процесс «упал» сразу после того, как Telegram принял фото.
                await super().send_photo(user_id, photo, caption)
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run(bot.run_announcement(CrashingBot(), "mellowagent"))

        again = FakeBot()
        run(bot.run_announcement(again, "mellowagent"))
        self.assertEqual(again.sent, [])

    def test_disabled_flag_does_nothing(self):
        self.insert_user(401, "2026-01-01 00:00:00")
        with mock.patch.dict(os.environ, {"ANNOUNCEMENT_BROADCAST": ""}):
            fake = FakeBot()
            result = run(bot.run_announcement(fake, "mellowagent"))
        self.assertEqual(result.get("skipped"), "disabled")
        self.assertEqual(fake.sent, [])
        self.assertEqual(bot.announcement_snapshot_at(), "")

    def test_report_counts_and_run_finished(self):
        for chat_id in (501, 502):
            self.insert_user(chat_id, "2026-01-01 00:00:00")
        run(bot.run_announcement(FakeBot(fail_users={502}), "mellowagent"))
        report = bot.announcement_report()
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["sent"], 1)
        self.assertEqual(report["failed"], 1)

    def test_delivery_is_a_single_photo_with_caption(self):
        self.insert_user(601, "2026-01-01 00:00:00")
        fake = FakeBot()
        run(bot.run_announcement(fake, "mellowagent"))
        kind, chat_id, caption = fake.sent[0]
        self.assertEqual((kind, chat_id), ("photo", 601))
        self.assertIn(bot.ANNOUNCE_TITLE.lower(), caption.lower())
        self.assertIn("@mellowagent", caption)
