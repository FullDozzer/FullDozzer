# -*- coding: utf-8 -*-
"""Тесты 3-шагового выбора группы, NULL group_id, legacy-миграции и изоляции."""

import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import college
from test_schedule_bot import DBTestCase, FakeBot, bot, make_schedule, run


class RecordingMessage:
    def __init__(self, chat_id=10001, text="", user_id=None, chat_type="private",
                 full_name="Test User", username="testuser", title=""):
        uid = chat_id if user_id is None else user_id
        self.chat = SimpleNamespace(
            id=chat_id, type=chat_type, title=title, full_name=full_name
        )
        self.from_user = SimpleNamespace(
            id=uid, full_name=full_name, username=username
        )
        self.text = text
        self.answers = []
        self.edits = []
        self.photos = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.photos.append((photo, caption, reply_markup))


class RecordingCallback:
    def __init__(self, data, message, user=None, bot_obj=None):
        self.data = data
        self.message = message
        self.from_user = user or message.from_user
        self.bot = bot_obj
        self.callback_answers = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.callback_answers.append((text, show_alert))


def _button_labels(markup):
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _button_callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


class TestSpecialtyGrouping(DBTestCase):
    def test_specialty_id_takes_precedence_over_group_name(self):
        custom = college.GroupInfo(
            id=9999,
            name="НЕОБЫЧНАЯ-26",
            course=1,
            semester=1,
            study="Бюджетная",
            education="9",
            is_correspondence=False,
            has_schedule=True,
            specialty_id=28,
        )
        self.assertEqual(college.group_specialty(custom), "ЭС")

    def test_correspondence_and_fulltime_share_specialty(self):
        es_full = bot.get_group(508)   # ЭС7-24
        es_corr = bot.get_group(512)   # ЭСз1-24
        bs_full = bot.get_group(542)   # БС1-26
        bs_corr = next(g for g in bot.GROUPS.values() if g.name == "БСз-26")
        ep_full_4 = next(g for g in bot.GROUPS.values() if g.name == "ЭП1-23")
        ep_corr_4 = next(g for g in bot.GROUPS.values() if g.name == "ЭПз-23")

        self.assertEqual(college.group_specialty(es_full), "ЭС")
        self.assertEqual(college.group_specialty(es_corr), "ЭС")
        self.assertEqual(college.group_specialty(bs_full), "БС")
        self.assertEqual(college.group_specialty(bs_corr), "БС")
        self.assertEqual(college.group_specialty(ep_full_4), "ЭП")
        self.assertEqual(college.group_specialty(ep_corr_4), "ЭП")

    def test_available_specialties_have_no_empty_buttons_and_support_dynamic_catalog(self):
        for course in college.available_courses(bot.GROUPS):
            specs = college.available_specialties(bot.GROUPS, course=course)
            self.assertTrue(specs)
            for spec in specs:
                groups = college.sorted_groups(bot.GROUPS, course=course, specialty=spec)
                self.assertGreater(len(groups), 0, f"Пустая кнопка {spec} на {course} курсе")

        # 1 курс включает ОО (ООз-26), а 2 курс — нет.
        self.assertIn("ОО", college.available_specialties(bot.GROUPS, course=1))
        self.assertNotIn("ОО", college.available_specialties(bot.GROUPS, course=2))

        # Динамическая специальность с новым specialty_id появляется автоматически.
        dynamic = dict(bot.GROUPS)
        dynamic[9001] = college.GroupInfo(
            id=9001, name="ИТ1-26", course=2, semester=3, study="Бюджетная",
            education="9", is_correspondence=False, has_schedule=True,
            specialty_id=999,
        )
        dynamic[9002] = college.GroupInfo(
            id=9002, name="ИТз-26", course=2, semester=3, study="Коммерческая",
            education="11", is_correspondence=True, has_schedule=True,
            specialty_id=999,
        )
        specs_2 = college.available_specialties(dynamic, course=2)
        self.assertIn("ИТ", specs_2)
        it_groups = college.sorted_groups(dynamic, course=2, specialty="ИТ")
        self.assertEqual([g.name for g in it_groups], ["ИТ1-26", "ИТз-26"])


