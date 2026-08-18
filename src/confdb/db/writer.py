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
import sqlite3
import xml.sax.saxutils
from datetime import datetime

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
    'ChartOfCharacteristicTypes': 'План видов характеристик',
    'ChartOfCharacteristicTypesForm': 'Форма плана видов характеристик',
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
            self.links.append((uuid, None))
            return f'Ссылка: {name}'
        path = self.dt_map.get(uuid)
        if path is not None:
            self.links.append((uuid, path))
            members = self._dt_members(path, _seen or set())
            if members:
                return f'ОпределяемыйТип: {path} ({" | ".join(members)})'
            return f'ОпределяемыйТип: {path}'
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


def _extract_attributes(header, resolver=None):
    """Извлекает реквизиты и поля табличных частей: список
    (name, type_str, links, tabular) в порядке объявления.

    Запись реквизита в заголовке: ["2", CORE, TYPEDESC] (простой тип) или
    ["2", CORE, <uuid>, TYPEDESC, ..] (составной тип — дескриптор в node[3]).
    Поля табличной части — те же записи внутри блока полей секции; tabular —
    имя табличной части (иначе None).
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
        name, typedesc = _attr_record(node)
        if name and name not in seen:
            seen.add(name)
            if resolver:
                result.append((name, resolver.describe(typedesc),
                               list(resolver.links), section))
            else:
                result.append((name, None, [], section))
        for i, child in enumerate(node):
            if _is_section_bag(node, i):
                walk(child, _section_name(node[i - 2]) or section)
            else:
                walk(child, section)

    walk(header.get('header'), None)
    return result


def _attr_record(node):
    """Имя и дескриптор типа из записи реквизита/поля, иначе (None, None)."""
    if (len(node) >= 3 and node[0] == '2' and isinstance(node[1], list)
            and len(node[1]) >= 3 and node[1][0] == '3'
            and isinstance(node[1][2], str)):
        if isinstance(node[2], list):
            return _unquote(node[1][2]), node[2]
        if len(node) > 3 and isinstance(node[3], list):
            return _unquote(node[1][2]), node[3]
        return _unquote(node[1][2]), None
    return None, None


def _section_name(rec):
    """Имя табличной части из записи секции: ищем core ["3",..,ИМЯ,..]."""
    stack = [rec]
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            if (len(node) >= 3 and str(node[0]) == '3'
                    and isinstance(node[2], str) and node[2].startswith('"')):
                return _unquote(node[2])
            stack.extend(node)
    return None


def _is_section_bag(parent, idx):
    """Блок полей секции: [.., ЗАПИСЬ_СЕКЦИИ, 1, [VT_FIELDS_KEY, N, поля..], ..]."""
    node = parent[idx]
    return (isinstance(node, list) and bool(node) and node[0] == VT_FIELDS_KEY
            and idx >= 2 and str(parent[idx - 1]) == '1'
            and isinstance(parent[idx - 2], list))


def _extract_tabular(header):
    """Табличные части объекта: список имён в порядке объявления."""
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
                name = _section_name(node[i - 2])
                if name and name not in result:
                    result.append(name)
            walk(child)

    walk(header.get('header'))
    return result


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


def _extract_predefined(bin_path):
    """Предопределённые элементы из 'Предустановленные данные.bin'.

    Запись значения: ['2', <id>, <n>, ..слоты.., ['S', имя], ['S', код],
    ['S', наименование], ..] — берём первые три строковых слота.
    """
    if not os.path.isfile(bin_path):
        return []
    try:
        data = helper.brace_file_read(os.path.dirname(bin_path),
                                      os.path.basename(bin_path))
    except (OSError, ValueError, IndexError):
        return []
    result = []

    def walk(node):
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
            return
        if not isinstance(node, list):
            return
        if len(node) >= 9 and node[0] == '2' and str(node[1]).isdigit():
            strs = [_unquote(x[1]) for x in node
                    if isinstance(x, list) and len(x) >= 2
                    and str(x[0]).strip('"') == 'S' and isinstance(x[1], str)]
            if len(strs) >= 3 and strs[0]:
                result.append((strs[0], strs[1], strs[2]))
        for child in node:
            walk(child)

    walk(data)
    return result


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
    root_uuid TEXT
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
CREATE TABLE meta_attribute (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    name TEXT NOT NULL,
    type_str TEXT,
    tabular TEXT
);
CREATE INDEX ix_meta_attribute_object ON meta_attribute(object_id, ord);
CREATE TABLE meta_tabular (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES meta_object(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    name TEXT NOT NULL
);
CREATE INDEX ix_meta_tabular_object ON meta_tabular(object_id, ord);
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
    name TEXT NOT NULL,
    code TEXT,
    display TEXT
);
CREATE INDEX ix_predefined_object ON predefined(object_id, ord);
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
"""

