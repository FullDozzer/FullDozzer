# -*- coding: utf-8 -*-
"""Карточка анонса «бот теперь для всего колледжа».

Проверяем то, что легко сломать незаметно: картинка действительно
рендерится в 1080 px, текст анонса целиком живёт в подписи, а высота
холста растёт вместе с контентом (никаких обрезанных строк).
"""

from PIL import Image

from test_schedule_bot import DBTestCase, bot


class TestAnnouncementCard(DBTestCase):
    def test_card_size_and_format(self):
        path = bot.render_announcement_image("mellowagent")
        self.assertTrue(path.exists())
        with Image.open(path) as image:
            self.assertEqual(image.size[0], bot.SCHEDULE_WIDTH)
            self.assertEqual(image.format, "PNG")
            # Высота по контенту, но в разумных для карточки границах.
            self.assertGreater(image.size[1], 900)
            self.assertLess(image.size[1], bot.SCHEDULE_HEIGHT_ABS_MAX)

    def test_height_follows_content(self):
        """Больше пунктов — выше холст: высота не зашита константой."""
        short = bot.render_announcement_image("")
        with Image.open(short) as image:
            base = image.size[1]

        features = bot.ANNOUNCE_FEATURES
        try:
            bot.ANNOUNCE_FEATURES = features + (("zap", "Ещё одна строка", ""),) * 3
            taller = bot.render_announcement_image("")
            with Image.open(taller) as image:
                grown = image.size[1]
        finally:
            bot.ANNOUNCE_FEATURES = features
        self.assertGreater(grown, base)

    def test_caption_contains_every_announcement_item(self):
        caption = bot.announce_caption("mellowagent")
        self.assertIn("теперь для всего колледжа", caption.lower())
        for _icon, title, note in bot.ANNOUNCE_FEATURES:
            self.assertIn(title, caption)
            if note:
                self.assertIn(note, caption)
        self.assertIn(bot.ANNOUNCE_CALL_BODY, caption)
        self.assertIn("@mellowagent", caption)
        self.assertLess(len(caption), 1024)     # лимит подписи к фото

    def test_caption_without_username_has_no_empty_link(self):
        caption = bot.announce_caption("")
        self.assertNotIn("@", caption)

    def test_caption_escapes_html(self):
        caption = bot.announce_caption("<script>")
        self.assertNotIn("<script>", caption)

    def test_icons_are_vendored_lucide(self):
        """Иконки карточки — только вендоренные SVG из assets/icons."""
        for icon, _title, _note in bot.ANNOUNCE_FEATURES:
            self.assertIn(icon, bot._KNOWN_ICONS)
        for icon in ("rocket", "megaphone"):
            self.assertIn(icon, bot._KNOWN_ICONS)
