"""Тесты поддержки длинных путей Windows (MAX_PATH)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.v8.helper import long_path  # noqa: E402


def test_long_path_windows():
    """На Windows long_path добавляет префикс \\\\?\\ к абсолютным путям."""
    if os.name != 'nt':
        return  # Пропускаем на не-Windows системах
    
    # Относительный путь -> абсолютный с префиксом
    result = long_path('test.txt')
    assert result.startswith('\\\\?\\')
    assert os.path.isabs(result[4:])  # После префикса путь абсолютный
    
    # Абсолютный путь -> с префиксом
    abs_path = os.path.abspath('test.txt')
    result = long_path(abs_path)
    assert result.startswith('\\\\?\\')
    assert result[4:] == abs_path
    
    # Путь уже с префиксом -> не добавляем второй раз
    prefixed = '\\\\?\\C:\\test.txt'
    result = long_path(prefixed)
    assert result == prefixed
    assert result.count('\\\\?\\') == 1


def test_long_path_non_windows():
    """На не-Windows системах long_path возвращает путь без изменений."""
    if os.name == 'nt':
        return  # Пропускаем на Windows
    
    # На Linux/Mac префикс не добавляется
    result = long_path('/tmp/test.txt')
    assert result == '/tmp/test.txt'
    assert not result.startswith('\\\\?\\')


def test_long_path_with_real_file(tmp_path):
    """Проверяем, что файл можно создать и прочитать с длинным путём."""
    if os.name != 'nt':
        return  # Пропускаем на не-Windows системах
    
    # Создаём файл с обычным путём
    test_file = tmp_path / 'test.txt'
    test_file.write_text('test content', encoding='utf-8')
    
    # Читаем через long_path
    long_file_path = long_path(str(test_file))
    assert long_file_path.startswith('\\\\?\\')
    
    # Файл должен читаться
    with open(long_file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert content == 'test content'
