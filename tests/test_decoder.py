"""Тесты парсера скобкофайлов (в т.ч. поведение оптимизированного decode_object).

Формат 1С — построчный: вложенный объект начинается с '{' в начале строки,
значения не начинаются с начала строки, пробелов после запятых нет.
"""
import io

import pytest

from confdb.v8.json_container_decoder import JsonContainerDecoder, BigBase64
from confdb.v8.ext_exception import ExtException


def decode_text(text):
    decoder = JsonContainerDecoder(src_dir='.', file_name='test')
    return decoder.decode_file(io.StringIO(text))


def test_single_line_object():
    assert decode_text('{1,"строка",2}\n') == [['1', '"строка"', '2']]


def test_string_with_special_chars():
    # запятые и скобки внутри кавычек — часть значения
    assert decode_text('{"a,b","c}d","e{f",""}\n') == [['"a,b"', '"c}d"', '"e{f"', '""']]


def test_escaped_quotes():
    assert decode_text('{"он сказал ""привет"", и ушёл"}\n') == [['"он сказал ""привет"", и ушёл"']]


def test_value_across_lines():
    # строка, начавшаяся на одной строке и закончившаяся на следующей
    assert decode_text('{"раз\nдва"}\n') == [['"раз\nдва"']]


def test_nested_objects():
    # вложенность задаётся '{' в начале строки; после '}' значения пишутся в родителя
    text = '{1,\n{2,3},4}\n'
    assert decode_text(text) == [['1', ['2', '3'], '4']]


def test_deep_nesting():
    text = '{a,\n{b,\n{c,\n{d}\n}\n}\n}\n'
    assert decode_text(text) == [['a', ['b', ['c', ['d']]]]]


def test_base64_inline():
    assert decode_text('{#base64:AAAA\n}\n') == [['#base64:AAAA']]


def test_big_base64_raises():
    # объект из 2 элементов, первый '1', второй начинается с {#base64
    with pytest.raises(BigBase64):
        decode_text('{1,\n{#base64:AAAA\n}\n')


def test_empty():
    assert decode_text('') == ''


def test_empty_value_after_closed_object_is_none():
    # «,,» после закрытого вложенного объекта: эквивалентность v8unpack — None
    data = decode_text('{1,\n{2,3},,60}\n')
    assert data == [['1', ['2', '3'], None, '60']]


def test_plain_text_not_brace():
    # не-скобкофайл: decode_file не рассчитан на чистый текст и завершается ошибкой
    with pytest.raises(ExtException):
        decode_text('просто текст\nвторая строка\n')
