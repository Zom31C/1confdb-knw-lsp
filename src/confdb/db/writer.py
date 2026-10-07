"""Запись распакованного дерева (стадия 3) в SQLite.

Обход результата декодера: каталог объекта содержит `<Класс>.json` (заголовок)
и `<Класс>.id.json` (uuid); корневой объект — `<Класс>.json` в корне дампа
без парного `.id.json`. Модули — `<Класс>.<имя>.bsl`; каждый модуль
дополнительно разбирается на процедуры/функции (таблица method).

Дерево метаданных хранится в обход «как в конфигураторе»: parent_id + ord
(порядок братьев из заголовка родителя), секции — по type, состав подсистем —
в subsystem_content.
"""
import json
import os
import re
import shutil
import sqlite3
import xml.sax.saxutils
from datetime import datetime

from .. import rights, xdto
from ..bsl_parser import parse_methods
from ..v8 import helper

ROOT_TYPES = ('Configuration', 'ConfigurationExtension', 'ExternalDataProcessor')

RE_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

# Русские имена типов объектов «как в конфигураторе» (ключ — англ. stem).
TYPE_RU = {
    'Configuration': 'Конфигурация',
    'ConfigurationExtension': 'Расширение конфигурации',
    'ExternalDataProcessor': 'Внешняя обработка',
    'Subsystem': 'Подсистема',
    'CommonModule': 'Общий модуль',
    'CommonForm': 'Общая форма',
    'CommonTemplate': 'Общий макет',
    'CommonPicture': 'Общая картинка',
    'CommonCommand': 'Общая команда',
    'CommonAttribute': 'Общий реквизит',
    'CommandGroup': 'Группа команд',
    'Catalog': 'Справочник',
    'CatalogForm': 'Форма справочника',
    'CatalogCommand': 'Команда справочника',
    'Document': 'Документ',
    'DocumentForm': 'Форма документа',
    'DocumentCommand': 'Команда документа',
    'DocumentJournal': 'Журнал документов',
    'DocumentJournalForm': 'Форма журнала документа',
    'DocumentNumerators': 'Нумератор документов',
    'Enum': 'Перечисление',
    'EnumForm': 'Форма перечисления',
    'Report': 'Отчёт',
    'ReportForm': 'Форма отчёта',
    'ReportCommand': 'Команда отчёта',
    'DataProcessor': 'Обработка',
    'DataProcessorForm': 'Форма обработки',
    'DataProcessorCommand': 'Команда обработки',
    'InformationRegister': 'Регистр сведений',
    'InformationRegisterForm': 'Форма регистра сведений',
    'InformationRegisterCommand': 'Команда регистра сведений',
    'AccumulationRegister': 'Регистр накопления',
    'AccumulationRegisterForm': 'Форма регистра накопления',
    'AccountingRegister': 'Регистр бухгалтерии',
    'AccountingRegisterForm': 'Форма регистра бухгалтерии',
    'CalculationRegister': 'Регистр расчёта',
    'CalculationRegisterForm': 'Форма регистра расчёта',
    'BusinessProcess': 'Бизнес-процесс',
    'BusinessProcessForm': 'Форма бизнес-процесса',
    'Task': 'Задача',
    'TaskForm': 'Форма задачи',
    'ChartOfAccounts': 'План счетов',
    'ChartOfAccountsForm': 'Форма плана счетов',
    'ChartOfCharacteristicType': 'План видов характеристик',
    'ChartOfCharacteristicTypeForm': 'Форма плана видов характеристик',
    'ChartOfCalculationTypes': 'План видов расчёта',
    'ChartOfCalculationTypesForm': 'Форма плана видов расчёта',
    'ExchangePlan': 'План обмена',
    'ExchangePlanForm': 'Форма плана обмена',
    'FilterCriterion': 'Критерий отбора',
    'FilterCriterionForm': 'Форма критерия отбора',
    'Constant': 'Константа',
    'SettingsStorage': 'Хранилище настроек',
    'SettingsStorageForm': 'Форма хранилища настроек',
    'DefinedType': 'Определяемый тип',
    'FunctionalOption': 'Функциональная опция',
    'FunctionalOptionsParameter': 'Параметр функциональных опций',
    'HTTPService': 'HTTP-сервис',
    'WebService': 'Web-сервис',
    'WSReference': 'Ссылка на Web-сервис',
    'XDTOPackage': 'Пакет XDTO',
    'ExternalDataSource': 'Внешний источник данных',
    'ExternalDataSourceTable': 'Таблица внешнего источника данных',
    'ExternalDataSourceCube': 'Куб внешнего источника данных',
    'EventSubscription': 'Подписка на событие',
    'ScheduledJob': 'Регламентное задание',
    'SessionParameter': 'Параметр сеанса',
    'Style': 'Стиль',
    'StyleItem': 'Элемент стиля',
    'Language': 'Язык',
    'Interface': 'Интерфейс',
    'Role': 'Роль',
    'Template': 'Макет',
    'Form': 'Форма',
    'Command': 'Команда',
}

# Коды примитивных типов в дескрипторах типов реквизитов.
PRIMITIVE_TYPES = {
    'S': 'Строка',
    'N': 'Число',
    'D': 'Дата',
    'B': 'Булево',
    'T': 'Тип',
}


# Типы объектов верхнего уровня, на которые могут вести ссылки реквизитов.
# Формы, команды, подсистемы и т.п. целями ссылок не бывают — используется
# для дизамбигуации коллизий имён в таблице ссылочных uuid.
REF_TARGET_TYPES = frozenset((
    'Catalog', 'Document', 'Enum', 'InformationRegister', 'AccumulationRegister',
    'AccountingRegister', 'CalculationRegister', 'ChartOfAccounts',
    'ChartOfCharacteristicType', 'ChartOfCalculationTypes', 'BusinessProcess',
    'Task', 'ExchangePlan', 'DataProcessor', 'Report', 'DocumentJournal',
    'FilterCriterion', 'SettingsStorage',
))


def _unquote(value):
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _read_refmap(path):
    """Таблица {ссылочный uuid: имя объекта} из потока .10 корневого объекта."""
    if not os.path.isfile(path):
        return {}
    try:
        data = _read_json(path)[0][3]
    except (ValueError, OSError, IndexError, TypeError):
        return {}
    res = {}
    for group in data if isinstance(data, list) else []:
        if not isinstance(group, list):
            continue
        for pair in group[1:]:
            if isinstance(pair, list) and len(pair) >= 2 and isinstance(pair[0], str):
                res[pair[0]] = _unquote(pair[1]) if isinstance(pair[1], str) else str(pair[1])
    return res


def _defined_type_map(infos):
    """Собственный ссылочный uuid определяемого типа и состав его членов.

    У DefinedType запись header[0][1] = ["0", <ссылочный uuid>, .., CORE,
    ["Pattern", члены..]] — по ней связываем ссылки с объектом и раскрываем состав.
    """
    dt_map, dt_members = {}, {}
    for rel, info in infos.items():
        if info['stem'] != 'DefinedType':
            continue
        try:
            rec = info['header']['header'][0][1]
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(rec, list) and len(rec) >= 5 and isinstance(rec[1], str):
            dt_map[rec[1]] = rel
            dt_members[rel] = rec[4]
    return dt_map, dt_members


