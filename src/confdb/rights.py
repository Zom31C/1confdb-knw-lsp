r"""Разбор прав роли из `Role.0.c1brace`.

Формат снят зондами на всех 921 ролях УНФ (страница state3 `role-rights-format`):

    root = [ версия, цели, шаблоны_RLS, маска3, флаг4, флаг5, маска6 ]
    цели = ["N", группа, …]
    группа = [ запись_цели, запись_прав ]          # всегда 2 записи, 35781/35781
    запись_цели = ['1', uuid, флагA, флагB]
                | ['1', uuid, '1', ['-K', uuid_коллекции], флагB]   # подобъект
    запись_прав = ['0', uuid_права, значение, …]                    # 34291
                | ['1', <число пар>, uuid_права, значение, …]       # 1490, с RLS
    RLS права   = … uuid_права, значение, '1', [uuid_права, ['1', ['1', '"текст"', '0']]]
    шаблоны_RLS = ["M", ['"имя шаблона"', '"текст шаблона"'], …]

Записи различаются ПО ПОЗИЦИИ в группе, а не по флагу: флаг '1' бывает и у цели,
и у записи прав (тогда второй элемент — число пар).

Имена прав в конфигурации ОТСУТСТВУЮТ (uuid прав — платформенные константы:
поиск по всем .json дампа не дал ни одного попадания), поэтому модуль возвращает
uuid как есть; словарь `RIGHT_NAMES` заполняется после сверки с редактором прав
конфигуратора и до тех пор пуст — инструменты обязаны показывать uuid с явной
пометкой, а не выдуманное имя.

Значения прав: рабочая гипотеза 1 = разрешено, 0 = запрещено, -1 = не задано
(наследуется). Гипотеза НЕ подтверждена конфигуратором, поэтому наружу отдаётся
и число, и признак.
"""
import re
from dataclasses import dataclass, field

# uuid права -> русское имя как в редакторе прав конфигуратора.
# Заполнено из Rights.xml конфигурации SmallBusinessKz (35 из 66 UUID).
# Оставшиеся 31 UUID отображаются с пометкой "имя не подтверждено".
RIGHT_NAMES = {
    '013a262e-165f-4815-bdae-7a1bed6a68e4': 'Delete',
    '1b762bf9-df7f-4255-bbe6-f7578f41368d': 'Edit',
    '1c87578f-9e09-4ec0-a991-5629c87b1588': 'Edit',
    '1d306db2-d97e-4b57-9b6f-951a009228ce': 'Get',
    '21b4742a-d335-4234-bf0f-a3074a0e31ac': 'InputByString',
    '24abfe06-289a-48c5-8bb4-032c733e45c5': 'Read',
    '287b74b8-3a66-4a76-ba27-4f1f6a93770e': 'EditDataHistoryVersionComment',
    '33200740-82b0-4de7-8556-d3fb25ca4328': 'InteractiveClearDeletionMark',
    '408c56c0-e210-4e2e-8e82-610050a08a39': 'InteractiveClearDeletionMark',
    '479a42c0-c3e9-4ae7-bf4a-75cebc14fec4': 'Read',
    '499e8968-ca89-43f0-9955-8756058b1b53': 'Set',
    '4d0d77ec-8511-430d-bd77-8407f27bc8f4': 'InteractiveClearDeletionMark',
    '5b3ea0e2-fdb9-41f6-bf6c-25747906b4cb': 'ReadDataHistory',
    '5d167fcc-b11f-403a-9a37-1eda64c19df1': 'InteractiveInsert',
    '64319ca1-f3d8-472e-82ce-5da233e6daaa': 'SwitchToDataHistoryVersion',
    '65e5f92c-40ff-4130-9652-c0e7612d0609': 'InteractiveDeleteMarkedPredefinedData',
    '798cf688-ad74-44fe-a464-236b49e910e0': 'InteractiveSetDeletionMark',
    '8497054a-ffd1-4ca7-bdfe-340b9ddc050a': 'Update',
    '9342b152-a7ae-4c79-9b7b-f4f028a36479': 'UpdateDataHistory',
    'a679c969-8ea1-4b8b-9e61-8a414ba448f4': 'UpdateDataHistorySettings',
    'aa6448f2-be0f-42ea-ba26-1af7f52b5b65': 'Edit',
    'b0c0cbfc-f2cc-4b80-8460-5d5d7a599d9d': 'InteractiveSetDeletionMark',
    'b162ff57-0296-483e-9af8-dc37576802cb': 'View',
    'b53db6ed-6e5b-4035-8d24-f10083d646ed': 'ReadDataHistoryOfMissingData',
    'b5f861d3-d9c5-45ec-98bf-0ed4d489a351': 'View',
    'b7bab52d-c1b1-4bd8-8276-02db08d42352': 'View',
    'c0028105-4cc1-41ca-aef1-bfbd8fc8f8c4': 'UpdateDataHistory',
    'c4ab1331-e58d-4a46-ad2e-fe6d80b72aa4': 'ViewDataHistory',
    'c6de80da-a4f7-4ce9-bbeb-0b00ea564ec1': 'View',
    'd76b72ba-5388-4b7f-af64-1b351f63a1e1': 'View',
    'e060de25-bffd-42fd-bb09-f3a788d65760': 'UndoPosting',
    'e7f9daf9-eac2-4ada-9c26-c380858f3589': 'UpdateDataHistoryVersionComment',
    'f55a8f7f-2c65-404f-b530-093d9006adba': 'Update',
    'fa6dbe86-856a-4ac4-b8ac-bce99f8b8b22': 'View',
    'fb88c756-91c9-4351-9cdf-e027879886c6': 'ViewDataHistory',
}

UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                     r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

VALUE_GRANTED = '1'
VALUE_DENIED = '0'
VALUE_UNSET = '-1'


@dataclass
class RightEntry:
    """Одно право одной цели: uuid, значение и текст RLS, если он есть."""

    right_uuid: str
    value: str
    rls_text: str = None

    @property
    def granted(self):
        """True — выдано, False — запрещено, None — не задано/не распознано."""
        if self.value == VALUE_GRANTED:
            return True
        if self.value == VALUE_DENIED:
            return False
        return None

    @property
    def right_name(self):
        """Имя права из словаря или None (тогда показывать uuid с пометкой)."""
        return RIGHT_NAMES.get(self.right_uuid.lower())


@dataclass
class RightTarget:
    """Цель прав: объект метаданных либо его подобъект (реквизит, ТЧ, форма)."""

    uuid: str
    rights: list = field(default_factory=list)
    sub_index: int = None
    collection_uuid: str = None
    flag_a: str = None
    flag_b: str = None

    @property
    def is_subitem(self):
        """True, если цель адресована подобъектом, а не объектом целиком."""
        return self.sub_index is not None


@dataclass
class RoleRights:
    """Права одной роли: версия формата, цели, шаблоны RLS и хвостовые флаги."""

    version: str
    targets: list = field(default_factory=list)
    rls_templates: tuple = ()
    flags: tuple = ()

    def entries(self):
        """Плоский перечень (цель, право) — то, что writer пишет в role_right."""
        for target in self.targets:
            for entry in target.rights:
                yield target, entry


