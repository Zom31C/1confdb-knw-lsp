"""Тесты типа загруженного файла, режима совместимости и подписок на события."""
import json

from confdb.header_props import (compatibility_line, compatibility_short,
                                 event_name, event_subscription, kind_of,
                                 self_ref_uuid, source_ext, version_noun)


def test_source_ext():
    assert source_ext('D:\\cf\\Расширение1.cfe') == '.cfe'
    assert source_ext('t.CF') == '.cf'
    assert source_ext('') is None
    assert source_ext(None) is None


def test_kind_of_prefers_file_extension():
    # .erf декодирует класс ExternalDataProcessor, поэтому тип корня
    # у внешнего отчёта неотличим от внешней обработки
    assert kind_of('ExternalDataProcessor', 'Внешняя обработка',
                   'D:\\Отчёты\\МойОтчёт.erf') == ('Внешний отчёт', '.erf')
    assert kind_of('ConfigurationExtension', 'Расширение конфигурации',
                   'Расширение1.cfe') == ('Расширение конфигурации', '.cfe')


def test_kind_of_falls_back_to_root_type():
    assert kind_of('Configuration', 'Конфигурация', None) == ('Конфигурация', None)
    assert kind_of('Configuration', 'Конфигурация', '') == ('Конфигурация', None)
    # type_ru допускается NULL — тогда английский stem
    assert kind_of('Configuration', None, None) == ('Конфигурация', None)
    # неизвестный тип и неизвестное расширение — как есть, без догадок
    assert kind_of('Something', 'Что-то', 'x.dat') == ('Что-то', '.dat')
    assert kind_of(None, None, None) == (None, None)


def test_version_noun():
    assert version_noun('Конфигурация') == 'конфигурации'
    assert version_noun('Расширение конфигурации') == 'расширения'
    assert version_noun('Внешняя обработка') == 'обработки'
    assert version_noun('Внешний отчёт') == 'отчёта'
    assert version_noun(None) == 'конфигурации'


def test_compatibility_of_configuration():
    assert compatibility_line('Configuration', '8.3.21') == '8.3.21'
    assert compatibility_line('Configuration', None) == 'не определён'
    assert compatibility_short('Configuration', '8.3.21') == '8.3.21'
    assert compatibility_short('Configuration', None) is None


def test_compatibility_of_extension_is_inherited():
    line = compatibility_line('ConfigurationExtension', '8.3.16')
    assert 'наследуется от основной конфигурации' in line
    assert '8.3.16' in line
    assert compatibility_line('ConfigurationExtension', None) \
        == 'наследуется от основной конфигурации'
    assert compatibility_short('ConfigurationExtension', '8.3.16') \
        == '8.3.16 (наследуется)'
    assert compatibility_short('ConfigurationExtension', None) \
        == 'наследуется от основной конфигурации'


def test_compatibility_of_processor_and_report_is_absent():
    for root in ('ExternalDataProcessor', 'ExternalReport'):
        assert compatibility_line(root, None) == 'не задаётся'
        assert compatibility_short(root, None) is None


def test_event_subscription_props():
    handler = 'bc07f408-470d-4021-9545-f072accda37a'
    src1, src2 = ('aaaaaaaa-0000-0000-0000-000000000001',
                  'bbbbbbbb-0000-0000-0000-000000000002')
    header = json.dumps({'header': [['1', [
        '1', ['3', ['1', '0', 'в отдельном файле'], '"ПодпискаТест"'],
        ['"Pattern"', ['"#"', src1], ['"#"', src2]],
        '"BeforeWrite_ПередЗаписью"', handler, '"ПриЗаписиДокумента"'],
        '0']]}, ensure_ascii=False)
    assert event_subscription('EventSubscription', header) == {
        'event': 'ПередЗаписью',
        'handler_uuid': handler, 'handler_method': 'ПриЗаписиДокумента',
        'sources': [src1, src2]}
    # не подписка и не заголовок вовсе — пустой словарь, а не исключение
    assert event_subscription('Catalog', header) == {}
    assert event_subscription('EventSubscription', 'не json') == {}
    assert event_subscription('EventSubscription', None) == {}
    assert event_subscription('EventSubscription',
                              json.dumps({'header': [['1', ['1']]]})) == {}


def test_event_name():
    # наружу — только русское имя события, как в конфигураторе
    assert event_name('"BeforeWrite_ПередЗаписью"') == 'ПередЗаписью'
    assert event_name('"Filling_ОбработкаЗаполнения"') == 'ОбработкаЗаполнения'
    # строка без английской части возвращается как есть
    assert event_name('"ПередЗаписью"') == 'ПередЗаписью'
    assert event_name(None) is None


def test_self_ref_uuid_positions():
    ref = 'aaaaaaaa-0000-0000-0000-000000000001'
    # справочник/документ/регистр: свой «источниковый» uuid в header[0][1][1]
    assert self_ref_uuid(json.dumps({'header': [['1', ['56', ref, 'x']]]})) == ref
    # константа: позиция [1] занята записью типа, uuid сдвинут в [4]
    constant = {'header': [['1', ['16', ['27', 'тип'], 'u2', 'u3', ref]]]}
    assert self_ref_uuid(json.dumps(constant)) == ref
    # у общего модуля и прочих такого uuid нет: None, а не выдуманное значение
    assert self_ref_uuid(json.dumps({'header': [['1', ['81', '0', '1']]]})) is None
    assert self_ref_uuid('не json') is None
    assert self_ref_uuid(None) is None
