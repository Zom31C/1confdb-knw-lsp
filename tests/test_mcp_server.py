"""Тесты MCP-сервера: протокол и инструменты на синтетической базе."""
import json
import os
import sqlite3
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import confdb.mcp_server as mcp_server  # noqa: E402
from confdb.db.writer import write_db  # noqa: E402
from confdb.mcp_server import McpServer, resolve_db, start_http_server  # noqa: E402

from test_writer import make_chart_dump, make_dump  # noqa: E402


def _server(tmp_path_factory):
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / 't.sqlite')
    write_db(dump, db, source_file='t.cf')
    return McpServer(db)


def _call(server, tool, **args):
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': tool, 'arguments': args}})
    return resp['result']


def test_initialize_has_primer(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    assert 'meta_object' in resp['result']['instructions']
    assert resp['result']['protocolVersion']


def test_tools_list(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    names = {t['name'] for t in resp['result']['tools']}
    assert names == {'find_objects', 'object_card', 'object_tree', 'find_field',
                     'refs_of', 'module_outline', 'get_method',
                     'find_method_context', 'method_dependencies',
                     'method_result_schema', 'find_methods',
                     'skd_of', 'find_skd', 'xdto_of', 'find_xdto',
                     'check_query', 'sql',
                     'compare_object', 'extension_diff', 'configuration_info',
                     'db_list', 'db_open', 'db_use', 'db_close',
                     'group_create', 'group_add_db', 'group_remove_db',
                     'group_list', 'group_use', 'group_close'}
    # проверку синтаксиса модулей делает BSL Language Server (1confdb-knw-lsp),
    # в mainline её быть не должно
    assert 'check_bsl' not in names
    # каждый инструмент указывает функцию-обработчик и схему
    for tool in resp['result']['tools']:
        assert tool['description'] and tool['inputSchema']['type'] == 'object'


def test_find_objects_and_card(tmp_path_factory):
    server = _server(tmp_path_factory)
    text = _call(server, 'find_objects', mask='Справочник1')['content'][0]['text']
    assert 'Справочник.Справочник1' in text
    # вход в русском точечном формате
    card = _call(server, 'object_card', path='Справочник.Справочник1')['content'][0]['text']
    assert 'СсылкаАтрибут' in card and 'Товары' not in card
    # и в старом слэш-формате
    card_doc = _call(server, 'object_card',
                     path='Document/ЗаказПокупателя')['content'][0]['text']
    assert 'Документ.ЗаказПокупателя' in card_doc
    assert 'Табличная часть Товары' in card_doc and 'ТоварыНоменклатура' in card_doc


def test_tree_and_field(tmp_path_factory):
    server = _server(tmp_path_factory)
    tree = _call(server, 'object_tree', path='', depth=3)['content'][0]['text']
    assert 'Справочник.Справочник1' in tree
    assert 'Справочник.Справочник1.ФормаЭлемента' in tree
    fields = _call(server, 'find_field', name='Товары')['content'][0]['text']
    assert 'Документ.ЗаказПокупателя' in fields and '[табчасть Товары]' in fields


def test_methods(tmp_path_factory):
    server = _server(tmp_path_factory)
    found = _call(server, 'find_methods', mask='Тест')['content'][0]['text']
    assert 'процедура Тест()' in found
    method = _call(server, 'get_method', path='Catalog/Справочник1',
                   code_name='obj', name='Тест')['content'][0]['text']
    assert 'КонецПроцедуры' in method
    outline = _call(server, 'module_outline',
                    path='Catalog/Справочник1')['content'][0]['text']
    assert 'Процедура Тест()' in outline


def test_check_and_sql(tmp_path_factory):
    server = _server(tmp_path_factory)
    ok = _call(server, 'check_query',
               text='ВЫБРАТЬ Т.СсылкаАтрибут ИЗ Справочник.Справочник1 КАК Т')
    text = ok['content'][0]['text']
    # Ответ содержит заголовок базы и 'OK'
    assert '=== база' in text and 'OK' in text
    bad = _call(server, 'check_query',
                text='ВЫБРАТЬ Т.Х ИЗ Справочник.Нет КАК Т')['content'][0]['text']
    assert 'неизвестная таблица' in bad
    res = _call(server, 'sql', query='SELECT COUNT(*) AS n FROM meta_object')
    assert 'n' in res['content'][0]['text']
    # русский точечный путь в литерале sql конвертируется во внутренний формат
    res = _call(server, 'sql',
                query="SELECT path FROM meta_object WHERE path = 'Справочник.Справочник1'")
    assert 'Catalog/Справочник1' in res['content'][0]['text']
    denied = _call(server, 'sql', query='DELETE FROM meta_object')
    assert denied.get('isError')


def test_refs_and_skd_empty(tmp_path_factory):
    server = _server(tmp_path_factory)
    refs = _call(server, 'refs_of', path='Catalog/Справочник1')['content'][0]['text']
    assert 'Ссылается на' in refs
    skd = _call(server, 'skd_of', path='Catalog/Справочник1')['content'][0]['text']
    assert 'нет запросов' in skd


# -- регистры, формы и паспорт конфигурации ---------------------------------

def test_object_card_register_groups(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='РегистрСведений.РегистрСведений1')['content'][0]['text']
    assert 'Периодичность: день' in card
    assert 'Режим записи: подчинение регистратору' in card
    # измерения/ресурсы/реквизиты — отдельными группами, а не плоским списком
    assert 'Измерения: Измерение1: Ссылка: Справочник.Справочник1' in card
    assert 'Ресурсы: Ресурс1: Число' in card
    assert 'Реквизиты: Реквизит1: Строка(10)' in card
    assert card.index('Измерения:') < card.index('Ресурсы:') < card.index('Реквизиты:')

    acc = _call(server, 'object_card',
                path='РегистрНакопления.РегистрНакопления1')['content'][0]['text']
    # у регистра накопления периодичности не бывает — и строки о ней нет
    assert 'Периодичность' not in acc
    assert 'Режим записи: подчинение регистратору' in acc
    assert 'Измерения: ИзмерениеНакопления' in acc
    assert 'Ресурсы: РесурсНакопления: Число' in acc


def test_object_card_children_and_plain_attributes(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='Справочник.Справочник1')['content'][0]['text']
    assert 'Формы: ФормаЭлемента' in card
    # у справочника групп регистра нет: реквизиты одним списком, как раньше
    assert 'Измерения:' not in card
    assert card.count('Реквизиты:') == 1


def test_object_card_tabular_sections(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='Документ.ЗаказПокупателя')['content'][0]['text']
    assert ('Табличная часть Товары: ТоварыНоменклатура: '
            'Ссылка: Справочник.Справочник1') in card
    # одноимённое поле другой ТЧ — своя запись, а не отброшенный дубликат
    assert 'Табличная часть Оплата: НомерЗаказа: Строка(11)' in card
    # у секции без извлечённых полей состав называется отсутствующим,
    # а не печатается полем с именем None; конфигурация не объявила в ней
    # ни одного поля — это факт, а не пробел извлечения
    assert 'Табличная часть Доставка: полей не объявлено' in card
    assert 'None' not in card


def test_object_card_predefined_accounts(tmp_path_factory):
    # предопределённые счета плана печатаются деревом, с видами субконто и их
    # флагами; корневой узел «Счета» в отчёт не попадает, но входит в дерево
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    make_chart_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / 't.sqlite')
    write_db(dump, db, source_file='t.cf')
    server = McpServer(db)
    card = _call(server, 'object_card',
                 path='ПланСчетов.ПланСчетов1')['content'][0]['text']
    assert 'Предопределённые счета (3):' in card
    assert '\n  01 ОсновныеСредства — Основные средства\n' in card
    assert '\n    01.01 ОСвОрганизации — Основные средства в организации\n' in card
    assert ('      субконто: Основные средства [Суммовой;Валютный;Количественный]'
            in card)
    assert ('  41 Товары\n'
            '    субконто: Номенклатура [Суммовой;Валютный;Количественный];'
            ' Основные средства') in card

    # у справочника тот же блок называется «элементы» и идёт деревом
    card_cat = _call(server, 'object_card',
                     path='Справочник.Справочник1')['content'][0]['text']
    assert 'Предопределённые элементы (1):' in card_cat
    assert '  001 ПредЗначение — Предопределенное значение' in card_cat


def test_object_card_sees_unextracted_tabular_fields(tmp_path_factory):
    # секция объявляет поля, но в базе их нет: паспорт называет это пробелом
    # извлечения и не выдаёт за пустую секцию конфигурации
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / 't.sqlite')
    write_db(dump, db, source_file='t.cf')
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM meta_attribute WHERE tabular='Оплата'")
    conn.commit()
    conn.close()
    card = _call(McpServer(db), 'object_card',
                 path='Документ.ЗаказПокупателя')['content'][0]['text']
    assert 'Табличная часть Оплата: полей не извлечено' in card
    assert 'Табличная часть Доставка: полей не объявлено' in card


