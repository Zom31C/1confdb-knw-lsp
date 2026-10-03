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
    assert not args.skip_errors
    args = build_parser().parse_args(['extract', 'config.cf', '--skip-errors'])
    assert args.skip_errors


def test_main_requires_target():
    with pytest.raises(SystemExit) as exc:
        main(['extract', 'config.cf'])
    assert exc.value.code == 2


def test_main_missing_file(tmp_path):
    rc = main(['extract', str(tmp_path / 'нет-такого.cf'), '--dump', str(tmp_path / 'out')])
    assert rc == 2


def test_mcp_db_optional():
    # без пути база ищется автоматически (last_db из конфига, затем обход)
    args = build_parser().parse_args(['1confdb-knw'])
    assert args.db == []
    args = build_parser().parse_args(['1confdb-knw', 'out.db', '--port', '8765'])
    assert args.db == ['out.db'] and args.port == 8765
    # несколько баз сразу: основная конфигурация + расширения/обработки
    args = build_parser().parse_args(['1confdb-knw', 'main.db', 'ext.db'])
    assert args.db == ['main.db', 'ext.db']


def test_main_mcp_missing_db(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(['1confdb-knw', str(tmp_path / 'нет.db')])
    assert exc.value.code == 2


def test_parser_no_fts_and_fts_command():
    args = build_parser().parse_args(['extract', 'config.cf', '--db', 'o.db', '--no-fts'])
    assert args.no_fts
    assert not build_parser().parse_args(['extract', 'config.cf', '--db', 'o.db']).no_fts
    args = build_parser().parse_args(['fts', 'out.db', '--workers', '4'])
    assert args.cmd == 'fts' and args.db == 'out.db' and args.workers == 4
    assert build_parser().parse_args(['fts', 'out.db']).workers is None


def test_main_fts_missing_db(tmp_path):
    assert main(['fts', str(tmp_path / 'нет.db')]) == 2


def test_main_fts_rejects_foreign_db(tmp_path):
    import sqlite3

    other = str(tmp_path / 'other.sqlite')
    conn = sqlite3.connect(other)
    conn.execute('CREATE TABLE t (x)')
    conn.commit()
    conn.close()
    assert main(['fts', other]) == 1


def test_fts_command_builds_index(tmp_path):
    """confdb fts достраивает приставной индекс для базы, собранной с --no-fts."""
    import sqlite3

    from confdb.db.writer import FTS_TABLE, fts_index_info, fts_shard_paths, write_db

    from test_writer import make_dump
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    db = str(tmp_path / 'out.sqlite')
    write_db(dump, db, source_file='t.cf', build_fts=False)

    assert main(['fts', db, '--workers', '2']) == 0
    shards = fts_shard_paths(db)
    info = fts_index_info(db)
    assert info and shards and info['shards'] == len(shards)
    conn = sqlite3.connect(db)
    methods = conn.execute('SELECT COUNT(*) FROM method').fetchone()[0]
    conn.close()
    assert info['methods'] == methods
    total = 0
    for path in shards:
        shard = sqlite3.connect(path)
        total += shard.execute(f'SELECT COUNT(*) FROM {FTS_TABLE}').fetchone()[0]
        shard.close()
    assert total == methods