class _TypeResolver:
    """Человекочитаемые строки типов реквизитов по дескрипторам заголовка."""

    def __init__(self, uuid_to_path, name2paths, ref2name, dt_map, dt_members):
        self.uuid_to_path = uuid_to_path
        self.name2paths = name2paths
        self.ref2name = ref2name
        self.dt_map = dt_map
        self.dt_members = dt_members
        self.links = []

    def describe(self, desc):
        """Тип реквизита: простая форма ["Pattern", T] или составная ["0", N, T1..TN].

        Параллельно заполняет self.links — список (uuid, путь или None) для
        таблицы attribute_ref: зависимость реквизита от объектов метаданных.
        """
        self.links = []
        if not isinstance(desc, list) or not desc:
            return None
        head = str(desc[0]).strip('"')
        if head == 'Pattern':
            text = self._single(desc[1]) if len(desc) > 1 else None
        elif head == '0' and len(desc) > 1 and str(desc[1]).isdigit():
            parts = []
            for entry in desc[2:]:
                part = self._composite_entry(entry)
                if part and part not in parts:
                    parts.append(part)
            text = ' | '.join(parts) or None
        else:
            text = None
        seen = set()
        dedup = []
        for link in self.links:
            if link not in seen:
                seen.add(link)
                dedup.append(link)
        self.links = dedup
        return text

    def _single(self, inner):
        if not isinstance(inner, list) or not inner:
            return None
        code = str(inner[0]).strip('"')
        if code in PRIMITIVE_TYPES:
            name = PRIMITIVE_TYPES[code]
            if code == 'S' and len(inner) > 1:
                name += f'({inner[1]})'
            return name
        if code == '#' and len(inner) > 1:
            return self.ref(inner[1])
        return None

    def _composite_entry(self, entry):
        if not isinstance(entry, list) or not entry:
            return None
        if str(entry[0]).strip('"') != '#':
            return self._single(entry)
        # конкретный член: собственный uuid объекта вложен в дескриптор
        found = []
        self._collect_own(entry[2:], found)
        if found:
            self.links.extend(found)
            return ' | '.join(f'Ссылка: {p}' for _, p in found)
        return self.ref(entry[1]) if len(entry) > 1 else 'Ссылка'

    def _collect_own(self, node, found):
        if isinstance(node, str):
            path = self.uuid_to_path.get(node)
            # команды/формы вложенными не считаются — только ссылочные типы
            if path and self._is_ref_target(path) and (node, path) not in found:
                found.append((node, path))
        elif isinstance(node, list):
            for child in node:
                self._collect_own(child, found)

    @staticmethod
    def _is_ref_target(path):
        parts = path.split('/')
        return len(parts) == 2 and parts[0] in REF_TARGET_TYPES

    def ref(self, uuid, _seen=None):
        """Ссылка по ссылочному uuid: таблица .10, затем определяемые типы."""
        name = self.ref2name.get(uuid)
        if name is not None:
            paths = [p for p in self.name2paths.get(name, []) if self._is_ref_target(p)]
            if paths:
                self.links.extend((uuid, p) for p in paths)
                return ' | '.join(f'Ссылка: {p}' for p in paths)
            # имя в таблице .10 есть, а объекта ссылочного типа с ним в этой
            # базе нет — отличие от обобщённого типа ниже: цель известна по
            # имени и просто отсутствует среди извлечённых объектов
            self.links.append((uuid, None))
            return f'Ссылка: {name} (объект не найден в базе)'
        path = self.dt_map.get(uuid)
        if path is not None:
            self.links.append((uuid, path))
            members = self._dt_members(path, _seen or set())
            if members:
                return f'ОпределяемыйТип: {path} ({" | ".join(members)})'
            return f'ОпределяемыйТип: {path}'
        # uuid не соответствует ни одному объекту и отсутствует в таблице .10:
        # обобщённый тип платформы (ЛюбаяСсылка, Характеристика) — конкретной
        # цели у него нет и в конфигурации она не описана
        self.links.append((uuid, None))
        return 'Ссылка'

    def _dt_members(self, path, seen):
        if path in seen:
            return []
        seen.add(path)
        node = self.dt_members.get(path)
        if not isinstance(node, list):
            return []
        out = []
        for member in node[1:]:
            if isinstance(member, list) and member and str(member[0]).strip('"') == '#':
                part = self.ref(member[1], seen) if len(member) > 1 else None
            else:
                part = self._single(member)
            if part and part not in out:
                out.append(part)
        return out


# канонический uuid блока полей табличной части в заголовке объекта
VT_FIELDS_KEY = '888744e1-b616-11d4-9436-004095e12fc7'


def _extract_attributes(header, resolver=None, bags=None):
    """Извлекает реквизиты и поля табличных частей: список
    (name, type_str, links, tabular, uuid) в порядке объявления.

    Запись реквизита в заголовке: ["2", CORE, TYPEDESC] (простой тип) или
    ["2", CORE, <uuid>, TYPEDESC, ..] (составной тип — дескриптор в node[3]).
    Поля табличной части — те же записи внутри блока полей секции; tabular —
    имя табличной части (иначе None). uuid — идентификатор самого поля: по нему
    цель права роли (role_right.target_uuid) разрешается в реквизит.

    Дедуп — только внутри одной секции: имя уникально в пределах табличной
    части, а не объекта, поэтому одноимённые поля разных ТЧ и реквизит объекта
    с именем поля ТЧ — разные записи.

    :param bags: список, в который за тот же обход складываются блоки полей
        табличных частей как (имя, объявленное число полей, uuid секции) —
        результат совпадает с _section_bags(header), но заголовок обходится
        один раз (на конфигурации УНФ это 6 млн рекурсивных вызовов на проход).
    """
    result = []
    seen = set()

    def walk(node, section):
        if isinstance(node, dict):
            for child in node.values():
                walk(child, section)
            return
        if not isinstance(node, list):
            return
        name, typedesc, uuid = _attr_record(node)
        if name and (section, name) not in seen:
            seen.add((section, name))
            if resolver:
                result.append((name, resolver.describe(typedesc),
                               list(resolver.links), section, uuid))
            else:
                result.append((name, None, [], section, uuid))
        for i, child in enumerate(node):
            if _is_section_bag(node, i):
                sec_name, sec_uuid = _section_record(node[i - 2])
                if bags is not None and sec_name:
                    bags.append((sec_name, _bag_count(child), sec_uuid))
                walk(child, sec_name or section)
            else:
                walk(child, section)

    walk(header.get('header'), None)
    return result


# маркер CORE у реквизитов справочника/документа и у реквизитов регистра
ATTR_CORE_MARKER = '3'


def _record_uuid(core):
    """uuid именованной записи CORE = [МАРКЕР, [флаг, флаг, uuid], ИМЯ, ...]."""
    inner = core[1]
    if (isinstance(inner, list) and len(inner) >= 3
            and isinstance(inner[2], str) and RE_UUID.match(inner[2])):
        return inner[2]
    return None


def _attr_record(node):
    """(имя, дескриптор типа, uuid) из записи реквизита/поля; None — не запись.

    CORE у полей разных коллекций начинается разным маркером: '3' у реквизитов
    справочника/документа и реквизитов регистра, '0'/'1'/'2' у измерений и
    ресурсов регистра (замер на УНФ: 33949 записей с маркером '3' и 2090 с
    другими, все имена — идентификаторы). Запись с чужим маркером принимается
    только когда на месте uuid: без него отличить поле от похожей структуры
    нечем.
    """
    if not (isinstance(node, list) and len(node) >= 3
            and node[0] == '2' and isinstance(node[1], list)
            and len(node[1]) >= 3 and isinstance(node[1][2], str)):
        return None, None, None
    core = node[1]
    uuid = _record_uuid(core)
    if core[0] != ATTR_CORE_MARKER and not uuid:
        return None, None, None
    if isinstance(node[2], list):
        typedesc = node[2]
    elif len(node) > 3 and isinstance(node[3], list):
        typedesc = node[3]
    else:
        typedesc = None
    return _unquote(core[2]), typedesc, uuid


def _section_record(rec):
    """(имя, uuid) табличной части из записи секции, иначе (None, None).

    CORE имени вложен в запись секции произвольно глубоко —
    ["1", ["11", uuid, .., ["0", CORE], ..], ..], поэтому обход в ширину.
    """
    stack = [rec]
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            if (len(node) >= 3 and str(node[0]) == ATTR_CORE_MARKER
                    and isinstance(node[2], str) and node[2].startswith('"')):
                return _unquote(node[2]), _record_uuid(node)
            stack.extend(node)
    return None, None


def _is_section_bag(parent, idx):
    """Блок полей секции: [.., ЗАПИСЬ_СЕКЦИИ, 1, [VT_FIELDS_KEY, N, поля..], ..]."""
    node = parent[idx]
    return (isinstance(node, list) and bool(node) and node[0] == VT_FIELDS_KEY
            and idx >= 2 and str(parent[idx - 1]) == '1'
            and isinstance(parent[idx - 2], list))


def _section_bags(header):
    """Блоки полей табличных частей: (имя, объявленное число полей, uuid).

    Второй элемент блока — счётчик записей: [VT_FIELDS_KEY, '2', поле, поле].
    Он равен числу извлечённых полей (проверено на всех 1609 блоках базы УНФ
    и на 3 блоках тестовой конфигурации v8unpack), поэтому по нему отличают
    «в конфигурации полей не объявлено» от «поля не извлечены».
    """
    result = []

    def walk(node):
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
            return
        if not isinstance(node, list):
            return
        for i, child in enumerate(node):
            if _is_section_bag(node, i):
                name, uuid = _section_record(node[i - 2])
                if name:
                    result.append((name, _bag_count(child), uuid))
            walk(child)

    walk(header.get('header'))
    return result


