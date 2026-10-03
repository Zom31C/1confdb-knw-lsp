"""Тесты записи дампа стадии 3 в SQLite на синтетическом дереве."""
import json
import os
import sqlite3

from confdb.db.writer import VT_FIELDS_KEY, tabular_field_counts, write_db
from confdb.header_props import (AR_DIMENSIONS, AR_RESOURCES, IR_ATTRIBUTES,
                                 IR_DIMENSIONS, IR_FORMS, IR_RESOURCES)

ROOT_UUID = 'aaaaaaaa-0000-0000-0000-000000000001'
CAT_UUID = 'bbbbbbbb-0000-0000-0000-000000000002'
FORM_UUID = 'cccccccc-0000-0000-0000-000000000003'
DT_UUID = 'dddddddd-0000-0000-0000-000000000004'
ENUM_UUID = 'eeeeeeee-0000-0000-0000-000000000005'
COMMON_UUID = 'ffffffff-0000-0000-0000-000000000006'
DOC_UUID = '0d0d0d0d-0000-0000-0000-000000000007'
IR_UUID = '0e0e0e0e-0000-0000-0000-000000000008'
AR_UUID = '0f0f0f0f-0000-0000-0000-000000000009'
CM_UUID = '0a0a0a0a-0000-0000-0000-00000000000a'
XDTO_UUID = '0b0b0b0b-0000-0000-0000-00000000000b'
ES_UUID = '0c0c0c0c-0000-0000-0000-00000000000c'
EP_UUID = '0d0d0d0d-0000-0000-0000-00000000000d'
REF_CAT = '11111111-1111-1111-1111-111111111111'  # ссылочный uuid справочника в .10
REF_DT = '22222222-2222-2222-2222-222222222222'   # собственный ссылочный uuid DT
REF_ORPHAN = '44444444-4444-4444-4444-444444444444'  # имя в .10 есть, объекта в базе нет
REF_UNKNOWN = '99999999-9999-9999-9999-999999999999'  # источник подписки без объекта

# платформенные uuid типа «уникальный идентификатор» и таблицы видов субконто —
# те же, что в реальных .cf (БП_РФ, УНФ)
PREDEF_TYPE = 'ae135932-4f94-44df-92c1-c91f15a92848'
SUBCONTO_TABLE_TYPE = 'acf6192e-81ca-46ef-93a6-5a6968b78663'
FLAG_SUM_UUID = 'bc4c2981-98f7-4f8f-a232-1024d34b754b'   # «Суммовой»
FLAG_CUR_UUID = '2c278dca-06f0-4dbd-8379-e73f74822973'   # «Валютный»
FLAG_QTY_UUID = '25661fe8-4c82-4116-a412-2b3443ac4ca2'   # «Количественный»
ZERO_UUID = '00000000-0000-0000-0000-000000000000'
PRE_ELEM_UUID = '4a4a4a4a-0000-0000-0000-0000000000a1'
CHART_UUID = '1a1a1a1a-0000-0000-0000-0000000000b1'
CHX_UUID = '1b1b1b1b-0000-0000-0000-0000000000c1'
ACC_OS_UUID = '2a2a2a2a-0000-0000-0000-000000000001'
ACC_OS_ORG_UUID = '2a2a2a2a-0000-0000-0000-000000000002'
ACC_GOODS_UUID = '2a2a2a2a-0000-0000-0000-000000000003'
KIND_OS_UUID = '3a3a3a3a-0000-0000-0000-000000000001'
KIND_GOODS_UUID = '3a3a3a3a-0000-0000-0000-000000000002'
CHART_PATH = 'ChartOfAccounts/ПланСчетов1'
CHX_PATH = 'ChartOfCharacteristicType/ВидыСубконто'
NOCODE_UUID = '1c1c1c1c-0000-0000-0000-0000000000d1'
DRIVER_UUID = '5a5a5a5a-0000-0000-0000-0000000000e1'
NOCODE_PATH = 'Catalog/ДрайверыОборудования'

# модуль общего назначения с экспортной функцией (запрос многострочным
# литералом с '|') и закрытой процедурой, создающей таблицу значений
COMMON_MODULE_BSL = (
    'Функция Экспортная(Парам) Экспорт\n'
    '\tЗапрос = Новый Запрос;\n'
    '\tЗапрос.Текст = "ВЫБРАТЬ\n'
    '|\tСправочник1.СсылкаАтрибут КАК Ссылка\n'
    '|ИЗ\n'
    '|\tСправочник.Справочник1 КАК Справочник1";\n'
    '\tВозврат Запрос.Выполнить().Выгрузить();\n'
    'КонецФункции\n'
    '\n'
    'Процедура Закрытая()\n'
    '\tТаблица = Новый ТаблицаЗначений;\n'
    '\tТаблица.Колонки.Добавить("Сотрудник");\n'
    'КонецПроцедуры\n')


# содержимое XDTOPackage.bin: открытый XML с BOM, CRLF и пространством имён по
# умолчанию — как в реальном дампе стадии 3 (_tmp\probe_xdto_out.txt)
XDTO_PACKAGE_XML = (
    '\ufeff<package xmlns="http://v8.1c.ru/8.1/xdto" '
    'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'targetNamespace="http://v8.1c.ru/test/package/1.0">\r\n'
    '\t<import namespace="http://v8.1c.ru/test/base/1.0"/>\r\n'
    '\t<objectType xmlns:d2p1="http://v8.1c.ru/test/base/1.0" name="Товар" '
    'base="d2p1:БазовыйТовар">\r\n'
    '\t\t<property name="Наименование" type="xs:string" lowerBound="1" '
    'nillable="false" form="Attribute"/>\r\n'
    '\t\t<property name="Количество" type="xs:decimal" lowerBound="0" '
    'nillable="true"/>\r\n'
    '\t\t<property name="Позиции" lowerBound="0" upperBound="-1" nillable="true">\r\n'
    '\t\t\t<typeDef xsi:type="ObjectType">\r\n'
    '\t\t\t\t<property name="Номер" type="xs:integer" lowerBound="0" '
    'nillable="true" form="Element"/>\r\n'
    '\t\t\t</typeDef>\r\n'
    '\t\t</property>\r\n'
    '\t</objectType>\r\n'
    '\t<valueType name="ВидОперации" base="xs:string" maxLength="20">\r\n'
    '\t\t<enumeration xsi:type="xs:string">Приход</enumeration>\r\n'
    '\t\t<enumeration xsi:type="xs:string">Расход</enumeration>\r\n'
    '\t</valueType>\r\n'
    '\t<property name="КорневойЭлемент" type="xs:string" localName="root"/>\r\n'
    '</package>\r\n'
).encode('utf-8')


def _core(name):
    return ['3', ['1', '0', 'в отдельном файле'], f'"{name}"',
            ['1', '"ru"', f'"{name}"'], '""', '0', '0',
            '00000000-0000-0000-0000-000000000000', '0']


