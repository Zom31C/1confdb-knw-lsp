"""Тесты рабочего каталога извлечения: параллельное удаление и выбор тома."""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.db.writer import write_db  # noqa: E402
from confdb.extract import (check_same_source, file_sha256, make_temp_dir,  # noqa: E402
                            remove_tree, source_info)

from test_writer import make_dump  # noqa: E402


def _tree(root):
    """Дерево в два уровня вложенности: каталоги с файлами и файл в корне."""
    for sub in ('a', os.path.join('a', 'b')):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
        for i in range(2):
            with open(os.path.join(root, sub, f'f{i}.txt'), 'w', encoding='utf-8') as f:
                f.write('данные')
    with open(os.path.join(root, 'top.txt'), 'w', encoding='utf-8') as f:
        f.write('данные')


def _same(path, expected):
    return os.path.normcase(path) == os.path.normcase(expected)


def test_remove_tree_deletes_everything(tmp_path):
    root = os.path.join(str(tmp_path), 'work')
    _tree(root)
    remove_tree(root, workers=4)
    assert not os.path.exists(root)


def test_remove_tree_missing_path_is_not_an_error(tmp_path):
    remove_tree(os.path.join(str(tmp_path), 'нет'))


def test_make_temp_dir_uses_target_volume(tmp_path):
    temp = make_temp_dir(os.path.join(str(tmp_path), 'база.db'))
    try:
        assert _same(os.path.dirname(temp), str(tmp_path))
        assert os.path.basename(temp).startswith('confdb_')
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_make_temp_dir_without_target_uses_os_temp():
    temp = make_temp_dir()
    try:
        assert _same(os.path.dirname(temp), tempfile.gettempdir())
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_make_temp_dir_falls_back_when_target_unavailable(tmp_path):
    # каталог результата ещё не создан — mkdtemp(dir=…) падает, остаётся %TEMP%
    temp = make_temp_dir(os.path.join(str(tmp_path), 'нет', 'база.db'))
    try:
        assert _same(os.path.dirname(temp), tempfile.gettempdir())
    finally:
        shutil.rmtree(temp, ignore_errors=True)


# -- отпечаток исходного файла ------------------------------------------------

OLD_SOURCE = ('CREATE TABLE source (id INTEGER PRIMARY KEY AUTOINCREMENT, '
              'file TEXT NOT NULL, created TEXT NOT NULL, root_type TEXT, '
              'root_name TEXT, root_uuid TEXT)')
NEW_SOURCE = OLD_SOURCE[:-1] + ', file_size INTEGER, file_sha256 TEXT)'


def _cf(tmp_path, name='config.cf', data=b'1CV8' + b'x' * 4096):
    """Файл-«исходник»: для отпечатка важно только содержимое."""
    path = os.path.join(str(tmp_path), name)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def _db_with_source(tmp_path, src, sha=None, size=None, old=False):
    """База с одной строкой source — новой схемы (с отпечатком) или старой."""
    db = os.path.join(str(tmp_path), 'base.sqlite')
    conn = sqlite3.connect(db)
    conn.execute(OLD_SOURCE if old else NEW_SOURCE)
    if old:
        conn.execute('INSERT INTO source (file, created) VALUES (?, ?)',
                     (src, '2026-10-01T00:00:00'))
    else:
        conn.execute('INSERT INTO source (file, created, file_size, file_sha256) '
                     'VALUES (?, ?, ?, ?)',
                     (src, '2026-10-05T00:00:00', size, sha))
    conn.commit()
    conn.close()
    return db


def test_file_sha256_hashes_the_whole_file(tmp_path):
    src = _cf(tmp_path)
    with open(src, 'rb') as fh:
        assert file_sha256(src) == hashlib.sha256(fh.read()).hexdigest()


def test_write_db_stores_the_fingerprint(tmp_path):
    """Полный путь: write_db пишет отпечаток, source_info его читает."""
    dump = os.path.join(str(tmp_path), 'dump')
    make_dump(dump)
    db = os.path.join(str(tmp_path), 't.sqlite')
    write_db(dump, db, source_file='t.cf', source_sha256='ab' * 32,
             source_size=885012556)
    info = source_info(db)
    assert info['file'] == 't.cf'
    assert info['file_sha256'] == 'ab' * 32
    assert info['file_size'] == 885012556


def test_source_info_of_an_old_base_has_no_fingerprint(tmp_path):
    """В базах до появления отпечатка колонок нет — это «неизвестен», не ошибка."""
    src = _cf(tmp_path)
    info = source_info(_db_with_source(tmp_path, src, old=True))
    assert info['file'] == src
    assert info['file_sha256'] is None and info['file_size'] is None
    # нет файла / это не база знаний
    assert source_info(os.path.join(str(tmp_path), 'нет-такой.sqlite')) is None
    foreign = os.path.join(str(tmp_path), 'foreign.db')
    with open(foreign, 'wb') as fh:
        fh.write('не база знаний'.encode('utf-8'))
    assert source_info(foreign) is None


def test_check_same_source_skips_a_duplicate_extraction(tmp_path):
    """Та же конфигурация уже извлечена — распаковка не нужна, --force её возвращает."""
    src = _cf(tmp_path)
    db = _db_with_source(tmp_path, src, sha=file_sha256(src),
                         size=os.path.getsize(src))
    said = []
    sha, skip = check_same_source(src, db, log=said.append)
    assert sha == file_sha256(src)
    assert skip is True
    assert any('уже собрана из этого же файла' in line for line in said)
    assert any('--force' in line for line in said)
    # принудительно — извлекаем, тот же отпечаток возвращается
    forced, skip_forced = check_same_source(src, db, force=True,
                                            log=lambda *_: None)
    assert (forced, skip_forced) == (sha, False)


def test_check_same_source_warns_about_another_file(tmp_path):
    src = _cf(tmp_path)
    other = _cf(tmp_path, name='other.cf', data=b'1CV8' + b'y' * 4096)
    db = _db_with_source(tmp_path, other, sha=file_sha256(other),
                         size=os.path.getsize(other))
    said = []
    sha, skip = check_same_source(src, db, log=said.append)
    assert skip is False and sha == file_sha256(src)
    assert any('из другого файла' in line for line in said)


def test_check_same_source_without_a_fingerprint_extracts(tmp_path):
    """Старая база или новая цель — сравнивать не с чем, извлекаем как раньше."""
    src = _cf(tmp_path)
    old = _db_with_source(tmp_path, src, old=True)
    assert check_same_source(src, old, log=lambda *_: None)[1] is False
    fresh = os.path.join(str(tmp_path), 'new.sqlite')
    assert check_same_source(src, fresh, log=lambda *_: None)[1] is False

