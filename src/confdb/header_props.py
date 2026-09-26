"""Свойства объектов метаданных, извлекаемые из header_json готовой базы.

Заголовок объекта хранится в meta_object.header_json как есть (порт v8unpack),
поэтому часть сведений доступна без переизвлечения конфигурации: версия и режим
совместимости конфигурации, префикс имён расширения, состав измерений/ресурсов/
реквизитов регистра, его периодичность и режим записи.

Соответствие позиций и канонических uuid проверено на реальной базе УНФ
(949 регистров сведений, 123 регистра накопления) и на паре
configuration/extension из тестов v8unpack; подробности — в базе знаний
проекта, страницы `register-header-structure` и `configuration-header-props`.

Тип загруженного файла определяется расширением исходника, и оно точнее типа
корневого объекта: .erf и .epf декодирует один класс ExternalDataProcessor
(v8/decoder.py), поэтому корень внешнего отчёта попадает в базу как
ExternalDataProcessor.
"""
import json
import os

# Канонические uuid коллекций реквизитов регистра: узел коллекции в header[0]
# начинается таким uuid, позиция узла у разных типов регистра разная.
# Назначение подтверждено составом типов полей: ресурсы числовые, измерения
# ссылочные (РегистрСведений.ЦеныНоменклатуры, РегистрСведений.ОстаткиТоваров).
# Регистр сведений
IR_RESOURCES = '13134202-f60b-11d5-a3c7-0050bae0a776'
IR_DIMENSIONS = '13134203-f60b-11d5-a3c7-0050bae0a776'
IR_ATTRIBUTES = 'a2207540-1400-11d6-a3c7-0050bae0a776'
IR_FORMS = '13134204-f60b-11d5-a3c7-0050bae0a776'
# Регистр накопления
AR_RESOURCES = 'b64d9a41-1642-11d6-a3c7-0050bae0a776'
AR_ATTRIBUTES = 'b64d9a42-1642-11d6-a3c7-0050bae0a776'
AR_DIMENSIONS = 'b64d9a43-1642-11d6-a3c7-0050bae0a776'
AR_FORMS = 'b64d9a44-1642-11d6-a3c7-0050bae0a776'

REGISTER_COLLECTIONS = {
    IR_RESOURCES: 'Ресурсы',
    IR_DIMENSIONS: 'Измерения',
    IR_ATTRIBUTES: 'Реквизиты',
    AR_RESOURCES: 'Ресурсы',
    AR_ATTRIBUTES: 'Реквизиты',
    AR_DIMENSIONS: 'Измерения',
}

# Порядок групп в паспорте объекта — как в конфигураторе
REGISTER_KINDS = ('Измерения', 'Ресурсы', 'Реквизиты')

# Коды периодичности регистра сведений. Полный набор значений перечисления
# ПериодичностьРегистраСведений — из официальной справки платформы 8.3.26.1498
# (shcntx_ru.hbk; база syntax.db проекта 1c-syntax-db-extractor, тема
# InformationRegisterPeriodicity: внутренние номера значений 1582..1588,
# смещение 1582 и есть код в заголовке). Смещение подтверждено пятью кодами,
# расшифрованными раньше по семантике регистров УНФ (КоэффициентДефлятор=год,
# СведенияОВзносахВПФР=квартал, МинимальнаяОплатаТрудаРФ=месяц, КурсыВалют=день),
# и независимым признаком: все 10 регистров УНФ с кодом 6 подчинены регистратору,
# как того требует описание значения «ПозицияРегистратора».
PERIODICITY = {
    '0': 'непериодический', '1': 'год', '2': 'квартал', '3': 'месяц',
    '4': 'день', '5': 'секунда', '6': 'позиция регистратора',
}

# Регистры, которые платформа всегда подчиняет регистратору (независимого
# режима записи у них нет); периодичности у них тоже нет.
RECORDER_ONLY = frozenset({
    'AccumulationRegister', 'AccountingRegister', 'CalculationRegister',
})

# Позиции скалярных свойств в header[0][1] регистра сведений
IR_PERIODICITY = 18
IR_WRITE_MODE = 19