def _attr(name, typedesc):
    """Запись реквизита внутри узла коллекции — как в реальном заголовке."""
    return ['7', ['27', ['2', _core(name), typedesc]]]


def _cfg_props(version='"1.2.3.4"', prefix='""'):
    """Узел свойств конфигурации header[0][3][1][1]: [15] версия, [42] префикс."""
    node = ['66', ['0', _core('ТестКонф')]] + ['""'] * 44
    node[15] = version
    node[42] = prefix
    return node


def _register_header(inner_flags, collections):
    """header регистра: [0]=версия, [1]=флаги объекта, [2]=коллекции."""
    return [['1', inner_flags, '6'] + collections]


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, str):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(data)
    else:
        with open(path, 'wb') as f:
            f.write(data)


def _json(path, data):
    _write(path, json.dumps(data, ensure_ascii=False))


def _brace(node):
    """Текст brace-файла: скаляры в строку, вложенный список — с новой строки.

    Парсер (v8/json_container_decoder.py) построчный: токен не может переходить
    через '\\n', поэтому разметка повторяет вывод реального декодера. Кавычки у
    токенов — часть формата: '"S"' и '"текст"' пишутся буквально, uuid и числа без них.
    """
    if not isinstance(node, list):
        return str(node)
    body = ','.join('\n' + _brace(x) if isinstance(x, list) else _brace(x)
                    for x in node)
    if node and isinstance(node[-1], list):
        body += '\n'
    return '{' + body + '}'


def _pcol(col_id, ptype, name_uuid=None):
    """Дескриптор колонки: [colId, nameUuid, ['"Pattern"', <тип>…], '""', '0'].

    ptype — содержимое Pattern: [['"#"', UUID]], [['"S"', '4', '1']], [['"B"']]
    или [] — колонка с пустым Pattern, её занимает вложенная таблица.
    """
    return [str(col_id), f'"{name_uuid}"' if name_uuid else '""',
            ['"Pattern"'] + list(ptype), '""', '0']


def _puuid(uuid):
    return ['"#"', PREDEF_TYPE, ['1', uuid]]


def _pstr(text):
    return ['"S"', f'"{text}"']


def _pbool(flag):
    return ['"B"', str(int(flag))]


def _pnum(value):
    return ['"N"', str(value)]


def _prow(row_id, values, children=()):
    """Строка: ['2', id, n, <значения>, hasChildren, (['1', count, <дети>])]."""
    node = ['2', str(row_id), str(len(values))] + list(values)
    if children:
        return node + ['1', ['1', str(len(children))] + list(children)]
    return node + ['0']


def _ptable(cols, roots, pairs=None):
    """Узел таблицы: ['2', n, <пары (позиция, colId)>, ['1', count, <строки>], …].

    Порядок значений в строке задают пары, а не номера колонок; pairs=None — позиции
    идут подряд.
    """
    if pairs is None:
        pairs = [(i, str(c[0])) for i, c in enumerate(cols)]
    flat = [str(x) for pair in pairs for x in pair]
    return ['2', str(len(pairs))] + flat + \
        [['1', str(len(roots))] + list(roots)] + ['-1', '128']


def _pfile(cols, roots, pairs=None, tag='2'):
    """'Предустановленные данные.bin' целиком: {<tag>,{1,<схема>,<таблица>}}.

    tag у реальных файлов разный ('0' у справочника, '2' у плана счетов) и на разбор
    не влияет — извлечение читает только схему и таблицу.
    """
    return _brace([tag, ['1', [str(len(cols))] + list(cols),
                         _ptable(cols, roots, pairs)]])


def _psubconto(cols, rows):
    """Значение колонки с пустым Pattern — вложенная таблица видов субконто."""
    pairs = [(i, str(c[0])) for i, c in enumerate(cols)]
    flat = [str(x) for pair in pairs for x in pair]
    table = ['2', str(len(pairs))] + flat + \
        [['1', str(len(rows))] + list(rows)] + ['4', '-1']
    return ['"#"', SUBCONTO_TABLE_TYPE,
            ['9', [str(len(cols))] + list(cols), table]]


SUBCONTO_COLS = [_pcol(0, [['"#"', PREDEF_TYPE]]), _pcol(1, [['"B"']]),
                 _pcol(2, [['"B"']], FLAG_SUM_UUID),
                 _pcol(3, [['"B"']], FLAG_CUR_UUID),
                 _pcol(4, [['"B"']], FLAG_QTY_UUID)]


def _subconto(*kinds):
    """Вложенная таблица видов субконто: (uuid вида, суммовой, валютный, кол.)."""
    return _psubconto(SUBCONTO_COLS, [
        _prow(i, [_puuid(uuid), _pbool(0), _pbool(s), _pbool(c), _pbool(q)])
        for i, (uuid, s, c, q) in enumerate(kinds)])


def _flag_desc(uuid, name):
    """Дескриптор колонки ТЧ в заголовке: ['3', ['1', '0', uuid], '"Имя"', …]."""
    return ['3', ['1', '0', uuid], f'"{name}"', ['1', '"ru"', f'"{name}"'],
            '""', '0', '0', ZERO_UUID, '0']


