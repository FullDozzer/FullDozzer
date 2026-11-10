# -*- coding: utf-8 -*-
"""Проверки аналитики группы: динамическая высота, топ преподавателей,
график нагрузки с выбором периода.

Запуск:  python -m unittest discover -s tests -v
"""

from datetime import date, timedelta
from pathlib import Path
from PIL import Image

from test_group_selection import RecordingCallback, RecordingMessage
from test_schedule_bot import DBTestCase, bot, lesson, rendered_text, run


def seed_history(group_name: str, entries: list) -> None:
    """entries: (дата, пара, преподаватель, минуты)."""
    for day, pair, teacher, minutes in entries:
        bot.record_completed_lesson(
            group_name,
            day,
            pair,
            "08:30",
            "09:50",
            f"Предмет {pair}",
            teacher=teacher,
            room="УК101",
            duration=minutes,
        )


class TestDynamicCardHeight(DBTestCase):
    """Высота карточки расписания — по контенту, а не из лестницы форматов."""

    def sized(self, count: int, subgroups: int = 0) -> tuple:
        times = [("08:30", "09:50"), ("10:00", "11:20"), ("11:35", "12:55"),
                 ("13:25", "14:45"), ("14:55", "16:15"), ("16:25", "17:45"),
                 ("17:55", "19:15"), ("19:25", "20:45"), ("20:50", "22:10")]
        romans = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX"]
        lessons = []
        for index in range(count):
            start, end = times[index]
            if index < subgroups:
                for group in ("1", "2"):
                    lessons.append(lesson(
                        romans[index], group, "Ин.яз в проф", "ПК103",
                        "Мурзабулатова Ф.Ф.", start, end))
            else:
                lessons.append(lesson(
                    romans[index], None, "Пром безопасность", "СК201",
                    "Гайзуллин И.Т.", start, end))
        schedule = bot.Schedule(date=date(2026, 9, 24),
                                group=bot.GROUP_NAME, lessons=lessons)
        path = bot.render_schedule_image(schedule)
        with Image.open(path) as img:
            size = img.size
        return size, bot._LAST_RENDER["plan"]

    def test_short_day_is_a_compact_card(self):
        size, plan = self.sized(1)
        self.assertEqual(size[0], bot.SCHEDULE_WIDTH)
        self.assertGreaterEqual(size[1], bot.SCHEDULE_HEIGHT_FLOOR)
        self.assertEqual(size[1] % bot.SCHEDULE_HEIGHT_STEP, 0)
        self.assertEqual(plan["problems"] if "problems" in plan else [],
                         [])

    def test_more_pairs_make_the_canvas_taller(self):
        heights = []
        for count in (2, 4, 6, 8):
            size, _plan = self.sized(count)
            heights.append(size[1])
            self.assertEqual(bot._LAST_RENDER["problems"], [])
        self.assertEqual(heights, sorted(heights),
                         "больше пар — выше карточка")

    def test_subgroups_make_their_pair_card_taller(self):
        """Пара с подгруппами занимает больше места внутри карточки."""
        _size, plan = self.sized(4, subgroups=1)
        cards = [card["box"][3] - card["box"][1] for card in plan["cards"]]
        self.assertEqual(len(cards), 4)
        self.assertGreater(cards[0], cards[1],
                           "карточка с подгруппами выше обычной")
        self.assertGreaterEqual(plan["H"], bot.SCHEDULE_HEIGHT_FLOOR)

    def test_dense_day_stays_within_limits_and_downscales(self):
        size, plan = self.sized(9, subgroups=2)
        self.assertLessEqual(size[1], bot.SCHEDULE_HEIGHT_ABS_MAX)
        self.assertEqual(bot._LAST_RENDER["problems"], [])
        self.assertLess(plan["scale"], bot.S_SCALE_MAX,
                        "плотный день должен уплотниться")

    def test_width_is_never_changed(self):
        for count in (0, 1, 3, 7):
            size, _plan = self.sized(count)
            self.assertEqual(size[0], bot.SCHEDULE_WIDTH)


