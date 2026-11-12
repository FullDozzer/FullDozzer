# -*- coding: utf-8 -*-
"""Проверки редактирования экранов (bot.deliver) при полном запрете удалений.

Правила, которые здесь зафиксированы:

* callback с ответом того же типа правит прошлое сообщение бота (edit),
  а не присылает новое;
* картинка → картинка правится через editMessageMedia;
* при смене типа (текст → картинка) отправляется НОВОЕ сообщение, а
  прошлый экран остаётся на месте;
* новая команда (Message) никогда не правит и не удаляет — только новое
  сообщение внизу;
* бот НИКОГДА не удаляет сообщения: ни команды пользователя, ни свои
  прошлые экраны;
* «message is not modified» считается успехом; любая другая ошибка
  правки не ломает сценарий — ответ уходит новым сообщением;
* история ответов ограничена по размеру и по времени.
"""

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiogram.exceptions import TelegramBadRequest

from test_group_selection import RecordingCallback, RecordingMessage
from test_schedule_bot import DBTestCase, bot, run


class EditingBot(bot.Bot):
    """Фейковый Bot: записывает правки, считает попытки удалений.

    Наследник aiogram.Bot без вызова __init__ — нужен только isinstance(),
    который проверяет bot._edit_bot, и наши методы.
    """

    def __init__(self, fail_edit=()):
        self.edited = []
        self.deleted = []
        self.attempts = []
        self.fail_edit = set(fail_edit)

    async def delete_message(self, chat_id=None, message_id=None):
        # Бот не удаляет сообщения в принципе: если сюда пришли — тест падает.
        self.deleted.append((chat_id, message_id))

    async def edit_message_text(self, chat_id, message_id, text,
                                 reply_markup=None):
        self.attempts.append((chat_id, message_id, text))
        if message_id in self.fail_edit:
            raise TelegramBadRequest(
                method=None, message="Bad Request: message is not modified")
        self.edited.append(("text", chat_id, message_id, text))

    async def edit_message_media(self, chat_id, message_id, media,
                                  reply_markup=None):
        self.edited.append(("photo", chat_id, message_id, media))


class SentMessage(RecordingMessage):
    """Message с message_id и bot — как настоящее сообщение Telegram."""

    next_id = 500

    def __init__(self, chat_id=10001, text="", bot_obj=None, **kwargs):
        super().__init__(chat_id=chat_id, text=text, **kwargs)
        SentMessage.next_id += 1
        self.message_id = SentMessage.next_id
        self.bot = bot_obj
        self.sent_ids = []

    def _register(self):
        SentMessage.next_id += 1
        self.sent_ids.append(SentMessage.next_id)
        return SimpleNamespace(message_id=SentMessage.next_id)

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))
        return self._register()

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.photos.append((photo, caption, reply_markup))
        return self._register()


def _png(tmp_dir: Path, name: str) -> Path:
    """Минимальный PNG-файл для отправки фото."""
    from PIL import Image

    path = tmp_dir / name
    Image.new("RGB", (40, 40), (16, 40, 70)).save(path)
    return path


class EditingTestCase(DBTestCase):
    def setUp(self):
        super().setUp()
        bot._CHAT_SESSIONS.clear()
        self.addCleanup(bot._CHAT_SESSIONS.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def history(self, chat_id):
        return bot._chat_session(chat_id)


class TestEditInsteadOfNewMessage(EditingTestCase):
    def test_same_kind_callback_edits_previous_message(self):
        fake = EditingBot()
        message = SentMessage(bot_obj=fake)
        self.assertTrue(run(bot.deliver(message, text="Первый экран")))
        self.assertEqual(len(message.answers), 1)
        first_id = message.sent_ids[-1]
        message.answers.clear()

        callback = RecordingCallback("nav:next", message, bot_obj=fake)
        self.assertTrue(run(bot.deliver(callback, text="Второй экран", edit=True)))

        self.assertEqual(message.answers, [])          # нового сообщения нет
        self.assertEqual(fake.edited, [("text", message.chat.id, first_id,
                                       "Второй экран")])
        self.assertEqual(fake.deleted, [])              # и удалений нет
        self.assertEqual(self.history(message.chat.id)[-1][0], first_id)

    def test_photo_to_photo_callback_edits_media(self):
        fake = EditingBot()
        path = _png(self.tmpdir, "sched.png")
        message = SentMessage(bot_obj=fake)
        self.assertTrue(run(bot.deliver(message, photo=path, caption="Раз")))
        first_id = message.sent_ids[-1]
        message.photos.clear()

        callback = RecordingCallback("nav:next", message, bot_obj=fake)
        self.assertTrue(run(bot.deliver(callback, photo=path, caption="Два",
                                        edit=True)))

        self.assertEqual(message.photos, [])
        self.assertEqual(fake.edited[0][:3], ("photo", message.chat.id, first_id))
        self.assertEqual(fake.deleted, [])

    def test_new_command_never_edits_and_never_deletes(self):
        fake = EditingBot()
        message = SentMessage(bot_obj=fake)
        self.assertTrue(run(bot.deliver(message, text="Первый экран")))
        old_id = message.sent_ids[-1]

        second = SentMessage(bot_obj=fake, text="/top")
        self.assertTrue(run(bot.deliver(second, text="Второй экран")))

        self.assertEqual(len(second.answers), 1)
        self.assertEqual(fake.edited, [])
        self.assertEqual(fake.deleted, [])
        # Прошлый экран не удалён и не перезаписан: оба сообщения остались
        # и в чате, и в памяти бота (их ещё можно отредактировать).
        history = self.history(second.chat.id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1][0], second.sent_ids[-1])
        self.assertNotEqual(second.sent_ids[-1], old_id)

    def test_kind_switch_sends_new_message_and_keeps_previous(self):
        fake = EditingBot()
        path = _png(self.tmpdir, "graph.png")
        message = SentMessage(bot_obj=fake)
        self.assertTrue(run(bot.deliver(message, text="Текстовый экран")))
        text_id = message.sent_ids[-1]

        callback = RecordingCallback("load:1m", message, bot_obj=fake)
        self.assertTrue(run(bot.deliver(callback, photo=path, caption="График",
                                        edit=True)))

        self.assertEqual(len(message.photos), 1)     # картинка ушла новым сообщением
        self.assertEqual(fake.edited, [])            # текст картинкой не отредактировать
        self.assertEqual(fake.deleted, [])
        # Прошлый текстовый экран остался в чате и в истории бота.
        self.assertEqual(self.history(message.chat.id)[0][0], text_id)
        self.assertEqual(self.history(message.chat.id)[-1],
                         (message.sent_ids[-1], "photo",
                          self.history(message.chat.id)[-1][2]))

    def test_not_modified_counts_as_success(self):
        fake = EditingBot(fail_edit=[1])
        message = SentMessage(bot_obj=fake)
        bot._chat_session(message.chat.id).append((1, "text", time.time()))

        callback = RecordingCallback("nav:next", message, bot_obj=fake)
        self.assertTrue(run(bot.deliver(callback, text="Экран", edit=True)))

        self.assertEqual(message.answers, [])   # нового сообщения нет
        self.assertEqual(message.photos, [])
        self.assertEqual(fake.attempts, [(message.chat.id, 1, "Экран")])
        self.assertEqual(fake.deleted, [])
        # Экран остался тем же сообщением — запись просто продлили.
        self.assertEqual(self.history(message.chat.id)[-1][:2], (1, "text"))

    def test_failed_edit_falls_back_to_new_message(self):
        class RejectingBot(EditingBot):
            async def edit_message_text(self, *args, **kwargs):
                raise RuntimeError("нельзя редактировать это сообщение")

        fake = RejectingBot()
        message = SentMessage(bot_obj=fake)
        self.assertTrue(run(bot.deliver(message, text="Первый экран")))
        first_id = message.sent_ids[-1]
        message.answers.clear()

        callback = RecordingCallback("nav:next", message, bot_obj=fake)
        self.assertTrue(run(bot.deliver(callback, text="Второй экран", edit=True)))

        self.assertEqual(len(message.answers), 1)   # ответ всё равно доставлен
        self.assertEqual(fake.deleted, [])           # прошлый экран не удалён
        self.assertEqual(self.history(message.chat.id)[-1][0], message.sent_ids[-1])
        self.assertEqual(self.history(message.chat.id)[0][0], first_id)


class TestNoDeletionAnywhere(EditingTestCase):
    def test_user_command_is_never_deleted(self):
        fake = EditingBot()
        message = SentMessage(bot_obj=fake, text="топ")
        self.assertTrue(run(bot.deliver(message, text="Ответ")))

        self.assertEqual(fake.deleted, [])

    def test_only_real_bot_instances_may_edit(self):
        # Fake-бот, не являющийся aiogram.Bot, не получает правок — и не удалений.
        class NotABot:
            def __init__(self):
                self.edited = []
                self.deleted = []

            async def edit_message_text(self, *args, **kwargs):
                self.edited.append(args)

            async def delete_message(self, *args, **kwargs):
                self.deleted.append(args)

        plain = NotABot()
        message = SentMessage(bot_obj=plain)
        self.assertTrue(run(bot.deliver(message, text="Экран")))
        callback = RecordingCallback("nav:next", message, bot_obj=plain)
        self.assertTrue(run(bot.deliver(callback, text="Экран 2", edit=True)))

        self.assertEqual(plain.edited, [])
        self.assertEqual(plain.deleted, [])
        # Правка невозможна — ушёл новое сообщение, удалений не было.
        self.assertEqual(len(message.answers), 2)

    def test_message_without_bot_still_answers(self):
        message = SentMessage(bot_obj=None)
        self.assertTrue(run(bot.deliver(message, text="Ответ")))
        self.assertEqual(len(message.answers), 1)

    def test_delete_api_is_not_used_by_deliver(self):
        fake = EditingBot()
        message = SentMessage(bot_obj=fake)
        with mock.patch.object(bot.Bot, "delete_message",
                               side_effect=AssertionError("бот не удаляет")) as guard:
            for kind, kwargs in (("text", {"text": "Раз"}),
                                 ("text", {"text": "Два"})):
                self.assertTrue(run(bot.deliver(message, **kwargs)))
                self.assertTrue(run(bot.deliver(
                    RecordingCallback("nav:next", message, bot_obj=fake),
                    edit=True, **kwargs)))
        self.assertEqual(guard.call_count, 0)


class TestHelpers(EditingTestCase):
    def test_deliver_edits_only_for_callbacks(self):
        fake = EditingBot()
        message = SentMessage(bot_obj=fake)
        callback = RecordingCallback("nav:next", message, bot_obj=fake)

        self.assertFalse(bot._deliver_edits(message))
        self.assertTrue(bot._deliver_edits(callback))

    def test_send_text_edits_for_callback_only(self):
        fake = EditingBot()
        message = SentMessage(bot_obj=fake)
        self.assertTrue(run(bot._send_text(message, "Команда")))
        self.assertTrue(run(bot._send_text(
            RecordingCallback("nav:next", message, bot_obj=fake), "Кнопка")))

        self.assertEqual(len(message.answers), 1)   # команда — новое сообщение
        self.assertEqual(len(fake.edited), 1)        # кнопка — правка на месте
        self.assertEqual(fake.deleted, [])

    def test_history_is_bounded(self):
        fake = EditingBot()
        message = SentMessage(bot_obj=fake)
        chat_id = message.chat.id
        for i in range(bot.CHAT_HISTORY_LIMIT + 5):
            self.assertTrue(run(bot.deliver(message, text=f"Экран {i}")))
        self.assertLessEqual(len(self.history(chat_id)), bot.CHAT_HISTORY_LIMIT)
        self.assertEqual(fake.deleted, [])

    def test_stale_entries_expire(self):
        fake = EditingBot()
        message = SentMessage(bot_obj=fake)
        chat_id = message.chat.id
        self.assertTrue(run(bot.deliver(message, text="Экран")))
        stale = self.history(chat_id)[0][2] - bot.CHAT_HISTORY_TTL - 1
        bot._CHAT_SESSIONS[chat_id] = [
            (mid, kind, stale) for mid, kind, _stamp in self.history(chat_id)
        ]

        self.assertEqual(self.history(chat_id), [])
        callback = RecordingCallback("nav:next", message, bot_obj=fake)
        self.assertTrue(run(bot.deliver(callback, text="Новый экран", edit=True)))
        self.assertEqual(fake.edited, [])            # править уже нечего
        self.assertEqual(len(message.answers), 2)   # ушло новое сообщение
        self.assertEqual(fake.deleted, [])
