# -*- coding: utf-8 -*-
"""Каталог групп колледжа и клиент публичного JSON API ishnk.ru."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class GroupInfo:
    id: int
    name: str
    course: int
    semester: int
    study: str
    education: str
    is_correspondence: bool
    has_schedule: bool
    start_1: str = "2026-09-01"
    end_1: str = "2026-12-31"
    start_2: str = "2027-01-12"
    end_2: str = "2027-06-30"
    curator_id: int = 0
    specialty_id: int = 0

    @property
    def form(self) -> str:
        return "Заочная" if self.is_correspondence else "Очная"

    @property
    def base(self) -> str:
        return f"База {self.education} классов"

    @property
    def study_period(self) -> str:
        return f"{_short_date(self.start_1)} — {_short_date(self.end_2)}"

    def to_dict(self) -> dict:
        return asdict(self)


def _short_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
        return parsed.strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(value or "—")


def _int(payload: dict, *keys: str, default: int = 0) -> int:
    for key in keys:
        try:
            return int(payload.get(key, default) or default)
        except (TypeError, ValueError):
            pass
    return default


def group_from_api(group_id: int, payload: dict) -> GroupInfo:
    return GroupInfo(
        id=group_id,
        name=str(payload.get("Name") or payload.get("outName") or group_id).removeprefix("гр."),
        course=_int(payload, "realCourse", "nowCourse", "Kurs", default=1),
        semester=_int(payload, "nowSemestr", default=1),
        study=str(payload.get("Study") or "Не указано"),
        education=str(payload.get("Education") or "9"),
        is_correspondence=bool(_int(payload, "isCD")),
        has_schedule=bool(_int(payload, "isSchedule")),
        start_1=str(payload.get("dateStart1") or "2026-09-01"),
        end_1=str(payload.get("dateEnd1") or "2026-12-31"),
        start_2=str(payload.get("dateStart2") or "2027-01-12"),
        end_2=str(payload.get("dateEnd2") or "2027-06-30"),
        curator_id=_int(payload, "idFormMaster"),
        specialty_id=_int(payload, "idSpecialty"),
    )


def parse_groups_payload(payload: dict) -> dict[int, GroupInfo]:
    """Оставляет только реальные группы, исключая служебные dep_* ключи."""
    result: dict[int, GroupInfo] = {}
    for raw_id, raw_group in (payload.get("groups") or {}).items():
        if not str(raw_id).isdigit() or not isinstance(raw_group, dict):
            continue
        group = group_from_api(int(raw_id), raw_group)
        result[group.id] = group
    return result


def groups_to_payload(groups: dict[int, GroupInfo]) -> list[dict]:
    return [group.to_dict() for group in groups.values()]


def groups_from_payload(rows: list[dict]) -> dict[int, GroupInfo]:
    result = {}
    for row in rows:
        try:
            group = GroupInfo(**row)
            result[group.id] = group
        except (TypeError, ValueError):
            continue
    return result


# Снимок ответа /2025/json/groups от 10.11.2026. Используется только до
# первого успешного обновления каталога и при временной недоступности сайта.
# id, name, course, semester, study, education, correspondence, schedule,
# end_2, curator, specialty
_SEED = (
    (479,"ЭП1-23",4,7,"Бюджетная","9",0,0,"2027-06-16",87,37),
    (552,"ЭП2-26",1,1,"Коммерческая","9",0,1,"2027-06-30",294,52),
    (546,"ЭС4-26",1,1,"Коммерческая","9",0,1,"2027-06-30",295,28),
    (525,"ЭП2-25",2,3,"Коммерческая","9",0,1,"2027-06-30",288,52),
    (547,"ЭС3-26",1,1,"Коммерческая","9",0,1,"2027-06-30",245,28),
    (512,"ЭСз1-24",3,5,"Коммерческая","11",1,0,"2027-06-16",0,28),
    (516,"ЭС1-25",2,3,"Бюджетная","9",0,1,"2027-06-30",81,28),
    (524,"БС2-25",2,3,"Коммерческая","9",0,1,"2027-06-30",63,29),
    (508,"ЭС7-24",3,5,"Коммерческая","9",0,1,"2027-06-30",332,28),
    (493,"ЭСз2-23",4,7,"Коммерческая","11",1,0,"2027-06-16",0,28),
    (491,"БСз-23",4,7,"Коммерческая","11",1,0,"2027-06-16",0,29),
    (522,"СТ-25",2,3,"Коммерческая","9",0,1,"2027-06-30",347,31),
    (526,"ЭС3-25",2,3,"Коммерческая","9",0,1,"2027-06-30",190,28),
    (513,"БСз-24",3,5,"Коммерческая","11",1,0,"2027-06-16",0,29),
    (480,"ЭС6-23",4,7,"Коммерческая","9",0,0,"2027-06-30",294,28),
    (507,"ЭС6-24",3,5,"Коммерческая","9",0,1,"2027-06-30",217,28),
    (529,"ЭС5-25",2,3,"Коммерческая","9",0,1,"2027-06-30",228,28),
    (520,"ОМ-25",2,3,"Бюджетная","9",0,1,"2027-06-30",337,51),
    (557,"ООз-26",1,1,"Коммерческая","9",1,0,"2027-06-30",0,0),
    (555,"ЭСз-26",1,1,"Коммерческая","11",1,0,"2027-06-30",0,28),
    (551,"ЭП1-26",1,1,"Бюджетная","9",0,1,"2027-06-30",322,52),
    (515,"ЭСз2-24",3,5,"Коммерческая","11",1,0,"2027-06-16",0,28),
    (537,"ЭСз-25",2,3,"Коммерческая","11",1,0,"2027-06-30",0,28),
    (517,"БС1-25",2,3,"Бюджетная","9",0,1,"2027-06-30",63,29),
    (502,"БС2-24",3,5,"Коммерческая","9",0,1,"2027-06-30",228,29),
    (495,"ЭС2-24",3,5,"Коммерческая","9",0,1,"2027-06-30",333,28),
    (556,"БСз-26",1,1,"Коммерческая","11",1,0,"2027-06-30",0,29),
    (534,"ЭС8-25",2,3,"Коммерческая","9",0,0,"2027-06-30",87,28),
    (544,"ЭС2-26",1,1,"Бюджетная","9",0,1,"2027-06-30",212,28),
    (533,"ЭС7-25",2,3,"Коммерческая","9",0,0,"2027-06-30",345,28),
    (519,"РМ1-25",2,3,"Бюджетная","9",0,1,"2027-06-30",81,53),
    (548,"СТ-26",1,1,"Коммерческая","9",0,1,"2027-06-30",69,31),
    (498,"ЭП1-24",3,5,"Бюджетная","9",0,1,"2027-06-30",238,52),
    (549,"ОМ-26",1,1,"Бюджетная","9",0,1,"2027-06-30",215,51),
    (532,"ЭС6-25",2,3,"Коммерческая","9",0,1,"2027-06-30",217,28),
    (509,"ЭС8-24",3,5,"Коммерческая","9",0,1,"2027-06-30",353,28),
    (536,"БСз-25",2,3,"Коммерческая","11",1,0,"2027-06-16",0,29),
    (476,"ЭС3-23",4,7,"Коммерческая","9",0,0,"2027-06-30",219,28),
    (494,"ЭС1-24",3,5,"Бюджетная","9",0,1,"2027-06-30",77,28),
    (518,"ЭП1-25",2,3,"Бюджетная","9",0,1,"2027-06-30",37,52),
    (550,"РМ-26",1,1,"Бюджетная","9",0,1,"2027-06-30",209,53),
    (514,"ЭПз-24",3,5,"Коммерческая","11",1,0,"2027-06-16",0,52),
    (474,"ЭС2-23",4,7,"Коммерческая","9",0,0,"2027-06-30",209,28),
    (554,"ЭС7-26",1,1,"Коммерческая","9",0,1,"2027-06-30",366,28),
    (475,"ЭС4-23",4,7,"Коммерческая","9",0,0,"2027-06-30",102,28),
    (545,"ЭС5-26",1,1,"Коммерческая","9",0,1,"2027-06-30",218,28),
    (535,"ЭП3-25",2,3,"Коммерческая","9",0,1,"2027-06-16",200,52),
    (501,"БС1-24",3,5,"Бюджетная","9",0,1,"2027-06-30",212,29),
    (487,"ЭС7-23",4,7,"Коммерческая","9",0,0,"2027-06-30",345,28),
    (543,"ЭС1-26",1,1,"Бюджетная","9",0,1,"2027-06-30",86,28),
    (500,"РМ-24",3,5,"Бюджетная","9",0,1,"2027-06-30",272,53),
    (523,"ЭС2-25",2,3,"Коммерческая","9",0,1,"2027-06-30",355,28),
    (483,"СТ-23",4,7,"Коммерческая","9",0,0,"2027-06-16",67,31),
    (485,"БС2-23",4,7,"Коммерческая","9",0,0,"2027-06-30",322,29),
    (503,"ОМ1-24",3,5,"Бюджетная","9",0,1,"2027-06-30",32,51),
    (490,"ЭСз1-23",4,7,"Коммерческая","11",1,0,"2027-06-16",0,28),
    (553,"ЭС6-26",1,1,"Коммерческая","9",0,1,"2027-06-30",368,28),
    (506,"ЭС5-24",3,5,"Коммерческая","9",0,1,"2027-06-16",13,28),
    (478,"ЭП2-23",4,7,"Коммерческая","9",0,0,"2027-06-30",82,37),
    (484,"ОМ-23",4,7,"Коммерческая","9",0,0,"2027-06-30",295,51),
    (504,"СТ-24",3,5,"Коммерческая","9",0,1,"2027-06-16",112,31),
    (528,"ЭС4-25",2,3,"Коммерческая","9",0,1,"2027-06-30",258,28),
    (505,"ОМ2-24",3,5,"Бюджетная","9",0,1,"2027-06-30",337,51),
    (486,"ЭС5-23",4,7,"Коммерческая","9",0,0,"2027-06-30",347,28),
    (492,"ЭПз-23",4,7,"Коммерческая","11",1,0,"2027-06-16",0,30),
    (541,"БС2-26",1,1,"Коммерческая","9",0,1,"2027-06-30",269,29),
    (542,"БС1-26",1,1,"Бюджетная","9",0,1,"2027-06-30",323,29),
    (521,"РМ2-25",2,3,"Бюджетная","9",0,1,"2027-06-30",85,53),
    (499,"ЭП2-24",3,5,"Коммерческая","9",0,1,"2027-06-30",99,52),
    (496,"ЭС3-24",3,5,"Коммерческая","9",0,1,"2027-06-30",321,28),
    (482,"РМ-23",4,7,"Коммерческая","9",0,0,"2027-06-30",78,47),
    (481,"БС1-23",4,7,"Коммерческая","9",0,0,"2027-06-30",245,29),
    (497,"ЭС4-24",3,5,"Коммерческая","9",0,1,"2027-06-30",293,28),
)

FALLBACK_GROUPS = {
    row[0]: GroupInfo(
        id=row[0], name=row[1], course=row[2], semester=row[3], study=row[4],
        education=row[5], is_correspondence=bool(row[6]), has_schedule=bool(row[7]),
        end_2=row[8], curator_id=row[9], specialty_id=row[10],
        end_1="2026-12-31", start_1="2026-09-01", start_2="2027-01-12",
    )
    for row in _SEED
}


def sorted_groups(groups: dict[int, GroupInfo], course: Optional[int] = None,
                  correspondence: Optional[bool] = None) -> list[GroupInfo]:
    values = groups.values()
    if course is not None:
        values = (group for group in values if group.course == course)
    if correspondence is not None:
        values = (group for group in values if group.is_correspondence == correspondence)
    return sorted(values, key=lambda group: (group.course, group.name.casefold(), group.id))
