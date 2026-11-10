#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сэмплы аналитики: «топ преподавателей» и «график нагрузки».

Запуск (из корня репозитория):
    python scripts/make_sample_analytics.py

Скрипт наполняет изолированную БД (DATA_DIR во временном каталоге)
реалистичной историей занятий и сохраняет в samples/:

  analytics_top.png        — топ преподавателей группы по изученным часам;
  analytics_load_<период>.png — график нагрузки для месяца / 3 / 6 / 12;
  analytics_load_preview_390.png — превью самого частого периода (3 месяца).

Ничего в производственной data/ не пишется.
"""

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="analytics_sample_")

from PIL import Image  # noqa: E402

import bot  # noqa: E402

GROUP = bot.GROUP_NAME
TODAY = date(2026, 11, 10)
TEACHERS = (
    ("Гайзуллин И.Т.", "Пром безопасность"),
    ("Арнаутова А.В.", "Химия Н и Г"),
    ("Степанов С.В.", "Эксплуатация Н/Г мест"),
    ("Мурзабулатова Ф.Ф.", "Ин.яз в проф"),
    ("Кинзябаев А.И.", "Физ-ра"),
)
# Плотность недели: где-то по 4 пары в день, где-то почти пусто.
WEEK_PROFILE = (3, 2, 0, 2, 1, 0, 0)


# Вес преподавателя: у первого пар больше — топ получается «живым».
WEIGHTS = (0.42, 0.24, 0.16, 0.11, 0.07)


def pick_teacher(seed: int) -> tuple:
    """Выбирает преподавателя по весам, но детерминированно."""
    roll = ((seed * 7919) % 100) / 100.0
    acc = 0.0
    for (teacher, subject), weight in zip(TEACHERS, WEIGHTS):
        acc += weight
        if roll <= acc:
            return teacher, subject
    return TEACHERS[-1]


def seed_history() -> None:
    """Заполняет историю занятий с начала семестра по «сегодня»."""
    start = bot.get_academic_year_start(TODAY)
    day = start
    index = 0
    while day < TODAY:
        per_day = WEEK_PROFILE[day.weekday()]
        if per_day and day.weekday() != 6:
            for pair in range(per_day):
                teacher, subject = pick_teacher(index * 5 + pair)
                bot.record_completed_lesson(
                    GROUP,
                    day,
                    ["I", "II", "III", "IV", "V"][pair],
                    "08:30",
                    "09:50",
                    subject,
                    teacher=teacher,
                    room=f"УК{200 + pair}",
                    duration=80,
                )
            index += 1
        day += timedelta(days=1)


def main() -> None:
    bot.init_db()
    seed_history()

    samples = _ROOT / "samples"
    samples.mkdir(exist_ok=True)

    # --- топ преподавателей ---
    ranking = bot.load_teacher_rankings(GROUP)
    print(f"Топ: {len(ranking)} преподавателей")
    for entry in ranking:
        print(f"  {entry.display_name} — {entry.academic_hours:.1f} акад. ч, "
              f"{entry.lessons} пар")
    top_path = bot.render_top_teachers_image(
        GROUP, ranking, "С начала учебного года"
    )
    (samples / "analytics_top.png").write_bytes(top_path.read_bytes())

    # --- график нагрузки по всем периодам ---
    for key, (label, _months) in bot.LOAD_CHART_PERIODS.items():
        data = bot.load_workload_series(GROUP, key, TODAY)
        path = bot.render_workload_chart(GROUP, data, key)
        target = samples / f"analytics_load_{key}.png"
        target.write_bytes(path.read_bytes())
        print(f"График {label}: {data['total_hours']:.1f} акад. ч, "
              f"пик {data['peak_hours']:.1f}, недель {len(data['points'])} "
              f"-> {target.name}")

    preview_src = samples / "analytics_load_3m.png"
    with Image.open(preview_src) as img:
        width = 390
        img.resize((width, round(img.size[1] * width / img.size[0])),
                   Image.LANCZOS).save(
            samples / "analytics_load_preview_390.png", "PNG", optimize=True
        )
    print("Готово: samples/analytics_top.png, samples/analytics_load_*.png")


if __name__ == "__main__":
    main()
