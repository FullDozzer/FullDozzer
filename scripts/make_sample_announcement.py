#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сэмпл карточки анонса «бот теперь для всего колледжа».

Запуск (из корня репозитория):
    python scripts/make_sample_announcement.py

Сохраняет в samples/:
  announcement.png             — карточка 1080 px (как её увидит подписчик);
  announcement_preview_390.png — превью шириной 390 px (ширина превью в чате).

Производственная data/ не трогается: DATA_DIR — временный каталог.
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="announce_sample_")

from PIL import Image  # noqa: E402

import bot  # noqa: E402

USERNAME = "mellowagent"


def main() -> None:
    bot.init_db()
    samples = _ROOT / "samples"
    samples.mkdir(exist_ok=True)

    path = bot.render_announcement_image(USERNAME)
    target = samples / "announcement.png"
    target.write_bytes(path.read_bytes())

    with Image.open(target) as image:
        height = round(image.size[1] * 390 / image.size[0])
        image.resize((390, height), Image.LANCZOS).save(
            samples / "announcement_preview_390.png", "PNG", optimize=True
        )
        print(f"Карточка: {image.size[0]}x{image.size[1]} → {target.name}")

    caption = bot.announce_caption(USERNAME)
    print(f"Подпись: {len(caption)} символов (лимит 1024)")
    print("-" * 60)
    print(caption)


if __name__ == "__main__":
    main()
