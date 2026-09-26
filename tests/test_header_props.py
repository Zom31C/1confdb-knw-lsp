"""Тесты типа загруженного файла и формулировок режима совместимости."""
from confdb.header_props import (compatibility_line, compatibility_short,
                                 kind_of, source_ext, version_noun)


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
