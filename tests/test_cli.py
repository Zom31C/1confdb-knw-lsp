"""Тесты CLI confdb (разбор аргументов)."""
import pytest

from confdb.__main__ import build_parser, main


def test_parser_full():
    args = build_parser().parse_args([
        'extract', 'config.cf', '--db', 'out.sqlite', '--dump', 'out',
        '--keep-temp', '--store-blobs', '--prefix', 'Префикс_',
    ])
    assert args.cmd == 'extract'
    assert args.src == 'config.cf'
    assert args.db == 'out.sqlite'
    assert args.dump == 'out'
    assert args.keep_temp and args.store_blobs
    assert args.prefix == 'Префикс_'


def test_main_requires_target():
    with pytest.raises(SystemExit) as exc:
        main(['extract', 'config.cf'])
    assert exc.value.code == 2


def test_main_missing_file(tmp_path):
    rc = main(['extract', str(tmp_path / 'нет-такого.cf'), '--dump', str(tmp_path / 'out')])
    assert rc == 2


def test_mcp_db_optional():
    # без пути база ищется автоматически (last_db из конфига, затем обход);
    # nargs='*' даёт пустой список, а не None
    args = build_parser().parse_args(['1confdb-knw'])
    assert args.db == []
    args = build_parser().parse_args(['1confdb-knw', 'out.db', '--port', '8765'])
    assert args.db == ['out.db'] and args.port == 8765


def test_main_mcp_missing_db(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(['1confdb-knw', str(tmp_path / 'нет.db')])
    assert exc.value.code == 2