class TestNewUserNullGroup(DBTestCase):
    def test_new_user_gets_none_group_id_not_508(self):
        settings = bot.get_user_settings(555001)
        self.assertIsNone(settings["group_id"])

        user = SimpleNamespace(id=555001, full_name="Ivan", username="ivan")
        bot.save_user_profile(user, 555001)
        self.assertIsNone(bot.get_user_settings(555001)["group_id"])

        bot.set_pending_action(555001, "date")
        self.assertIsNone(bot.get_user_settings(555001)["group_id"])

        bot.set_user_staff(555001, 321)
        self.assertIsNone(bot.get_user_settings(555001)["group_id"])

    def test_first_start_prompts_course_selection_without_assigning_group(self):
        msg = RecordingMessage(chat_id=555002, text="/start")
        run(bot.cmd_start(msg))

        self.assertIsNone(bot.get_user_settings(555002)["group_id"])
        self.assertEqual(len(msg.answers), 1)
        text, markup = msg.answers[0]
        self.assertIn("Выберите курс", text)
        labels = _button_labels(markup)
        self.assertIn("1 курс", labels)
        self.assertIn("2 курс", labels)
        self.assertIn("3 курс", labels)
        self.assertIn("4 курс", labels)
        self.assertNotIn("ЭС7-24", text)

    def test_commands_and_text_before_group_do_not_fetch_schedule_or_start_study_calc(self):
        chat_id = 555003
        fetch_calls = []

        async def fail_get_schedule(*args, **kwargs):
            fetch_calls.append(("get_schedule", args, kwargs))
            return make_schedule(date(2026, 11, 10), "Не должно загружаться")

        async def fail_get_cached(*args, **kwargs):
            fetch_calls.append(("get_schedule_cached", args, kwargs))
            return make_schedule(date(2026, 11, 10), "Не должно загружаться")

        study_calls = []

        def track_ensure_study(group_id):
            study_calls.append(group_id)
            return True

        with mock.patch.object(bot, "get_schedule", fail_get_schedule), \
             mock.patch.object(bot, "get_schedule_cached", fail_get_cached), \
             mock.patch.object(bot, "ensure_study_history", track_ensure_study):
            for handler, text in (
                (bot.cmd_today, "/today"),
                (bot.cmd_schedule, "/schedule"),
                (bot.cmd_date, "/date 10.11.2026"),
                (bot.cmd_date, "/date"),
                (bot.cmd_text_schedule, "расписание"),
                (bot.cmd_text_schedule, "расписание на сегодня"),
                (bot.cmd_subscribe, "/subscribe"),
            ):
                msg = RecordingMessage(chat_id=chat_id, text=text)
                run(handler(msg))
                self.assertEqual(len(msg.answers), 1, f"Нет ответа для {text}")
                reply_text, markup = msg.answers[0]
                self.assertIn("Сначала выберите группу", reply_text)
                self.assertIn("1 курс", _button_labels(markup))

            # Inline-кнопки без выбранной группы тоже не загружают расписание и не подписывают.
            for cb_data in ("groupinfo", "notifications", "subscribe", "date"):
                host_msg = RecordingMessage(chat_id=chat_id, text=".")
                cb = RecordingCallback(cb_data, host_msg)
                if cb_data == "groupinfo":
                    run(bot.cb_group_info(cb))
                elif cb_data == "notifications":
                    run(bot.cb_notifications(cb))
                elif cb_data == "subscribe":
                    run(bot.cb_subscribe(cb))
                elif cb_data == "date":
                    run(bot.cb_date(cb))
                self.assertTrue(host_msg.answers, f"Нет ответа для callback {cb_data}")
                self.assertIn("Сначала выберите группу", host_msg.answers[0][0])

        self.assertEqual(fetch_calls, [])
        self.assertEqual(study_calls, [])
        self.assertFalse(bot.subscribe_user(chat_id))
        self.assertIsNone(bot.subscriber_info(chat_id))
        self.assertEqual(bot.active_group_ids(), [])
        with bot.db_connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM study_jobs").fetchone()[0], 0
            )

    def test_schedule_monitor_parses_all_groups_without_backfill_when_no_active_groups(self):
        class StopLoop(Exception):
            pass

        async def stop_sleep(sec):
            raise StopLoop()

        backfill_calls = []
        recalc_calls = []
        schedule_calls = []

        async def track_backfill(*args, **kwargs):
            backfill_calls.append((args, kwargs))
            return 0

        async def track_recalc(*args, **kwargs):
            recalc_calls.append((args, kwargs))
            return 0

        async def track_get_schedule(day, group_id=None):
            schedule_calls.append((day, group_id))
            sched = make_schedule(day, "Плановая пара")
            if group_id is not None:
                sched.group = bot.get_group(group_id).name
            return sched

        # Новый пользователь зарегистрировался через /start, но группу не выбрал.
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
        run(bot.cmd_start(RecordingMessage(chat_id=555004, text="/start")))
        self.assertEqual(bot.active_group_ids(), [])
        fake_bot = FakeBot()

        with mock.patch.object(
            bot, "now_local", return_value=datetime(2026, 11, 10, 12, 0, 0)
        ), mock.patch.object(bot, "backfill_lesson_history", track_backfill), \
             mock.patch.object(bot, "recalculate_study_history", track_recalc), \
             mock.patch.object(bot, "get_schedule", track_get_schedule), \
             mock.patch.object(bot, "check_birthdays", mock.AsyncMock(return_value=False)), \
             mock.patch("asyncio.sleep", stop_sleep):
            with self.assertRaises(StopLoop):
                run(bot.schedule_monitor(fake_bot))

        # Без выбранных пользователями групп backfill истории не запускается,
        # но расписание сегодня/завтра парсится для всех групп каталога.
        self.assertEqual(backfill_calls, [])
        self.assertEqual(recalc_calls, [])
        self.assertEqual(fake_bot.sent, [])
        self.assertEqual(len(schedule_calls), len(bot.GROUPS) * 2)
        today = date(2026, 11, 10)
        tomorrow = date(2026, 11, 11)
        for gid in bot.GROUPS:
            self.assertIsNotNone(bot.load_cached_schedule(f"group:{gid}", today))
            self.assertIsNotNone(bot.load_cached_schedule(f"group:{gid}", tomorrow))
        with bot.db_connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lesson_history").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM study_jobs").fetchone()[0], 0)