def test_object_card_header_names_type_in_russian(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='Справочник.Справочник1')['content'][0]['text']
    # тип в паспорте — как в конфигураторе, без английского stem
    assert ('Справочник.Справочник1 — Справочник, имя Справочник1'
            in card.splitlines())
    assert '(Catalog)' not in card
    # в списке find_objects stem остаётся: это значение фильтра type= и
    # колонки meta_object.type для sql
    listed = _call(server, 'find_objects',
                   mask='Справочник1')['content'][0]['text']
    assert 'Справочник.Справочник1 — Справочник (Catalog)' in listed


def test_object_card_event_subscription(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='Подписка на событие.ПодпискаТест')['content'][0]['text']
    # имя события — как в конфигураторе, без внутреннего английского имени
    assert 'Событие: ПередЗаписью' in card
    assert 'BeforeWrite' not in card
    assert 'Обработчик: Общий модуль.ОбщийМодуль1.Экспортная' in card
    # один источник распознан, второго в базе нет — и это сказано, а не потеряно
    assert ('Источники (2): Определяемый тип.ТипТест; '
            '1 из 2 не распознано') in card


def test_object_card_shows_subscriptions_of_a_source(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='Определяемый тип.ТипТест')['content'][0]['text']
    assert ('Подписки на события: ПередЗаписью → '
            'Общий модуль.ОбщийМодуль1.Экспортная') in card
    # у объекта, на который ничего не подписано, такой строки нет
    other = _call(server, 'object_card',
                  path='Документ.ЗаказПокупателя')['content'][0]['text']
    assert 'Подписки на события' not in other


def test_get_method_names_event_subscriptions(tmp_path_factory):
    server = _server(tmp_path_factory)
    text = _call(server, 'get_method', path='Общий модуль.ОбщийМодуль1',
                 code_name='obj', name='Экспортная')['content'][0]['text']
    # обработчик подписки вызывает платформа: вызова в коде нет, поэтому
    # без этой строки метод выглядит неиспользуемым
    assert ('вызывается подписками на события: ПередЗаписью — '
            'Подписка на событие.ПодпискаТест (источников: 2)') in text
    other = _call(server, 'get_method', path='Общий модуль.ОбщийМодуль1',
                  code_name='obj', name='Закрытая')['content'][0]['text']
    assert 'вызывается подписками на события' not in other


def test_object_card_exchange_plan_content(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='План обмена.ПланОбмена1')['content'][0]['text']
    # состав плана — объекты, которые он синхронизирует; uuid в info обычные
    # meta_object.uuid, поэтому имена определяются, а не угадываются
    assert ('Состав плана обмена (2): Справочник.Справочник1, '
            'Определяемый тип.ТипТест') in card
    # у объекта, который не план обмена, такой строки нет
    other = _call(server, 'object_card',
                  path='Справочник.Справочник1')['content'][0]['text']
    assert 'Состав плана обмена' not in other


def test_object_card_names_why_a_type_is_unresolved(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='Справочник.Справочник1')['content'][0]['text']
    # обобщённый тип платформы: цели у него нет, это не пробел извлечения
    assert 'Ссылка (тип не конкретизирован)' in card
    assert 'цель не определена' not in card
    # цель известна по имени из таблицы .10, но объекта в этой базе нет
    assert ('ОсиротевшаяСсылка: Ссылка: УдаленныйСправочник '
            '(объект не найден в базе)') in card


def test_object_card_xdto_namespace(tmp_path_factory):
    server = _server(tmp_path_factory)
    card = _call(server, 'object_card',
                 path='Пакет XDTO.ПакетТест')['content'][0]['text']
    assert 'Пространство имён: http://v8.1c.ru/test/package/1.0' in card
    # состав пакета в паспорте — счётчиком: типов бывает сотни
    assert 'Типов XDTO: 3, свойств: 5 (см. xdto_of)' in card


def test_xdto_of(tmp_path_factory):
    server = _server(tmp_path_factory)
    text = _call(server, 'xdto_of',
                 path='Пакет XDTO.ПакетТест')['content'][0]['text']
    assert 'Импортирует пространства имён: http://v8.1c.ru/test/base/1.0' in text
    assert ('Тип Товар (objectType), базовый d2p1:БазовыйТовар '
            '[http://v8.1c.ru/test/base/1.0]') in text
    assert '  Наименование: xs:string, обязательное, атрибут' in text
    # свойство-список с анонимным вложенным типом: его состав — большим отступом
    assert '  Позиции: вложенный тип, список' in text
    assert '    Тип (без имени) (typeDef)' in text
    assert '      Номер: xs:integer, элемент' in text
    # простой тип: ограничения и допустимые значения перечисления
    assert ('Тип ВидОперации (valueType), базовый xs:string '
            '[http://www.w3.org/2001/XMLSchema]') in text
    assert '  ограничения: maxLength=20' in text
    assert '  значения: Приход | Расход' in text
    # свойство, объявленное в пакете вне типов
    assert 'Свойства пакета (вне типов):' in text
    assert '    КорневойЭлемент: xs:string [localName=root]' in text

    one = _call(server, 'xdto_of', path='Пакет XDTO.ПакетТест',
                type='Товар')['content'][0]['text']
    assert 'Тип Товар (objectType)' in one
    assert 'Количество' in one
    missing = _call(server, 'xdto_of', path='Пакет XDTO.ПакетТест',
                    type='НетТакого')['content'][0]['text']
    assert 'тип не найден в пакете: НетТакого' in missing