class TestTeacherTop(DBTestCase):
    """«топ» — преподаватели группы по изученным часам."""

    def setUp(self):
        super().setUp()
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
        seed_history(bot.GROUP_NAME, [
            (date(2026, 9, 1), "I", "Гайзуллин И.Т.", 80),
            (date(2026, 9, 2), "I", "Гайзуллин Ильдар Талгатович", 80),
            (date(2026, 9, 2), "II", "Арнаутова А.В.", 80),
            (date(2026, 9, 8), "I", "Гайзуллин И.Т.", 80),
            (date(2026, 9, 8), "II", "Степанов С.В.", 80),
            (date(2026, 9, 8), "III", "Степанов С.В.", 80),
            (date(2026, 9, 15), "I", "Арнаутова А.В.", 80),
            (date(2026, 9, 15), "II", "Мурзабулатова Ф.Ф.", 80),
        ])

    def test_top_is_sorted_by_minutes(self):
        ranking = bot.load_teacher_rankings(bot.GROUP_NAME)
        self.assertEqual([entry.minutes for entry in ranking],
                         sorted([entry.minutes for entry in ranking],
                                reverse=True))
        self.assertEqual(ranking[0].minutes, 240)
        self.assertEqual(ranking[0].lessons, 3)

    def test_short_and_full_names_merge(self):
        ranking = bot.load_teacher_rankings(bot.GROUP_NAME)
        keys = [bot._teacher_key(entry.name) for entry in ranking]
        self.assertEqual(len(keys), len(set(keys)),
                         "один преподаватель — одна строка топа")
        gayzullin = [entry for entry in ranking
                     if "Гайзуллин" in entry.display_name]
        self.assertEqual(len(gayzullin), 1)
        self.assertEqual(gayzullin[0].minutes, 240)

    def test_hours_are_academic(self):
        ranking = bot.load_teacher_rankings(bot.GROUP_NAME)
        top = ranking[0]
        self.assertAlmostEqual(top.academic_hours, 240 / 40)

    def test_limit_is_respected(self):
        ranking = bot.load_teacher_rankings(bot.GROUP_NAME, limit=2)
        self.assertEqual(len(ranking), 2)

    def test_text_mentions_every_teacher(self):
        ranking = bot.load_teacher_rankings(bot.GROUP_NAME)
        text = bot.top_teachers_text(ranking, bot.GROUP_NAME)
        for entry in ranking:
            # ФИО берётся из справочника — сверяем по фамилии.
            self.assertIn(entry.display_name.split()[0], text)
        self.assertIn("акад. ч", text)

    def test_empty_history_is_handled(self):
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
        ranking = bot.load_teacher_rankings(bot.GROUP_NAME)
        self.assertEqual(ranking, [])
        text = bot.top_teachers_text(ranking, bot.GROUP_NAME)
        self.assertIn("Пока нет завершённых занятий", text)

    def test_image_is_rendered_with_a_row_per_teacher(self):
        ranking = bot.load_teacher_rankings(bot.GROUP_NAME)
        path = bot.render_top_teachers_image(bot.GROUP_NAME, ranking)
        self.assertTrue(Path(path).exists())
        with Image.open(path) as img:
            self.assertEqual(img.size[0], bot.SCHEDULE_WIDTH)
        self.assertEqual(bot._LAST_TOP["rows"], len(ranking))
        # компактный список: строки не «съедают» пол-холста
        self.assertLess(bot._LAST_TOP["size"][1], 1600)

    def test_text_commands_are_parsed(self):
        self.assertTrue(bot.parse_top_text("топ"))
        self.assertTrue(bot.parse_top_text("ТОП преподавателей"))
        self.assertFalse(bot.parse_top_text("расписание"))

    def test_command_handler_sends_a_photo(self):
        chat_id = 555
        bot.set_user_group(chat_id, 508)
        message = RecordingMessage(text="/top", chat_id=chat_id)
        run(bot.cmd_top(message))
        self.assertTrue(message.photos, "топ должен уходить картинкой")


