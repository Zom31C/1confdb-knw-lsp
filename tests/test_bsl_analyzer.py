"""Тесты лексического анализа BSL и свойств регистра из header_json.

Регрессии на дефекты, найденные ревью: ключевое слово 'Знач' не должно резать
имена вроде 'Значение', запрос, собранный конкатенацией с переменной, не должен
уходить в парсер как полный, локальная переменная с именем менеджера метаданных
не должна считаться обращением к метаданным, а отсутствующее значение в
заголовке — выдаваться за факт.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.bsl_analyzer import analyze, extract_queries, split_params  # noqa: E402
from confdb.header_props import IR_PERIODICITY, IR_WRITE_MODE, register_props  # noqa: E402


# -- split_params: 'Знач' это префикс, а не начало имени ---------------------

def test_split_params_keeps_names_starting_with_znach():
    assert split_params('Значение, Знач Ссылка, П3 = Неопределено') \
        == ['Значение', 'Ссылка', 'П3']


def test_split_params_strips_znach_keyword():
    assert split_params('Знач Параметр1, знач Параметр2') \
        == ['Параметр1', 'Параметр2']


# -- extract_queries: полнота текста запроса ---------------------------------

def test_full_query_is_complete():
    text = 'Запрос.Текст = "ВЫБРАТЬ Поле ИЗ Справочник.Номенклатура";'
    found = extract_queries(text)
    assert len(found) == 1
    line, query, complete = found[0]
    assert line == 1
    assert query == 'ВЫБРАТЬ Поле ИЗ Справочник.Номенклатура'
    assert complete is True


def test_query_concatenated_with_variable_is_incomplete():
    # самый частый шаблон динамического запроса: таблица подставляется переменной
    text = 'Запрос.Текст = "ВЫБРАТЬ * ИЗ " + ИмяТаблицы;'
    found = extract_queries(text)
    assert len(found) == 1
    _line, query, complete = found[0]
    assert query == 'ВЫБРАТЬ * ИЗ '
    assert complete is False


def test_query_cut_on_condition_is_incomplete():
    text = ('Запрос.Текст = "ВЫБРАТЬ Поле ИЗ Справочник.Х ГДЕ " + Условие;')
    _line, _query, complete = extract_queries(text)[0]
    assert complete is False


def test_comment_between_query_parts_does_not_break_it():
    text = ('Запрос = "ВЫБРАТЬ Поле1" // временно\n'
            '         + " ИЗ Справочник.Х";')
    found = extract_queries(text)
    assert len(found) == 1
    _line, query, complete = found[0]
    assert 'ВЫБРАТЬ Поле1' in query and 'ИЗ Справочник.Х' in query
    assert complete is True


def test_multiline_literal_query_is_complete():
    text = ('Запрос.Текст = "ВЫБРАТЬ\n'
            '               |Поле\n'
            '               |ИЗ Справочник.Х";')
    found = extract_queries(text)
    assert len(found) == 1
    _line, query, complete = found[0]
    assert 'ИЗ Справочник.Х' in query
    assert complete is True


def test_incomplete_query_is_not_reported_as_syntax_error():
    # analyze не должен гонять обрезанный текст через парсер: ошибки были бы ложными
    text = 'Запрос.Текст = "ВЫБРАТЬ * ИЗ " + ИмяТаблицы;'
    report = analyze(text)
    assert len(report['queries']) == 1
    _line, _query, complete, errors, unverified = report['queries'][0]
    assert complete is False and errors == [] and unverified == []


# -- analyze: имя менеджера метаданных как имя переменной -------------------

def test_local_variable_named_like_manager_is_not_metadata():
    text = 'Обработки = Новый Массив;\nОбработки.Добавить(Элемент);'
    report = analyze(text)
    assert report['metadata'] == []


def test_real_manager_call_is_metadata():
    text = 'Объект = Справочники.Номенклатура.Создать();'
    report = analyze(text)
    assert [(m, n) for m, n, _line, _path in report['metadata']] \
        == [('Справочники', 'Номенклатура')]


# -- register_props: отсутствие значения не выдаётся за факт -----------------

def _ir_header(periodicity, write_mode):
    inner = [None] * (IR_WRITE_MODE + 1)
    inner[IR_PERIODICITY] = periodicity
    inner[IR_WRITE_MODE] = write_mode
    return json.dumps({'header': [['узел', inner]]})


def test_register_props_reads_periodicity_and_write_mode():
    lines = register_props('InformationRegister', _ir_header('3', '1'))
    assert lines == ['Периодичность: месяц',
                     'Режим записи: подчинение регистратору']


def test_register_props_skips_null_and_bool_values():
    # null и true в этой позиции — не код периодичности: выдумывать значение нельзя
    for value in (None, True, False, ''):
        lines = register_props('InformationRegister', _ir_header(value, value))
        assert lines == [], value
        assert not any('код' in line for line in lines), lines


def test_register_props_unknown_code_is_still_reported():
    lines = register_props('InformationRegister', _ir_header('6', '0'))
    assert lines == ['Периодичность: есть, код 6 (не расшифрован)',
                     'Режим записи: независимый']


def test_register_props_accumulation_register_is_recorder_only():
    lines = register_props('AccumulationRegister',
                           _ir_header('3', '0'))
    assert lines == ['Режим записи: подчинение регистратору']