def _text(value):
    """Строка скобкофайла -> обычный текст: снять кавычки и удвоения."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.replace('""', '"')


def _rls_of(node):
    """Текст ограничения доступа из вложенной структуры значения права.

    Наблюдаемая форма: [uuid_права, ['1', ['1', '"ГДЕ …"', '0']]] — текст лежит
    на произвольной глубине, поэтому ищем первую строку в кавычках.
    """
    if isinstance(node, str):
        return _text(node) if node.lstrip().startswith('"') else None
    if isinstance(node, list):
        for item in node:
            found = _rls_of(item)
            if found:
                return found
    return None


def _is_uuid(value):
    return isinstance(value, str) and bool(UUID_RE.match(value))


def _attach_rls(entries, node):
    """Привязать текст ограничения к праву, которое названо в самом блоке.

    Блок RLS — `[uuid_права, ['1', ['1', '"текст"', '0']]]`: uuid права лежит
    ВНУТРИ блока, поэтому текст достаётся уже разобранной паре, а не той, что
    шла перед ним.
    """
    if not isinstance(node, list) or not node or not _is_uuid(node[0]):
        return
    text = _rls_of(node[1] if len(node) > 1 else node)
    if not text:
        return
    key = node[0].lower()
    for entry in reversed(entries):
        if entry.right_uuid == key:
            entry.rls_text = text
            return
    entries.append(RightEntry(key, VALUE_UNSET, text))


def _parse_rights(record):
    """Запись прав -> список RightEntry (пары uuid/значение плюс RLS).

    Раскладка (снята на реальных файлах):
      ['0', uuid, значение, uuid, значение, …]                     — без счётчиков;
      ['1', <число пар>, uuid, значение, …, <число RLS>, блок_RLS…] — со счётчиками.
    Блоки RLS идут списком после пар, каждый со своим uuid права, поэтому
    нестроковые токены разбираются отдельно, а счётчики просто пропускаются.
    """
    items = record[1:]
    if items and str(items[0]).lstrip('-').isdigit():
        items = items[1:]
    out = []
    i = 0
    while i < len(items):
        item = items[i]
        if isinstance(item, list):
            _attach_rls(out, item)
            i += 1
            continue
        if not _is_uuid(item) or i + 1 >= len(items):
            i += 1                      # счётчик RLS, маркер или хвост без значения
            continue
        value = items[i + 1]
        if isinstance(value, list):
            _attach_rls(out, value)
            out.append(RightEntry(item.lower(), VALUE_UNSET))
        else:
            out.append(RightEntry(item.lower(), str(value)))
        i += 2
    return out


def _parse_target(record):
    """Запись цели ['1', uuid, …] -> RightTarget (без прав)."""
    uuid = str(record[1]).lower() if len(record) > 1 else ''
    target = RightTarget(uuid=uuid)
    target.flag_a = record[2] if len(record) > 2 else None
    nested = record[3] if len(record) > 3 else None
    if isinstance(nested, list) and nested:
        try:
            target.sub_index = int(nested[0])
        except (TypeError, ValueError):
            target.sub_index = None
        if len(nested) > 1 and isinstance(nested[1], str):
            target.collection_uuid = nested[1].lower()
        target.flag_b = record[4] if len(record) > 4 else None
    else:
        target.flag_b = nested
    return target


def _is_rights_record(record):
    """Отличить запись прав от записи цели: у прав второй элемент — число пар."""
    if not isinstance(record, list) or len(record) < 2:
        return False
    if record[0] == '0':
        return True
    return record[0] == '1' and str(record[1]).lstrip('-').isdigit()


def parse_role_rights(data):
    """Разобрать структуру из `helper.brace_file_read` в RoleRights.

    Принимает именно список (содержимое скобкофайла), а не путь — так парсер
    тестируется на синтетической фикстуре без дампа конфигурации.
    """
    root = data[0] if (data and isinstance(data[0], list)) else data
    # битый или чужой файл обязан быть ошибкой, а не «пустыми правами»:
    # иначе «данные недоступны» неотличимы от «права роли не заданы»
    if (not isinstance(root, list) or len(root) < 3
            or str(root[0]) not in ('8', '9', '10')):
        raise ValueError(f'нераспознанный формат прав роли: {str(root)[:60]}')
    version = str(root[0]) if root else ''
    targets_raw = root[1] if len(root) > 1 else ['0']
    templates_raw = root[2] if len(root) > 2 else ['0']
    flags = tuple(str(x) for x in root[3:7])

    targets = []
    for group in (targets_raw[1:] if isinstance(targets_raw, list) else []):
        records = group if (group and isinstance(group[0], list)) else [group]
        if not records:
            continue
        target = _parse_target(records[0])
        targets.append(target)
        rest = records[1:]
        if rest and _is_rights_record(rest[0]):
            target.rights.extend(_parse_rights(rest[0]))
            rest = rest[1:]
        # защита: цель без прав, за которой сразу следующая цель
        for record in rest:
            if isinstance(record, list) and record and record[0] == '1':
                targets.append(_parse_target(record))

    templates = []
    for item in (templates_raw[1:] if isinstance(templates_raw, list) else []):
        rows = item if (item and isinstance(item[0], list)) else [item]
        for row in rows:
            if isinstance(row, list) and len(row) >= 2:
                templates.append((_text(row[0]), _text(row[1])))
    return RoleRights(version=version, targets=targets,
                      rls_templates=tuple(templates), flags=flags)


def parse_role_file(directory, file_name='Role.0.c1brace'):
    """Прочитать и разобрать файл прав роли из каталога дампа."""
    from .v8 import helper

    return parse_role_rights(helper.brace_file_read(directory, file_name))
