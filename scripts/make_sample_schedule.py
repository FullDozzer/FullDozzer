#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тестовая карточка расписания (вертикаль 2:3) на реалистичных данных.

Запуск (из корня репозитория):
    python scripts/make_sample_schedule.py

Данные (Четверг, 24 сентября 2026):
  I   08:30–09:50  Пром безопас          СК201      Гайзуллин И.Т.
  II  10:00–11:20  длинное название      УК103      длинная фамилия (ауд. изменена)
  III 11:35–12:55  Химия Н и Г           УК307      Арнаутова А.В.
  IV  13:25–14:45  1 п/гр. — отменена; 2 п/гр. — Химия Н и Г, УК312
  V   14:55–16:15  Физ-ра                бол зал 2  Кинзябаев А.И.

Изменения — внутри карточек (отдельной колонки «Изменения» больше нет):
  II  изменена аудитория УК105 → УК103;
  IV  1 п/гр. — занятие отменено (ОТМЕНА), розовая плашка в карточке;
  IV  2 п/гр. — Химия Н и Г, УК312 · добавлено.

Формат выбирается по плотности дня: 1080×1620 (2:3) — базовый, при
большом количестве занятий холст растёт по лестнице SCHEDULE_RATIO_LADDER
(1740/1860/1980), но не выше SCHEDULE_HEIGHT_MAX; кропа не бывает.

Проверки:
  - формат вертикальный, ширина 1080, высота из лестницы форматов;
  - validate_layout() не нашёл проблем: текст в границах карточек и safe area,
    без пересечений, аудитория не мельче ROOM_FONT_MIN;
  - у каждой аудитории — зелёная плашка «Ауд. …» с иконкой, часы зелёным;
  - мелкие пометки (перемена, подгруппы, ИЗМЕНЕНО) — в строке времени;
  - кромка холста (20 px) — только фон и декоративные подложки;
  - дополнительно сохраняется preview на 390 px — ширина превью чата.

Результат: samples/schedule_sample_<W>x<H>.png + превью на 390 px
(и обычный рендер в IMAGE_DIR).
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

from PIL import Image  # noqa: E402

import bot  # noqa: E402

DAY = date(2026, 9, 24)  # четверг
EDGE = 20               # ширина полосы «кромка холста», которую проверяем


def make_lesson(pair, start, end, subject, room, teacher, subgroup=None,
                break_duration=""):
    return bot.Lesson(
        pair=pair, time=f"{start} - {end}", subject=subject,
        teacher=teacher, room=room, start=start, end=end,
        subgroup=subgroup, break_duration=break_duration,
    )


def build_schedule(kind):
    """kind="new" — с отменой/добавлением, kind="old" — как было раньше."""
    if kind == "old":
        return bot.Schedule(date=DAY, group=bot.GROUP_NAME, lessons=[
            make_lesson("I", "08:30", "09:50", "Пром безопас", "СК201",
                        "Гайзуллин И.Т.", break_duration="15 мин"),
            make_lesson("II", "10:00", "11:20",
                        "Операционные системы и программирование: "
                        "принципы, модели, практика",
                        "УК105", "Степанов Сергей Владимирович",
                        break_duration="15 мин"),
            make_lesson("III", "11:35", "12:55", "Химия Н и Г", "УК307",
                        "Арнаутова А.В.", break_duration="30 мин"),
            make_lesson("V", "14:55", "16:15", "Физ-ра", "бол зал 2",
                        "Кинзябаев А.И."),
        ])
    return bot.Schedule(date=DAY, group=bot.GROUP_NAME, lessons=[
        make_lesson("I", "08:30", "09:50", "Пром безопас", "СК201",
                    "Гайзуллин И.Т.", break_duration="15 мин"),
        make_lesson("II", "10:00", "11:20",
                    "Операционные системы и программирование: "
                    "принципы, модели, практика",
                    "УК103", "Степанов Сергей Владимирович",
                    break_duration="15 мин"),
        make_lesson("III", "11:35", "12:55", "Химия Н и Г", "УК307",
                    "Арнаутова А.В.", break_duration="30 мин"),
        make_lesson("IV", "13:25", "14:45", "~..............", "—", "—",
                    subgroup="1", break_duration="10 мин"),
        make_lesson("IV", "13:25", "14:45", "Химия Н и Г", "УК312",
                    "Арнаутова А.В.", subgroup="2", break_duration="10 мин"),
        make_lesson("V", "14:55", "16:15", "Физ-ра", "бол зал 2",
                    "Кинзябаев А.И."),
    ])