def test_find_xdto(tmp_path_factory):
    server = _server(tmp_path_factory)
    hits = _call(server, 'find_xdto', mask='Количеств')['content'][0]['text']
    assert 'Пакет XDTO.ПакетТест — Товар.Количество: xs:decimal' in hits
    by_type = _call(server, 'find_xdto', mask='Товар')['content'][0]['text']
    assert 'Пакет XDTO.ПакетТест — тип Товар (базовый d2p1:БазовыйТовар)' in by_type
    assert 'в пакетах XDTO ничего не найдено по «НетТакогоСвойства»' in \
        _call(server, 'find_xdto', mask='НетТакогоСвойства')['content'][0]['text']


def test_characteristic_type_stem_is_translated():
    # класс декодера — ChartOfCharacteristicType, в единственном числе: при
    # множественном ключе в TYPE_RU тип печатался по-английски (и type_ru в
    # базе оказывался равен stem), а русское имя не находило объект
    assert mcp_server.ru_path('ChartOfCharacteristicType/Свойства') \
        == 'План видов характеристик.Свойства'
    assert 'ChartOfCharacteristicType' in \
        mcp_server._RU2TYPES_LOW['планвидовхарактеристик']


def test_path_type_is_case_insensitive(tmp_path_factory):
    server = _server(tmp_path_factory)
    # форма языка запросов и форма конфигуратора ведут к одному объекту
    query_form = _call(server, 'object_card',
                       path='РегистрНакопления.РегистрНакопления1')
    card_form = _call(server, 'object_card',
                      path='Регистр накопления.РегистрНакопления1')
    assert query_form['content'][0]['text'] == card_form['content'][0]['text']
    assert 'Измерения:' in query_form['content'][0]['text']


def test_configuration_info(tmp_path_factory):
    server = _server(tmp_path_factory)
    info = _call(server, 'configuration_info')['content'][0]['text']
    assert 'Конфигурация: ТестКонф (Тестовая конфигурация)' in info
    assert 'Версия конфигурации: 1.2.3.4' in info
    assert 'Режим совместимости: 8.3.21' in info
    assert 'Версия платформы: в файле конфигурации не хранится' in info
    assert 'Источник выгрузки: t.cf' in info
    assert 'запросов СКД' in info
    # db_list кратко повторяет тип файла, версию и режим совместимости
    lst = _call(server, 'db_list')['content'][0]['text']
    assert '1.2.3.4' in lst and '8.3.21' in lst
    assert 'Конфигурация (.cf)' in lst


# заголовок корня внешней обработки: ни версии, ни режима совместимости
PROC_HEADER = json.dumps({'name': '"ТестОбработка"', 'obj_version': '803'},
                         ensure_ascii=False)