def _bag_count(bag):
    """Объявленное число полей блока; None — счётчик не распознан."""
    try:
        count = int(bag[1])
    except (IndexError, TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _tabular_rows(bags):
    """(имя, uuid) табличных частей: без повторов, в порядке объявления."""
    result = []
    seen = set()
    for name, _count, uuid in bags:
        if name not in seen:
            seen.add(name)
            result.append((name, uuid))
    return result


def _tabular_names(bags):
    """Имена табличных частей из блоков полей: без повторов, в порядке объявления."""
    return [name for name, _uuid in _tabular_rows(bags)]


def _extract_tabular(header):
    """Табличные части объекта: список имён в порядке объявления."""
    return _tabular_names(_section_bags(header))


def tabular_field_counts(header_json):
    """{имя табличной части: объявленное число полей} из meta_object.header_json.

    Счётчик берётся из блока полей заголовка, поэтому доступен и в готовой
    базе, без переизвлечения конфигурации. Секции нет в словаре — блок полей
    не распознан; значение None — счётчик не число.
    """
    try:
        header = json.loads(header_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(header, dict):
        return {}
    counts = {}
    for name, count, _uuid in _section_bags(header):
        counts.setdefault(name, count)
    return counts


def _extract_enum_values(header):
    """Имена значений перечисления в порядке объявления.

    Запись значения — обёртка [[0, CORE], x], где CORE как у реквизита:
    ["3", [..uuid..], ИМЯ, ..].
    """
    result = []
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
            return
        if not isinstance(node, list):
            return
        if (len(node) == 2 and isinstance(node[0], list) and len(node[0]) >= 2
                and str(node[0][0]) == '0' and isinstance(node[0][1], list)
                and len(node[0][1]) >= 3 and str(node[0][1][0]) == '3'
                and isinstance(node[0][1][2], str)):
            name = _unquote(node[0][1][2])
            if name and name not in seen:
                seen.add(name)
                result.append(name)
        for child in node:
            walk(child)

    walk(header.get('header'))
    return result


def _predef_columns(schema):
    """{colId: (тип, uuid-имя)} из узла схемы ['<n>', desc…].

    desc = [colId, nameUuid, ['"Pattern"', <тип>…], '""', '0']; у колонки с пустым
    Pattern типа нет — её занимает вложенная таблица.
    """
    columns = {}
    for desc in schema[1:] if isinstance(schema, list) else []:
        if not isinstance(desc, list) or len(desc) < 3:
            continue
        pattern = desc[2]
        ptype = None
        if (isinstance(pattern, list) and len(pattern) > 1
                and isinstance(pattern[1], list) and pattern[1]):
            ptype = _unquote(pattern[1][0])
        columns[str(desc[0])] = (ptype, _unquote(desc[1]))
    return columns


def _predef_rows(table):
    """Узел таблицы -> ({позиция: colId}, строки).

    ['2', nCols, <nCols пар (позиция, colId)>, ['1', nRows, <строки>…], …]:
    порядок значений в строке задают пары, а не номера колонок.
    """
    ncols = int(table[1])
    pos_to_col = {int(table[2 + 2 * i]): str(table[3 + 2 * i]) for i in range(ncols)}
    block = table[2 + 2 * ncols]
    return pos_to_col, block[2:2 + int(block[1])]


def _predef_cell(slots, pos_to_col, col_id):
    for pos, cid in pos_to_col.items():
        if cid == col_id and pos < len(slots):
            return slots[pos]
    return None


def _predef_uuid(value):
    """['"#"', <тип>, ['1', <uuid>]] -> uuid."""
    if isinstance(value, list) and len(value) > 2 and _unquote(value[0]) == '#':
        leaf = value[2]
        if isinstance(leaf, list) and len(leaf) > 1:
            return _unquote(leaf[1])
    return None


def _predef_str(value):
    """['"S"', '"текст"'] и ['"B"', '1'] -> значение без кавычек."""
    if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
        return _unquote(value[1])
    return ''


def _predef_property_names(header):
    """{uuid свойства: имя} из дескрипторов ['3', ['1', '0', uuid], '"Имя"', …].

    В заголовке плана счетов так объявлены колонки табличной части «Виды субконто»:
    по uuid колонки находится её имя — «Суммовой», «Валютный», «Количественный».
    """
    names = {}

    def walk(node):
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
            return
        if not isinstance(node, list):
            return
        if (len(node) >= 3 and str(node[0]) == '3' and isinstance(node[1], list)
                and len(node[1]) >= 3 and isinstance(node[2], str)):
            uuid = _unquote(node[1][2])
            name = _unquote(node[2])
            if name and len(uuid) == 36 and uuid.count('-') == 4 and uuid not in names:
                names[uuid] = name
        for child in node:
            walk(child)

    walk(header.get('header') if isinstance(header, dict) else None)
    return names


def _extract_predefined(bin_path, header=None):
    """Предопределённые элементы из 'Предустановленные данные.bin'.

    Возвращает (элементы, субконто):
      элементы — (ord, parent_ord, uuid, name, code, display), где ord — номер в
        обходе в глубину, а parent_ord None у корневого узла («Счета», «Элементы»),
        который элементом не является;
      субконто — (ord счёта, ord вида, uuid вида, 'флаг;флаг') из вложенной таблицы
        колонки с пустым Pattern.

    Номера колонок у разных типов объектов разные (план счетов с 0, ПВХ с 1), поэтому
    колонки ищутся по типу: первая '#' — uuid элемента, первые три 'S' — имя, код,
    наименование (у справочника без кода строковых колонок две — имя и наименование).
    Вторая '#' неоднозначна (у справочника — родитель, у ПВХ — тип значения) и не
    используется. Формат — страница знаний predefined-structure.
    """
    if not os.path.isfile(bin_path):
        return [], []
    try:
        data = helper.brace_file_read(os.path.dirname(bin_path),
                                      os.path.basename(bin_path))
        wrapper = data[0][1]
        columns = _predef_columns(wrapper[1])
        pos_to_col, top_rows = _predef_rows(wrapper[2])
    except (OSError, ValueError, IndexError, TypeError, KeyError):
        return [], []

    col_to_pos = {c: p for p, c in pos_to_col.items()}
    ordered = sorted(columns, key=lambda c: col_to_pos.get(c, len(columns)))
    uuid_col = next((c for c in ordered if columns[c][0] == '#'), None)
    text_cols = [c for c in ordered if columns[c][0] == 'S']
    nested_col = next((c for c in ordered if columns[c][0] is None), None)
    if not text_cols:
        return [], []
    # у справочника без кода строковых колонок две: имя и наименование
    if len(text_cols) >= 3:
        text_cols = text_cols[:3]
    elif len(text_cols) == 2:
        text_cols = [text_cols[0], None, text_cols[1]]
    else:
        text_cols = [text_cols[0], None, None]
    flag_names = _predef_property_names(header) if header else {}
    elements = []
    subcontos = []

    def collect_subconto(value, owner_ord):
        """Вложенная таблица ['#', <тип>, [<схема>, <таблица>, …]] — виды субконто."""
        if not isinstance(value, list) or len(value) < 3 or not isinstance(value[2], list):
            return
        payload = value[2]
        if len(payload) < 3:
            return
        try:
            scols = _predef_columns(payload[1])
            spos, srows = _predef_rows(payload[2])
        except (ValueError, IndexError, TypeError):
            return
        s_to_pos = {c: p for p, c in spos.items()}
        sordered = sorted(scols, key=lambda c: s_to_pos.get(c, len(scols)))
        suuid = next((c for c in sordered if scols[c][0] == '#'), None)
        # безымянный флаг пропускаем: его смысл не установлен
        sflags = [c for c in sordered if scols[c][0] == 'B' and scols[c][1] in flag_names]
        done = 0
        for row in srows:
            if not isinstance(row, list) or len(row) < 4 or str(row[0]) != '2':
                continue
            try:
                slots = row[3:3 + int(row[2])]
            except (TypeError, ValueError):
                continue
            flags = ';'.join(flag_names[scols[c][1]] for c in sflags
                             if _predef_str(_predef_cell(slots, spos, c)) == '1')
            subcontos.append((owner_ord, done,
                              _predef_uuid(_predef_cell(slots, spos, suuid)) if suuid else None,
                              flags or None))
            done += 1

    def walk(node, parent_ord):
        if not isinstance(node, list) or len(node) < 4 or str(node[0]) != '2':
            return
        try:
            ncols = int(node[2])
        except (TypeError, ValueError):
            return
        slots = node[3:3 + ncols]
        ord_no = len(elements)
        texts = [_predef_str(_predef_cell(slots, pos_to_col, c)) for c in text_cols]
        elements.append((ord_no, parent_ord,
                         _predef_uuid(_predef_cell(slots, pos_to_col, uuid_col))
                         if uuid_col else None,
                         texts[0], texts[1], texts[2]))
        if nested_col is not None:
            collect_subconto(_predef_cell(slots, pos_to_col, nested_col), ord_no)
        for child in node[3 + ncols:]:
            if not isinstance(child, list) or not child or str(child[0]) != '1':
                continue
            try:
                grand = child[2:2 + int(child[1])]
            except (TypeError, ValueError):
                continue
            for sub in grand:
                walk(sub, ord_no)

    for row in top_rows:
        walk(row, None)
    return elements, subcontos


def _extract_common_targets(header, uuid_to_id, own_id):
    """Объекты, к которым прикреплён общий реквизит (uuid в заголовке)."""
    flat = []
    _flatten_uuids(header, flat)
    targets = []
    seen = set()
    for uuid in flat:
        obj_id = uuid_to_id.get(uuid)
        if obj_id is not None and obj_id != own_id and obj_id not in seen:
            seen.add(obj_id)
            targets.append(obj_id)
    return targets


SCHEMA = """
CREATE TABLE source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file TEXT NOT NULL,
    created TEXT NOT NULL,
    root_type TEXT,
    root_name TEXT,
    root_uuid TEXT,
    file_size INTEGER,
    file_sha256 TEXT
);
CREATE TABLE meta_object (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER,
    path TEXT NOT NULL,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    uuid TEXT,
    comment TEXT,
    obj_version TEXT,
    type_ru TEXT,
    header_json TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_meta_object_path ON meta_object(source_id, path);
CREATE INDEX ix_meta_object_type ON meta_object(type);
CREATE INDEX ix_meta_object_parent ON meta_object(parent_id, ord);
CREATE TABLE module (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    code_name TEXT NOT NULL,
    context TEXT,
    body TEXT
);
CREATE INDEX ix_module_object ON module(object_id);
CREATE TABLE method (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id INTEGER NOT NULL REFERENCES module(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    signature TEXT,
    is_export INTEGER NOT NULL DEFAULT 0,
    directives TEXT,
    description TEXT,
    line_start INTEGER,
    line_end INTEGER,
    body TEXT NOT NULL
);
CREATE INDEX ix_method_module ON method(module_id, ord);
CREATE INDEX ix_method_name ON method(name);
CREATE TABLE skd_query (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    query TEXT NOT NULL
);
CREATE INDEX ix_skd_query_object ON skd_query(object_id, ord);
CREATE TABLE xdto_import (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    namespace TEXT NOT NULL
);
CREATE INDEX ix_xdto_import_object ON xdto_import(object_id, ord);
CREATE INDEX ix_xdto_import_ns ON xdto_import(namespace);
CREATE TABLE xdto_type (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    name TEXT,
    kind TEXT NOT NULL,
    base TEXT,
    base_ns TEXT,
    facets TEXT,
    enum_values TEXT
);
CREATE INDEX ix_xdto_type_object ON xdto_type(object_id, ord);
CREATE INDEX ix_xdto_type_name ON xdto_type(name);
CREATE INDEX ix_xdto_type_base_ns ON xdto_type(base_ns);
CREATE TABLE xdto_property (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type_id INTEGER REFERENCES xdto_type(id) ON DELETE CASCADE,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    name TEXT,
    type TEXT,
    type_ns TEXT,
    lower_bound INTEGER,
    upper_bound INTEGER,
    nillable INTEGER,
    form TEXT,
    extra TEXT,
    nested_type_id INTEGER REFERENCES xdto_type(id) ON DELETE CASCADE
);
CREATE INDEX ix_xdto_property_type ON xdto_property(type_id, ord);
CREATE INDEX ix_xdto_property_name ON xdto_property(name);
CREATE TABLE meta_attribute (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    name TEXT NOT NULL,
    type_str TEXT,
    tabular TEXT,
    uuid TEXT
);
CREATE INDEX ix_meta_attribute_object ON meta_attribute(object_id, ord);
CREATE INDEX ix_meta_attribute_uuid ON meta_attribute(uuid);
CREATE TABLE meta_tabular (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    name TEXT NOT NULL,
    uuid TEXT
);
CREATE INDEX ix_meta_tabular_object ON meta_tabular(object_id, ord);
CREATE INDEX ix_meta_tabular_uuid ON meta_tabular(uuid);
CREATE TABLE attribute_ref (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute_id INTEGER NOT NULL REFERENCES meta_attribute(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    uuid TEXT NOT NULL,
    object_id INTEGER REFERENCES meta_object(id) ON DELETE CASCADE
);
CREATE INDEX ix_attribute_ref_attr ON attribute_ref(attribute_id, ord);
CREATE INDEX ix_attribute_ref_object ON attribute_ref(object_id);
CREATE INDEX ix_attribute_ref_uuid ON attribute_ref(uuid);
CREATE TABLE enum_value (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    name TEXT NOT NULL
);
CREATE INDEX ix_enum_value_object ON enum_value(object_id, ord);
CREATE TABLE predefined (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    parent_ord INTEGER,
    uuid TEXT,
    name TEXT NOT NULL,
    code TEXT,
    display TEXT
);
CREATE UNIQUE INDEX ix_predefined_object ON predefined(object_id, ord);
CREATE INDEX ix_predefined_uuid ON predefined(uuid);
CREATE TABLE predefined_subconto (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    predefined_id INTEGER NOT NULL REFERENCES predefined(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    uuid TEXT,
    kind_id INTEGER REFERENCES predefined(id) ON DELETE SET NULL,
    flags TEXT
);
CREATE INDEX ix_predef_subconto ON predefined_subconto(predefined_id, ord);
CREATE TABLE common_target (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    common_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE
);
CREATE INDEX ix_common_target_common ON common_target(common_id);
CREATE INDEX ix_common_target_target ON common_target(target_id);
CREATE TABLE subsystem_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subsystem_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL
);
CREATE INDEX ix_subsystem_content ON subsystem_content(subsystem_id, ord);
CREATE TABLE file (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    object_id INTEGER REFERENCES meta_object(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    data BLOB
);
CREATE INDEX ix_file_object ON file(object_id);
CREATE TABLE role_right (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    target_uuid TEXT NOT NULL,
    target_object_id INTEGER REFERENCES meta_object(id) ON DELETE CASCADE,
    target_attr_id INTEGER REFERENCES meta_attribute(id) ON DELETE CASCADE,
    target_tabular_id INTEGER REFERENCES meta_tabular(id) ON DELETE CASCADE,
    sub_index INTEGER,
    collection_uuid TEXT,
    target_flags TEXT,
    right_uuid TEXT NOT NULL,
    value TEXT NOT NULL,
    rls_text TEXT
);
CREATE INDEX ix_role_right_role ON role_right(role_id);
CREATE INDEX ix_role_right_target ON role_right(target_uuid);
CREATE INDEX ix_role_right_object ON role_right(target_object_id);
CREATE INDEX ix_role_right_attr ON role_right(target_attr_id);
CREATE INDEX ix_role_right_right ON role_right(right_uuid);
CREATE TABLE role_rls_template (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    name TEXT,
    text TEXT
);
CREATE INDEX ix_role_rls_template_role ON role_rls_template(role_id, ord);
CREATE TABLE role_rights_state (
    role_id INTEGER PRIMARY KEY REFERENCES meta_object(id) ON DELETE CASCADE,
    version TEXT,
    parsed INTEGER NOT NULL,
    targets INTEGER NOT NULL,
    rights INTEGER NOT NULL,
    rls_templates INTEGER NOT NULL,
    error TEXT
);
"""

TEXT_KINDS = ('bsl', 'html', 'htm', 'txt', 'json', 'xml', 'css', 'js')


def _scan_tree(root):
    """Один обход дампа: список (dirpath, [(имя файла, размер)]) в порядке os.walk.

    Заменяет три полных обхода записи (поиск объектов, предпроход модулей BSL,
    основной цикл) и os.path.getsize по каждому файлу — размер отдаёт os.scandir.
    Порядок совпадает с прежним os.walk + sorted(filenames): каталоги в порядке
    scandir, файлы по имени (от него зависят id строк в базе).
    """
    result = []

    def rec(dirpath):
        files = []
        dirs = []
        with os.scandir(helper.long_path(dirpath)) as it:
            for entry in it:
                if entry.is_dir():
                    if not entry.is_symlink():
                        dirs.append(entry.name)
                else:
                    files.append((entry.name, entry.stat().st_size))
        files.sort()
        result.append((dirpath, files))
        for name in dirs:
            rec(os.path.join(dirpath, name))

    rec(root)
    return result


def _rel_paths(tree, dump_dir):
    """{dirpath: путь от корня дампа через '/'}; корень — пустая строка."""
    rel_by_dir = {}
    for dirpath, _files in tree:
        if dirpath == dump_dir:
            rel_by_dir[dirpath] = ''
            continue
        parent_rel = rel_by_dir[os.path.dirname(dirpath)]
        name = os.path.basename(dirpath)
        rel_by_dir[dirpath] = parent_rel + '/' + name if parent_rel else name
    return rel_by_dir


def _find_objects(tree, dump_dir):
    """Возвращает {абсолютный путь каталога: имя класса}; корень — по особой логике."""
    objects = {}
    root_candidates = []
    for dirpath, files in tree:
        filenames = [f[0] for f in files]
        stems_with_id = set()
        plain_json_stems = set()
        for fn in filenames:
            if fn.endswith('.id.json'):
                stems_with_id.add(fn[:-len('.id.json')])
            elif fn.endswith('.json') and '.' not in fn[:-len('.json')]:
                plain_json_stems.add(fn[:-len('.json')])
        for stem in stems_with_id:
            if f'{stem}.json' in filenames:
                objects[dirpath] = stem
        if dirpath == dump_dir:
            for stem in sorted(plain_json_stems - stems_with_id):
                root_candidates.append(stem)
    root_stem = None
    for stem in root_candidates:
        if stem in ROOT_TYPES:
            root_stem = stem
            break
    if root_stem is None and root_candidates:
        root_stem = root_candidates[0]
    if root_stem:
        objects[dump_dir] = root_stem
    return objects


def _read_json(path):
    # байты + loads быстрее json.load через TextIOWrapper: заголовков в дампе
    # десятки тысяч (УНФ — 23 513 объекта, 322 МБ JSON)
    with open(helper.long_path(path), 'rb') as f:
        return json.loads(f.read())


def _read_header(path):
    """Заголовок объекта и его текст для meta_object.header_json.

    Компактный дамп — это ровно тот текст, который дала бы повторная сериализация
    заголовка (те же разделители и ensure_ascii=False), поэтому 114 МиБ заголовков
    УНФ не нужно сериализовать второй раз (1.8 с). Дампу с --dump-indent текст не
    подходит — тогда колонку пишет json.dumps, как раньше.
    """
    with open(helper.long_path(path), 'r', encoding='utf-8') as f:
        text = f.read()
    return json.loads(text), (None if '\n' in text else text)


RE_ID_UUID = re.compile(rb'"uuid"\s*:\s*"([^"]*)"')


def _read_id_uuid(path):
    """uuid из <Класс>.id.json: файл — одна пара ключ-значение, разбор JSON лишний."""
    with open(helper.long_path(path), 'rb') as f:
        raw = f.read()
    found = RE_ID_UUID.search(raw)
    if found:
        return found.group(1).decode('utf-8')
    return _read_json(path).get('uuid')


def _read_text(path):
    for encoding in ('utf-8-sig', 'windows-1251'):
        try:
            with open(helper.long_path(path), 'r', encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(helper.long_path(path), 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def _flatten_uuids(node, out):
    if isinstance(node, list):
        for item in node:
            _flatten_uuids(item, out)
    elif isinstance(node, dict):
        for item in node.values():
            _flatten_uuids(item, out)
    elif isinstance(node, str) and RE_UUID.match(node):
        out.append(node)


def _uuid_first_index(header):
    flat = []
    _flatten_uuids(header, flat)
    index = {}
    for pos, value in enumerate(flat):
        index.setdefault(value, pos)
    return index


def _common_module_context(header):
    """Контекст выполнения общего модуля по флагам записи заголовка."""
    try:
        rec = header['header'][0][1]
        flags = [x for x in rec[2:] if not isinstance(x, list)]
        if len(flags) < 8:
            return None
    except (KeyError, IndexError, TypeError):
        return None
    names = []
    if flags[1] == '1':
        names.append('Сервер')
    if flags[2] == '1':
        names.append('Внешнее соединение')
    if flags[0] == '1':
        names.append('Клиент (обычное приложение)')
    if flags[5] == '1':
        names.append('Клиент (управляемое приложение)')
    if flags[7] == '1':
        names.append('Вызов сервера')
    if flags[4] == '1':
        names.append('Глобальный')
    if flags[6] in ('1', '2'):
        names.append('Повторное использование')
    return ','.join(names) or None


RE_SKD_QUERY = re.compile(r'<query\b[^>]*>(.*?)</query>', re.S)


def _extract_skd_queries(path):
    """Извлекает тексты запросов из макета схемы компоновки данных (XML в .bin).

    Возвращает список строк запросов или None, если файл не является СКД.
    """
    try:
        with open(helper.long_path(path), 'rb') as f:
            raw = f.read()
    except OSError:
        return None
    if b'<?xml' not in raw or b'<query' not in raw:
        return None
    text = raw[raw.find(b'<?xml'):].decode('utf-8', errors='replace')
    queries = [xml.sax.saxutils.unescape(m).strip()
               for m in RE_SKD_QUERY.findall(text)]
    queries = [q for q in queries if q]
    return queries or None


def _insert_xdto_props(conn, object_id, type_id, props, stats):
    """Пишет свойства типа; type_id None — свойства уровня самого пакета."""
    for ord_no, prop in enumerate(props):
        nested_id = (_insert_xdto_type(conn, object_id, ord_no,
                                       prop['nested'], stats)
                     if prop['nested'] is not None else None)
        conn.execute(
            'INSERT INTO xdto_property (type_id, object_id, ord, name, type,'
            ' type_ns, lower_bound, upper_bound, nillable, form, extra,'
            ' nested_type_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (type_id, object_id, ord_no, prop['name'], prop['type'],
             prop['type_ns'], prop['lower_bound'], prop['upper_bound'],
             prop['nillable'], prop['form'], prop['extra'], nested_id))
        stats['xdto_properties'] += 1


def _insert_xdto_type(conn, object_id, ord_no, item, stats):
    """Пишет тип пакета XDTO и его свойства; возвращает id типа.

    Вложенный анонимный тип пишется раньше свойства-владельца: на него
    ссылается nested_type_id. Пакеты небольшие, поэтому вставка построчная.
    """
    type_id = conn.execute(
        'INSERT INTO xdto_type (object_id, ord, name, kind, base, base_ns,'
        ' facets, enum_values) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (object_id, ord_no, item['name'], item['kind'], item['base'],
         item['base_ns'], item['facets'], item['values'])).lastrowid
    stats['xdto_types'] += 1
    _insert_xdto_props(conn, object_id, type_id, item['props'], stats)
    return type_id


def _insert_xdto(conn, object_id, package, stats):
    """Пишет состав пакета XDTO: импорты, типы и свойства уровня пакета."""
    for ord_no, namespace in enumerate(package['imports']):
        conn.execute('INSERT INTO xdto_import (object_id, ord, namespace)'
                     ' VALUES (?, ?, ?)', (object_id, ord_no, namespace))
    for ord_no, item in enumerate(package['types']):
        _insert_xdto_type(conn, object_id, ord_no, item, stats)
    _insert_xdto_props(conn, object_id, None, package['props'], stats)


# Разбор модулей масштабируется примерно до 8 процессов: дальше накладные
# расходы на spawn и передачу тел методов через IPC превышают выигрыш.
MAX_PARSE_WORKERS = 8


def _parse_bsl_worker(path):
    """Разбор модуля в рабочем процессе пула (данные остаются на диске)."""
    return parse_methods(_read_text(path))


# Индекс именно по телам МЕТОДОВ: module.body — это модуль минус код методов
# (bsl_parser.parse_methods), поэтому индекс по телам модулей не покрыл бы
# содержимое методов и молча обнулил бы выдачу find_methods.
FTS_TABLE = 'method_fts'

# Индекс живёт в приставных файлах-шардах `<база>.fts\0.db…`, а не в файле базы.
# Сборка упирается в токенизацию (УНФ, 262 МБ тел — 49.1 с одним процессом), в один
# файл SQLite пишет только один процесс, а слияние готовых шардов стоит как сборка
# (47.9 с: строки при переносе токенизируются заново) — поэтому один файл и
# параллельная сборка несовместимы. Шарды по `id % N`: 2 → 25.8 с, 4 → 12.8 с,
# 8 → 7.5 с (6.5x), размер индекса +2.6%, выдача MATCH та же. База остаётся одним
# файлом и без индекса работоспособна — поиск по телам уходит на body_has (~0.3 с).
# Замеры: `_tmp\probe_fts_shards.py`, page `fts5-method-body`.
FTS_DIR_SUFFIX = '.fts'
MAX_FTS_SHARDS = 16


def fts_dir(db_path):
    """Каталог приставных файлов FTS-индекса: `<база>.fts`."""
    return os.path.abspath(db_path) + FTS_DIR_SUFFIX


def fts_shard_paths(db_path):
    """Файлы шардов по порядку номеров; пустой список — индекса нет."""
    d = fts_dir(db_path)
    if not os.path.isdir(d):
        return []
    names = [n for n in os.listdir(d) if n.endswith('.db') and n[:-3].isdigit()]
    names.sort(key=lambda n: int(n[:-3]))
    return [os.path.join(d, n) for n in names]


def drop_fts_index(db_path):
    """Удаляет приставной FTS-индекс базы (каталог `<база>.fts` целиком)."""
    d = fts_dir(db_path)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


def fts_index_info(db_path):
    """Манифест приставного индекса или None, если индекса нет/он неполный.

    Манифест `index.json` пишется последним, поэтому его отсутствие означает
    прерванную сборку, а несовпадение числа шардов — частично удалённый индекс.
    В обоих случаях сервер обязан искать через body_has, а не молча возвращать
    часть выдачи.

    :return: {'shards': N, 'methods': M, 'created': iso} или None
    """
    manifest = os.path.join(fts_dir(db_path), 'index.json')
    if not os.path.isfile(manifest):
        return None
    try:
        with open(manifest, 'r', encoding='utf-8') as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    if len(fts_shard_paths(db_path)) != int(info.get('shards', -1)):
        return None
    return info


def _build_fts_shard(args):
    """Сборка одного шарда; рабочий процесс пула. База открывается read-only."""
    db_path, shard_path, part, total = args
    if os.path.exists(shard_path):
        os.remove(shard_path)
    src_uri = 'file:' + os.path.abspath(db_path).replace(os.sep, '/') + '?mode=ro'
    conn = sqlite3.connect(src_uri, uri=True)
    try:
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA cache_size=-65536')
        conn.execute('ATTACH DATABASE ? AS dst', (shard_path,))
        conn.execute(f'CREATE VIRTUAL TABLE dst.{FTS_TABLE}'
                     ' USING fts5(body, tokenize=trigram)')
        conn.execute('BEGIN')
        # rowid = method.id во всех шардах, поэтому части не пересекаются и
        # объединение выдачи не требует перенумерации
        conn.execute(f'INSERT INTO dst.{FTS_TABLE}(rowid, body)'
                     f' SELECT id, body FROM main.method WHERE id % {total} = {part}')
        conn.commit()
    finally:
        conn.close()
    return shard_path


def build_fts_index(db_path, workers=1):
    """Строит (перестраивает) приставной FTS-индекс базы; возвращает число методов.

    Отдельная команда для извлечения с --no-fts: на УНФ индекс дороже самой записи
    (49.1 с одним процессом, 7.5 с в 8 шардов), и его можно собрать позже, не
    переизвлекая конфигурацию. Индекс прежней схемы (таблица `method_fts` внутри
    базы) при пересборке удаляется: это производные данные, и две схемы сразу не
    нужны.

    :param db_path: путь к базе знаний
    :param workers: число процессов сборки (1 — последовательно, без пула)
    """
    conn = sqlite3.connect(db_path)
    try:
        try:
            conn.execute('SELECT id FROM method LIMIT 1')
        except sqlite3.Error:
            raise ValueError(f'это не база знаний confdb (нет таблицы method): {db_path}')
        methods = conn.execute('SELECT COUNT(*) FROM method').fetchone()[0]
        conn.execute(f'DROP TABLE IF EXISTS {FTS_TABLE}')
        conn.commit()
    finally:
        conn.close()

    drop_fts_index(db_path)
    shards = max(1, min(int(workers or 1), MAX_FTS_SHARDS, max(methods, 1)))
    os.makedirs(helper.long_path(fts_dir(db_path)), exist_ok=True)
    tasks = [(db_path, os.path.join(fts_dir(db_path), f'{k}.db'), k, shards)
             for k in range(shards)]
    if shards > 1:
        import multiprocessing
        mp_ctx = multiprocessing.get_context('spawn')
        with mp_ctx.Pool(shards) as pool:
            pool.map(_build_fts_shard, tasks)
    else:
        _build_fts_shard(tasks[0])
    # манифест — последним: пока его нет, индекс считается неполным
    # (fts_index_info) и поиск идёт через body_has
    with open(os.path.join(fts_dir(db_path), 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'shards': shards, 'methods': methods,
                   'created': datetime.now().isoformat(timespec='seconds')}, f)
    return methods


_ROLE_RIGHT_SQL = ('INSERT INTO role_right (role_id, target_uuid,'
                   ' target_object_id, target_attr_id, target_tabular_id,'
                   ' sub_index, collection_uuid, target_flags,'
                   ' right_uuid, value, rls_text)'
                   ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
_ROLE_STATE_SQL = ('INSERT INTO role_rights_state (role_id, version, parsed,'
                   ' targets, rights, rls_templates, error)'
                   ' VALUES (?, ?, ?, ?, ?, ?, ?)')


def _flag_token(value):
    """Один флаг записи цели как текст: блок ['-12', uuid] -> '-12:uuid'."""
    if isinstance(value, (list, tuple)):
        return ':'.join(str(item) for item in value)
    return str(value)


def _target_flags(target):
    """Флаги записи цели дословно ('0/1'); None — в записи их нет.

    Смысл флагов не подтверждён, поэтому они хранятся как есть и не печатаются
    без нужды. Хранить их обязывает счётчик целей: один и тот же uuid встречается
    в роли несколько раз с разными флагами, и без них цели сливаются в одну
    (у УНФ так терялись 4 цели роли ДобавлениеИзменениеВозвратовПоставщикам —
    7335 в файле против 7331 различимых). Флаг бывает и вложенным блоком
    ['-K', uuid] — вторая ступень адресации внутри цели (8 строк у той же роли),
    поэтому блок раскладывается через ':', а не печатается как список Python.
    """
    if target.flag_a is None and target.flag_b is None:
        return None
    return '/'.join(_flag_token(value) for value in (target.flag_a, target.flag_b))


def _right_targets(conn):
    """Карта uuid цели права -> (объект-владелец, реквизит, табличная часть).

    Цель — объект метаданных, его реквизит/поле ТЧ или его табличная часть,
    поэтому uuid ищется в трёх таблицах; незаполненные id остаются None.
    `target_object_id` для реквизита и ТЧ — объект-владелец, так что все права,
    затрагивающие объект, выбираются одним условием. Замер на УНФ: 16953 цели
    разрешаются объектом, 12276 — реквизитом, 520 — табличной частью, 258
    (операции веб-сервисов, методы HTTP-сервисов, специальный маркер
    de29c81d-…) не опознаны вовсе.
    """
    targets = {}
    for uuid, obj_id in conn.execute(
            'SELECT uuid, id FROM meta_object WHERE uuid IS NOT NULL'):
        targets[uuid] = (obj_id, None, None)
    for uuid, attr_id, obj_id in conn.execute(
            'SELECT uuid, id, object_id FROM meta_attribute'
            ' WHERE uuid IS NOT NULL'):
        targets[uuid] = (obj_id, attr_id, None)
    for uuid, tab_id, obj_id in conn.execute(
            'SELECT uuid, id, object_id FROM meta_tabular WHERE uuid IS NOT NULL'):
        targets[uuid] = (obj_id, None, tab_id)
    return targets


def _write_role_rights(conn, dump_dir, stats):
    """Права ролей из `Role/<имя>/Role.0.c1brace` — в role_right/role_rls_template.

    Файл прав не входит в поток заголовков и остальной записью не читается,
    поэтому разбирается отдельно (`confdb.rights`) и уже после объектов: нужны
    id ролей и карта uuid -> цель (`_right_targets`), чтобы разрешить цели прав.
    Хранятся только ЯВНЫЕ записи (sparse): отсутствие строки означает «право не
    задано», а не «запрещено».

    `role_rights_state` различает два случая, которые иначе неотличимы:
    parsed=0 — данные недоступны (файла нет или он не разобрался, причина в
    `error`), parsed=1 при rights=0 — права роли действительно не заданы
    (например, старые версии формата 8/9). Сбой разбора одной роли не прерывает
    запись всей базы.
    """
    targets = _right_targets(conn)
    right_params = []
    tmpl_params = []
    state_params = []
    roles = conn.execute(
        "SELECT id, path FROM meta_object WHERE type='Role' ORDER BY id").fetchall()
    for role_id, path in roles:
        role_dir = os.path.join(dump_dir, *path.split('/'))
        try:
            parsed = rights.parse_role_file(role_dir)
        except Exception as err:  # noqa: BLE001
            state_params.append((role_id, None, 0, 0, 0, 0, str(err)[:300]))
            stats['role_rights_failed'] += 1
            continue
        count = 0
        for target, entry in parsed.entries():
            obj_id, attr_id, tab_id = targets.get(target.uuid, (None, None, None))
            right_params.append((role_id, target.uuid, obj_id, attr_id, tab_id,
                                 target.sub_index, target.collection_uuid,
                                 _target_flags(target), entry.right_uuid,
                                 entry.value, entry.rls_text))
            count += 1
        for ord_no, (name, text) in enumerate(parsed.rls_templates):
            tmpl_params.append((role_id, ord_no, name, text))
        state_params.append((role_id, parsed.version, 1, len(parsed.targets),
                             count, len(parsed.rls_templates), None))
        stats['role_rights'] += count
        stats['role_rls_templates'] += len(parsed.rls_templates)
        if len(right_params) >= 20000:
            conn.executemany(_ROLE_RIGHT_SQL, right_params)
            right_params.clear()
    conn.executemany(_ROLE_RIGHT_SQL, right_params)
    conn.executemany(
        'INSERT INTO role_rls_template (role_id, ord, name, text)'
        ' VALUES (?, ?, ?, ?)', tmpl_params)
    conn.executemany(_ROLE_STATE_SQL, state_params)


def write_db(dump_dir, db_path, *, source_file=None, source_sha256=None,
             source_size=None, store_blobs=False, workers=1,
             build_fts=True, headers_dir=None):
    """Пишет дамп каталога stage 3 в SQLite. Возвращает статистику.

    :param dump_dir: каталог результата декодера (стадия 3)
    :param db_path: файл SQLite (существующий перезаписывается)
    :param source_file: путь к исходному .cf/.cfe/.epf (для таблицы source)
    :param source_sha256: отпечаток исходного файла — по нему повторное
        извлечение той же конфигурации видно до распаковки (`extract.same_source`)
    :param source_size: размер исходного файла в байтах
    :param store_blobs: хранить бинарные файлы (image/bin) как BLOB
    :param workers: число процессов для разбора модулей BSL (1 — последовательно)
    :param build_fts: строить FTS5-индекс по телам методов — приставные файлы
        `<база>.fts\0.db…`, по одному на процесс (`workers`). На УНФ это дороже
        самой записи (7.5 с в 8 шардов против 23 с записи); без индекса база
        рабочая (поиск по телам идёт через body_has), индекс собирается позже —
        `confdb fts <db>`
    :param headers_dir: каталог потока заголовков декодера (helper.sink_put).
        Когда он задан, заголовки объектов берутся оттуда, а не из 47 тысяч
        файлов <Класс>.json и <Класс>.id.json дампа — на УНФ это 6 с из 27
    """
    dump_dir = os.path.abspath(dump_dir)
    if not os.path.isdir(dump_dir):
        raise FileNotFoundError(dump_dir)
    if os.path.isdir(db_path):
        raise ValueError(f'Путь БД указывает на каталог: {db_path}')
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(helper.long_path(parent), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    # приставной индекс — производные данные ПЕРЕЗАПИСЫВАЕМОЙ базы: без его
    # очистки сборка с --no-fts оставила бы серверу шарды от прошлой базы
    drop_fts_index(db_path)

    # снимок обхода один на все проходы записи (поиск объектов, предпроход BSL,
    # основной цикл): дамп УНФ — 26 107 каталогов и 73 537 файлов
    tree = _scan_tree(dump_dir)
    rel_by_dir = _rel_paths(tree, dump_dir)
    headers = helper.sink_read(headers_dir) if headers_dir else None
    if headers is not None:
        # объекты известны из потока заголовков: файлов <Класс>.json в дампе нет.
        # Обход всё равно ведёт дерево — от его порядка зависят id объектов
        # (sorted() по глубине устойчив, поэтому порядок dict objects сохраняется)
        objects = {}
        for dirpath, _files in tree:
            record = headers.get(rel_by_dir[dirpath])
            if record:
                objects[dirpath] = record[0]
    else:
        objects = _find_objects(tree, dump_dir)
    if dump_dir not in objects:
        raise ValueError(f'В {dump_dir} не найден корневой объект (Configuration.json и т.п.)')

    conn = sqlite3.connect(db_path)
    stats = {'objects': 0, 'modules': 0, 'methods': 0, 'files': 0, 'files_content': 0,
             'skd': 0, 'attributes': 0, 'refs': 0, 'enum_values': 0, 'predefined': 0,
             'subconto': 0,
             'common_targets': 0, 'tabular': 0, 'xdto_types': 0, 'xdto_properties': 0,
             'role_rights': 0, 'role_rls_templates': 0, 'role_rights_failed': 0}
    try:
        # база пересоздаётся с нуля при каждом запуске: отключаем fsync для скорости,
        # но журнал оставляем rollback (не MEMORY) — прерванная запись должна
        # откатываться к согласованному состоянию, а не оставлять «полупустой» файл
        conn.execute('PRAGMA synchronous=OFF')
        # увеличиваем кэш страниц с 2 МБ до 256 МБ — меньше чтений с диска при вставке
        conn.execute('PRAGMA cache_size=-262144')
        # страница 16 КБ до создания таблиц: меньше узлов B-дерева на тех же данных
        conn.execute('PRAGMA page_size=16384')
        conn.executescript(SCHEMA)
        conn.execute('BEGIN')
        cur = conn.execute(
            'INSERT INTO source (file, created, file_size, file_sha256) '
            'VALUES (?, ?, ?, ?)',
            (source_file or '', datetime.now().isoformat(timespec='seconds'),
             source_size, source_sha256)
        )
        source_id = cur.lastrowid

        # первый проход: собираем сведения об объектах и порядок братьев (ord)
        files_of = dict(tree)
        infos = {}
        for dirpath in sorted(objects, key=lambda d: d.count(os.sep)):
            stem = objects[dirpath]
            rel = rel_by_dir[dirpath]
            if headers is not None:
                stem, uuid, header_text = headers[rel]
                header = json.loads(header_text)
            else:
                header, header_text = _read_header(os.path.join(dirpath, f'{stem}.json'))
                id_name = f'{stem}.id.json'
                # состав каталога уже есть в снимке обхода — os.path.isfile не нужен
                if any(fn == id_name for fn, _size in files_of[dirpath]):
                    uuid = _read_id_uuid(os.path.join(dirpath, id_name))
                else:
                    uuid = header.get('uuid')
            parent_rel = None
            parent_dir = os.path.dirname(dirpath)
            while parent_dir and parent_dir.startswith(dump_dir):
                candidate = rel_by_dir.get(parent_dir)
                if candidate is not None and candidate in infos:
                    parent_rel = candidate
                    break
                parent_dir = os.path.dirname(parent_dir)
            infos[rel] = dict(dirpath=dirpath, stem=stem, uuid=uuid,
                              name=header.get('name') or os.path.basename(dirpath),
                              header=header, header_text=header_text, parent_rel=parent_rel)

        # ord: позиция uuid объекта в заголовке родителя (корня — в заголовке корня)
        uuid_index_cache = {}
        for rel, info in infos.items():
            parent_rel = info['parent_rel']
            parent_header = infos[parent_rel]['header'] if parent_rel is not None else None
            if parent_header is None or not info['uuid']:
                info['ord'] = None
                continue
            if parent_rel not in uuid_index_cache:
                uuid_index_cache[parent_rel] = _uuid_first_index(parent_header)
            info['ord'] = uuid_index_cache[parent_rel].get(info['uuid'])

        # id объектов — по порядку вставки (таблица пересоздаётся с нуля)
        dir_to_id = {}
        obj_header_file = {}
        obj_params = []
        for rel in sorted(infos, key=lambda r: r.count('/')):
            info = infos[rel]
            dir_to_id[rel] = len(obj_params) + 1
            obj_header_file[info['dirpath']] = f'{info["stem"]}.json'
            obj_params.append((
                source_id, dir_to_id.get(info['parent_rel']),
                info['ord'], rel, info['stem'], TYPE_RU.get(info['stem'], info['stem']),
                info['name'], info['uuid'],
                info['header'].get('comment'), info['header'].get('obj_version'),
                info['header_text'] or json.dumps(info['header'], ensure_ascii=False)))
        conn.executemany(
            'INSERT INTO meta_object (source_id, parent_id, ord, path, type, type_ru, name,'
            ' uuid, comment, obj_version, header_json)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', obj_params)
        stats['objects'] += len(obj_params)
        for rel, info in infos.items():
            if info['parent_rel'] is None:
                conn.execute(
                    'UPDATE source SET root_type=?, root_name=?, root_uuid=? WHERE id=?',
                    (info['stem'], info['name'], info['uuid'], source_id)
                )

        rel_to_id = {rel: dir_to_id[rel] for rel in infos}
        uuid_to_id = {i['uuid']: rel_to_id[r] for r, i in infos.items() if i['uuid']}
        uuid_to_path = {i['uuid']: r for r, i in infos.items() if i['uuid']}

        # реквизиты объектов (имя + тип) в порядке объявления
        name2paths = {}
        for rel, info in infos.items():
            name2paths.setdefault(info['name'], []).append(rel)
        root_stem = objects.get(dump_dir, 'Configuration')
        ref2name = _read_refmap(os.path.join(dump_dir, f'{root_stem}.10.json'))
        dt_map, dt_members = _defined_type_map(infos)
        resolver = _TypeResolver(uuid_to_path, name2paths, ref2name, dt_map, dt_members)
        attr_params = []
        ref_params = []
        for rel, info in infos.items():
            bags = []
            attrs = _extract_attributes(info['header'], resolver, bags)
            # табличные части — из того же обхода заголовка (отдельный
            # _section_bags обошёл бы дерево ещё раз)
            info['tabular'] = _tabular_rows(bags)
            for ord_no, (name, type_str, links, tabular, uuid) in enumerate(attrs):
                attr_params.append((dir_to_id[rel], ord_no, name, type_str,
                                    tabular, uuid))
                attr_id = len(attr_params)
                # связи реквизита с объектами метаданных по ссылочным uuid
                for l_ord, (l_uuid, l_path) in enumerate(links):
                    ref_params.append((attr_id, l_ord, l_uuid,
                                       dir_to_id.get(l_path) if l_path else None))
        conn.executemany(
            'INSERT INTO meta_attribute'
            ' (object_id, ord, name, type_str, tabular, uuid)'
            ' VALUES (?, ?, ?, ?, ?, ?)',
            attr_params)
        conn.executemany(
            'INSERT INTO attribute_ref (attribute_id, ord, uuid, object_id)'
            ' VALUES (?, ?, ?, ?)', ref_params)
        stats['attributes'] += len(attr_params)
        stats['refs'] += len(ref_params)

        # значения перечислений, предопределённые, привязки общих реквизитов
        tab_params = []
        enum_params = []
        common_params = []
        predef_params = []
        subconto_params = []
        for rel, info in infos.items():
            obj_id = dir_to_id[rel]
            for ord_no, (name, uuid) in enumerate(info['tabular']):
                tab_params.append((obj_id, ord_no, name, uuid))
            if info['stem'] == 'Enum':
                for ord_no, name in enumerate(_extract_enum_values(info['header'])):
                    enum_params.append((obj_id, ord_no, name))
            elif info['stem'] == 'CommonAttribute':
                for target in _extract_common_targets(info['header'], uuid_to_id, obj_id):
                    common_params.append((obj_id, target))
            elements, subcontos = _extract_predefined(
                os.path.join(info['dirpath'], 'Предустановленные данные.bin'),
                info['header'])
            for element in elements:
                predef_params.append((obj_id,) + element)
            for subconto in subcontos:
                subconto_params.append((obj_id,) + subconto)
        conn.executemany(
            'INSERT INTO meta_tabular (object_id, ord, name, uuid)'
            ' VALUES (?, ?, ?, ?)',
            tab_params)
        conn.executemany(
            'INSERT INTO enum_value (object_id, ord, name) VALUES (?, ?, ?)',
            enum_params)
        conn.executemany(
            'INSERT INTO common_target (common_id, target_id) VALUES (?, ?)',
            common_params)
        conn.executemany(
            'INSERT INTO predefined (object_id, ord, parent_ord, uuid, name, code, display)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?)', predef_params)
        stats['tabular'] += len(tab_params)
        stats['enum_values'] += len(enum_params)
        stats['common_targets'] += len(common_params)
        stats['predefined'] += len(predef_params)
        if subconto_params:
            # id предопределённых известны только после вставки, а вид субконто — это
            # другой предопределённый элемент (ПВХ), поэтому ссылки разрешаются вторым проходом
            ids = {(o, n): i for i, o, n in conn.execute(
                'SELECT id, object_id, ord FROM predefined')}
            kinds = {u: i for i, u in conn.execute(
                'SELECT id, uuid FROM predefined WHERE uuid IS NOT NULL')}
            sub_rows = [(ids[(o, n)], s, u, kinds.get(u), f)
                        for o, n, s, u, f in subconto_params if (o, n) in ids]
            conn.executemany(
                'INSERT INTO predefined_subconto'
                ' (predefined_id, ord, uuid, kind_id, flags) VALUES (?, ?, ?, ?, ?)',
                sub_rows)
            stats['subconto'] += len(sub_rows)

        # состав подсистем: ссылки из заголовка подсистемы в порядке объявления
        sub_params = []
        for rel, info in infos.items():
            if info['stem'] != 'Subsystem':
                continue
            flat = []
            _flatten_uuids(info['header'], flat)
            parent_uuid = infos[info['parent_rel']]['uuid'] if info['parent_rel'] else None
            seen = set()
            ord_no = 0
            for value in flat:
                target = uuid_to_id.get(value)
                if target is None or value == info['uuid'] or value == parent_uuid:
                    continue
                if value in seen:
                    continue
                seen.add(value)
                sub_params.append((rel_to_id[rel], target, ord_no))
                ord_no += 1
        conn.executemany(
            'INSERT INTO subsystem_content (subsystem_id, target_id, ord) VALUES (?, ?, ?)',
            sub_params)

        module_re_tpl = {}

        def module_re(stem):
            res = module_re_tpl.get(stem)
            if res is None:
                res = re.compile(rf'^{re.escape(stem)}\.(?P<code>.+)\.(?P<ext>bsl|text|image|bin)$')
                module_re_tpl[stem] = res
            return res

        # предпроход: список модулей в порядке обхода — для параллельного разбора
        bsl_tasks = []  # (path, object_id, code_name, context)
        for dirpath, files in tree:
            rel = rel_by_dir[dirpath]
            object_id = dir_to_id.get(rel)
            if not object_id:
                continue
            stem = objects.get(dirpath)
            for fn, _size in files:
                m = module_re(stem).match(fn)
                if m and m.group('ext') == 'bsl':
                    context = None
                    if stem == 'CommonModule':
                        context = _common_module_context(infos[rel]['header'])
                    bsl_tasks.append((os.path.join(dirpath, fn), object_id,
                                      m.group('code'), context))

        # CPU-тяжёлый разбор модулей — в пуле процессов; данные остаются на диске
        if workers > 1 and len(bsl_tasks) > 1:
            import multiprocessing
            pool_size = min(workers, MAX_PARSE_WORKERS, len(bsl_tasks))
            mp_ctx = multiprocessing.get_context('spawn')
            with mp_ctx.Pool(pool_size) as pool:
                parsed = pool.map(_parse_bsl_worker, [t[0] for t in bsl_tasks],
                                  chunksize=max(1, len(bsl_tasks) // (pool_size * 8)))
        else:
            parsed = [_parse_bsl_worker(t[0]) for t in bsl_tasks]
        parsed_by_path = {t[0]: (res[0], res[1], t[2], t[3])
                          for t, res in zip(bsl_tasks, parsed)}

        module_params = []
        method_params = []
        file_params = []
        skd_params = []
        module_id_seq = 0

        def flush(buf, sql):
            if buf:
                conn.executemany(sql, buf)
                buf.clear()

        for dirpath, files in tree:
            rel = rel_by_dir[dirpath]
            object_id = dir_to_id.get(rel)
            stem = objects.get(dirpath)
            prefix = rel + '/' if rel else ''
            for fn, size in files:
                full = os.path.join(dirpath, fn)
                rel_file = prefix + fn
                if object_id and fn == obj_header_file.get(dirpath):
                    continue
                if object_id and fn == f'{stem}.id.json':
                    continue
                ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
                if object_id:
                    m = module_re(stem).match(fn)
                    if m and m.group('ext') == 'bsl':
                        # pop — отдаём память по мере вставки
                        module_body, methods, code_name, context = parsed_by_path.pop(full)
                        module_id_seq += 1
                        module_params.append((object_id, code_name, context, module_body))
                        ord_no = 0
                        for method in methods:
                            method_params.append((
                                module_id_seq, ord_no, method['kind'], method['name'],
                                method['signature'], int(method['is_export']),
                                ', '.join(method['directives']), method['description'],
                                method['line_start'], method['line_end'],
                                method['body']))
                            ord_no += 1
                        stats['modules'] += 1
                        stats['methods'] += len(methods)
                        # файл модуля тоже отражаем в file (без data — тело в module):
                        # это полная карта «объект → файл дампа» для внешних инструментов
                        file_params.append((source_id, object_id, rel_file, 'bsl',
                                            size, None))
                        stats['files'] += 1
                        if len(method_params) >= 50000:
                            flush(module_params,
                                  'INSERT INTO module (object_id, code_name, context, body)'
                                  ' VALUES (?, ?, ?, ?)')
                            flush(method_params,
                                  'INSERT INTO method (module_id, ord, kind, name, signature,'
                                  ' is_export, directives, description, line_start, line_end, body)'
                                  ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
                        continue
                data = None
                if ext in TEXT_KINDS:
                    data = _read_text(full).encode('utf-8')
                    stats['files_content'] += 1
                elif store_blobs:
                    with open(helper.long_path(full), 'rb') as f:
                        data = f.read()
                    stats['files_content'] += 1
                if object_id and ext == 'bin':
                    skd = _extract_skd_queries(full)
                    if skd:
                        for ord_no, query in enumerate(skd):
                            skd_params.append((object_id, ord_no, query))
                        stats['skd'] += len(skd)
                    # содержимое пакета XDTO — тоже XML в .bin, но другой разметки
                    package = xdto.parse_package(full)
                    if package:
                        _insert_xdto(conn, object_id, package, stats)
                file_params.append((source_id, object_id, rel_file, ext or 'bin', size, data))
                stats['files'] += 1
                if len(file_params) >= 2000:
                    flush(file_params,
                          'INSERT INTO file (source_id, object_id, path, kind, size, data)'
                          ' VALUES (?, ?, ?, ?, ?, ?)')
                    flush(skd_params,
                          'INSERT INTO skd_query (object_id, ord, query) VALUES (?, ?, ?)')
        flush(module_params,
              'INSERT INTO module (object_id, code_name, context, body) VALUES (?, ?, ?, ?)')
        flush(method_params,
              'INSERT INTO method (module_id, ord, kind, name, signature,'
              ' is_export, directives, description, line_start, line_end, body)'
              ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
        flush(file_params,
              'INSERT INTO file (source_id, object_id, path, kind, size, data)'
              ' VALUES (?, ?, ?, ?, ?, ?)')
        flush(skd_params,
              'INSERT INTO skd_query (object_id, ord, query) VALUES (?, ?, ?)')
        # права ролей — после объектов: нужны их id и карта uuid -> объект
        _write_role_rights(conn, dump_dir, stats)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # FTS-индекс — после закрытия базы: он собирается в приставные файлы-шарды,
    # а рабочие процессы открывают базу read-only
    if build_fts:
        build_fts_index(db_path, workers=workers)
    return stats
