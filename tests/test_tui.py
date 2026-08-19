"""Тесты вспомогательных функций консольного интерфейса."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.tui import _unquote  # noqa: E402


def test_unquote_stips_surrounding_quotes():
    assert _unquote('"D:\\base\\x.db"') == 'D:\\base\\x.db'
    assert _unquote('  "C:\\tmp"  ') == 'C:\\tmp'


def test_unquote_keeps_plain_paths():
    assert _unquote('D:\\base\\x.db') == 'D:\\base\\x.db'
    assert _unquote('') == ''