# Позиции в узле свойств конфигурации header[0][3][1][1]
CFG_VERSION = 15
CFG_NAME_PREFIX = 42

# Тип загруженного файла — по расширению исходника и по типу корневого объекта
FILE_KINDS = {
    '.cf': 'Конфигурация',
    '.cfe': 'Расширение конфигурации',
    '.epf': 'Внешняя обработка',
    '.erf': 'Внешний отчёт',
}

ROOT_KINDS = {
    'Configuration': 'Конфигурация',
    'ConfigurationExtension': 'Расширение конфигурации',
    'ExternalDataProcessor': 'Внешняя обработка',
    'ExternalReport': 'Внешний отчёт',
}

# Родительный падеж названия типа — для строки «Версия …» паспорта
VERSION_NOUNS = {
    'Конфигурация': 'конфигурации',
    'Расширение конфигурации': 'расширения',
    'Внешняя обработка': 'обработки',
    'Внешний отчёт': 'отчёта',
}

# Режим совместимости — свойство конфигурации: у внешних обработок и отчётов
# он не задаётся, а расширение выполняется в режиме основной конфигурации.
WITHOUT_COMPATIBILITY = frozenset({'ExternalDataProcessor', 'ExternalReport'})


def unquote(value):
    """'"3.0.4.4"' -> '3.0.4.4'; не-строки возвращает как есть."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' \
            and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def compatibility_str(code):
    """'80321' -> '8.3.21'; то, что не похоже на код, возвращает как есть."""
    text = str(code or '').strip()
    if len(text) >= 4 and text.isdigit() and text[0] in '78':
        return f'{text[0]}.{int(text[1:3])}.{int(text[3:])}'
    return text or None


def source_ext(path):
    """Расширение исходного файла ('.cfe') или None."""
    return os.path.splitext(str(path or ''))[1].lower() or None


def kind_of(root_type=None, root_type_ru=None, source_file=None):
    """(тип загруженного файла, его расширение) — ('Внешний отчёт', '.erf').

    Расширение исходника важнее типа корня: корень .erf записывается в базу
    как ExternalDataProcessor, и только '.erf' говорит, что это отчёт.
    """
    ext = source_ext(source_file)
    name = (FILE_KINDS.get(ext) or ROOT_KINDS.get(root_type)
            or root_type_ru or root_type)
    return name, ext


def version_noun(kind_name):
    """'Расширение конфигурации' -> 'расширения' — для строки «Версия …»."""
    return VERSION_NOUNS.get(kind_name, 'конфигурации')


def compatibility_line(root_type, compatibility):
    """Режим совместимости для паспорта базы, с учётом типа корневого объекта.

    У внешних обработок и отчётов такого свойства нет вообще, а расширение
    выполняется в режиме основной конфигурации — значение из его файла
    показывается как справочное, а не как действующий режим.
    """
    if root_type in WITHOUT_COMPATIBILITY:
        return compatibility or 'не задаётся'
    if root_type == 'ConfigurationExtension':
        text = 'наследуется от основной конфигурации'
        return (f'{text}; в файле расширения указан {compatibility}'
                if compatibility else text)
    return compatibility or 'не определён'


def compatibility_short(root_type, compatibility):
    """То же одной фразой для списка баз; None — выводить нечего."""
    if root_type in WITHOUT_COMPATIBILITY:
        return compatibility
    if root_type == 'ConfigurationExtension':
        return (f'{compatibility} (наследуется)' if compatibility
                else 'наследуется от основной конфигурации')
    return compatibility


def _load(header_json):
    try:
        return json.loads(header_json)
    except (TypeError, ValueError):
        return None


def _is_attr(node):
    """Запись реквизита ['2', CORE, TYPEDESC] -> имя, иначе None."""
    if not isinstance(node, list) or len(node) < 3 or str(node[0]) != '2':
        return None
    core = node[1]
    if (isinstance(core, list) and len(core) >= 3 and str(core[0]) == '3'
            and isinstance(core[2], str)):
        return unquote(core[2])
    return None


def _attr_names(node, out=None):
    """Имена всех реквизитов внутри узла (обход в глубину)."""
    if out is None:
        out = []
    if isinstance(node, dict):
        for value in node.values():
            _attr_names(value, out)
    elif isinstance(node, list):
        name = _is_attr(node)
        if name is not None:
            out.append(name)
            return out
        for child in node:
            _attr_names(child, out)
    return out


def register_field_kinds(header_json):
    """{имя реквизита: 'Измерения'|'Ресурсы'|'Реквизиты'} для регистра.

    Пустой словарь — коллекции не распознаны (не регистр, неизвестный тип
    регистра или другой формат заголовка): вызывающий код показывает реквизиты
    одним плоским списком, как раньше.
    """
    data = _load(header_json)
    if data is None:
        return {}
    try:
        top = data['header'][0]
    except (KeyError, IndexError, TypeError):
        return {}
    kinds = {}
    for node in top if isinstance(top, list) else []:
        if not isinstance(node, list) or not node or not isinstance(node[0], str):
            continue
        kind = REGISTER_COLLECTIONS.get(node[0].strip('"').lower())
        if kind is None:
            continue
        for name in _attr_names(node):
            kinds.setdefault(name, kind)
    return kinds


def register_props(obj_type, header_json):
    """Строки свойств регистра: периодичность и режим записи.

    Возвращает только то, что подтверждено: у регистра накопления позиция [18]
    булева, а не код периодичности, поэтому периодичность выводится лишь для
    регистра сведений.
    """
    if obj_type in RECORDER_ONLY:
        return ['Режим записи: подчинение регистратору']
    if obj_type != 'InformationRegister':
        return []
    data = _load(header_json)
    if data is None:
        return []
    try:
        inner = data['header'][0][1]
    except (KeyError, IndexError, TypeError):
        return []
    if not isinstance(inner, list):
        return []
    lines = []
    code = _scalar(inner, IR_PERIODICITY)
    if code is not None:
        period = PERIODICITY.get(code)
        if period is None:
            # код встречается в реальных конфигурациях, но не расшифрован:
            # важно хотя бы то, что регистр периодический
            period = f'есть, код {code} (не расшифрован)' if code != '0' else 'нет'
        lines.append(f'Периодичность: {period}')
    mode = _scalar(inner, IR_WRITE_MODE)
    if mode == '1':
        lines.append('Режим записи: подчинение регистратору')
    elif mode == '0':
        lines.append('Режим записи: независимый')
    return lines


def _scalar(inner, index):
    """Скалярное значение позиции заголовка или None (нет/не скаляр).

    None, булево значение и пустая строка — не факт о свойстве: позиция в
    заголовке присутствует, но ничего не говорит. Вернуть их строкой значило бы
    выдумать значение — «Периодичность: есть, код None (не расшифрован)».
    """
    if len(inner) <= index or isinstance(inner[index], (list, dict)):
        return None
    value = inner[index]
    if value is None or isinstance(value, bool):
        return None
    value = str(value)
    return value or None


def config_props(header_json):
    """Сведения о конфигурации/расширении из заголовка корневого объекта.

    Возвращает словарь: name, synonym, version, compatibility, name_prefix,
    obj_version — отсутствующие значения не включаются.
    """
    data = _load(header_json)
    if not isinstance(data, dict):
        return {}
    out = {}
    name = data.get('name')
    if name:
        out['name'] = unquote(name)
    synonyms = data.get('name2')
    if isinstance(synonyms, dict):
        synonym = synonyms.get('ru')
        if synonym:
            out['synonym'] = unquote(synonym)
    compat = compatibility_str(data.get('compatibility_version'))
    if compat:
        out['compatibility'] = compat
    if data.get('obj_version'):
        out['obj_version'] = str(data['obj_version'])
    props = _config_props_node(data)
    if props is not None:
        version = unquote(_scalar(props, CFG_VERSION) or '')
        if version:
            out['version'] = version
        prefix = unquote(_scalar(props, CFG_NAME_PREFIX) or '')
        if prefix:
            out['name_prefix'] = prefix
    return out


def _config_props_node(data):
    """Узел свойств конфигурации header[0][3][1][1] (длинный список скаляров)."""
    try:
        node = data['header'][0][3][1][1]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(node, list) or len(node) <= CFG_NAME_PREFIX:
        return None
    return node