class TestThreeStepSelectionFlow(DBTestCase):
    def test_course_specialty_group_back_pagination_and_selection(self):
        chat_id = 666001
        host_msg = RecordingMessage(chat_id=chat_id, text="/groups")

        # Шаг 1: курсы.
        run(bot.cmd_groups(host_msg))
        _, course_markup = host_msg.answers[-1]
        self.assertIn("1 курс", _button_labels(course_markup))
        self.assertIn("3 курс", _button_labels(course_markup))

        # Шаг 2: выбираем 3 курс -> видим только реальные специальности 3 курса (включая ЭС, без ОО).
        cb_course = RecordingCallback("groups:course:3", host_msg)
        run(bot.cb_groups_course(cb_course))
        text_step2, spec_markup = host_msg.edits[-1]
        self.assertIn("3 курс", text_step2)
        spec_labels = _button_labels(spec_markup)
        self.assertIn("ЭС", spec_labels)
        self.assertIn("БС", spec_labels)
        self.assertNotIn("ОО", spec_labels)
        self.assertIn("← Назад к курсам", spec_labels)

        # Кнопка «Назад к курсам» возвращает к курсам.
        run(bot.cb_groups_courses(RecordingCallback("groups:courses", host_msg)))
        _, back_to_courses = host_msg.edits[-1]
        self.assertIn("1 курс", _button_labels(back_to_courses))

        # Снова 3 курс -> специальность ЭС -> страница 1 и пагинация на страницу 2 (ЭСз1-24, ЭСз2-24).
        run(bot.cb_groups_course(RecordingCallback("groups:course:3", host_msg)))
        run(bot.cb_groups_spec(RecordingCallback("groups:spec:3:ЭС:0", host_msg)))
        text_step3_p1, grp_markup_p1 = host_msg.edits[-1]
        self.assertIn("ЭС", text_step3_p1)
        p1_labels = _button_labels(grp_markup_p1)
        self.assertIn("ЭС1-24", p1_labels)
        self.assertIn("1/2", p1_labels)
        self.assertNotIn("БС1-24", p1_labels)
        self.assertIn("← Назад к специальностям", p1_labels)

        run(bot.cb_groups_spec(RecordingCallback("groups:spec:3:ЭС:1", host_msg)))
        _, grp_markup_p2 = host_msg.edits[-1]
        p2_labels = _button_labels(grp_markup_p2)
        self.assertIn("ЭСз1-24", p2_labels)
        self.assertIn("ЭСз2-24", p2_labels)
        self.assertIn("2/2", p2_labels)

        # Кнопка «Назад к специальностям» из списка групп возвращает к специальностям 3 курса.
        run(bot.cb_groups_course(RecordingCallback("groups:course:3", host_msg)))
        _, back_to_specs = host_msg.edits[-1]
        self.assertIn("ЭС", _button_labels(back_to_specs))
        self.assertIn("← Назад к курсам", _button_labels(back_to_specs))

        # До выбора группы ensure_study_history не запускался.
        self.assertIsNone(bot.get_user_settings(chat_id)["group_id"])

        # Выбор группы запускает подсчёт изученных часов и сохраняет группу.
        es_corr = next(g for g in bot.GROUPS.values() if g.name == "ЭСз1-24")
        study_started = []
        with mock.patch.object(
            bot, "ensure_study_history",
            side_effect=lambda gid: study_started.append(gid) or True,
        ):
            run(bot.cb_group_set(RecordingCallback(f"group:set:{es_corr.id}", host_msg)))

        self.assertEqual(study_started, [es_corr.id])
        self.assertEqual(bot.get_user_settings(chat_id)["group_id"], es_corr.id)
        confirm_text, confirm_markup = host_msg.answers[-1]
        self.assertIn("ЭСз1-24", confirm_text)
        self.assertIn("первичный подсчёт", confirm_text)
        self.assertIn("Сегодня", _button_labels(confirm_markup))
        self.assertIn("О группе", _button_labels(confirm_markup))

        # Повторный /start уже не заставляет выбирать группу заново.
        start_msg = RecordingMessage(chat_id=chat_id, text="/start")
        run(bot.cmd_start(start_msg))
        self.assertIn("ЭСз1-24", start_msg.answers[0][0])


