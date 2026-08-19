"""Тесты подготовки дампа для BSL Language Server (prep-lsp)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.__main__ import main  # noqa: E402
from confdb.lsp_prep import prep_dump  # noqa: E402

BOM = b'\xef\xbb\xbf'


def make_dump(root):
    """Синтетический дамп: модули в стиле выгрузки 1С и служебные файлы."""
    mod = root / 'Catalog' / 'Товары'
    mod.mkdir(parents=True)
    (mod / 'Товары.obj.bsl').write_bytes(
        BOM + 'Процедура Тест()\r\nКонецПроцедуры\r\n'.encode('utf-8'))
    (mod / 'Товары.json').write_text('{}', encoding='utf-8')
    ok = root / 'Catalog' / 'Готовый'
    ok.mkdir()
    (ok / 'Готовый.obj.bsl').write_bytes(
        'Процедура Ок()\nКонецПроцедуры\n'.encode('utf-8'))
    return root


def test_prep_inplace(tmp_path):
    dump = make_dump(tmp_path / 'dump')
    stats = prep_dump(str(dump))
    assert stats == {'files': 2, 'converted': 1, 'skipped': 1}
    data = (dump / 'Catalog' / 'Товары' / 'Товары.obj.bsl').read_bytes()
    assert not data.startswith(BOM)
    assert b'\r' not in data
    assert data.decode('utf-8') == 'Процедура Тест()\nКонецПроцедуры\n'
    # уже корректный файл не тронут
    assert (dump / 'Catalog' / 'Готовый' / 'Готовый.obj.bsl').read_bytes() == \
        'Процедура Ок()\nКонецПроцедуры\n'.encode('utf-8')
    # не-.bsl файлы не трогаются
    assert (dump / 'Catalog' / 'Товары' / 'Товары.json').read_text(encoding='utf-8') == '{}'


def test_prep_into(tmp_path):
    dump = make_dump(tmp_path / 'dump')
    dst = tmp_path / 'lsp'
    stats = prep_dump(str(dump), dst_dir=str(dst))
    assert stats['converted'] == 1 and stats['skipped'] == 1
    # источник не изменён
    assert (dump / 'Catalog' / 'Товары' / 'Товары.obj.bsl').read_bytes().startswith(BOM)
    # назначение — LF без BOM, структура сохранена, json скопирован
    data = (dst / 'Catalog' / 'Товары' / 'Товары.obj.bsl').read_bytes()
    assert not data.startswith(BOM) and b'\r' not in data
    assert (dst / 'Catalog' / 'Товары' / 'Товары.json').read_text(encoding='utf-8') == '{}'


def test_prep_lone_cr(tmp_path):
    dump = tmp_path / 'dump'
    dump.mkdir()
    (dump / 'a.bsl').write_bytes(BOM + 'A = 1;\rB = 2;\r\n'.encode('utf-8'))
    stats = prep_dump(str(dump))
    assert stats['converted'] == 1
    assert (dump / 'a.bsl').read_bytes() == b'A = 1;\nB = 2;\n'


def test_prep_non_utf8_preserved(tmp_path):
    dump = tmp_path / 'dump'
    dump.mkdir()
    raw = 'Процедура'.encode('cp1251') + b'\r\n'  # валидный cp1251, невалидный utf-8
    (dump / 'a.bsl').write_bytes(raw)
    stats = prep_dump(str(dump), dst_dir=str(tmp_path / 'lsp'))
    assert stats == {'files': 1, 'converted': 0, 'skipped': 1}
    assert (tmp_path / 'lsp' / 'a.bsl').read_bytes() == raw
    assert (dump / 'a.bsl').read_bytes() == raw


def test_prep_bad_dirs(tmp_path):
    with pytest.raises(FileNotFoundError):
        prep_dump(str(tmp_path / 'нет'))
    dump = make_dump(tmp_path / 'dump')
    with pytest.raises(ValueError):
        prep_dump(str(dump), dst_dir=str(dump))
    with pytest.raises(ValueError):
        prep_dump(str(dump), dst_dir=str(dump / 'вложенный'))


def test_cli_prep_lsp(tmp_path, capsys):
    dump = make_dump(tmp_path / 'dump')
    assert main(['prep-lsp', str(dump)]) == 0
    assert 'переписано: 1' in capsys.readouterr().out
    dst = tmp_path / 'lsp'
    assert main(['prep-lsp', str(dump), '--into', str(dst)]) == 0
    assert main(['prep-lsp', str(tmp_path / 'нет')]) == 2
