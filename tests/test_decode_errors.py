"""Ошибки декодирования объектов: форма сообщения и режим --skip-errors.

Синтетически воспроизводится ситуация «падает одна подсистема» (как на
больших конфигурациях): испорченные файлы объекта на стадии 3.
"""
import os
import uuid as uuid_mod

import pytest

from confdb.v8 import decoder as v8_decoder
from confdb.v8.decoder import Decoder
from confdb.v8.ext_exception import ExtException


def _write(path, name, content):
    with open(os.path.join(path, name), 'w', encoding='utf-8') as f:
        f.write(content)


@pytest.fixture(autouse=True)
def _fresh_err_counter():
    v8_decoder._reset_err_counter()


def _good_subsystem_src(tmp_path, file_name='sub.1', obj_uuid=None):
    """Минимальная корректная подсистема: заголовок без дочерних объектов.

    Формат реального скобкофайла: значения не начинаются с начала строки,
    вложенный объект — '{' в начале строки. Структура как у подсистемы:
    [ [ '1', [ '0', ЗАГОЛОВОК ], КОЛИЧЕСТВО_ТИПОВ_ДЕТЕЙ ] ].
    """
    obj_uuid = obj_uuid or str(uuid_mod.uuid4())
    src = str(tmp_path / 'src')
    os.makedirs(src, exist_ok=True)
    content = (
        '{1,\n'
        '{0,\n'
        '{3,\n'
        '{1,0,%s},"ТестПодсистема",\n'
        '{1,"ru","ТестПодсистема"},""}\n'
        '},0}\n' % obj_uuid)
    _write(src, file_name, content)
    return src


def _params(src, file_name, dest_dir, options=None):
    opts = {'obj_version': '803'}  # в конвейере проставляет detect_version
    if options:
        opts.update(options)
    return ['Subsystem', [src, file_name, dest_dir, 'Subsystem', None, opts]]


def test_subsystem_decode_ok(tmp_path):
    """Корректная синтетическая подсистема декодируется."""
    src = _good_subsystem_src(tmp_path)
    dest = str(tmp_path / 'dest')
    tasks = Decoder.decode_include(_params(src, 'sub.1', dest))
    assert tasks == []
    assert os.path.isfile(os.path.join(dest, 'Subsystem', 'ТестПодсистема',
                                       'Subsystem.json'))


def test_decode_error_shape_matches_log(tmp_path):
    """Испорченный файл: исключение обёрнуто как 'Decoder.decode_include Subsystem'
    (та же форма, что в сообщении пользователя)."""
    src = _good_subsystem_src(tmp_path)
    _write(src, 'bad.1', '{1,{{{битый скобкофайл')
    with pytest.raises(ExtException) as exc:
        Decoder.decode_include(_params(src, 'bad.1', str(tmp_path / 'dest')))
    err = exc.value
    assert err.action == 'Decoder.decode_include Subsystem'
    assert 'Ошибка декодирования' in str(err)


def test_help_unknown_prefix_is_fatal(tmp_path):
    """Файл справки (.0) с неизвестным форматом содержимого роняет объект
    (кандидат в причину ошибки на БухгалтерииПредприятия)."""
    obj_uuid = str(uuid_mod.uuid4())
    src = _good_subsystem_src(tmp_path, obj_uuid=obj_uuid)
    # data[0][3][0] есть, но префикс не ##base64:/#base64:/#data:
    _write(src, f'{obj_uuid}.0', '{0,0,0,\n{"неизвестный формат"}\n}\n')
    with pytest.raises(ExtException) as exc:
        Decoder.decode_include(_params(src, 'sub.1', str(tmp_path / 'dest')))
    assert exc.value.action == 'Decoder.decode_include Subsystem'
    assert 'decode_html_data' in str(exc.value)


def test_skip_errors_skips_bad_object(tmp_path, capsys):
    """--skip-errors: битый объект пропускается, корректный соседний — нет."""
    src = _good_subsystem_src(tmp_path, file_name='good.1')
    _write(src, 'bad.1', '{1,{{{битый скобкофайл')
    dest = str(tmp_path / 'dest')
    opts = {'skip_errors': True}
    assert Decoder.decode_include(_params(src, 'bad.1', dest, opts)) == []
    assert Decoder.decode_include(_params(src, 'good.1', dest, opts)) == []
    assert os.path.isfile(os.path.join(dest, 'Subsystem', 'ТестПодсистема',
                                       'Subsystem.json'))
    err_out = capsys.readouterr().err
    assert 'Пропуск объекта Subsystem' in err_out
    assert v8_decoder._err_counter().value == 1