TEXT_KINDS = ('bsl', 'html', 'htm', 'txt', 'json', 'xml', 'css', 'js')


def _find_objects(dump_dir):
    """Возвращает {абсолютный путь каталога: имя класса}; корень — по особой логике."""
    objects = {}
    root_candidates = []
    for dirpath, dirnames, filenames in os.walk(dump_dir):
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
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _read_text(path):
    for encoding in ('utf-8-sig', 'windows-1251'):
        try:
            with open(path, 'r', encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
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
        with open(path, 'rb') as f:
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


# Разбор модулей масштабируется примерно до 8 процессов: дальше накладные
# расходы на spawn и передачу тел методов через IPC превышают выигрыш.
MAX_PARSE_WORKERS = 8


def _parse_bsl_worker(path):
    """Разбор модуля в рабочем процессе пула (данные остаются на диске)."""
    return parse_methods(_read_text(path))


def write_db(dump_dir, db_path, *, source_file=None, store_blobs=False, workers=1):
    """Пишет дамп каталога stage 3 в SQLite. Возвращает статистику.

    :param dump_dir: каталог результата декодера (стадия 3)
    :param db_path: файл SQLite (существующий перезаписывается)
    :param source_file: путь к исходному .cf/.cfe/.epf (для таблицы source)
    :param store_blobs: хранить бинарные файлы (image/bin) как BLOB
    :param workers: число процессов для разбора модулей BSL (1 — последовательно)
    """
    dump_dir = os.path.abspath(dump_dir)
    if not os.path.isdir(dump_dir):
        raise FileNotFoundError(dump_dir)
    if os.path.isdir(db_path):
        raise ValueError(f'Путь БД указывает на каталог: {db_path}')
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)

    objects = _find_objects(dump_dir)
    if dump_dir not in objects:
        raise ValueError(f'В {dump_dir} не найден корневой объект (Configuration.json и т.п.)')

    conn = sqlite3.connect(db_path)
    stats = {'objects': 0, 'modules': 0, 'methods': 0, 'files': 0, 'files_content': 0,
             'skd': 0, 'attributes': 0, 'refs': 0, 'enum_values': 0, 'predefined': 0,
             'common_targets': 0, 'tabular': 0}
    try:
        # база пересоздаётся с нуля при каждом запуске: отключаем fsync для скорости,
        # но журнал оставляем rollback (не MEMORY) — прерванная запись должна
        # откатываться к согласованному состоянию, а не оставлять «полупустой» файл
        conn.execute('PRAGMA synchronous=OFF')
        conn.executescript(SCHEMA)
        conn.execute('BEGIN')
        cur = conn.execute(
            'INSERT INTO source (file, created) VALUES (?, ?)',
            (source_file or '', datetime.now().isoformat(timespec='seconds'))
        )
        source_id = cur.lastrowid

        # первый проход: собираем сведения об объектах и порядок братьев (ord)
        infos = {}
        for dirpath in sorted(objects, key=lambda d: d.count(os.sep)):
            stem = objects[dirpath]
            rel = os.path.relpath(dirpath, dump_dir)
            rel = '' if rel == '.' else rel.replace(os.sep, '/')
            header = _read_json(os.path.join(dirpath, f'{stem}.json'))
            id_file = os.path.join(dirpath, f'{stem}.id.json')
            if os.path.isfile(id_file):
                uuid = _read_json(id_file).get('uuid')
            else:
                uuid = header.get('uuid')
            parent_rel = None
            parent_dir = os.path.dirname(dirpath)
            while parent_dir and parent_dir.startswith(dump_dir):
                candidate = os.path.relpath(parent_dir, dump_dir)
                candidate = '' if candidate == '.' else candidate.replace(os.sep, '/')
                if candidate in infos:
                    parent_rel = candidate
                    break
                parent_dir = os.path.dirname(parent_dir)
            infos[rel] = dict(dirpath=dirpath, stem=stem, uuid=uuid,
                              name=header.get('name') or os.path.basename(dirpath),
                              header=header, parent_rel=parent_rel)

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
                json.dumps(info['header'], ensure_ascii=False)))
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
            attrs = _extract_attributes(info['header'], resolver)
            for ord_no, (name, type_str, links, tabular) in enumerate(attrs):
                attr_params.append((dir_to_id[rel], ord_no, name, type_str, tabular))
                attr_id = len(attr_params)
                # связи реквизита с объектами метаданных по ссылочным uuid
                for l_ord, (l_uuid, l_path) in enumerate(links):
                    ref_params.append((attr_id, l_ord, l_uuid,
                                       dir_to_id.get(l_path) if l_path else None))
        conn.executemany(
            'INSERT INTO meta_attribute'
            ' (object_id, ord, name, type_str, tabular) VALUES (?, ?, ?, ?, ?)',
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
        for rel, info in infos.items():
            obj_id = dir_to_id[rel]
            for ord_no, name in enumerate(_extract_tabular(info['header'])):
                tab_params.append((obj_id, ord_no, name))
            if info['stem'] == 'Enum':
                for ord_no, name in enumerate(_extract_enum_values(info['header'])):
                    enum_params.append((obj_id, ord_no, name))
            elif info['stem'] == 'CommonAttribute':
                for target in _extract_common_targets(info['header'], uuid_to_id, obj_id):
                    common_params.append((obj_id, target))
            for ord_no, (name, code, display) in enumerate(_extract_predefined(
                    os.path.join(info['dirpath'], 'Предустановленные данные.bin'))):
                predef_params.append((obj_id, ord_no, name, code, display))
        conn.executemany(
            'INSERT INTO meta_tabular (object_id, ord, name) VALUES (?, ?, ?)',
            tab_params)
        conn.executemany(
            'INSERT INTO enum_value (object_id, ord, name) VALUES (?, ?, ?)',
            enum_params)
        conn.executemany(
            'INSERT INTO common_target (common_id, target_id) VALUES (?, ?)',
            common_params)
        conn.executemany(
            'INSERT INTO predefined (object_id, ord, name, code, display)'
            ' VALUES (?, ?, ?, ?, ?)', predef_params)
        stats['tabular'] += len(tab_params)
        stats['enum_values'] += len(enum_params)
        stats['common_targets'] += len(common_params)
        stats['predefined'] += len(predef_params)

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

        rel_by_dir = {i['dirpath']: r for r, i in infos.items()}

        # предпроход: список модулей в порядке обхода — для параллельного разбора
        bsl_tasks = []  # (path, object_id, code_name, context)
        for dirpath, dirnames, filenames in os.walk(dump_dir):
            rel = rel_by_dir.get(dirpath)
            object_id = dir_to_id.get(rel) if rel is not None else None
            if not object_id:
                continue
            stem = objects.get(dirpath)
            for fn in sorted(filenames):
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

        for dirpath, dirnames, filenames in os.walk(dump_dir):
            rel = rel_by_dir.get(dirpath)
            object_id = dir_to_id.get(rel) if rel is not None else None
            stem = objects.get(dirpath)
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel_file = os.path.relpath(full, dump_dir).replace(os.sep, '/')
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
                        if len(method_params) >= 50000:
                            flush(module_params,
                                  'INSERT INTO module (object_id, code_name, context, body)'
                                  ' VALUES (?, ?, ?, ?)')
                            flush(method_params,
                                  'INSERT INTO method (module_id, ord, kind, name, signature,'
                                  ' is_export, directives, description, line_start, line_end, body)'
                                  ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
                        continue
                size = os.path.getsize(full)
                data = None
                if ext in TEXT_KINDS:
                    data = _read_text(full).encode('utf-8')
                    stats['files_content'] += 1
                elif store_blobs:
                    with open(full, 'rb') as f:
                        data = f.read()
                    stats['files_content'] += 1
                if object_id and ext == 'bin':
                    skd = _extract_skd_queries(full)
                    if skd:
                        for ord_no, query in enumerate(skd):
                            skd_params.append((object_id, ord_no, query))
                        stats['skd'] += len(skd)
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats
