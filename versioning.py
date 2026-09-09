# -*- coding: utf-8 -*-
"""Система версий и changelog Telegram-бота.

Центральное место, где живут:
- BOT_VERSION — текущая версия бота (единственная константа);
- CHANGELOG — список изменений по версиям (структура легко расширяется:
  достаточно добавить запись для 2.1.5, 2.1.6, 2.2.0 и т.д.);
- released_at — дата/время релиза каждой версии.

Правило для released_at:
- дата НЕ выдумывается;
- значение берётся из переменной окружения (например
  CHANGELOG_2_1_4_RELEASED_AT) либо вписывается вручную в поле
  "released_at" в момент релиза;
- если значение не задано, changelog этой версии не рассылается
  (в логах появится предупреждение).

Время релиза интерпретируется в часовом поясе бота —
Asia/Yekaterinburg (UTC+5).
"""

import os
from datetime import datetime
from typing import Optional

# Текущая версия бота. Меняется здесь и нигде больше.
BOT_VERSION = "2.1.5"

# Формат даты/времени релиза (в часовом поясе бота).
RELEASED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"

# ============================================================
# CHANGELOG
# ============================================================

CHANGELOG = {
    "2.1.4": {
        # Дата/время релиза можно указать прямо здесь (вручную при релизе)…
        "released_at": "",
        # …или передать через переменную окружения (приоритет у неё).
        "released_at_env": "CHANGELOG_2_1_4_RELEASED_AT",
        "changes": [
            "Добавлена команда /today для просмотра расписания на сегодня.",
            "/schedule теперь показывает расписание только на завтра.",
            "Добавлена текстовая команда «расписание» без слэша: "
            "«расписание» — расписание на завтра, "
            "«расписание на <дата>» — расписание на указанную дату.",
            "Парсер расписания понимает русские форматы дат: "
            "сегодня, завтра, послезавтра, 4 сентября, "
            "4 сентября 2026 (…года), 04.09.2026, 04.09.26.",
            "Интервал автоматической проверки расписания изменён "
            "с 30 минут на 5 минут.",
            "Автоматическая проверка теперь отслеживает изменения "
            "расписания и на сегодня, и на завтра.",
            "Исправлена проблема, при которой могло показываться "
            "расписание другой даты.",
            "Установлен часовой пояс Екатеринбурга: "
            "Asia/Yekaterinburg (UTC+5).",
            "Исправлена обработка подгрупп: одна пара теперь "
            "может содержать несколько занятий (1 п/гр., 2 п/гр. и т.д.).",
            "Добавлено визуальное отображение изменений расписания "
            "на изображении: «Что изменилось» и «было → стало».",
        ],
    },
    "2.1.5": {
        # Точная дата релиза не следует из истории исходного проекта.
        # Заполняется конфигурацией CHANGELOG_2_1_5_RELEASED_AT.
        "released_at": "",
        "released_at_env": "CHANGELOG_2_1_5_RELEASED_AT",
        "changes": [
            "👨‍🏫 Добавлено расписание преподавателей",
            "🛡 Добавлена защита от спама",
            "🎨 Полностью обновлён дизайн бота",
        ],
    },
}


def version_key(version: str) -> tuple:
    """Версия для сравнения: '2.1.4' -> (2, 1, 4)."""
    parts = []
    for item in str(version).split("."):
        digits = "".join(ch for ch in item if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def pending_versions(last_notified_version: str) -> list:
    """Версии из CHANGELOG, которые пользователь ещё не получал.

    Учитывается только «последняя доставленная версия»; сами правила
    eligbility (существовал ли пользователь до релиза) проверяются
    отдельно для каждой версии в bot.py.
    """
    last_key = version_key(last_notified_version or "0")
    return sorted(
        (version for version in CHANGELOG if version_key(version) > last_key),
        key=version_key,
    )


def get_released_at(version: str) -> Optional[datetime]:
    """Дата/время релиза версии (naive datetime, Asia/Yekaterinburg).

    Возвращает None, если релиз ещё не назначен.
    """
    meta = CHANGELOG.get(version)
    if not meta:
        return None

    raw = os.getenv(meta.get("released_at_env", ""), "").strip()
    if not raw:
        raw = (meta.get("released_at") or "").strip()
    if not raw:
        return None

    for fmt in (RELEASED_AT_FORMAT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def changelog_text(version: str) -> str:
    """Аккуратный текст changelog для отправки пользователю."""
    meta = CHANGELOG.get(version)
    if not meta:
        return ""

    lines = [
        f"📦 <b>Обновление: версия {version}</b>",
        "",
        "Что нового:",
    ]
    for item in meta.get("changes", []):
        lines.append(f"• {item}")
    return "\n".join(lines)