def _server_as(tmp_path_factory, root_type, root_type_ru, source_file,
               header_json=None):
    """Сервер над базой, чей корень переписан под другой тип файла."""
    dump = str(tmp_path_factory.mktemp('dump-kind'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db-kind') / 'k.sqlite')
    write_db(dump, db, source_file=source_file)
    conn = sqlite3.connect(db)
    try:
        conn.execute('UPDATE meta_object SET type=?, type_ru=? '
                     'WHERE parent_id IS NULL', (root_type, root_type_ru))
        conn.execute('UPDATE source SET file=?', (source_file,))
        if header_json is not None:
            conn.execute('UPDATE meta_object SET header_json=? '
                         'WHERE parent_id IS NULL', (header_json,))
        conn.commit()
    finally:
        conn.close()
    return McpServer(db)


def test_configuration_info_extension(tmp_path_factory):
    server = _server_as(tmp_path_factory, 'ConfigurationExtension',
                        'Расширение конфигурации', 'Расширение1.cfe')
    info = _call(server, 'configuration_info')['content'][0]['text']
    assert 'Расширение конфигурации: ТестКонф' in info
    assert 'Версия расширения: 1.2.3.4' in info
    # режим совместимости расширение наследует, значение файла — справочное
    assert ('Режим совместимости: наследуется от основной конфигурации; '
            'в файле расширения указан 8.3.21') in info
    lst = _call(server, 'db_list')['content'][0]['text']
    assert 'Расширение конфигурации (.cfe)' in lst
    assert 'режим совместимости 8.3.21 (наследуется)' in lst


def test_configuration_info_external_report(tmp_path_factory):
    # .erf декодирует класс ExternalDataProcessor: тип файла виден только
    # по расширению исходника
    server = _server_as(tmp_path_factory, 'ExternalDataProcessor',
                        'Внешняя обработка', 'D:\\Отчёты\\МойОтчёт.erf',
                        header_json=PROC_HEADER)
    info = _call(server, 'configuration_info')['content'][0]['text']
    assert 'Внешний отчёт: ТестОбработка' in info
    assert 'Тип корня: Внешняя обработка (ExternalDataProcessor)' in info
    assert 'Версия отчёта: в файле не указана' in info
    assert 'Режим совместимости: не задаётся' in info
    lst = _call(server, 'db_list')['content'][0]['text']
    assert 'Внешний отчёт (.erf)' in lst
    assert 'режим совместимости' not in lst


# -- инструменты работы с кодом ---------------------------------------------

CM = 'CommonModule/ОбщийМодуль1'


def test_find_method_context(tmp_path_factory):
    server = _server(tmp_path_factory)
    out = _call(server, 'find_method_context', path=CM, code_name='obj',
                name='Экспортная', match='Выгрузить', before=1,
                after=1)['content'][0]['text']
    assert '>>' in out and 'Маркеры вставки' in out
    assert 'следующий оператор:  КонецФункции' in out
    # номера строк — модуля, а не тела метода
    assert 'строки модуля' in out
    miss = _call(server, 'find_method_context', path=CM, code_name='obj',
                 name='Экспортная', match='ЧегоНетВКоде')['content'][0]['text']
    assert 'не найдено' in miss and 'get_method' in miss
    nomatch = _call(server, 'find_method_context', path=CM, code_name='obj',
                    name='Закрытая')['content'][0]['text']
    assert 'match не задан' in nomatch


def test_method_dependencies(tmp_path_factory):
    server = _server(tmp_path_factory)
    out = _call(server, 'method_dependencies', path=CM, code_name='obj',
                name='Экспортная')['content'][0]['text']
    assert 'Параметры: Парам' in out
    assert 'Справочник.Справочник1' in out      # таблица запроса из кода
    assert 'Справочник1.СсылкаАтрибут' in out   # поле запроса
    assert 'ошибок 0' in out


def test_method_result_schema(tmp_path_factory):
    server = _server(tmp_path_factory)
    table = _call(server, 'method_result_schema', path=CM, code_name='obj',
                  name='Закрытая')['content'][0]['text']
    assert 'Сотрудник' in table and 'Колонка' in table
    assert 'таблиц значений: 1' in table
    assert 'эвристика' in table
    query = _call(server, 'method_result_schema', path=CM, code_name='obj',
                  name='Экспортная')['content'][0]['text']
    assert 'Колонка запроса' in query and 'Ссылка' in query
    assert 'Выгрузить' in query
    empty = _call(server, 'method_result_schema', path='Catalog/Справочник1',
                  code_name='obj', name='Тест')['content'][0]['text']
    assert 'не найдено' in empty


def test_analyze_resolves_modules_and_context(tmp_path_factory):
    """Разрешение вызовов: общие модули, метаданные, контекст клиент/сервер.

    Синтаксис модуля не проверяется — это делает BSL Language Server в варианте
    1confdb-knw-lsp, поэтому здесь только ссылки на метаданные и общие модули.
    """
    from confdb.bsl_analyzer import analyze, caller_context
    server = _server(tmp_path_factory)
    code = ('Процедура П(Данные)\n'
            '    ОбщийМодуль1.Экспортная(Данные);\n'
            '    ОбщийМодуль1.Закрытая();\n'
            '    ОбщийМодуль1.НетТакого();\n'
            '    Справочники.Справочник1.НайтиПоКоду("1");\n'
            '    Справочники.НетТакого.Создать();\n'
            '    Таблица = Новый ТаблицаЗначений;\n'
            '    Таблица.Колонки.Добавить("Сотрудник");\n'
            'КонецПроцедуры\n')
    report = analyze(code, server.bsl_ctx(), params=[],
                     caller_context=caller_context('&НаКлиенте', None))
    states = {(mod, name): state for mod, name, _line, state
              in report['modules']}
    assert states[('ОбщийМодуль1', 'Экспортная')] == 'ok'
    assert 'не Экспорт' in states[('ОбщийМодуль1', 'Закрытая')]
    assert 'не найден' in states[('ОбщийМодуль1', 'НетТакого')]
    meta = {(mgr, name): path for mgr, name, _line, path in report['metadata']}
    assert meta[('Справочники', 'Справочник1')] == 'Catalog/Справочник1'
    assert meta[('Справочники', 'НетТакого')] is None
    # цепочка Таблица.Колонки.Добавить() и параметр Данные — не «не разрешено»
    assert report['unknown'] == []
    # общий модуль с контекстом «Сервер» вызывается из клиентского кода
    assert report['context_warnings']
    assert 'серверный общий модуль' in report['context_warnings'][0]


def test_analyze_detects_dynamic_calls(tmp_path_factory):
    """Вычислить/Выполнить со строковыми литералами — динамические вызовы.

    Анализатор должен найти строковые литералы, переданные в Вычислить() и
    Выполнить(), и попытаться разрешить их как имена методов/модулей/объектов.
    """
    from confdb.bsl_analyzer import analyze
    server = _server(tmp_path_factory)
    code = ('Процедура Тест()\n'
            '    Вычислить("ОбщийМодуль1.Экспортная");\n'
            '    Вычислить("ОбщийМодуль1.Закрытая");\n'
            '    Вычислить("ОбщийМодуль1.НетТакого");\n'
            '    Вычислить("Справочники.Справочник1");\n'
            '    Вычислить("Справочники.НетТакого");\n'
            '    Вычислить("Неизвестный.Метод");\n'
            '    Вычислить("ОбщийМодуль1");\n'
            '    Выполнить("1 + 1");\n'
            'КонецПроцедуры\n')
    report = analyze(code, server.bsl_ctx())
    dynamic = report['dynamic_calls']
    # Литерал "1 + 1" не похож на идентификатор — не попадает
    by_literal = {lit: (func, res) for _line, func, lit, res in dynamic}
    assert 'ОбщийМодуль1.Экспортная' in by_literal
    assert 'ok' in by_literal['ОбщийМодуль1.Экспортная'][1]
    assert 'ОбщийМодуль1.Закрытая' in by_literal
    assert 'не Экспорт' in by_literal['ОбщийМодуль1.Закрытая'][1]
    assert 'ОбщийМодуль1.НетТакого' in by_literal
    assert 'не найден' in by_literal['ОбщийМодуль1.НетТакого'][1]
    assert 'Справочники.Справочник1' in by_literal
    assert 'объект метаданных' in by_literal['Справочники.Справочник1'][1]
    assert 'Справочники.НетТакого' in by_literal
    assert 'не найден' in by_literal['Справочники.НетТакого'][1]
    assert 'Неизвестный.Метод' in by_literal
    assert 'не разрешено' in by_literal['Неизвестный.Метод'][1]
    assert 'ОбщийМодуль1' in by_literal
    assert 'общий модуль' in by_literal['ОбщийМодуль1'][1]
    assert '1 + 1' not in by_literal  # не идентификатор


def test_find_methods_searches_bodies(tmp_path_factory):
    """text ищет подстроку внутри тел методов — того, чего mask дать не может."""
    server = _server(tmp_path_factory)
    out = _call(server, 'find_methods',
                text='Справочник.Справочник1')['content'][0]['text']
    assert 'ОбщийМодуль1' in out          # запрос с этой таблицей — в его теле
    assert 'строка ' in out               # с номером строки модуля
    # по имени/сигнатуре/описанию такой подстроки нет: без text модель
    # уходила бы в полнотекстовый sql
    by_name = _call(server, 'find_methods',
                    mask='Справочник.Справочник1')['content'][0]['text']
    assert 'ничего не найдено' in by_name


def test_find_methods_body_search_ignores_case(tmp_path_factory):
    """LIKE в SQLite сворачивает регистр только для ASCII — кириллицу сворачиваем сами."""
    server = _server(tmp_path_factory)
    for needle in ('Справочник.Справочник1', 'справочник.справочник1'):
        out = _call(server, 'find_methods',
                    text=needle)['content'][0]['text']
        assert 'ОбщийМодуль1' in out, needle


def _server_no_fts(tmp_path_factory):
    """Сервер над базой, собранной без FTS-индекса (extract --no-fts)."""
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / 't.sqlite')
    write_db(dump, db, source_file='t.cf', build_fts=False)
    return McpServer(db), db


def test_find_methods_without_fts_falls_back_to_body_has(tmp_path_factory):
    """Без индекса поиск по телам работает (медленно), а не молча возвращает ноль."""
    server, _db = _server_no_fts(tmp_path_factory)
    assert server.dbs[server.active]['fts'] is None
    out = _call(server, 'find_methods',
                text='Справочник.Справочник1')['content'][0]['text']
    assert 'ОбщийМодуль1' in out


def test_find_methods_same_result_after_late_fts_build(tmp_path_factory):
    """Индекс, собранный позже (confdb fts), даёт ту же выдачу, что body_has."""
    from confdb.db.writer import build_fts_index
    server, db = _server_no_fts(tmp_path_factory)
    plain = _call(server, 'find_methods',
                  text='Справочник.Справочник1')['content'][0]['text']
    server.close_db()

    build_fts_index(db, workers=2)
    server = McpServer(db)
    # индекс приставной: таблицы внутри базы нет, сервер держит список шардов
    assert server.dbs[server.active]['fts'] is None
    assert server.dbs[server.active]['fts_shards']
    indexed = _call(server, 'find_methods',
                    text='Справочник.Справочник1')['content'][0]['text']
    # порядок строк без ORDER BY зависит от плана запроса — сравниваем состав
    assert sorted(indexed.splitlines()) == sorted(plain.splitlines())
    server.close_db()


def test_incomplete_fts_shards_are_not_used(tmp_path_factory):
    """Неполный приставной индекс — поиск через body_has, а не частичная выдача."""
    import os

    from confdb.db.writer import build_fts_index, fts_dir, fts_shard_paths
    server, db = _server_no_fts(tmp_path_factory)
    server.close_db()
    build_fts_index(db, workers=2)

    # сборку прервали: манифест пишется последним
    os.remove(os.path.join(fts_dir(db), 'index.json'))
    server = McpServer(db)
    assert server.dbs[server.active]['fts_shards'] is None
    out = _call(server, 'find_methods',
                text='Справочник.Справочник1')['content'][0]['text']
    assert 'ОбщийМодуль1' in out
    server.close_db()

    # шард потеряли: число файлов не сходится с манифестом
    build_fts_index(db, workers=2)
    shards = fts_shard_paths(db)
    assert shards
    os.remove(shards[-1])
    server = McpServer(db)
    assert server.dbs[server.active]['fts_shards'] is None
    server.close_db()


def test_empty_fts_index_is_not_used(tmp_path_factory):
    """Пустой индекс (сборку прервали) — не повод молча вернуть ноль."""
    from confdb.db.writer import FTS_TABLE
    server, db = _server_no_fts(tmp_path_factory)
    server.close_db()
    conn = sqlite3.connect(db)
    conn.execute(f'CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5(body, tokenize=trigram)')
    conn.commit()
    conn.close()

    server = McpServer(db)
    assert server.dbs[server.active]['fts'] is None
    out = _call(server, 'find_methods',
                text='Справочник.Справочник1')['content'][0]['text']
    assert 'ОбщийМодуль1' in out


def test_find_methods_path_is_a_real_filter(tmp_path_factory):
    """path ограничивает поиск объектом.

    AND связывается сильнее OR, поэтому без скобок вокруг OR-группы фильтр
    применялся только к последней ветке и поиск «в одном объекте» молча
    возвращал методы всей конфигурации.
    """
    server = _server(tmp_path_factory)
    out = _call(server, 'find_methods', mask='Экспортная',
                path='Catalog/Справочник1')['content'][0]['text']
    assert 'ничего не найдено' in out
    own = _call(server, 'find_methods', mask='Экспортная',
                path=CM)['content'][0]['text']
    assert 'Экспортная' in own


def test_sql_body_has_folds_case_itself(tmp_path_factory):
    """body_has видна модели через sql — сворачивать регистр она обязана сама.

    Иначе вызов с иглой в верхнем или смешанном регистре молча вернул бы ноль,
    и модель сделала бы вывод, что обращений к объекту в коде нет.
    Игла намеренно не путь метаданных: литералы вида 'Справочник.Х'
    инструмент sql переписывает в 'Catalog/Х' ещё до выполнения (_sql_rewrite).
    """
    server = _server(tmp_path_factory)
    needle = 'КонецПроцедуры'
    counts = []
    for variant in (needle, needle.lower(), needle.upper()):
        out = _call(server, 'sql',
                    query='SELECT COUNT(*) AS n FROM method '
                          "WHERE body_has(body, '%s')" % variant
                    )['content'][0]['text']
        counts.append(out.rstrip().split()[-1])
    assert counts[0] != '0', counts
    assert counts[0] == counts[1] == counts[2], counts


def _ext_with_foreign_calls(tmp_path_factory):
    """«Расширение»: нет ни общего модуля, ни справочника основной базы,
    но есть метод, который к ним обращается (обычный случай для EPF/расширения)."""
    main_db, ext_db = _two_dbs(tmp_path_factory)
    conn = sqlite3.connect(ext_db)
    for path in (CM, 'Catalog/Справочник1'):
        conn.execute('DELETE FROM meta_object WHERE path=?', (path,))
    oid = conn.execute('SELECT id FROM meta_object '
                       "WHERE path='DataProcessor/ДопОбработка'").fetchone()[0]
    conn.execute("INSERT INTO module (object_id, code_name, context, body) "
                 "VALUES (?, 'obj', '', '')", (oid,))
    mid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    body = ('Процедура Обработать()\n'
            '    ОбщийМодуль1.Экспортная();\n'
            '    Справочники.Справочник1.НайтиПоКоду("1");\n'
            '    Справочники.НетТакого.Создать();\n'
            '    Запрос.Текст = "ВЫБРАТЬ 1 ИЗ Справочник.Справочник1";\n'
            'КонецПроцедуры\n')
    conn.execute('INSERT INTO method (module_id, ord, kind, name, signature, '
                 'is_export, directives, description, line_start, body) '
                 "VALUES (?, 1, 'процедура', 'Обработать', '', 0, '', '', 1, ?)",
                 (mid, body))
    conn.commit()
    conn.close()
    return main_db, ext_db


def test_method_dependencies_resolves_in_other_open_base(tmp_path_factory):
    """Зависимости EPF/расширения ищутся и в открытой рядом основной базе."""
    main_db, ext_db = _ext_with_foreign_calls(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    out = _call(server, 'method_dependencies', db='расширение',
                path='DataProcessor/ДопОбработка', code_name='obj',
                name='Обработать')['content'][0]['text']
    # объект основной конфигурации найден в соседней базе, а не «не найден»
    assert 'есть в базе «основная»' in out
    assert 'Справочник.Справочник1' in out
    # общий модуль — тоже оттуда, с проверкой Экспорт
    assert 'Зависимости из другой открытой базы' in out
    assert 'общий модуль в базе «основная»: Экспортная — Экспорт' in out
    # чего нет нигде — честно сказано, что проверены и соседние базы
    assert 'НЕ НАЙДЕНО в этой базе и в открытых рядом' in out
    # запрос проверен по метаданным своей базы — подсказываем перепроверить
    assert 'по метаданным ЭТОЙ базы' in out
    assert 'check_query(text, db=' in out


def test_method_dependencies_single_base_has_no_foreign_sections(tmp_path_factory):
    """Когда база одна, никаких «соседних баз» в ответе не появляется."""
    server = _server(tmp_path_factory)
    out = _call(server, 'method_dependencies', path=CM, code_name='obj',
                name='Экспортная')['content'][0]['text']
    assert 'Зависимости из другой открытой базы' not in out
    assert 'и в открытых рядом' not in out


def test_error_categories(tmp_path_factory):
    server = _server(tmp_path_factory)
    denied = _call(server, 'sql', query='DELETE FROM meta_object')
    assert denied.get('isError')
    assert 'ошибка [BAD_REQUEST]' in denied['content'][0]['text']
    # неизвестный аргумент — BAD_ARGS, а не INTERNAL
    bad_args = _call(server, 'find_objects', mask='Х', несуществующийПараметр=1)
    assert bad_args.get('isError')
    assert 'ошибка [BAD_ARGS]' in bad_args['content'][0]['text']
    # неизвестная база — BAD_REQUEST
    bad_db = _call(server, 'find_objects', mask='Х', db='неттакой')
    assert bad_db.get('isError')
    assert 'ошибка [BAD_REQUEST]' in bad_db['content'][0]['text']


def test_jsonrpc_roundtrip(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle(json.loads('{"jsonrpc":"2.0","id":9,'
                                    '"method":"notifications/initialized"}'))
    assert resp is None
    resp = server.handle({'jsonrpc': '2.0', 'id': 10, 'method': 'nope'})
    assert resp['error']['code'] == -32601


def _post(port, msg, path='/mcp'):
    req = urllib.request.Request(
        f'http://127.0.0.1:{port}{path}',
        data=json.dumps(msg).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_http_transport(tmp_path_factory):
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = _post(port, {'jsonrpc': '2.0', 'id': 1,
                                    'method': 'initialize'})
        assert status == 200
        assert 'instructions' in json.loads(body)['result']
        status, _ = _post(port, {'jsonrpc': '2.0',
                                 'method': 'notifications/initialized'})
        assert status == 202
        status, body = _post(port, {'jsonrpc': '2.0', 'id': 2,
                                    'method': 'tools/call',
                                    'params': {'name': 'find_objects',
                                               'arguments': {'mask': 'Справочник1'}}})
        assert status == 200
        assert 'Справочник.Справочник1' in json.loads(body)['result']['content'][0]['text']
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_streamable_http_get_stream(tmp_path_factory):
    # клиенты Streamable HTTP (Claude) открывают SSE-поток через GET /mcp
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/mcp',
                                    timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get('Content-Type') == 'text/event-stream'
            # legacy SSE-клиенты получают endpoint; streamable игнорируют его
            assert resp.readline().decode().strip() == 'event: endpoint'
            assert 'session_id=' in resp.readline().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_sse_transport(tmp_path_factory):
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/sse',
                                    timeout=5) as sse:
            assert sse.readline().decode().strip() == 'event: endpoint'
            data = sse.readline().decode().strip()
            sid = data.split('session_id=')[1]
            assert sse.readline() == b'\n'  # разделитель события
            status, _ = _post(port, {'jsonrpc': '2.0', 'id': 7,
                                     'method': 'tools/list'},
                              path=f'/messages?session_id={sid}')
            assert status == 202
            assert sse.readline().decode().strip() == 'event: message'
            payload = json.loads(sse.readline().decode().split('data: ', 1)[1])
            assert payload['id'] == 7 and 'tools' in payload['result']
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- несколько баз одновременно: алиасы, переключение, параметр db -------

def _make_db(tmp_path_factory, name):
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / name)
    write_db(dump, db, source_file=name + '.cf')
    return db


def _two_dbs(tmp_path_factory):
    """Основная база и «расширение» с объектом, которого нет в основной."""
    main_db = _make_db(tmp_path_factory, 'основная.sqlite')
    ext_db = _make_db(tmp_path_factory, 'расширение.sqlite')
    conn = sqlite3.connect(ext_db)
    conn.execute(
        'INSERT INTO meta_object(source_id, ord, path, type, name, type_ru, '
        'header_json) VALUES (1, 99, ?, ?, ?, ?, ?)',
        ('DataProcessor/ДопОбработка', 'DataProcessor', 'ДопОбработка',
         'Обработка', '{}'))
    conn.commit()
    conn.close()
    return main_db, ext_db


def test_multidb_routing(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    assert list(server.dbs) == ['основная', 'расширение']
    assert server.active == 'основная'  # первая указанная — активная
    # объекта расширения нет в активной основной базе…
    miss = _call(server, 'find_objects', mask='ДопОбработка')
    assert 'ничего не найдено' in miss['content'][0]['text']
    # …но находится по явному алиасу (без переключения активной)
    hit = _call(server, 'find_objects', mask='ДопОбработка',
                db='расширение')
    assert 'Обработка.ДопОбработка' in hit['content'][0]['text']
    assert server.active == 'основная'
    # алиасы нечувствительны к регистру
    hit = _call(server, 'object_card', path='DataProcessor/ДопОбработка',
                db='РАСШИРЕНИЕ')
    assert 'ДопОбработка' in hit['content'][0]['text']
    # переключение активной базы — инструменты работают уже без db
    use = _call(server, 'db_use', alias='расширение')
    assert 'расширение' in use['content'][0]['text']
    hit = _call(server, 'find_objects', mask='ДопОбработка')
    assert 'Обработка.ДопОбработка' in hit['content'][0]['text']
    # check_query/sql тоже смотрят в выбранную базу
    ok = _call(server, 'check_query',
               text='ВЫБРАТЬ Т.СсылкаАтрибут ИЗ Справочник.Справочник1 КАК Т',
               db='основная')
    text = ok['content'][0]['text']
    # Ответ содержит заголовок базы 'основная' и 'OK'
    assert '=== база основная' in text and 'OK' in text
    res = _call(server, 'sql',
                query="SELECT path FROM meta_object WHERE name='ДопОбработка'",
                db='расширение')
    assert 'DataProcessor/ДопОбработка' in res['content'][0]['text']


def test_db_identifier_in_responses(tmp_path_factory):
    """Каждый ответ инструмента данных содержит идентификатор базы."""
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    
    # Запрос к активной базе (основная)
    res = _call(server, 'find_objects', mask='Справочник1')['content'][0]['text']
    assert '=== база основная' in res
    assert 'Справочник.Справочник1' in res
    
    # Запрос к указанной базе (расширение)
    res = _call(server, 'find_objects', mask='ДопОбработка',
                db='расширение')['content'][0]['text']
    assert '=== база расширение' in res
    assert 'Обработка.ДопОбработка' in res
    
    # SQL тоже содержит идентификатор
    res = _call(server, 'sql', query='SELECT COUNT(*) AS n FROM meta_object')['content'][0]['text']
    assert '=== база основная' in res
    assert 'n' in res
    
    # Инструменты управления базами НЕ содержат идентификатор (они сами управляют базами)
    res = _call(server, 'db_list')['content'][0]['text']
    assert '=== база' not in res  # db_list уже содержит алиасы в своём формате


def test_db_star_queries_every_base_at_once(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    # db='*' — инструмент выполняется по всем открытым базам сразу
    res = _call(server, 'find_objects', mask='ДопОбработка',
                db='*')['content'][0]['text']
    assert '=== база основная' in res and '=== база расширение' in res
    assert 'Обработка.ДопОбработка' in res  # нашлась во второй базе
    assert server.active == 'основная'  # активная база не меняется
    # '*' по пустому серверу — та же ошибка, что у обычного вызова
    _call(server, 'db_close', alias='основная')
    _call(server, 'db_close', alias='расширение')
    err = _call(server, 'find_objects', mask='Тест', db='*')
    assert err.get('isError') and 'нет открытых баз' in err['content'][0]['text']


def test_compare_object(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    same = _call(server, 'compare_object', path='Catalog/Справочник1',
                 db_left='основная',
                 db_right='расширение')['content'][0]['text']
    assert 'Различий нет' in same
    only = _call(server, 'compare_object', path='DataProcessor/ДопОбработка',
                 db_left='основная',
                 db_right='расширение')['content'][0]['text']
    assert 'объект есть только в базе «расширение»' in only
    miss = _call(server, 'compare_object', path='Catalog/НетТакого',
                 db_left='основная',
                 db_right='расширение')['content'][0]['text']
    assert 'не найден ни в базе «основная»' in miss
    # неизвестный алиас — ошибка, а не пустой отчёт
    bad = _call(server, 'compare_object', path='Catalog/Справочник1',
                db_left='основная', db_right='неттакой')
    assert bad.get('isError') and 'не открыта' in bad['content'][0]['text']


def test_compare_object_shows_field_and_type_diff(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    conn = sqlite3.connect(ext_db)
    oid = conn.execute("SELECT id FROM meta_object "
                       "WHERE path='Catalog/Справочник1'").fetchone()[0]
    conn.execute('INSERT INTO meta_attribute (object_id, ord, name, type_str) '
                 'VALUES (?, 99, ?, ?)', (oid, 'лст_НовыйРеквизит', 'Строка(20)'))
    conn.execute("UPDATE meta_attribute SET type_str='Число' "
                 "WHERE object_id=? AND name='СсылкаАтрибут'", (oid,))
    conn.commit()
    conn.close()
    server = McpServer([main_db, ext_db])
    out = _call(server, 'compare_object', path='Справочник.Справочник1',
                db_left='основная',
                db_right='расширение')['content'][0]['text']
    assert 'только в базе «расширение»: лст_НовыйРеквизит: Строка(20)' in out
    # тип показан в русской точечной форме, как в остальных инструментах
    assert 'тип Ссылка: Справочник.Справочник1 -> Число' in out


def test_extension_diff(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    out = _call(server, 'extension_diff', extension_db='расширение',
                base_db='основная')['content'][0]['text']
    assert 'Новые объекты расширения (1)' in out
    assert 'Обработка.ДопОбработка' in out
    assert 'Заимствованные объекты' in out
    assert 'Справочник.Справочник1' in out
    # корень тестовой базы — конфигурация, а не расширение: честное предупреждение
    assert 'не расширение конфигурации' in out


def test_db_management_tools(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer(main_db)  # строка тоже принимается (одна база)
    lst = _call(server, 'db_list')['content'][0]['text']
    assert '* основная' in lst and 'объектов:' in lst
    # db_open на ходу: вторая база открывается и становится активной
    opened = _call(server, 'db_open', path=ext_db, alias='расш')
    assert 'расш' in opened['content'][0]['text']
    assert server.active == 'расш'
    # повторное открытие того же файла — тот же алиас, без дублей
    again = _call(server, 'db_open', path=ext_db)
    assert 'расш' in again['content'][0]['text']
    assert list(server.dbs) == ['основная', 'расш']
    # алиас из имени файла, совпадениям — суффикс; новая база становится активной
    third = _make_db(tmp_path_factory, 'основная.sqlite')
    _call(server, 'db_open', path=third)
    assert 'основная_2' in server.dbs and server.active == 'основная_2'
    # db_list помечает активную
    lst = _call(server, 'db_list')['content'][0]['text']
    assert '* основная_2 —' in lst
    assert ' основная —' in lst and ' расш —' in lst
    # неизвестный алиас — ошибка со списком открытых
    bad = _call(server, 'db_use', alias='неттакой')
    assert bad.get('isError') and 'не открыта' in bad['content'][0]['text']
    # вернули активную и закрываем: сначала неактивную, потом активную
    _call(server, 'db_use', alias='расш')
    closed = _call(server, 'db_close', alias='основная_2')
    assert 'закрыта' in closed['content'][0]['text']
    closed = _call(server, 'db_close')  # активная ('расш')
    assert 'закрыта' in closed['content'][0]['text']
    assert server.active == 'основная'
    # закрыли последнюю — инструменты сообщают открыть базу
    _call(server, 'db_close')
    assert server.dbs == {} and server.active is None
    err = _call(server, 'find_objects', mask='Тест')
    assert err.get('isError') and 'нет открытых баз' in err['content'][0]['text']


def test_db_open_rejects_bad_files(tmp_path_factory):
    server = McpServer(_make_db(tmp_path_factory, 'т.sqlite'))
    miss = _call(server, 'db_open', path=str(tmp_path_factory.mktemp('x') / 'нет.db'))
    assert miss.get('isError') and 'не найден' in miss['content'][0]['text']
    # sqlite-файл без таблицы meta_object — не база знаний
    foreign = str(tmp_path_factory.mktemp('y') / 'чужая.db')
    conn = sqlite3.connect(foreign)
    conn.execute('CREATE TABLE t (x)')
    conn.close()
    bad = _call(server, 'db_open', path=foreign)
    assert bad.get('isError') and 'не база знаний' in bad['content'][0]['text']
    assert foreign not in [d['path'] for d in server.dbs.values()]
    # занятый алиас
    busy = _call(server, 'db_open',
                 path=_make_db(tmp_path_factory, 'вторая.sqlite'),
                 alias='т')
    assert busy.get('isError') and 'занят' in busy['content'][0]['text']


def test_resolve_dbs(tmp_path, capsys):
    one = tmp_path / 'одна.db'
    two = tmp_path / 'две.db'
    one.write_bytes(b'x')
    two.write_bytes(b'x')
    assert mcp_server.resolve_dbs([str(one), str(two)]) == [str(one), str(two)]
    with pytest.raises(SystemExit) as exc:
        mcp_server.resolve_dbs([str(one), str(tmp_path / 'нет.db')])
    assert exc.value.code == 2
    assert 'не найден' in capsys.readouterr().err


# -- запуск без пути: проверка и автопоиск базы --------------------------

def test_resolve_db_explicit(tmp_path, capsys):
    db = tmp_path / 't.sqlite'
    db.write_bytes(b'x')
    assert resolve_db(str(db)) == str(db)
    with pytest.raises(SystemExit) as exc:
        resolve_db(str(tmp_path / 'нет.sqlite'))
    assert exc.value.code == 2
    assert 'не найден' in capsys.readouterr().err


def test_resolve_db_last_db(tmp_path, monkeypatch):
    # last_db из конфига имеет приоритет над автопоиском
    db = tmp_path / 'база.sqlite'
    db.write_bytes(b'x')
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'last_db': str(db)}), encoding='utf-8')
    monkeypatch.setattr('confdb.config.CONFIG_PATH', str(cfg))
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert resolve_db(None) == str(db)


def test_resolve_db_autofind(tmp_path, monkeypatch, capsys):
    # без конфига (свежая установка) база находится обходом каталогов
    monkeypatch.setattr('confdb.config.CONFIG_PATH', str(tmp_path / 'нет.json'))
    monkeypatch.setattr(mcp_server, '_scan_roots', lambda: [str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        resolve_db(None)
    assert exc.value.code == 2
    assert 'не найдена' in capsys.readouterr().err
    (tmp_path / 'db').mkdir()
    one = tmp_path / 'db' / 'одна.sqlite'
    one.write_bytes(b'x')
    assert resolve_db(None) == str(one)
    two = tmp_path / 'вторая.db'
    two.write_bytes(b'x')
    with pytest.raises(SystemExit) as exc:
        resolve_db(None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(one) in err and str(two) in err


def test_find_db_candidates_dedup(tmp_path, monkeypatch):
    # db/ и _out/ просматриваются, повторы и каталоги не попадают в список
    monkeypatch.setattr(mcp_server, '_scan_roots', lambda: [str(tmp_path)])
    (tmp_path / 'db').mkdir()
    (tmp_path / '_out').mkdir()
    (tmp_path / 'a.db').write_bytes(b'x')
    (tmp_path / 'db' / 'b.sqlite').write_bytes(b'x')
    (tmp_path / '_out' / 'c.db').write_bytes(b'x')
    (tmp_path / 'каталог.db').mkdir()
    found = mcp_server.find_db_candidates()
    assert found == [str(tmp_path / 'a.db'),
                     str(tmp_path / 'db' / 'b.sqlite'),
                     str(tmp_path / '_out' / 'c.db')]


# -- тесты групп конфигураций ------------------------------------------------

def _get_text(result):
    """Извлекает текст из результата вызова инструмента."""
    if isinstance(result, dict) and 'content' in result:
        return result['content'][0]['text']
    return str(result)


def test_group_create_and_list(tmp_path_factory):
    server = _server(tmp_path_factory)
    # Создаём группу
    result = _call(server, 'group_create', name='тестовая группа')
    text = _get_text(result)
    assert 'группа создана' in text
    assert 'тестовая группа' in text
    # Список групп
    groups = _call(server, 'group_list')
    text = _get_text(groups)
    assert 'тестовая группа' in text
    assert '0 баз' in text


def test_group_add_and_remove_db(tmp_path_factory):
    server = _server(tmp_path_factory)
    # Получаем реальный алиас базы
    db_alias = list(server.dbs.keys())[0]
    # Открываем ту же базу под вторым алиасом
    db_path = server.dbs[db_alias]['path']
    server.open_db(db_path, alias='db2')
    # Создаём группу и добавляем базы
    _call(server, 'group_create', name='группа1')
    result = _call(server, 'group_add_db', group='группа1', db=db_alias)
    text = _get_text(result)
    assert 'добавлена в группу' in text
    result = _call(server, 'group_add_db', group='группа1', db='db2')
    text = _get_text(result)
    assert 'добавлена в группу' in text
    # Проверяем список
    groups = _call(server, 'group_list')
    text = _get_text(groups)
    assert 'группа1 (2 баз)' in text
    # Убираем одну базу
    result = _call(server, 'group_remove_db', group='группа1', db='db2')
    text = _get_text(result)
    assert 'удалена из группы' in text
    groups = _call(server, 'group_list')
    text = _get_text(groups)
    assert 'группа1 (1 баз)' in text


def test_group_use(tmp_path_factory):
    server = _server(tmp_path_factory)
    _call(server, 'group_create', name='группа1')
    _call(server, 'group_create', name='группа2')
    # Первая созданная группа становится активной
    groups = _call(server, 'group_list')
    text = _get_text(groups)
    assert '* группа1' in text
    # Переключаем активную группу
    result = _call(server, 'group_use', group='группа2')
    text = _get_text(result)
    assert 'активная группа: группа2' in text
    groups = _call(server, 'group_list')
    text = _get_text(groups)
    assert '* группа2' in text


def test_group_close(tmp_path_factory):
    server = _server(tmp_path_factory)
    _call(server, 'group_create', name='группа1')
    _call(server, 'group_create', name='группа2')
    result = _call(server, 'group_close', group='группа1')
    text = _get_text(result)
    assert 'группа группа1 удалена' in text
    groups = _call(server, 'group_list')
    text = _get_text(groups)
    assert 'группа1' not in text
    assert 'группа2' in text


def test_group_fanout_in_tools(tmp_path_factory):
    """Параметр group выполняет инструмент по всем базам группы."""
    server = _server(tmp_path_factory)
    db_alias = list(server.dbs.keys())[0]
    # Открываем ту же базу под вторым алиасом
    db_path = server.dbs[db_alias]['path']
    server.open_db(db_path, alias='db2')
    # Создаём группу с двумя базами
    _call(server, 'group_create', name='все базы')
    _call(server, 'group_add_db', group='все базы', db=db_alias)
    _call(server, 'group_add_db', group='все базы', db='db2')
    # Запрос с group — должен вернуть результаты из обеих баз
    result = _call(server, 'find_objects', mask='Справочник', group='все базы')
    text = _get_text(result)
    assert 'группа все базы / база' in text
    assert 'Справочник.Справочник1' in text


def test_group_star_fanout(tmp_path_factory):
    """group='*' выполняет инструмент по всем группам."""
    server = _server(tmp_path_factory)
    db_alias = list(server.dbs.keys())[0]
    # Открываем ту же базу под вторым алиасом
    db_path = server.dbs[db_alias]['path']
    server.open_db(db_path, alias='db2')
    # Создаём две группы
    _call(server, 'group_create', name='группа1')
    _call(server, 'group_add_db', group='группа1', db=db_alias)
    _call(server, 'group_create', name='группа2')
    _call(server, 'group_add_db', group='группа2', db='db2')
    # Запрос с group='*'
    result = _call(server, 'find_objects', mask='Справочник', group='*')
    text = _get_text(result)
    assert '=== группа группа1 ===' in text
    assert '=== группа группа2 ===' in text
    assert 'Справочник.Справочник1' in text


def test_group_priority_over_db(tmp_path_factory):
    """Параметр group имеет приоритет над db."""
    server = _server(tmp_path_factory)
    db_alias = list(server.dbs.keys())[0]
    # Открываем ту же базу под вторым алиасом
    db_path = server.dbs[db_alias]['path']
    server.open_db(db_path, alias='db2')
    _call(server, 'group_create', name='группа1')
    _call(server, 'group_add_db', group='группа1', db=db_alias)
    _call(server, 'group_add_db', group='группа1', db='db2')
    # Указаны и group, и db — group должен победить
    result = _call(server, 'find_objects', mask='Справочник', group='группа1', db=db_alias)
    text = _get_text(result)
    # Должны вернуться результаты из обеих баз группы, а не только из db
    assert 'группа группа1 / база' in text


def test_group_header_in_response(tmp_path_factory):
    """Заголовки ответов идентифицируют группу при работе с группой."""
    server = _server(tmp_path_factory)
    db_alias = list(server.dbs.keys())[0]
    _call(server, 'group_create', name='моя группа')
    _call(server, 'group_add_db', group='моя группа', db=db_alias)
    _call(server, 'group_use', group='моя группа')
    # Запрос к активной группе
    result = _call(server, 'find_objects', mask='Справочник')
    text = _get_text(result)
    assert 'группа моя группа / база' in text