class TestWorkloadChart(DBTestCase):
    """«график» — нагрузка по неделям с периодом месяц / 3 / 6 / год."""

    def setUp(self):
        super().setUp()
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
        today = date(2026, 11, 10)
        entries = []
        for offset, minutes in ((70, 80), (69, 80), (60, 160), (45, 80),
                                (30, 240), (20, 80), (5, 160)):
            entries.append((today - timedelta(days=offset), "I",
                            "Гайзуллин И.Т.", minutes))
        seed_history(bot.GROUP_NAME, entries)
        self.today = today

    def test_all_periods_are_available(self):
        self.assertEqual(list(bot.LOAD_CHART_PERIODS), ["1m", "3m", "6m", "12m"])
        labels = [label for label, _months in bot.LOAD_CHART_PERIODS.values()]
        self.assertEqual(labels,
                         ["Месяц", "3 месяца", "6 месяцев", "Год"])

    def test_longer_period_covers_more_time(self):
        series = [bot.load_workload_series(bot.GROUP_NAME, key, self.today)
                  for key in bot.LOAD_CHART_PERIODS]
        starts = [item["start"] for item in series]
        self.assertEqual(starts, sorted(starts, reverse=True),
                         "чем больше период, тем раньше начинается график")
        self.assertEqual(len(series[-1]["points"]) > len(series[0]["points"]),
                         True)

    def test_points_are_weeks_and_sum_matches_history(self):
        data = bot.load_workload_series(bot.GROUP_NAME, "3m", self.today)
        self.assertTrue(data["points"])
        for point in data["points"]:
            self.assertEqual(point["start"].weekday(), 0,
                             "неделя начинается с понедельника")
            self.assertGreaterEqual(point["minutes"], 0)
        self.assertGreater(data["total_minutes"], 0)
        self.assertEqual(data["total_lessons"], 7)
        self.assertGreaterEqual(data["peak_hours"], data["average_hours"])

    def test_pairs_are_not_double_counted(self):
        """Пара с подгруппами — один слот времени группы и в графике."""
        with bot.db_connect() as conn:
            conn.execute("DELETE FROM lesson_history")
        day = self.today - timedelta(days=3)
        bot.record_completed_lesson(bot.GROUP_NAME, day, "II", "10:00",
                                    "11:20", "Ин.яз", subgroup="1",
                                    duration=80)
        bot.record_completed_lesson(bot.GROUP_NAME, day, "II", "10:00",
                                    "11:20", "Химия", subgroup="2",
                                    duration=80)
        data = bot.load_workload_series(bot.GROUP_NAME, "1m", self.today)
        self.assertEqual(data["total_minutes"], 80)
        self.assertEqual(data["total_lessons"], 1)

    def test_empty_period_is_explained_in_text(self):
        data = bot.load_workload_series(bot.GROUP_NAME, "1m",
                                        date(2027, 5, 10))
        self.assertEqual(data["total_minutes"], 0)
        text = bot.workload_text(data, bot.GROUP_NAME)
        self.assertIn("нет", text)

    def test_chart_image_is_rendered(self):
        for key in bot.LOAD_CHART_PERIODS:
            data = bot.load_workload_series(bot.GROUP_NAME, key, self.today)
            path = bot.render_workload_chart(bot.GROUP_NAME, data, key)
            self.assertTrue(Path(path).exists())
            with Image.open(path) as img:
                self.assertEqual(img.size[0], bot.SCHEDULE_WIDTH)
            self.assertEqual(bot._LAST_WORKLOAD["period"], key)

    def test_period_keyboard_marks_selection(self):
        markup = bot.workload_period_keyboard("6m")
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual([button.callback_data for button in buttons],
                         [f"load:{key}" for key in bot.LOAD_CHART_PERIODS])
        self.assertTrue(buttons[2].text.startswith("• "))

    def test_text_command_periods_are_parsed(self):
        self.assertEqual(bot.parse_graph_text("график"), (True, "3m"))
        self.assertEqual(bot.parse_graph_text("график за месяц"), (True, "1m"))
        self.assertEqual(bot.parse_graph_text("график 3 месяца"), (True, "3m"))
        self.assertEqual(bot.parse_graph_text("график за полгода"), (True, "6m"))
        self.assertEqual(bot.parse_graph_text("график за год"), (True, "12m"))
        self.assertEqual(bot.parse_graph_text("расписание"), (False, None))

    def test_command_handler_sends_a_photo(self):
        chat_id = 777
        bot.set_user_group(chat_id, 508)
        message = RecordingMessage(text="/graph", chat_id=chat_id)
        run(bot.cmd_graph(message))
        self.assertTrue(message.photos, "график должен уходить картинкой")

    def test_period_callback_renders_selected_period(self):
        chat_id = 778
        bot.set_user_group(chat_id, 508)
        message = RecordingMessage(text="", chat_id=chat_id)
        callback = RecordingCallback("load:12m", message)
        run(bot.cb_load_period(callback))
        self.assertTrue(message.photos)
        self.assertEqual(bot._LAST_WORKLOAD["period"], "12m")
        _photo, _caption, markup = message.photos[-1]
        self.assertIsNotNone(markup, "период можно переключить кнопками")


class TestAnalyticsCache(DBTestCase):
    """Повторный топ/график не перерисовывается, пока данные не изменились."""

    def test_second_request_reuses_the_png(self):
        bot._ANALYTICS_CACHE.clear()
        calls = []

        def render():
            calls.append(1)
            target = bot.IMAGE_DIR / "cached_analytics.png"
            target.write_bytes(b"png")
            return target

        path = bot.analytics_image_path("top", "ЭС7-24", signature=(1,),
                                        render=render)
        self.assertTrue(path.exists())
        again = bot.analytics_image_path("top", "ЭС7-24", signature=(1,),
                                        render=render)
        self.assertEqual(path, again)
        self.assertEqual(len(calls), 1, "второй раз рисовать не нужно")

    def test_changed_signature_triggers_redraw(self):
        bot._ANALYTICS_CACHE.clear()
        calls = []

        def render():
            calls.append(1)
            target = bot.IMAGE_DIR / "cached_analytics_2.png"
            target.write_bytes(b"png")
            return target

        bot.analytics_image_path("load", "ЭС7-24", period="3m",
                                 signature=(1,), render=render)
        bot.analytics_image_path("load", "ЭС7-24", period="3m",
                                 signature=(2,), render=render)
        self.assertEqual(len(calls), 2)

    def test_render_failure_returns_none(self):
        bot._ANALYTICS_CACHE.clear()

        def boom():
            raise RuntimeError("нет шрифта")

        self.assertIsNone(bot.analytics_image_path(
            "load", "ЭС7-24", period="1m", signature=("x",), render=boom
        ))
