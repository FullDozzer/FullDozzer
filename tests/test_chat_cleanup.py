# -*- coding: utf-8 -*-
"""Проверки умного очищения чата (bot.deliver и ChatCleanupMiddleware).

Правила, которые здесь зафиксированы:

* callback с ответом того же типа правит прошлое сообщение бота (edit),
  а не присылает новое;
* callback со сменой типа (текст → картинка) удаляет прошлый экран;
* новая команда (Message) всегда удаляет прошлый экран и отправляет новый —
  ответ должен быть внизу и заметным;
* команда пользователя удаляется после успешной доставки ответа;
* без прав/без реального Bot ничего не падает и не ломает ответ;
* служебные сценарии (ввод даты, поиск преподавателя, модерация рассылки)
  и сообщения от ботов не запоминаются;
* CHAT_CLEANUP=0 полностью выключает очистку.
"""

import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiogram.exceptions import TelegramBadRequest

from test_group_selection import RecordingCallback, RecordingMessage
from test_schedule_bot import DBTestCase, bot, make_schedule, run


class CleanupBot(bot.Bot):
    """Фейковый Bot: записывает отправки, правки и удаления.

    Наследник aiogram.Bot без вызова __init__ — нужен только isinstance(),
    который проверяет bot._cleanup_bot, и наши методы.
    """

    def __init__(self, fail_delete=(), fail_edit=()):
        self.sent = []
        self.deleted = []
        self.edited = []
        self.fail_delete = set(fail_delete)
        self.fail_edit = set(fail_edit)

    async def delete_message(self, chat_id, message_id):
        if message_id in self.fail_delete:
            raise RuntimeError("no rights")
        self.deleted.append((chat_id, message_id))

    async def edit_message_text(self, chat_id, message_id, text,
                                reply_markup=None):
        if message_id in self.fail_edit:
            raise TelegramBadRequest(method=None,
                                     message="Bad Request: message is not modified")
        self.edited.append(("text", chat_id, message_id, text))

    async def edit_message_media(self, chat_id, message_id, media,
                                 reply_markup=None):
        self.edited.append(("photo", chat_id, message_id, media))


class CleanupMessage(RecordingMessage):
    """Message с message_id и bot — как настоящее сообщение Telegram."""

    next_id = 500

    def __init__(self, chat_id=10001, text="", bot_obj=None, message_id=None,
                 **kwargs):
        super().__init__(chat_id=chat_id, text=text, **kwargs)
        CleanupMessage.next_id += 1
        self.message_id = CleanupMessage.next_id if message_id is None else message_id
        self.bot = bot_obj
        self.sent_ids = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))
        CleanupMessage.next_id += 1
        self.sent_ids.append(CleanupMessage.next_id)
        return SimpleNamespace(message_id=CleanupMessage.next_id)

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.photos.append((photo, caption, reply_markup))
        CleanupMessage.next_id += 1
        self.sent_ids.append(CleanupMessage.next_id)
        return SimpleNamespace(message_id=CleanupMessage.next_id)


class CleanupCallback(RecordingCallback):
    """CallbackQuery на сообщении бота."""

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.callback_answers.append((text, show_alert))


def _png(tmp_dir: Path, name: str) -> Path:
    """Минимальный PNG-файл для отправки фото."""
    from PIL import Image

    path = tmp_dir / name
    Image.new("RGB", (40, 40), (16, 40, 70)).save(path)
    return path