def make_chart_dump(base):
    """План счетов с субконто и ПВХ видов субконто — как в реальном дампе стадии 3.

    Номера колонок ПВХ начинаются с 1, а не с 0: значимые колонки извлекаются по типу.
    """
    chart_cols = [_pcol(0, [['"#"', PREDEF_TYPE]]), _pcol(1, [['"S"']]),
                  _pcol(2, [['"S"', '8', '1']]), _pcol(3, [['"S"', '120', '1']]),
                  _pcol(4, [['"N"']]), _pcol(5, [['"B"']]), _pcol(6, [])]

    def account(row_id, uuid, name, code, display, sub, children=()):
        return _prow(row_id, [_puuid(uuid), _pstr(name), _pstr(code),
                              _pstr(display), _pnum(0), _pbool(0), sub], children)

    chart_dir = os.path.join(base, 'ChartOfAccounts', 'ПланСчетов1')
    _json(os.path.join(chart_dir, 'ChartOfAccounts.json'), {
        'name': 'ПланСчетов1', 'comment': '', 'obj_version': '803',
        'header': [['1', ['0', '1', _flag_desc(FLAG_SUM_UUID, 'Суммовой'),
                          _flag_desc(FLAG_CUR_UUID, 'Валютный'),
                          _flag_desc(FLAG_QTY_UUID, 'Количественный')]]],
    })
    _json(os.path.join(chart_dir, 'ChartOfAccounts.id.json'), {'uuid': CHART_UUID})
    _write(os.path.join(chart_dir, 'Предустановленные данные.bin'),
           _pfile(chart_cols, [
               account(0, ZERO_UUID, 'Счета', '', '', ['"U"'], [
                   account(1, ACC_OS_UUID, 'ОсновныеСредства', '01',
                           'Основные средства',
                           _subconto((KIND_OS_UUID, 1, 0, 1)), [
                       account(2, ACC_OS_ORG_UUID, 'ОСвОрганизации', '01.01',
                               'Основные средства в организации',
                               _subconto((KIND_OS_UUID, 1, 1, 1)))]),
                   account(3, ACC_GOODS_UUID, 'Товары', '41', 'Товары',
                           _subconto((KIND_GOODS_UUID, 1, 1, 1),
                                     (KIND_OS_UUID, 0, 0, 0)))])]))

    chx_cols = [_pcol(1, [['"#"', PREDEF_TYPE]]), _pcol(2, [['"B"']]),
                _pcol(3, [['"S"']]), _pcol(4, [['"S"', '5', '1']]),
                _pcol(5, [['"S"', '50', '1']])]
    chx_dir = os.path.join(base, 'ChartOfCharacteristicType', 'ВидыСубконто')
    _json(os.path.join(chx_dir, 'ChartOfCharacteristicType.json'), {
        'name': 'ВидыСубконто', 'comment': '', 'obj_version': '803',
        'header': [['1', ['0', '1']]],
    })
    _json(os.path.join(chx_dir, 'ChartOfCharacteristicType.id.json'), {'uuid': CHX_UUID})
    _write(os.path.join(chx_dir, 'Предустановленные данные.bin'),
           _pfile(chx_cols, [
               _prow(0, [_puuid(ZERO_UUID), _pbool(1), _pstr('Характеристики'),
                         _pstr('     '), _pstr('')], [
                   _prow(1, [_puuid(KIND_OS_UUID), _pbool(0),
                             _pstr('ОсновныеСредства'), _pstr('00001'),
                             _pstr('Основные средства')]),
                   _prow(2, [_puuid(KIND_GOODS_UUID), _pbool(0),
                             _pstr('Номенклатура'), _pstr('00002'),
                             _pstr('Номенклатура')])])],
               pairs=[(0, '1'), (1, '2'), (2, '3'), (3, '4'), (4, '5')]))

    # справочник без кода: строковых колонок две (имя и наименование) — так устроены
    # Catalog/ДрайверыОборудования и Catalog/ВидыИспользованияРабочегоВремени в УНФ и БП
    nocode_cols = [_pcol(0, [['"#"', PREDEF_TYPE]]), _pcol(1, [['"B"']]),
                   _pcol(2, [['"#"', PREDEF_TYPE]]), _pcol(3, [['"S"']]),
                   _pcol(4, [['"N"']]), _pcol(5, [['"S"', '100', '1']])]
    nocode_dir = os.path.join(base, 'Catalog', 'ДрайверыОборудования')
    _json(os.path.join(nocode_dir, 'Catalog.json'), {
        'name': 'ДрайверыОборудования', 'comment': '', 'obj_version': '803',
        'header': [['1', ['0', '1']]],
    })
    _json(os.path.join(nocode_dir, 'Catalog.id.json'), {'uuid': NOCODE_UUID})
    _write(os.path.join(nocode_dir, 'Предустановленные данные.bin'),
           _pfile(nocode_cols, [
               _prow(0, [_puuid(ZERO_UUID), _pbool(1), _puuid(ZERO_UUID),
                         _pstr('Элементы'), _pnum(0), _pstr('')], [
                   _prow(1, [_puuid(DRIVER_UUID), _pbool(0), _puuid(ZERO_UUID),
                             _pstr('ДрайверСканера'), _pnum(0),
                             _pstr('Драйвер сканера')])])], tag='0'))


def make_dump(base):
    """Минимальное дерево в стиле вывода декодера (стадия 3)."""
    _json(os.path.join(base, 'Configuration.json'), {
        'uuid': ROOT_UUID, 'name': 'ТестКонф', 'comment': 'комментарий',
        'name2': {'ru': 'Тестовая конфигурация'},
        'compatibility_version': '80321',
        'obj_version': '803',
        'header': [['2', [ROOT_UUID], '7',
                    ['9cd510cd-abfc-11d4-9434-004095e12fc7',
                     ['1', _cfg_props()]]]],
    })
    _write(os.path.join(base, 'Configuration.con.bsl'), 'Перем Тест;')
    _write(os.path.join(base, 'help.html'), '<html></html>')
    _json(os.path.join(base, 'Configuration.4.json'), {'info': True})  # инфо-файл, не объект
    # таблица ссылочных uuid корневого потока .10: {REF_CAT: Справочник1};
    # REF_ORPHAN именует объект, которого в конфигурации нет
    _json(os.path.join(base, 'Configuration.10.json'),
          [['2', 'a', 'b', [['1', [REF_CAT, '"Справочник1"']],
                            ['1', [REF_ORPHAN, '"УдаленныйСправочник"']]], '0']])

    cat_dir = os.path.join(base, 'Catalog', 'Справочник1')
    _json(os.path.join(cat_dir, 'Catalog.json'), {
        'name': 'Справочник1', 'comment': '', 'obj_version': '803',
        'header': [
            # простая ссылка через таблицу .10
            ['2', _core('СсылкаАтрибут'), ['"Pattern"', ['"#"', REF_CAT]]],
            # имя есть в таблице .10, но объекта с таким именем в базе нет
            ['2', _core('ОсиротевшаяСсылка'), ['"Pattern"', ['"#"', REF_ORPHAN]]],
            # ссылка на определяемый тип (раскрывается состав)
            ['2', _core('ТипАтрибут'), ['"Pattern"', ['"#"', REF_DT]]],
            # составной тип: собственный uuid объекта вложен в дескриптор
            ['2', _core('СоставнойАтрибут'), '99999999-0000-0000-0000-000000000000',
             ['0', '2',
              ['"#"', '3ea29ea5-0000-0000-0000-000000000000',
               ['0', ['"#"', '157fa490-0000-0000-0000-000000000000', ['1', CAT_UUID]]]],
              ['"#"', '3ea29ea5-0000-0000-0000-000000000000',
               ['0', ['"#"', '157fa490-0000-0000-0000-000000000000',
                      ['1', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee']]]],
              ], '1'],
        ],
    })
    _json(os.path.join(cat_dir, 'Catalog.id.json'), {'uuid': CAT_UUID})
    _write(os.path.join(cat_dir, 'Catalog.obj.bsl'), 'Процедура Тест() КонецПроцедуры')

    doc_dir = os.path.join(base, 'Document', 'ЗаказПокупателя')
    _json(os.path.join(doc_dir, 'Document.json'), {
        'name': 'ЗаказПокупателя', 'comment': '', 'obj_version': '803',
        'header': [
            ['2', _core('НомерЗаказа'), ['"Pattern"', ['"S"', '11', '1']]],
            # запись секции, 1, блок полей табличной части
            ['1', ['11', 'aaaaaaaa-0000-0000-0000-000000000010',
                   ['0', ['3', ['1', '0', 'aaaaaaaa-0000-0000-0000-000000000011'],
                    '"Товары"', ['1', '"ru"', '"Товары"'], '""', '0', '0',
                    '00000000-0000-0000-0000-000000000000', '0']]],
             '0', ['0'], ['1', '"ru"', '"Товары"']],
            '1',
            [VT_FIELDS_KEY, '2',
             ['8', ['27', ['2', _core('ТоварыНоменклатура'),
                     ['"Pattern"', ['"#"', REF_CAT]]]], '0'],
             ['8', ['27', ['2', _core('ТоварыКоличество'),
                     ['"Pattern"', ['"N"', '15', '3', '1']]]], '0']],
            # вторая табличная часть с одноимёнными полями: имя уникально
            # в пределах секции, а не объекта
            ['1', ['11', 'aaaaaaaa-0000-0000-0000-000000000012',
                   ['0', ['3', ['1', '0', 'aaaaaaaa-0000-0000-0000-000000000013'],
                    '"Оплата"', ['1', '"ru"', '"Оплата"'], '""', '0', '0',
                    '00000000-0000-0000-0000-000000000000', '0']]],
             '0', ['0'], ['1', '"ru"', '"Оплата"']],
            '1',
            [VT_FIELDS_KEY, '3',
             ['8', ['27', ['2', _core('НомерЗаказа'),
                     ['"Pattern"', ['"S"', '11', '1']]]], '0'],
             ['8', ['27', ['2', _core('ТоварыКоличество'),
                     ['"Pattern"', ['"N"', '15', '3', '1']]]], '0'],
             ['8', ['27', ['2', _core('Контрагент'),
                     ['"Pattern"', ['"#"', REF_CAT]]]], '0']],
            # табличная часть без полей: блок полей объявляет ноль записей
            ['1', ['11', 'aaaaaaaa-0000-0000-0000-000000000014',
                   ['0', ['3', ['1', '0', 'aaaaaaaa-0000-0000-0000-000000000015'],
                    '"Доставка"', ['1', '"ru"', '"Доставка"'], '""', '0', '0',
                    '00000000-0000-0000-0000-000000000000', '0']]],
             '0', ['0'], ['1', '"ru"', '"Доставка"']],
            '1',
            [VT_FIELDS_KEY, '0'],
        ],
    })
    _json(os.path.join(doc_dir, 'Document.id.json'), {'uuid': DOC_UUID})

    enum_dir = os.path.join(base, 'Enum', 'ТестПеречисление')
    _json(os.path.join(enum_dir, 'Enum.json'), {
        'name': 'ТестПеречисление', 'comment': '', 'obj_version': '803',
        'header': [[[[0, _core('Значение1')], 0], [[0, _core('Значение2')], 0]]],
    })
    _json(os.path.join(enum_dir, 'Enum.id.json'), {'uuid': ENUM_UUID})

    common_dir = os.path.join(base, 'CommonAttribute', 'ОбщийТест')
    _json(os.path.join(common_dir, 'CommonAttribute.json'), {
        'name': 'ОбщийТест', 'comment': '', 'obj_version': '803',
        'header': [['3', '1', CAT_UUID,
                    ['2', '1', '00000000-0000-0000-0000-000000000000']]],
    })
    _json(os.path.join(common_dir, 'CommonAttribute.id.json'), {'uuid': COMMON_UUID})

    # предопределённые справочника: корневой узел «Элементы» (не элемент) и один
    # элемент под ним; колонки 0 и 2 — ссылочные uuid, 3/4/5 — имя, код, наименование
    cat_cols = [_pcol(0, [['"#"', PREDEF_TYPE]]), _pcol(1, [['"B"']]),
                _pcol(2, [['"#"', PREDEF_TYPE]]), _pcol(3, [['"S"']]),
                _pcol(4, [['"S"', '9', '1']]), _pcol(5, [['"S"', '150', '1']])]
    _write(os.path.join(cat_dir, 'Предустановленные данные.bin'),
           _pfile(cat_cols, [
               _prow(0, [_puuid(ZERO_UUID), _pbool(1), _puuid(ZERO_UUID),
                         _pstr('Элементы'), _pstr(''), _pstr('')], [
                   _prow(1, [_puuid(PRE_ELEM_UUID), _pbool(0), _puuid(ZERO_UUID),
                             _pstr('ПредЗначение'), _pstr('001'),
                             _pstr('Предопределенное значение')])])], tag='0'))

    dt_dir = os.path.join(base, 'DefinedType', 'ТипТест')
    # запись header[0][1] определяемого типа: собственный ссылочный uuid + состав
    _json(os.path.join(dt_dir, 'DefinedType.json'), {
        'name': 'ТипТест', 'comment': '', 'obj_version': '803',
        'header': [['0', ['0', REF_DT, '33333333-0000-0000-0000-000000000000',
                          _core('ТипТест'), ['"Pattern"', ['"#"', REF_CAT]]]]],
    })
    _json(os.path.join(dt_dir, 'DefinedType.id.json'), {'uuid': DT_UUID})

    # регистр сведений: коллекции измерений/ресурсов/реквизитов — узлы header[0],
    # начинающиеся каноническим uuid; флаги объекта: [18]=код периодичности
    # (4 = день), [19]='1' = подчинение регистратору
    ir_dir = os.path.join(base, 'InformationRegister', 'РегистрСведений1')
    _json(os.path.join(ir_dir, 'InformationRegister.json'), {
        'name': 'РегистрСведений1', 'comment': '', 'obj_version': '803',
        'header': _register_header(
            ['33'] + ['0'] * 17 + ['4', '1', '0'],
            [[IR_RESOURCES, '1', _attr('Ресурс1', ['"Pattern"', ['"N"', '15', '3', '1']])],
             [IR_DIMENSIONS, '1', _attr('Измерение1', ['"Pattern"', ['"#"', REF_CAT]])],
             [IR_FORMS, '0'],
             [IR_ATTRIBUTES, '1', _attr('Реквизит1', ['"Pattern"', ['"S"', '10', '1']])]]),
    })
    _json(os.path.join(ir_dir, 'InformationRegister.id.json'), {'uuid': IR_UUID})

    # регистр накопления: периодичности у него нет, режим записи всегда
    # «подчинение регистратору»
    ar_dir = os.path.join(base, 'AccumulationRegister', 'РегистрНакопления1')
    _json(os.path.join(ar_dir, 'AccumulationRegister.json'), {
        'name': 'РегистрНакопления1', 'comment': '', 'obj_version': '803',
        'header': _register_header(
            ['28'] + ['0'] * 20,
            [[AR_RESOURCES, '1',
              _attr('РесурсНакопления', ['"Pattern"', ['"N"', '15', '3', '1']])],
             [AR_DIMENSIONS, '1',
              _attr('ИзмерениеНакопления', ['"Pattern"', ['"#"', REF_CAT]])]]),
    })
    _json(os.path.join(ar_dir, 'AccumulationRegister.id.json'), {'uuid': AR_UUID})

    # общий модуль: контекст «Сервер» (флаги rec[2:], позиция 1), экспортный и
    # закрытый методы, запрос многострочным литералом и таблица значений
    cm_dir = os.path.join(base, 'CommonModule', 'ОбщийМодуль1')
    _json(os.path.join(cm_dir, 'CommonModule.json'), {
        'name': 'ОбщийМодуль1', 'comment': '', 'obj_version': '803',
        'header': [['1', ['81', '00000000-0000-0000-0000-000000000000',
                          '0', '1', '0', '0', '0', '0', '0', '0']]],
    })
    _json(os.path.join(cm_dir, 'CommonModule.id.json'), {'uuid': CM_UUID})
    _write(os.path.join(cm_dir, 'CommonModule.obj.bsl'), COMMON_MODULE_BSL)

    # подписка на событие: header[0][1] = ['1', CORE, ИСТОЧНИКИ, СОБЫТИЕ,
    # UUID_ОБРАБОТЧИКА, ИМЯ_МЕТОДА]. Источники — «источниковые» uuid объектов
    # (у определяемого типа это REF_DT из его header[0][1][1]), а обработчик —
    # обычный meta_object.uuid общего модуля; REF_UNKNOWN в базе не встречается
    es_dir = os.path.join(base, 'EventSubscription', 'ПодпискаТест')
    _json(os.path.join(es_dir, 'EventSubscription.json'), {
        'name': 'ПодпискаТест', 'comment': '', 'obj_version': '803',
        'header': [['1',
                    ['1', _core('ПодпискаТест'),
                     ['"Pattern"', ['"#"', REF_DT], ['"#"', REF_UNKNOWN]],
                     '"BeforeWrite_ПередЗаписью"', CM_UUID, '"Экспортная"'],
                    '0']],
    })
    _json(os.path.join(es_dir, 'EventSubscription.id.json'), {'uuid': ES_UUID})

    # план обмена: состав лежит в потоке .1 (ключ 'info') — плоский список пар
    # (uuid объекта, флаг), у реальных планов обёрнутый в один список; uuid
    # здесь обычные meta_object.uuid, в отличие от источников подписки
    ep_dir = os.path.join(base, 'ExchangePlan', 'ПланОбмена1')
    _json(os.path.join(ep_dir, 'ExchangePlan.json'), {
        'name': 'ПланОбмена1', 'comment': '', 'obj_version': '803',
        'header': [['1', ['0', _core('ПланОбмена1')]]],
        'info': [['2', '2', CAT_UUID, '0', DT_UUID, '1']],
    })
    _json(os.path.join(ep_dir, 'ExchangePlan.id.json'), {'uuid': EP_UUID})

    # пакет XDTO: целевое пространство имён в записи header[0][1][2], а состав
    # типов и свойств — в XDTOPackage.bin (открытый XML с BOM и пространством
    # имён по умолчанию, как в реальном дампе)
    xdto_dir = os.path.join(base, 'XDTOPackage', 'ПакетТест')
    _json(os.path.join(xdto_dir, 'XDTOPackage.json'), {
        'name': 'ПакетТест', 'comment': '', 'obj_version': '803',
        'header': [['1',
                    ['1',
                     ['0', ['0', '0', 'в отдельном файле'], '"ПакетТест"',
                      ['1', '"ru"', '"Тестовый пакет"'], '""'],
                     '"http://v8.1c.ru/test/package/1.0"'],
                    '0']],
    })
    _json(os.path.join(xdto_dir, 'XDTOPackage.id.json'), {'uuid': XDTO_UUID})
    _write(os.path.join(xdto_dir, 'XDTOPackage.bin'), XDTO_PACKAGE_XML)

    form_dir = os.path.join(cat_dir, 'Form', 'ФормаЭлемента')
    _json(os.path.join(form_dir, 'CatalogForm.json'), {
        'name': 'ФормаЭлемента', 'comment': '', 'obj_version': '803', 'header': {},
    })
    _json(os.path.join(form_dir, 'CatalogForm.id.json'), {'uuid': FORM_UUID})
    _write(os.path.join(form_dir, 'CatalogForm.mod.bsl'), '// модуль формы')


def test_write_db(tmp_path):
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    db_path = str(tmp_path / 'out.sqlite')

    stats = write_db(dump, db_path, source_file='test.cf')
    assert stats == {'objects': 13, 'modules': 4, 'methods': 3, 'files': 9, 'files_content': 3,
                     'skd': 0, 'attributes': 15, 'refs': 10, 'enum_values': 2,
                     'predefined': 2, 'subconto': 0, 'common_targets': 1, 'tabular': 3,
                     'xdto_types': 3, 'xdto_properties': 5}

    conn = sqlite3.connect(db_path)
    q = conn.execute

    src = q('SELECT file, root_type, root_name, root_uuid FROM source').fetchone()
    assert src == ('test.cf', 'Configuration', 'ТестКонф', ROOT_UUID)

    # type_ru — русские имена типов «как в конфигураторе»
    assert q("SELECT type_ru FROM meta_object WHERE type='Catalog'").fetchone()[0] == 'Справочник'
    assert q("SELECT type_ru FROM meta_object WHERE type='CatalogForm'").fetchone()[0] == 'Форма справочника'

    # типы реквизитов: простая ссылка, определяемый тип с составом, составной
    attrs = {r[0]: r[1] for r in q(
        'SELECT a.name, a.type_str FROM meta_attribute a JOIN meta_object o ON o.id=a.object_id'
        " WHERE o.path='Catalog/Справочник1' ORDER BY a.ord")}
    assert attrs['СсылкаАтрибут'] == 'Ссылка: Catalog/Справочник1'
    # цель известна по имени из таблицы .10, но объекта в базе нет: это не
    # абстрактный тип, а отсутствующая цель (так выглядит ссылка из расширения)
    assert attrs['ОсиротевшаяСсылка'] == \
        'Ссылка: УдаленныйСправочник (объект не найден в базе)'
    assert attrs['ТипАтрибут'] == \
        'ОпределяемыйТип: DefinedType/ТипТест (Ссылка: Catalog/Справочник1)'
    assert attrs['СоставнойАтрибут'] == 'Ссылка: Catalog/Справочник1 | Ссылка'

    # attribute_ref: uuid ссылки → объект (включая состав определяемого типа)
    refs = {(r[0], r[1], r[2]) for r in q(
        'SELECT a.name, r.uuid, o.path FROM attribute_ref r'
        ' JOIN meta_attribute a ON a.id=r.attribute_id'
        ' LEFT JOIN meta_object o ON o.id=r.object_id')}
    assert refs == {
        ('СсылкаАтрибут', REF_CAT, 'Catalog/Справочник1'),
        ('ОсиротевшаяСсылка', REF_ORPHAN, None),
        ('ТипАтрибут', REF_DT, 'DefinedType/ТипТест'),
        ('ТипАтрибут', REF_CAT, 'Catalog/Справочник1'),
        ('СоставнойАтрибут', CAT_UUID, 'Catalog/Справочник1'),
        ('СоставнойАтрибут', '3ea29ea5-0000-0000-0000-000000000000', None),
        ('ТоварыНоменклатура', REF_CAT, 'Catalog/Справочник1'),
        ('Контрагент', REF_CAT, 'Catalog/Справочник1'),
        ('Измерение1', REF_CAT, 'Catalog/Справочник1'),
        ('ИзмерениеНакопления', REF_CAT, 'Catalog/Справочник1'),
    }

    # методы: только процедуры/функции, без «преамбул»
    methods = {(r[0], r[1], r[2]) for r in q(
        'SELECT o.path, mt.kind, mt.name FROM method mt'
        ' JOIN module mo ON mo.id = mt.module_id'
        ' JOIN meta_object o ON o.id = mo.object_id')}
    assert methods == {('Catalog/Справочник1', 'процедура', 'Тест'),
                       ('CommonModule/ОбщийМодуль1', 'функция', 'Экспортная'),
                       ('CommonModule/ОбщийМодуль1', 'процедура', 'Закрытая')}

    objects = {r[1]: r for r in q(
        'SELECT id, path, type, name, uuid, parent_id FROM meta_object ORDER BY path')}
    assert set(objects) == {'', 'Catalog/Справочник1',
                            'Catalog/Справочник1/Form/ФормаЭлемента', 'DefinedType/ТипТест',
                            'Enum/ТестПеречисление', 'CommonAttribute/ОбщийТест',
                            'Document/ЗаказПокупателя',
                            'InformationRegister/РегистрСведений1',
                            'AccumulationRegister/РегистрНакопления1',
                            'CommonModule/ОбщийМодуль1', 'XDTOPackage/ПакетТест',
                            'EventSubscription/ПодпискаТест',
                            'ExchangePlan/ПланОбмена1'}

    # табличные части и их поля
    assert [r[0] for r in q(
        'SELECT t.name FROM meta_tabular t JOIN meta_object o ON o.id=t.object_id'
        " WHERE o.path='Document/ЗаказПокупателя' ORDER BY t.ord")] == \
        ['Товары', 'Оплата', 'Доставка']
    assert [r[0] for r in q(
        'SELECT a.name FROM meta_attribute a JOIN meta_object o ON o.id=a.object_id'
        " WHERE o.path='Document/ЗаказПокупателя' AND a.tabular='Товары' "
        'ORDER BY a.ord')] == ['ТоварыНоменклатура', 'ТоварыКоличество']

    # одноимённые поля разных ТЧ и реквизит объекта с именем поля ТЧ — все на
    # месте: дедуп имён действует внутри секции, а не по всему объекту
    assert [r[0] for r in q(
        'SELECT a.name FROM meta_attribute a JOIN meta_object o ON o.id=a.object_id'
        " WHERE o.path='Document/ЗаказПокупателя' AND a.tabular='Оплата' "
        'ORDER BY a.ord')] == ['НомерЗаказа', 'ТоварыКоличество', 'Контрагент']
    assert q('SELECT COUNT(*) FROM meta_attribute a JOIN meta_object o ON o.id=a.object_id'
             " WHERE o.path='Document/ЗаказПокупателя' AND a.name='НомерЗаказа'"
             ).fetchone()[0] == 2
    # секция, у которой конфигурация не объявила ни одного поля: запись
    # в meta_tabular есть, полей нет
    assert q('SELECT COUNT(*) FROM meta_attribute a JOIN meta_object o ON o.id=a.object_id'
             " WHERE o.path='Document/ЗаказПокупателя' AND a.tabular='Доставка'"
             ).fetchone()[0] == 0

    # объявленное число полей секции читается из header_json готовой базы:
    # по нему пустая секция конфигурации отличается от пробела извлечения
    doc_header = q('SELECT header_json FROM meta_object'
                   " WHERE path='Document/ЗаказПокупателя'").fetchone()[0]
    assert tabular_field_counts(doc_header) == {
        'Товары': 2, 'Оплата': 3, 'Доставка': 0}
    assert tabular_field_counts('не json') == {}
    assert tabular_field_counts(None) == {}

    # содержимое пакета XDTO: импорты, типы, свойства и вложенный анонимный тип
    xdto = " JOIN meta_object o ON o.id=t.object_id WHERE o.path='XDTOPackage/ПакетТест'"
    assert [r[0] for r in q(
        'SELECT i.namespace FROM xdto_import i JOIN meta_object o ON o.id=i.object_id'
        " WHERE o.path='XDTOPackage/ПакетТест' ORDER BY i.ord")] == \
        ['http://v8.1c.ru/test/base/1.0']
    assert [r for r in q(
        'SELECT t.name, t.kind, t.base, t.base_ns, t.facets, t.enum_values'
        ' FROM xdto_type t' + xdto + ' ORDER BY t.id')] == [
        ('Товар', 'objectType', 'd2p1:БазовыйТовар',
         'http://v8.1c.ru/test/base/1.0', None, None),
        (None, 'typeDef', None, None, 'type=ObjectType', None),
        ('ВидОперации', 'valueType', 'xs:string',
         'http://www.w3.org/2001/XMLSchema', 'maxLength=20',
         'Приход | Расход')]
    assert [r for r in q(
        'SELECT p.name, p.type, p.type_ns, p.lower_bound, p.upper_bound,'
        ' p.nillable, p.form, p.nested_type_id IS NOT NULL, p.extra'
        ' FROM xdto_property p JOIN xdto_type t ON t.id=p.type_id' + xdto
        + ' ORDER BY p.type_id, p.ord')] == [
        ('Наименование', 'xs:string', 'http://www.w3.org/2001/XMLSchema',
         1, None, 0, 'Attribute', 0, None),
        ('Количество', 'xs:decimal', 'http://www.w3.org/2001/XMLSchema',
         0, None, 1, None, 0, None),
        ('Позиции', None, None, 0, -1, 1, None, 1, None),
        ('Номер', 'xs:integer', 'http://www.w3.org/2001/XMLSchema',
         0, None, 1, 'Element', 0, None)]
    # свойство, объявленное в самом пакете (вне типов), и его localName
    assert [r for r in q(
        'SELECT p.name, p.type, p.extra FROM xdto_property p'
        ' JOIN meta_object o ON o.id=p.object_id'
        " WHERE p.type_id IS NULL AND o.path='XDTOPackage/ПакетТест'")] == \
        [('КорневойЭлемент', 'xs:string', 'localName=root')]

    # значения перечислений, предопределённые, привязки общих реквизитов
    assert [r[0] for r in q(
        'SELECT e.name FROM enum_value e JOIN meta_object o ON o.id=e.object_id'
        " WHERE o.path='Enum/ТестПеречисление' ORDER BY e.ord")] == ['Значение1', 'Значение2']
    assert q("SELECT name, code, display FROM predefined WHERE name='ПредЗначение'"
             ).fetchone() == ('ПредЗначение', '001', 'Предопределенное значение')
    # корневой узел «Элементы» элементом не является, но хранится: parent_ord NULL,
    # а элемент ссылается на него и несёт свой uuid
    assert [r for r in q(
        'SELECT ord, parent_ord, uuid, name FROM predefined ORDER BY ord')] == [
        (0, None, ZERO_UUID, 'Элементы'), (1, 0, PRE_ELEM_UUID, 'ПредЗначение')]
    assert [r[0] for r in q(
        'SELECT t.path FROM common_target ct JOIN meta_object t ON t.id=ct.target_id'
        ' JOIN meta_object c ON c.id=ct.common_id WHERE c.name=\'ОбщийТест\'')] == \
        ['Catalog/Справочник1']
    root, cat, form = objects[''], objects['Catalog/Справочник1'], \
        objects['Catalog/Справочник1/Form/ФормаЭлемента']
    assert root[2] == 'Configuration' and root[5] is None
    assert cat[2] == 'Catalog' and cat[4] == CAT_UUID and cat[5] == root[0]
    assert form[4] == FORM_UUID and form[2] == 'CatalogForm'

    # цепочка родителей: форма → справочник → корень
    chain = q('WITH RECURSIVE up(id) AS ('
              '  SELECT parent_id FROM meta_object WHERE id=?'
              '  UNION ALL'
              '  SELECT m.parent_id FROM meta_object m JOIN up ON m.id=up.id WHERE up.id IS NOT NULL'
              ') SELECT id FROM up', (form[0],)).fetchall()
    assert [c[0] for c in chain][:2] == [cat[0], root[0]]

    # модуль: паспорт + текст как есть без кода методов
    rows = q('SELECT m.code_name, m.body FROM module m '
             'JOIN meta_object o ON o.id=m.object_id '
             'WHERE o.path=?', ('Catalog/Справочник1',)).fetchall()
    assert rows and rows[0][0] == 'obj'
    assert rows[0][1] == 'Процедура Тест() КонецПроцедуры'
    # модуль без методов хранит весь текст
    conf = q('SELECT m.body FROM module m JOIN meta_object o ON o.id=m.object_id '
             "WHERE o.path='' AND m.code_name='con'").fetchone()
    assert conf[0] == 'Перем Тест;'

    files = {r[0]: (r[1], r[2]) for r in q('SELECT path, kind, data FROM file')}
    assert files['help.html'][0] == 'html'
    assert files['help.html'][1] == b'<html></html>'
    assert 'Configuration.4.json' in files
    # файлы модулей тоже отражаются в file (без data — тело в module)
    assert files['Catalog/Справочник1/Catalog.obj.bsl'] == ('bsl', None)
    assert sum(1 for kind, _ in files.values() if kind == 'bsl') == 4
    conn.close()


def test_write_db_no_root(tmp_path):
    dump = str(tmp_path / 'empty')
    os.makedirs(dump)
    db_path = str(tmp_path / 'out.sqlite')
    try:
        write_db(dump, db_path)
        raise AssertionError('ожидалась ошибка отсутствия корневого объекта')
    except ValueError:
        pass


def test_write_db_rejects_dir_as_db(tmp_path):
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    try:
        write_db(dump, str(tmp_path))
        raise AssertionError('ожидалась ошибка: путь БД — каталог')
    except ValueError:
        pass


def test_extract_skd_queries(tmp_path):
    from confdb.db.writer import _extract_skd_queries
    xml = ('<?xml version="1.0"?>'
           '<DataCompositionSchema xmlns="http://v8.1c.ru/2008/10/data-composition-schema">'
           '<dataSets><dataSet><query>ВЫБРАТЬ 1 КАК А</query></dataSet></dataSets>'
           '</DataCompositionSchema>')
    raw = b'\x00\x00\x00\x00\x01\x00\x00\x00' + xml.encode('utf-8')
    (tmp_path / 'Template.bin').write_bytes(raw)
    assert _extract_skd_queries(str(tmp_path / 'Template.bin')) == ['ВЫБРАТЬ 1 КАК А']
    assert _extract_skd_queries(str(tmp_path / 'нет.bin')) is None


def test_scan_tree_matches_os_walk(tmp_path):
    """Снимок обхода заменяет os.walk: порядок и размеры должны совпадать.

    От порядка каталогов и файлов зависят id строк в базе (module.id, file.id),
    поэтому _scan_tree обязан давать ровно то, что давал os.walk + sorted().
    """
    from confdb.db.writer import _rel_paths, _scan_tree
    dump = os.path.abspath(str(tmp_path / 'dump'))
    make_dump(dump)
    tree = _scan_tree(dump)
    expected = []
    for dirpath, dirnames, filenames in os.walk(dump):
        expected.append((dirpath, sorted(
            (fn, os.path.getsize(os.path.join(dirpath, fn))) for fn in filenames)))
    assert tree == expected

    rel = _rel_paths(tree, dump)
    assert rel[dump] == ''
    for dirpath, _files in tree:
        want = os.path.relpath(dirpath, dump).replace(os.sep, '/')
        assert rel[dirpath] == ('' if want == '.' else want)


def test_attribute_walk_collects_same_section_bags(tmp_path):
    """Блоки полей из обхода _extract_attributes == _section_bags(header).

    Запись собирает табличные части за тот же обход заголовка, что и реквизиты;
    отдельный обход (_section_bags) на УНФ стоил бы ещё 6 млн рекурсивных вызовов.
    """
    from confdb.db.writer import _extract_attributes, _section_bags
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    db_path = str(tmp_path / 'out.sqlite')
    write_db(dump, db_path)
    conn = sqlite3.connect(db_path)
    headers = [json.loads(r[0]) for r in conn.execute('SELECT header_json FROM meta_object')]
    conn.close()
    bags_total = 0
    for header in headers:
        bags = []
        _extract_attributes(header, None, bags)
        assert bags == _section_bags(header)
        bags_total += len(bags)
    assert bags_total >= 3  # в фикстуре три табличные части: иначе тест пустой


def test_write_db_without_fts_and_late_build(tmp_path):
    """build_fts=False не создаёт индекс; build_fts_index строит его позже шардами."""
    from confdb.db.writer import (FTS_TABLE, build_fts_index, fts_index_info,
                                  fts_shard_paths)
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    db_path = str(tmp_path / 'out.sqlite')
    write_db(dump, db_path, build_fts=False)

    conn = sqlite3.connect(db_path)
    assert not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',
                            (FTS_TABLE,)).fetchone()
    methods = conn.execute('SELECT COUNT(*) FROM method').fetchone()[0]
    conn.close()
    assert not fts_shard_paths(db_path)
    assert fts_index_info(db_path) is None

    assert build_fts_index(db_path, workers=3) == methods
    shards = fts_shard_paths(db_path)
    # пустых шардов не бывает: число файлов ограничено числом методов
    assert len(shards) == min(3, methods)
    info = fts_index_info(db_path)
    assert info['shards'] == len(shards) and info['methods'] == methods
    # индекс приставной: внутри базы таблицы FTS нет
    conn = sqlite3.connect(db_path)
    assert not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',
                            (FTS_TABLE,)).fetchone()
    conn.close()
    # rowid шарда = method.id: части не пересекаются, MATCH находит подстроку тела
    total = hits = 0
    ids = set()
    for path in shards:
        shard = sqlite3.connect(path)
        rows = shard.execute(f'SELECT rowid FROM {FTS_TABLE}').fetchall()
        total += len(rows)
        ids.update(r[0] for r in rows)
        hits += len(shard.execute(
            f'SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?',
            ('"Справочник.Справочник1"',)).fetchall())
        shard.close()
    assert total == methods == len(ids)
    assert hits

    # пересборка поверх существующего индекса не дублирует строки
    assert build_fts_index(db_path, workers=2) == methods
    shards = fts_shard_paths(db_path)
    assert len(shards) == min(2, methods)
    total = 0
    for path in shards:
        shard = sqlite3.connect(path)
        total += shard.execute(f'SELECT COUNT(*) FROM {FTS_TABLE}').fetchone()[0]
        shard.close()
    assert total == methods


def test_write_db_drops_stale_fts_shards(tmp_path):
    """Перезапись базы с --no-fts обязана снести старый приставной индекс.

    Иначе сервер нашёл бы шарды от ПРЕДЫДУЩЕЙ базы и молча выдавал чужие методы.
    """
    from confdb.db.writer import build_fts_index, fts_index_info, fts_shard_paths
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    db_path = str(tmp_path / 'out.sqlite')
    write_db(dump, db_path, build_fts=False)
    build_fts_index(db_path, workers=2)
    assert fts_shard_paths(db_path)

    write_db(dump, db_path, build_fts=False)
    assert not fts_shard_paths(db_path)
    assert fts_index_info(db_path) is None


def test_build_fts_index_rejects_foreign_db(tmp_path):
    from confdb.db.writer import build_fts_index
    other = str(tmp_path / 'other.sqlite')
    conn = sqlite3.connect(other)
    conn.execute('CREATE TABLE t (x)')
    conn.commit()
    conn.close()
    try:
        build_fts_index(other)
        raise AssertionError('ожидалась ошибка: это не база знаний confdb')
    except ValueError:
        pass


def test_predefined_chart_of_accounts_subconto(tmp_path):
    """Счета плана: иерархия, uuid и виды субконто, разрешённые в элементы ПВХ."""
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    make_chart_dump(dump)
    db_path = str(tmp_path / 'out.sqlite')
    stats = write_db(dump, db_path, source_file='test.cf')
    assert stats['subconto'] == 4

    conn = sqlite3.connect(db_path)
    q = conn.execute
    # иерархия: ord — обход в глубину, parent_ord — ord родителя, у корня NULL
    assert [r for r in q(
        'SELECT p.ord, p.parent_ord, p.uuid, p.name, p.code, p.display'
        ' FROM predefined p JOIN meta_object o ON o.id=p.object_id'
        ' WHERE o.path=? ORDER BY p.ord', (CHART_PATH,))] == [
        (0, None, ZERO_UUID, 'Счета', '', ''),
        (1, 0, ACC_OS_UUID, 'ОсновныеСредства', '01', 'Основные средства'),
        (2, 1, ACC_OS_ORG_UUID, 'ОСвОрганизации', '01.01',
         'Основные средства в организации'),
        (3, 0, ACC_GOODS_UUID, 'Товары', '41', 'Товары')]

    # вид субконто — предопределённый элемент ПВХ: kind_id указывает на него, а флаги
    # названы по-русски так, как их объявляет заголовок плана счетов
    assert [r for r in q(
        'SELECT a.code, s.ord, k.name, s.flags FROM predefined_subconto s'
        ' JOIN predefined a ON a.id=s.predefined_id'
        ' LEFT JOIN predefined k ON k.id=s.kind_id'
        ' WHERE a.object_id=(SELECT id FROM meta_object WHERE path=?)'
        ' ORDER BY a.ord, s.ord', (CHART_PATH,))] == [
        ('01', 0, 'ОсновныеСредства', 'Суммовой;Количественный'),
        ('01.01', 0, 'ОсновныеСредства', 'Суммовой;Валютный;Количественный'),
        ('41', 0, 'Номенклатура', 'Суммовой;Валютный;Количественный'),
        ('41', 1, 'ОсновныеСредства', None)]
    # uuid вида сохранён и тогда, когда разрешение не нужно
    assert q('SELECT s.uuid FROM predefined_subconto s'
             ' JOIN predefined a ON a.id=s.predefined_id'
             " WHERE a.code='01'").fetchone()[0] == KIND_OS_UUID

    # у ПВХ номера колонок начинаются с 1 — имя/код/наименование всё равно на месте
    assert [r for r in q(
        'SELECT p.ord, p.parent_ord, p.name, p.code, p.display FROM predefined p'
        ' JOIN meta_object o ON o.id=p.object_id WHERE o.path=? ORDER BY p.ord',
        (CHX_PATH,))] == [
        (0, None, 'Характеристики', '     ', ''),
        (1, 0, 'ОсновныеСредства', '00001', 'Основные средства'),
        (2, 0, 'Номенклатура', '00002', 'Номенклатура')]

    # у справочника без кода строковых колонок две: код пуст, наименование на месте
    assert [r for r in q(
        'SELECT p.ord, p.parent_ord, p.uuid, p.name, p.code, p.display FROM predefined p'
        ' JOIN meta_object o ON o.id=p.object_id WHERE o.path=? ORDER BY p.ord',
        (NOCODE_PATH,))] == [
        (0, None, ZERO_UUID, 'Элементы', '', ''),
        (1, 0, DRIVER_UUID, 'ДрайверСканера', '', 'Драйвер сканера')]
    conn.close()