class TestTeacherWithoutStudentGroup(DBTestCase):
    def test_teacher_search_and_notifications_work_without_student_group(self):
        chat_id = 777001
        staff_id = next(iter(bot.STAFF_BY_ID))
        member = bot.STAFF_BY_ID[staff_id]
        staff_sched = make_schedule(date(2026, 11, 11), "Информатика")
        staff_sched.schedule_type = "staff"
        staff_sched.staff_id = staff_id
        staff_sched.staff_name = member.full_name

        # 1. Запрос расписания преподавателя при group_id=None работает без выбора группы.
        surname = member.full_name.split()[0]
        msg = RecordingMessage(chat_id=chat_id, text=f"расписание преподавателя {surname}")
        with mock.patch.object(bot, "get_staff_schedule_cached", mock.AsyncMock(return_value=staff_sched)), \
             mock.patch.object(bot, "get_cached_image", mock.AsyncMock(return_value=Path("/tmp/staff.png"))), \
             mock.patch.object(bot, "FSInputFile", side_effect=lambda p: str(p)):
            run(bot.cmd_text_schedule(msg))

        self.assertIsNone(bot.get_user_settings(chat_id)["group_id"])
        self.assertEqual(bot.get_user_settings(chat_id)["role"], "teacher")
        self.assertTrue(msg.photos or msg.answers)

        # 2. Включение уведомлений преподавателя работает без выбранной студенческой группы.
        host_msg = RecordingMessage(chat_id=chat_id, text=".")
        run(bot.cb_teacher_notify(RecordingCallback(f"teacher:notify:{staff_id}:1", host_msg)))
        self.assertTrue(bot.teacher_subscription(chat_id, staff_id))
        self.assertIsNone(bot.get_user_settings(chat_id)["group_id"])

        # 3. Кнопка «Уведомления» показывает подписку преподавателя.
        notif_msg = RecordingMessage(chat_id=chat_id, text=".")
        run(bot.cb_notifications(RecordingCallback("notifications", notif_msg)))
        notif_text, notif_markup = notif_msg.answers[-1]
        self.assertIn(member.full_name, notif_text)
        self.assertIn(f"teacher:notify:{staff_id}:0", _button_callbacks(notif_markup))

        # 4. Монитор видит преподавателя даже при пустом active_group_ids().
        self.assertEqual(bot.active_group_ids(), [])
        self.assertEqual(bot.load_monitored_staff_ids(), [staff_id])


class TestLegacyMigrationCompatibility(DBTestCase):
    def test_pre_global_subscribers_migrate_to_legacy_group_508_while_new_users_stay_null(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE subscribers (user_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO subscribers VALUES (90001, '2025-10-01 12:00:00')"
            )
            conn.commit()
            conn.close()

            with mock.patch.object(bot, "DB_PATH", db_path):
                bot.init_db()
                # Legacy-подписчик сохранил подписку и получил ЭС7-24 (508).
                self.assertEqual(bot.load_subscribers(508), [90001])
                self.assertEqual(bot.get_user_settings(90001)["group_id"], 508)
                self.assertIsNotNone(bot.subscriber_info(90001))

                # Новый пользователь получает NULL, а не 508.
                new_user = SimpleNamespace(id=90002, full_name="New", username="new")
                bot.save_user_profile(new_user, 90002)
                self.assertIsNone(bot.get_user_settings(90002)["group_id"])
                self.assertNotIn(90002, bot.load_subscribers(508))

                # Старому пользователю /start сразу показывает ЭС7-24 без повторного выбора.
                legacy_msg = RecordingMessage(chat_id=90001, text="/start")
                run(bot.cmd_start(legacy_msg))
                self.assertIn("ЭС7-24", legacy_msg.answers[0][0])

    def test_old_user_settings_with_default_508_migrates_to_nullable_and_keeps_existing_groups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pr30.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE subscribers (
                    user_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    chat_type TEXT NOT NULL DEFAULT 'private',
                    title TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE user_settings (
                    chat_id INTEGER PRIMARY KEY,
                    group_id INTEGER NOT NULL DEFAULT 508,
                    staff_id INTEGER,
                    role TEXT NOT NULL DEFAULT 'student',
                    pending_action TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO subscribers VALUES (80001, '2026-01-10 08:00:00', 'private', '')"
            )
            conn.execute(
                "INSERT INTO user_settings (chat_id, group_id, updated_at) VALUES (80001, 508, '2026-01-10 08:00:00')"
            )
            conn.execute(
                "INSERT INTO user_settings (chat_id, group_id, updated_at) VALUES (80002, 542, '2026-01-11 08:00:00')"
            )
            conn.commit()
            conn.close()

            with mock.patch.object(bot, "DB_PATH", db_path):
                bot.init_db()
                self.assertEqual(bot.get_user_settings(80001)["group_id"], 508)
                self.assertEqual(bot.get_user_settings(80002)["group_id"], 542)
                self.assertEqual(bot.load_subscribers(508), [80001])

                # Схема user_settings теперь разрешает NULL без DEFAULT 508.
                with bot.db_connect() as check_conn:
                    cols = {
                        r["name"]: dict(r)
                        for r in check_conn.execute("PRAGMA table_info(user_settings)")
                    }
                self.assertEqual(cols["group_id"]["notnull"], 0)
                self.assertIsNone(cols["group_id"]["dflt_value"])

                # Новый пользователь после миграции получает NULL.
                bot.save_user_profile(
                    SimpleNamespace(id=80003, full_name="Fresh", username="fresh"),
                    80003,
                )
                self.assertIsNone(bot.get_user_settings(80003)["group_id"])
