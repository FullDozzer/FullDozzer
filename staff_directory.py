# -*- coding: utf-8 -*-
"""Единый справочник преподавателей и STAFF_ID.

Идентификаторы в этом файле являются источником истины. Они не выводятся из
порядка элементов в HTML и не генерируются приложением. Обновление списка
преподавателей не требует изменения обработчиков Telegram.
"""

from dataclasses import dataclass
import re


# Не изменять ФИО и ID без актуального справочника ish nk.ru.
STAFF_DIRECTORY: dict[int, str] = {
    69: "Аглиуллина Фирдаус Хуснулловна",
    182: "Акименко Марина Амировна",
    40: "Алексеева Маргарита Петровна",
    219: "Амирханова Гульнара Азатовна",
    366: "Арнаутова Анастасия Викторовна",
    337: "Артемьева Лилия Ринатовна",
    51: "Ахмадеев Рамиль Фаридович",
    345: "Ахметьянова Зульфия Саматовна",
    294: "Баканова Ксения Андреевна",
    315: "Баранов Евгений Сергеевич",
    323: "Басырова Адиля Фаиковна",
    102: "Боголь Татьяна Петровна",
    353: "Валиева Айгуль Радиковна",
    288: "Валитова Лидия Мигдатовна",
    77: "Валишина Лена Галимхановна",
    90: "Васильева Надежда Ивановна",
    5: "Вахрушина Елена Юрьевна",
    223: "Вепрева Резида Габдразаковна",
    359: "Гайзуллин Ильдар Талгатович",
    308: "Гайнуллина Азалия Рафисовна",
    200: "Гидиятуллина Танзиля Зуфаровна",
    293: "Гизатуллина Ренара Газимулловна",
    228: "Грачева Галина Викторовна",
    319: "Давлетбаев Фанур Фаизович",
    342: "Дербышев Александр Леонидович",
    271: "Дербышева Галина Леонидовна",
    340: "Дроздов Александр Павлович",
    86: "Елистратова Юлия Анатольевна",
    215: "Емельянова Снежана Михайловна",
    212: "Ефимова Наталья Николаевна",
    63: "Журавлёва Светлана Игоревна",
    338: "Зубайдуллин Закария Шарифуллович",
    245: "Заманова Алия Галинуровна",
    209: "Иванова Кристина Алексеевна",
    354: "Ивашкин Николай Анатольевич",
    84: "Идельбаев Ринат Гиниятович",
    112: "Игошева Марина Александровна",
    190: "Иткулова Лариса Владимировна",
    352: "Ишмухаметов Ильгиз Сафиевич",
    217: "Касаева Юлия Ильдаровна",
    183: "Карамов Ралиф Мударисович",
    269: "Карамов Наиль Ралифович",
    333: "Кинзябаев Айнур Ильнурович",
    13: "Кильдиярова Гюзель Радиковна",
    317: "Кильмухаметова Гульназ Фангизовна",
    135: "Комиссарова Галина Леонидовна",
    85: "Кострыгина Елена Валерьяновна",
    87: "Кужина Лейсан Ахметовна",
    22: "Латыпова Римма Абдрахимовна",
    136: "Лукьянчикова Евгения Николаевна",
    30: "Максютова Флюра Маратовна",
    321: "Маркина Алена Сергеевна",
    32: "Мацкевич Фирдаус Мунировна",
    273: "Ментененко Александр Евгеньевич",
    347: "Мирасов Ильдар Магазович",
    364: "Мингажева Роза Финусовна",
    268: "Мотовилов Борис Георгиевич",
    332: "Мукалляпова Альбина Ильгизовна",
    258: "Мурзабулатова Фаягуль Фаязовна",
    37: "Набиуллина Римма Абдразаковна",
    367: "Назаргалеева Файруза Индусовна",
    78: "Оксанич Людмила Васильевна",
    81: "Пестряева Валентина Ивановна",
    99: "Полякова Татьяна Владимировна",
    67: "Саварханов Рустам Раисович",
    48: "Свечникова Ирина Павловна",
    142: "Селезнёва Рушания Авзаловна",
    238: "Сидорова Светлана Геннадьевна",
    130: "Скотарев Сергей Семенович",
    50: "Смолин Иван Николаевич",
    330: "Соломко Виктор Леонидович",
    349: "Степанов Сергей Васильевич",
    368: "Таипова Юлия Геннадьевна",
    322: "Тимербаева Дарья Сергеевна",
    360: "Узаманов Ильдар Хамидуллаевич",
    355: "Утякова Райхана Ураловна",
    357: "Фахретдинов Радик Фаритович",
    58: "Фомичева Людмила Федоровна",
    336: "Хайруллин Азамат Хамзаевич",
    184: "Чаплыгина Татьяна Владимировна",
    256: "Чернышева Альбина Бейсеновна",
    82: "Юдина Нелли Валериевна",
    220: "Ялчикаева Ирина Ильшатовна",
    206: "Янбеков Мират Сальманович",
    272: "Яппарова Алсу Фанировна",
    218: "Ярмухаметова Фидалия Гафуровна",
}