def hex_rgb(value: str) -> tuple:
    text = str(value).lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def edge_pixels(img, *colors, tol=6):
    """Пиксели в кромке холста, не совпавшие ни с фоном, ни с подложками."""
    px = img.convert("RGB").load()
    w, h = img.size
    bad = []
    for y in range(h):
        for x in range(w):
            if min(x, y, w - 1 - x, h - 1 - y) >= EDGE:
                continue
            pixel = px[x, y]
            if any(all(abs(pixel[i] - color[i]) <= tol for i in range(3))
                   for color in colors):
                continue
            bad.append((x, y, pixel))
    return bad


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

    new = build_schedule("new")
    old = build_schedule("old")
    changes = bot.compare_schedules(old, new)
    print(f"Изменений: {len(changes)}")
    for c in changes:
        print(f"  - {c.kind} | пара {c.pair} | подгруппа {c.subgroup}")

    # --- рендер ---
    path = bot.render_schedule_image(new, changes=changes,
                                     title="РАСПИСАНИЕ ИЗМЕНИЛОСЬ")
    info = dict(bot._LAST_RENDER)
    plan = info["plan"]

    # --- проверка 1: вертикальный формат ---
    with Image.open(path) as img:
        size = img.size
    heights = [h for h, _ in bot.SCHEDULE_RATIO_LADDER]
    assert size[0] == bot.SCHEDULE_WIDTH, f"ширина {size[0]} != 1080"
    assert size[1] in heights, \
        f"высота {size[1]} вне лестницы форматов {heights}"
    assert size[1] > size[0], "картинка должна быть вертикальной"
    assert size[1] <= bot.SCHEDULE_HEIGHT_MAX, "холст не «чрезмерно» высокий"
    ratio = size[1] / size[0]
    assert 1.3 <= ratio <= 1.9, f"вне диапазона 2:3…высокий портрет: {ratio:.2f}"
    print(f"Размер: {size[0]}×{size[1]} (1:{ratio:.2f}) ✓ "
          f"масштаб {info['scale']}")

    # --- проверка 2: validate_layout (renderer уже провалидировал) ---
    assert info["problems"] == [], f"проблемы layout: {info['problems']}"
    print(f"validate_layout: проблем нет ✓ (элементов: {info['elements']}, "
          f"карточек: {info['cards']})")
    if info["truncated"]:
        print(f"Текст с ellipsis: {info['truncated']}")
    else:
        print("Эллипсис не понадобился (всё влезло полностью)")

    # --- проверка 3: аудитория — зелёная плашка с иконкой в каждой карточке ---
    rooms = [op for op in plan["ops"] if op.get("role") == "room"]
    assert rooms, "на картинке нет ни одного блока аудитории"
    drawn = " ".join(" ".join(op["lines"]) for op in rooms)
    for label in ("Ауд. СК201", "Ауд. УК103", "Ауд. УК307", "Ауд. УК312"):
        assert label in drawn, f"аудитория {label} не отрисована"
    assert min(op["size"] for op in rooms) >= bot.ROOM_FONT_MIN, \
        "шрифт аудитории мельче пола читаемости"
    panels = [op for op in plan["ops"] if op.get("kind") == "panel"
              and op.get("fill") == bot.S_GREEN_L]
    assert len(panels) == len(rooms), "не у каждой аудитории своя плашка"
    hours = [op for op in plan["ops"] if op.get("role") == "hours"]
    teachers = [op for op in plan["ops"] if op.get("role") == "teacher"]
    if teachers:
        assert min(op["size"] for op in rooms) >= \
            max(op["size"] for op in teachers), "аудитория мельче преподавателя"
    assert hours, "нигде не показано изученное время"
    assert min(op["size"] for op in hours) > max(op["size"] for op in rooms), \
        "строка изученных часов не акцент"
    icons = [op for op in plan["ops"] if ":panel:icon" in str(op.get("id"))]
    assert icons, "у плашек аудитории нет иконки"
    print(f"Аудиторий: {len(rooms)} (плашек {len(panels)}, иконок "
          f"{len(icons)}), кегль аудитории "
          f"{min(op['size'] for op in rooms)}, часов "
          f"{min(op['size'] for op in hours)} (пол {bot.ROOM_FONT_MIN}) ✓")

    # --- проверка 4: плашка отмены и инлайн-изменения ---
    texts = [op for op in plan["ops"] if op["op"] == "text"]
    all_text = "\n".join(" ".join(op["lines"]) for op in texts)
    assert "Занятие отменено" in all_text, "нет плашки отмены в карточке"
    assert "Аудитория: УК105 → УК103" in all_text, \
        "изменение аудитории не показано внутри карточки"
    assert "panel" not in plan["owners"], "колонка «Изменения» вернулась"
    pink = [op for op in plan["ops"] if op.get("kind") == "panel"
            and op.get("fill") == bot.S_RED_L]
    assert pink, "нет розовой плашки отмены"
    print("Отмена и правки — внутри карточек, колонки «Изменения» нет ✓")

    # --- проверка 5: тихие пометки в строке времени карточек ---
    metas = [op for op in texts if str(op.get("id")).endswith(":meta")]
    labels = " | ".join(" ".join(op["lines"]) for op in metas)
    assert sum("перемена" in " ".join(op["lines"]) for op in metas) >= 2, \
        f"пометок про перемену меньше, чем ожидалось: {labels}"
    captions = [op for op in plan["ops"] if op.get("role") == "subgroup"]
    assert captions, "нет подписей подгрупп"
    assert not any("подгрупп" in " ".join(op["lines"]) for op in metas), \
        "подгруппы подписаны в карточке — дублирующая пометка не нужна"
    cards = [c["box"] for c in plan["cards"]]
    for op in metas:
        x0, y0, x1, y1 = op["bbox"]
        assert any(b[0] <= x0 and x1 <= b[2] and b[1] <= y0 and y1 <= b[3]
                   for b in cards), f"пометка вне карточки: {op['bbox']}"
    print(f"Пометки в карточках: {labels} ✓")

    # --- проверка 6: кромка холста — только фон и подложки ---
    with Image.open(path) as img:
        bad = edge_pixels(img, hex_rgb(bot.S_BG), hex_rgb(bot.S_BG_LIGHT),
                          hex_rgb(bot.S_BG_DEEP))
    assert not bad, f"в кромке {EDGE} px что-то нарисовано: {bad[:5]}"
    print(f"Кромка холста {EDGE} px чистая (фон и подложки) ✓")

    # --- сохранить сэмпл (и превью чата) в репозиторий ---
    samples_dir = _ROOT / "samples"
    samples_dir.mkdir(exist_ok=True)
    out = samples_dir / f"schedule_sample_{size[0]}x{size[1]}.png"
    out.write_bytes(path.read_bytes())
    preview_w = 390
    preview = Image.open(path).resize(
        (preview_w, round(size[1] * preview_w / size[0])), Image.LANCZOS)
    preview_out = samples_dir / "schedule_sample_preview_390.png"
    preview.save(preview_out, "PNG", optimize=True)
    print(f"Сэмпл: {out}")
    print(f"Превью чата ({preview_w} px): {preview_out}")
    print(f"Рендер бота: {path}")


if __name__ == "__main__":
    main()
