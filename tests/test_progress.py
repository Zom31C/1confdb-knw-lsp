"""Тесты индикатора прогресса: проценты обновлением одной строки (\\r)."""
import io

from confdb.v8 import progress
from confdb.v8.progress import Progress


def test_updates_in_place_without_newlines():
    out = io.StringIO()
    bar = Progress('Тест', 100, enabled=True, stream=out)
    bar.update(50, 'a')
    bar._last = 0  # сброс троттлинга для детерминизма
    bar.update(50, 'b')
    text = out.getvalue()
    assert text.count('\n') == 0
    assert text.count('\r') == 2
    assert '50%' in text and '100%' in text


def test_finish_forces_100_and_newline():
    out = io.StringIO()
    bar = Progress('Тест', 100, enabled=True, stream=out)
    bar.update(30)
    bar.finish()
    text = out.getvalue()
    assert text.endswith('\n')
    assert '100%' in text


def test_disabled_writes_nothing():
    out = io.StringIO()
    bar = Progress('Тест', 100, enabled=False, stream=out)
    bar.update(50)
    bar.finish()
    assert out.getvalue() == ''


def test_note_read_without_active_progress():
    assert progress.note_read('нет-такого-файла') is None
