"""Тесты разбора модулей 1С на процедуры/функции."""
from confdb.bsl_parser import parse_methods

MODULE = '''Перем Кэш;

#Область Служебные

&НаСервере
Процедура ПриСозданииНаСервере(Отказ,
		СтандартнаяОбработка)

	Кэш = Неопределено;

КонецПроцедуры

&НаКлиенте
Функция ЗначениеПоУмолчанию() Экспорт

	Возврат 0;

КонецФункции

#КонецОбласти

Функция Простая(А, Б = 1)

	Возврат А + Б;

КонецФункции
'''

MODULE_TEXT = '''Перем Кэш;

#Область Служебные

&НаСервере
Процедура ПриСозданииНаСервере(Отказ,
		СтандартнаяОбработка)

&НаКлиенте
Функция ЗначениеПоУмолчанию() Экспорт

#КонецОбласти

Функция Простая(А, Б = 1)'''


def test_methods_count_and_kinds():
    _, methods = parse_methods(MODULE)
    assert [m['name'] for m in methods] == [
        'ПриСозданииНаСервере', 'ЗначениеПоУмолчанию', 'Простая']
    assert [m['kind'] for m in methods] == ['процедура', 'функция', 'функция']


def test_directives():
    _, methods = parse_methods(MODULE)
    assert methods[0]['directives'] == ['&НаСервере']
    assert methods[1]['directives'] == ['&НаКлиенте']
    assert methods[2]['directives'] == []


def test_multiline_signature_and_export():
    _, methods = parse_methods(MODULE)
    assert methods[0]['signature'] == 'Отказ, СтандартнаяОбработка'
    assert methods[0]['is_export'] is False
    assert methods[1]['is_export'] is True
    assert methods[2]['signature'] == 'А, Б = 1'


def test_module_text_without_method_code():
    module_text, methods = parse_methods(MODULE)
    # модуль как есть, но без кода методов; сигнатуры и #… сохранены
    assert module_text == MODULE_TEXT
    assert 'Кэш = Неопределено' not in module_text
    assert 'Возврат' not in module_text


def test_line_numbers_and_body():
    _, methods = parse_methods(MODULE)
    m0 = methods[0]
    # сигнатура — строка 6, КонецПроцедуры — строка 11
    assert m0['line_start'] == 6
    assert m0['line_end'] == 11
    assert m0['body'] == ('Процедура ПриСозданииНаСервере(Отказ,\n'
                          '\t\tСтандартнаяОбработка)\n'
                          '\n'
                          '\tКэш = Неопределено;\n'
                          '\n'
                          'КонецПроцедуры')
    assert '#Область' not in m0['body']


def test_description_and_module_comments():
    module = ('// заголовок модуля\n'
              '\n'
              '#Если Сервер Тогда\n'
              '\n'
              '// Параметры:\n'
              '//   А - Число\n'
              'Процедура А(А)\n'
              '\tБ = А;\n'
              'КонецПроцедуры\n'
              '\n'
              '#КонецЕсли\n')
    module_text, methods = parse_methods(module)
    m = methods[0]
    # прилипший блок комментариев — в description, как есть
    assert m['description'] == '// Параметры:\n//   А - Число'
    assert m['body'] == 'Процедура А(А)\n\tБ = А;\nКонецПроцедуры'
    assert m['line_start'] == 7 and m['line_end'] == 9
    # оторванный комментарий и препроцессор остаются в тексте модуля
    assert '// заголовок модуля' in module_text
    assert '#Если Сервер Тогда' in module_text
    assert '#КонецЕсли' in module_text
    assert '\tБ = А;' not in module_text
    # подстановка тела вместо сигнатуры восстанавливает исходник
    rebuilt = module_text.replace('Процедура А(А)', m['body'], 1)
    assert rebuilt == module.rstrip('\n')


def test_description_through_directive():
    module = ('// Возвращает значение по умолчанию\n'
              '&НаКлиенте\n'
              'Функция Значение() Экспорт\n'
              '\tВозврат 0;\n'
              'КонецФункции\n')
    _, methods = parse_methods(module)
    assert methods[0]['description'] == '// Возвращает значение по умолчанию'
    assert methods[0]['directives'] == ['&НаКлиенте']


def test_empty_module():
    module_text, methods = parse_methods('')
    assert module_text == '' and methods == []


def test_module_without_methods():
    module_text, methods = parse_methods('Перем А;\nПерем Б;\n')
    assert methods == []
    assert module_text == 'Перем А;\nПерем Б;'