class CleanupTestCase(DBTestCase):
    def setUp(self):
        super().setUp()
        bot._CHAT_SESSIONS.clear()
        self.addCleanup(bot._CHAT_SESSIONS.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def session(self, chat_id):
        return bot._chat_session(chat_id)


class TestEditInsteadOfNewMessage(CleanupTestCase):
    def test_same_kind_callback_edits_previous_message(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        chat_id = message.chat.id

        run(bot.deliver(message, text="Первый экран"))
        first_id = message.sent_ids[0]
        self.assertEqual(len(message.answers), 1)

        callback = CleanupCallback("menu", message)
        run(bot.deliver(callback, text="Второй экран", edit=True))

        # Никаких новых сообщений: правится то же самое.
        self.assertEqual(len(message.answers), 1)
        self.assertEqual(fake.edited, [("text", chat_id, first_id, "Второй экран")])
        self.assertEqual(fake.deleted, [])
        self.assertEqual([entry[0] for entry in self.session(chat_id)["bot"]],
                         [first_id])

    def test_photo_to_photo_callback_edits_media(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        chat_id = message.chat.id
        tmp = self.tmpdir

        run(bot.deliver(message, photo=_png(tmp, "a.png"), caption="топ"))
        first_id = message.sent_ids[0]
        callback = CleanupCallback("graph", message)
        run(bot.deliver(callback, photo=_png(tmp, "b.png"), caption="график",
                        edit=True))

        self.assertEqual(len(message.photos), 1)
        self.assertEqual(fake.edited[0][:3], ("photo", chat_id, first_id))
        self.assertEqual(fake.deleted, [])

    def test_new_command_never_edits(self):
        fake = CleanupBot()
        first = CleanupMessage(bot_obj=fake, text="/top")
        run(bot.deliver(first, text="Топ преподавателей"))
        old_id = first.sent_ids[0]

        second = CleanupMessage(bot_obj=fake, text="/graph")
        run(bot.deliver(second, text="График нагрузки"))

        # Новое сообщение отправлено, старое удалено, правок не было.
        self.assertEqual(len(second.answers), 1)
        self.assertEqual(fake.edited, [])
        self.assertEqual(fake.deleted, [(first.chat.id, old_id)])

    def test_kind_switch_deletes_previous_screen(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        tmp = self.tmpdir

        run(bot.deliver(message, text="Текстовый экран"))
        text_id = message.sent_ids[0]
        callback = CleanupCallback("top", message)
        run(bot.deliver(callback, photo=_png(tmp, "top.png"), caption="Топ",
                        edit=True))

        self.assertEqual(len(message.photos), 1)
        self.assertEqual(fake.edited, [])
        self.assertEqual(fake.deleted, [(message.chat.id, text_id)])
        kinds = [entry[1] for entry in self.session(message.chat.id)["bot"]]
        self.assertEqual(kinds[-1], "photo")

    def test_not_modified_counts_as_success(self):
        # Telegram отвечает «message is not modified», если экран уже такой.
        # Это не ошибка: новое сообщение отправлять не нужно.
        fake = CleanupBot(fail_edit={777})
        message = CleanupMessage(bot_obj=fake)
        bot.remember_bot_message(message.chat.id, 777, "text")
        callback = CleanupCallback("menu", message)
        self.assertTrue(run(bot.deliver(callback, text="Экран", edit=True)))
        self.assertEqual(message.answers, [])  # нового сообщения нет
        self.assertEqual(message.photos, [])


class TestCommandCleanup(CleanupTestCase):
    def test_user_command_is_deleted_after_answer(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, text="/today", message_id=321)
        bot.remember_user_command(message)
        run(bot.deliver(message, text="Расписание"))

        self.assertIn((message.chat.id, 321), fake.deleted)
        self.assertEqual(self.session(message.chat.id)["commands"], [])

    def test_failed_delete_does_not_break_answer(self):
        fake = CleanupBot(fail_delete={321})
        message = CleanupMessage(bot_obj=fake, message_id=321)
        bot.remember_user_command(message)
        self.assertTrue(run(bot.deliver(message, text="Ответ")))

        # Нет прав на удаление — ответ всё равно ушёл, память не копится.
        self.assertEqual(len(message.answers), 1)
        self.assertEqual(self.session(message.chat.id)["commands"], [])

    def test_only_answered_command_is_deleted(self):
        fake = CleanupBot()
        chatter = CleanupMessage(bot_obj=fake, text="привет", message_id=11)
        bot.remember_user_command(chatter)
        command = CleanupMessage(bot_obj=fake, text="/today", message_id=12,
                                 chat_id=chatter.chat.id)
        bot.remember_user_command(command)

        run(bot.deliver(command, text="Расписание"))

        # «привет» бот не отвечал — трогать его сообщение нельзя.
        self.assertEqual(fake.deleted, [(command.chat.id, 12)])

    def test_cleanup_can_be_skipped(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, message_id=99)
        bot.remember_user_command(message)
        run(bot.deliver(message, text="Ответ", cleanup_command=False))
        self.assertEqual(fake.deleted, [])

    def test_failed_send_keeps_command_for_retry(self):
        class BrokenMessage(CleanupMessage):
            async def answer(self, text, reply_markup=None, **kwargs):
                raise RuntimeError("telegram down")

        fake = CleanupBot()
        message = BrokenMessage(bot_obj=fake, message_id=55)
        bot.remember_user_command(message)
        self.assertFalse(run(bot.deliver(message, text="Ответ")))
        # Команду не удаляем: пользователь должен видеть, что запрос не ушёл.
        self.assertEqual(fake.deleted, [])
        self.assertEqual(len(self.session(message.chat.id)["commands"]), 1)


class TestNoRightsAndNoBot(CleanupTestCase):
    def test_message_without_bot_still_answers(self):
        message = RecordingMessage(text="/today")
        message.message_id = 42
        self.assertTrue(run(bot.deliver(message, text="Ответ")))
        self.assertEqual(len(message.answers), 1)

    def test_bot_message_is_never_remembered(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        message.from_user.is_bot = True
        bot.remember_user_command(message)
        self.assertEqual(self.session(message.chat.id)["commands"], [])
        run(bot.deliver(message, text="Ответ"))
        self.assertEqual(fake.deleted, [])

    def test_cleanup_disabled_by_env(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, message_id=1000)
        with mock.patch.object(bot, "CHAT_CLEANUP", False):
            bot.remember_user_command(message)
            run(bot.deliver(message, text="Ответ"))
            self.assertEqual(len(message.answers), 1)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(bot._CHAT_SESSIONS, {})


class TestMiddleware(CleanupTestCase):
    def _call(self, middleware, event):
        handled = []

        async def handler(ev, data):
            handled.append(ev)
            result = await bot.deliver(ev, text="Ответ бота")
            return result

        run(middleware(handler, event, {}))
        return handled

    def test_command_is_remembered_then_deleted(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, text="/today", message_id=12345)
        self._call(bot._cleanup_middleware, message)
        self.assertIn((message.chat.id, 12345), fake.deleted)

    def test_service_pending_is_skipped(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, text="10.11.2026", message_id=222)
        bot.set_pending_action(message.chat.id, "date")
        self._call(bot._cleanup_middleware, message)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(self.session(message.chat.id)["commands"], [])

    def test_teacher_search_text_is_skipped(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, text="Иванов", message_id=333)
        bot.set_pending_action(message.chat.id, "teacher_search")
        self._call(bot._cleanup_middleware, message)
        self.assertEqual(fake.deleted, [])

    def test_bot_author_message_is_skipped(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, text="что-то", message_id=444)
        message.from_user.is_bot = True
        self._call(bot._cleanup_middleware, message)
        self.assertEqual(self.session(message.chat.id)["commands"], [])


class TestHistoryLimits(CleanupTestCase):
    def test_history_is_bounded(self):
        chat_id = 777
        for index in range(bot.CHAT_HISTORY_LIMIT + 5):
            bot.remember_bot_message(chat_id, 10_000 + index, "text")
            bot._chat_session(chat_id)
        session = bot._chat_session(chat_id)
        self.assertLessEqual(len(session["bot"]), bot.CHAT_HISTORY_LIMIT)

    def test_stale_entries_expire(self):
        chat_id = 888
        bot.remember_bot_message(chat_id, 1, "text")
        bot.remember_user_command(
            SimpleNamespace(chat=SimpleNamespace(id=chat_id),
                            from_user=SimpleNamespace(is_bot=False),
                            message_id=2)
        )
        with mock.patch.object(bot, "CHAT_HISTORY_TTL", -1):
            session = bot._chat_session(chat_id)
        self.assertEqual(session["bot"], [])
        self.assertEqual(session["commands"], [])


class TestSendHelpers(CleanupTestCase):
    def test_send_text_edits_for_callback_only(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        run(bot.deliver(message, text="Экран"))
        callback = CleanupCallback("today", message)
        run(bot._send_text(callback, "Тот же экран"))
        self.assertEqual(len(message.answers), 1)
        self.assertEqual(len(fake.edited), 1)

    def test_send_text_sends_new_message_for_message(self):
        fake = CleanupBot()
        first = CleanupMessage(bot_obj=fake)
        run(bot.deliver(first, text="Экран"))
        second = CleanupMessage(chat_id=first.chat.id, bot_obj=fake)
        run(bot._send_text(second, "Новый экран"))
        self.assertEqual(len(second.answers), 1)
        self.assertEqual(len(fake.edited), 0)

    def test_send_photo_records_notification(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        schedule = make_schedule(date(2026, 11, 10), "Математика")
        png = _png(self.tmpdir, "schedule.png")
        with mock.patch.object(bot, "get_cached_image",
                               mock.AsyncMock(return_value=png)), \
                mock.patch.object(bot, "record_schedule_notification") as record:
            ok = run(bot._send_photo(message, schedule))
        self.assertTrue(ok)
        self.assertEqual(record.call_count, 1)

    def test_send_photo_without_lessons_skips_notification(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        schedule = make_schedule(date(2026, 11, 10), "Математика")
        schedule.lessons = []
        png = _png(self.tmpdir, "empty.png")
        with mock.patch.object(bot, "get_cached_image",
                               mock.AsyncMock(return_value=png)), \
                mock.patch.object(bot, "record_schedule_notification") as record:
            ok = run(bot._send_photo(message, schedule))
        self.assertTrue(ok)
        self.assertFalse(record.called)


class TestDeliverFallbacks(CleanupTestCase):
    def test_edit_failure_falls_back_to_new_message(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, message_id=900)
        run(bot.deliver(message, text="Экран"))
        old_id = message.sent_ids[0]
        with mock.patch.object(bot, "_edit_message",
                               mock.AsyncMock(return_value=False)):
            callback = CleanupCallback("menu", message)
            self.assertTrue(run(bot.deliver(callback, text="Новый", edit=True)))
        self.assertEqual(len(message.answers), 2)
        self.assertEqual(fake.deleted, [(message.chat.id, old_id)])
        ids = [entry[0] for entry in self.session(message.chat.id)["bot"]]
        self.assertNotIn(old_id, ids)

    def test_answer_exception_is_reported(self):
        class BrokenMessage(CleanupMessage):
            async def answer(self, text, reply_markup=None, **kwargs):
                raise RuntimeError("boom")

        self.assertFalse(run(bot.deliver(BrokenMessage(bot_obj=CleanupBot()),
                                         text="Ответ")))

    def test_callback_is_edit_destination(self):
        message = CleanupMessage(bot_obj=CleanupBot())
        self.assertTrue(bot._deliver_edits(CleanupCallback("menu", message)))
        self.assertFalse(bot._deliver_edits(message))


class TestCallbackScreens(CleanupTestCase):
    """Кнопки листают один экран вместо ленты сообщений."""

    def test_menu_callback_edits_screen(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        bot.set_user_group(message.chat.id, 508)
        callback = CleanupCallback("menu", message)
        run(bot.cb_menu(callback))
        run(bot.cb_menu(callback))

        self.assertEqual(len(message.answers), 1, "меню не должно размножаться")
        self.assertEqual(len(fake.edited), 1)

    def test_graph_period_switch_edits_photo(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        bot.set_user_group(message.chat.id, 508)
        callback = CleanupCallback("load:6m", message)
        run(bot.cb_load_period(callback))
        first_len = len(message.photos)
        callback = CleanupCallback("load:12m", message)
        run(bot.cb_load_period(callback))

        self.assertEqual(first_len, 1)
        self.assertEqual(len(message.photos), 1, "смена периода — тот же экран")
        self.assertEqual(fake.edited[-1][0], "photo")

    def test_group_step_switch_is_remembered(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake, message_id=1000,
                                 text="Шаг 1")
        callback = CleanupCallback("groups:courses", message)
        run(bot._edit_or_answer(callback, "Шаг 2", bot.group_course_keyboard()))
        self.assertEqual(len(message.edits), 1)
        self.assertEqual(message.answers, [], "шаг не плодит сообщения")
        # Правка запомнена — следующий ответ не оставит её висеть.
        session = self.session(message.chat.id)
        self.assertEqual([entry[0] for entry in session["bot"]], [1000])

    def test_notifications_screen_edits(self):
        fake = CleanupBot()
        message = CleanupMessage(bot_obj=fake)
        bot.set_user_group(message.chat.id, 508)
        run(bot.cb_notifications(CleanupCallback("notifications", message)))
        run(bot.cb_notifications(CleanupCallback("notifications", message)))
        self.assertEqual(len(message.answers), 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
