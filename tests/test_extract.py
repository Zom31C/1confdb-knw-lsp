"""Тесты рабочего каталога извлечения: параллельное удаление и выбор тома."""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.extract import make_temp_dir, remove_tree  # noqa: E402


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
