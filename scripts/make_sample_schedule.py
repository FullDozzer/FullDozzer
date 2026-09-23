#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тестовая карточка расписания 1080×920 на реалистичных данных.

Запуск (из корня репозитория):
    python scripts/make_sample_schedule.py

Данные (Четверг, 24 сентября 2026):
  I   08:30–09:50  Пром безопас          СК201     Гайзуллин И.Т.
  II  10:00–11:20  длинное название      УК103     длинная фамилия (изменена ауд.)
  III 11:35–12:55  Химия Н и Г           УК307     Арнаутова А.В.
  IV  13:25–14:45  1 п/гр. — отменена; 2 п/гр. — Химия Н и Г, УК312
  V   14:55–16:15  Физ-ра                бол зал 2 Кинзябаев А.И.

Изменения (right column «Изменения»):
  II  изменена аудитория УК105 → УК103;
  IV  1 п/гр. — занятие отменено (ОТМЕНА);
  IV  2 п/гр. — Химия Н и Г, УК312 · добавлено.

Проверки:
  - размер изображения 1080×920;
  - validate_layout() не нашёл проблем (текст в границах,
    без пересечений, бейджи и подгруппы помещаются);
  - края холста чистые (левая/правая/нижняя полосы — фон).

Результат сохраняется в samples/schedule_sample_1080x920.png
(и в IMAGE_DIR как обычный рендер бота).
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# Изолируем БД: сэмпл не должен трогать производственную data/.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="sched_sample_")

from datetime import date  # noqa: E402

import bot  # noqa: E402

DAY = date(2026, 9, 24)  # четверг


def make_lesson(pair, start, end, subject, room, teacher, subgroup=None):
    return bot.Lesson(
        pair=pair, time=f"{start} - {end}", subject=subject,
        teacher=teacher, room=room, start=start, end=end,
        subgroup=subgroup,
    )


def main():
    # --- история: чтобы прогресс был виден (8 акад. ч по Пром безопас) ---
    bot.record_completed_lesson(
        bot.GROUP_NAME, date(2026, 9, 20), "IV", "13:25", "14:45",
        "Пром безопас", duration=320,
    )
    bot.record_completed_lesson(
        bot.GROUP_NAME, date(2026, 9, 21), "I", "08:30", "09:50",
        "Химия Н и Г", duration=160,
    )

    lessons_new = [
        make_lesson("I", "08:30", "09:50", "Пром безопас", "СК201",
                    "Гайзуллин И.Т."),
        make_lesson("II", "10:00", "11:20",
                    "Операционные системы и программирование: "
                    "принципы, модели, практика",
                    "УК103", "Степанов Сергей Владимирович"),
        make_lesson("III", "11:35", "12:55", "Химия Н и Г", "УК307",
                    "Арнаутова А.В."),
        make_lesson("IV", "13:25", "14:45", "~..............", "—", "—",
                    subgroup="1"),
        make_lesson("IV", "13:25", "14:45", "Химия Н и Г", "УК312",
                    "Арнаутова А.В.", subgroup="2"),
        make_lesson("V", "14:55", "16:15", "Физ-ра", "бол зал 2",
                    "Кинзябаев А.И."),
    ]
    lessons_old = [
        make_lesson("I", "08:30", "09:50", "Пром безопас", "СК201",
                    "Гайзуллин И.Т."),
        make_lesson("II", "10:00", "11:20",
                    "Операционные системы и программирование: "
                    "принципы, модели, практика",
                    "УК105", "Степанов Сергей Владимирович"),
        make_lesson("III", "11:35", "12:55", "Химия Н и Г", "УК307",
                    "Арнаутова А.В."),
        make_lesson("V", "14:55", "16:15", "Физ-ра", "бол зал 2",
                    "Кинзябаев А.И."),
    ]
    old = bot.Schedule(date=DAY, group=bot.GROUP_NAME, lessons=lessons_old)
    new = bot.Schedule(date=DAY, group=bot.GROUP_NAME, lessons=lessons_new)
    changes = bot.compare_schedules(old, new)
    print(f"Изменений: {len(changes)}")
    for c in changes:
        print(f"  - {c.kind} | пара {c.pair} | подгруппа {c.subgroup}")

    # --- рендер ---
    path = bot.render_schedule_image(new, changes=changes,
                                     title="РАСПИСАНИЕ ИЗМЕНИЛОСЬ")
    info = dict(bot._LAST_RENDER)

    # --- проверка 1: размер ---
    from PIL import Image
    with Image.open(path) as img:
        size = img.size
    assert size[0] == 1080, f"ширина {size[0]} != 1080"
    assert size[1] == 920, f"высота {size[1]} != 920"
    print(f"Размер: {size[0]}×{size[1]} ✓")

    # --- проверка 2: validate_layout (renderer уже провалидировал) ---
    assert info["problems"] == [], f"проблемы layout: {info['problems']}"
    print(f"validate_layout: проблем нет ✓ "
          f"(элементов: {info['elements']}, уровень уплотнения: "
          f"{info['level']})")
    if info["truncated"]:
        print(f"Текст с ellipsis: {info['truncated']}")
    else:
        print("Эллипсис не понадобился (всё влезло полностью)")

    # --- проверка 3: края холста чистые (фон) ---
    with Image.open(path) as img:
        px = img.convert("RGB").load()
        bg = (0xF7, 0xF7, 0xFA)
        left_dirty = [y for y in range(0, 920, 2)
                      if px[8, y] != bg and px[8, y] != (0xDD, 0xD9, 0xEF)]
        right_dirty = [y for y in range(0, 920, 2)
                       if px[1071, y] != bg]
        bottom_dirty = [x for x in range(0, 1080, 4)
                        if px[x, 916] != bg]
    assert not left_dirty, f"левый край занят: {left_dirty[:5]}"
    assert not right_dirty, f"правый край занят: {right_dirty[:5]}"
    assert not bottom_dirty, f"нижний край занят: {bottom_dirty[:5]}"
    print("Края холста чистые (левый/правый/нижний) ✓")

    # --- сохранить сэмпл в репозиторий ---
    samples_dir = _ROOT / "samples"
    samples_dir.mkdir(exist_ok=True)
    out = samples_dir / "schedule_sample_1080x920.png"
    out.write_bytes(path.read_bytes())
    print(f"Сэмпл: {out}")
    print(f"Рендер бота: {path}")


if __name__ == "__main__":
    main()