@dataclass(frozen=True)
class StaffMember:
    """Преподаватель из справочника."""

    staff_id: int
    full_name: str

    @property
    def parts(self) -> tuple[str, str, str]:
        values = self.full_name.split()
        values += [""] * (3 - len(values))
        return values[0], values[1], values[2]

    @property
    def short_name(self) -> str:
        surname, first, patronymic = self.parts
        initials = "".join(f"{part[0]}." for part in (first, patronymic) if part)
        return f"{surname} {initials}".strip()


STAFF_MEMBERS: tuple[StaffMember, ...] = tuple(
    StaffMember(staff_id, full_name)
    for staff_id, full_name in STAFF_DIRECTORY.items()
)
STAFF_BY_ID: dict[int, StaffMember] = {
    member.staff_id: member for member in STAFF_MEMBERS
}
# Алиасы не создают второго справочника, но упрощают чтение из интеграций.
STAFF_IDS = STAFF_DIRECTORY
TEACHERS = STAFF_DIRECTORY


def normalize_staff_text(value: str) -> str:
    """Нормализует пользовательский запрос без неуверенного fuzzy matching."""
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[’'`«»\"()]", " ", text)
    text = re.sub(r"[^a-zа-я0-9.\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _word_forms(token: str) -> set[str]:
    """Небольшая нормализация очевидных русских падежных окончаний.

    Это не fuzzy matching: сравниваются только точные слова или их
    очевидные морфологические основы. Поэтому похожие, но разные фамилии
    сами по себе в результат не попадают.
    """
    token = normalize_staff_text(token).strip(".-")
    if not token:
        return set()
    forms = {token}
    if token.endswith("ой") and len(token) > 4:
        forms.add(token[:-2])
    if token.endswith("ою") and len(token) > 4:
        forms.add(token[:-3])
    if token.endswith("е") and len(token) > 4:
        forms.add(token[:-1])
    if token.endswith("у") and len(token) > 4:
        forms.add(token[:-1])
    if token.endswith("а") and len(token) > 4:
        forms.add(token[:-1])
    if token.endswith("ы") and len(token) > 4:
        forms.add(token[:-1])
    return forms


def _query_tokens(value: str) -> list[str]:
    """Разбивает также запись инициалов «Ф.Х.» на «ф», «х»."""
    return re.findall(r"[a-zа-я0-9]+", normalize_staff_text(value))


def _initial_tokens(value: str) -> set[str]:
    return {
        token
        for token in _query_tokens(value)
        if len(token) == 1 and token.isalpha()
    }


def _member_tokens(member: StaffMember) -> set[str]:
    return {
        form
        for word in normalize_staff_text(member.full_name).split()
        for form in _word_forms(word)
        if form
    }


def search_staff(query: str) -> list[StaffMember]:
    """Ищет преподавателей по ФИО, части ФИО и инициалам.

    Поиск регистронезависимый, поддерживает порядок «имя фамилия» и
    очевидные формы фамилии вроде «Аглиуллиной». Результаты всегда
    содержат ID из STAFF_DIRECTORY.
    """
    normalized = normalize_staff_text(query)
    if not normalized:
        return []

    query_tokens = _query_tokens(normalized)
    query_initials = _initial_tokens(normalized)
    query_forms = {
        form
        for token in query_tokens
        if len(token) > 1
        for form in _word_forms(token)
    }

    scored: list[tuple[int, StaffMember]] = []
    for member in STAFF_MEMBERS:
        member_words = normalize_staff_text(member.full_name).split()
        member_forms = _member_tokens(member)
        member_initials = {
            word[0] for word in member_words if word and word.isalpha()
        }

        # Инициалы должны совпадать с именем/отчеством, а не с произвольным
        # первым символом фамилии.
        if query_initials and not query_initials.issubset(member_initials):
            continue

        matched = 0
        for token in query_tokens:
            if len(token) == 1:
                continue
            forms = _word_forms(token)
            if forms & member_forms:
                matched += 1
                continue
            # Часть полного слова допускается только для достаточно длинного
            # токена и по границе слова, чтобы «Агли» находило фамилию.
            if len(token) >= 4 and any(
                word.startswith(token) or token.startswith(word)
                for word in member_words
            ):
                matched += 1

        if matched != len([t for t in query_tokens if len(t) > 1]):
            continue

        # Более точное совпадение выше частичного. Стабильный порядок
        # справочника сохраняется при равном score.
        exact = sum(1 for form in query_forms if form in member_forms)
        score = exact * 10 + matched
        scored.append((score, member))

    scored.sort(key=lambda item: (-item[0], item[1].full_name.casefold()))
    return [member for _score, member in scored]


__all__ = [
    "STAFF_DIRECTORY",
    "STAFF_MEMBERS",
    "STAFF_BY_ID",
    "STAFF_IDS",
    "TEACHERS",
    "StaffMember",
    "normalize_staff_text",
    "search_staff",
]
