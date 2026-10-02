"""Тесты вспомогательных функций консольного интерфейса."""
import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.tui import (Tui, _unquote, load_groups,  # noqa: E402
                        replace_server_dbs)


def test_unquote_stips_surrounding_quotes():
    assert _unquote('"D:\\base\\x.db"') == 'D:\\base\\x.db'
    assert _unquote('  "C:\\tmp"  ') == 'C:\\tmp'


def test_unquote_keeps_plain_paths():
    assert _unquote('D:\\base\\x.db') == 'D:\\base\\x.db'
    assert _unquote('') == ''


def test_load_groups_prefers_groups_over_packs():
    config = {'groups': {'а': ['1.db']}, 'packs': {'б': ['2.db']}}
    assert load_groups(config) == {'а': ['1.db']}


def test_load_groups_migrates_packs_when_no_groups():
    assert load_groups({'packs': {'б': ['2.db']}}) == {'б': ['2.db']}
    assert load_groups({}) == {}


def test_load_groups_skips_bad_entries():
    config = {'groups': {'ок': ['1.db'], 'плохая': 'не список'}}
    assert load_groups(config) == {'ок': ['1.db']}
    assert load_groups({'groups': 'мусор'}) == {}


class _FakeServer:
    """Минимальная копия реестра баз McpServer для проверки перезагрузки."""

    def __init__(self, aliases):
        self.dbs = dict.fromkeys(aliases)
        self.active = aliases[0] if aliases else None
        self.closed = []

    def close_db(self, alias):
        del self.dbs[alias]
        self.closed.append(alias)
        if self.active == alias:
            self.active = next(iter(self.dbs), None)
        return alias

    def open_db(self, path, activate=True):
        if 'битая' in path:
            raise ValueError(f'файл базы не найден: {path}')
        alias = os.path.splitext(os.path.basename(path))[0]
        self.dbs[alias] = {}
        if activate or self.active is None:
            self.active = alias
        return alias


def test_replace_server_dbs_closes_old_and_opens_new():
    server = _FakeServer(['старая'])
    opened, errors = replace_server_dbs(
        server, [os.path.join('x', 'первая.sqlite'),
                 os.path.join('x', 'вторая.db')])
    assert opened == ['первая', 'вторая']
    assert errors == []
    assert server.closed == ['старая']
    assert server.active == 'первая'  # первая открытая — активная


def test_replace_server_dbs_collects_errors():
    server = _FakeServer([])
    opened, errors = replace_server_dbs(
        server, ['битая.db', os.path.join('x', 'целая.sqlite')])
    assert opened == ['целая']
    assert len(errors) == 1 and 'не найден' in errors[0]
    assert server.active == 'целая'


# ---------- выбор пути: номер — пункт, остальной текст — путь ----------

class _Tui(Tui):
    """Tui без чтения ~/.confdb и без поиска баз по диску: список детерминирован."""

    def __init__(self, db_entries=()):
        self.last_db = ''
        self.recent_db = []
        self.db_dirs = []
        self._entries = list(db_entries)

    def _db_candidates(self):
        return list(self._entries)


@pytest.fixture
def tui(monkeypatch):
    monkeypatch.setattr('confdb.tui._cls', lambda: None)
    return _Tui()


def _feed(monkeypatch, answers):
    """Подменяет input(): ответы по очереди, без завершающего вопроса."""
    left = list(answers)
    monkeypatch.setattr(builtins, 'input',
                        lambda prompt='': left.pop(0) if left else '0')


def test_pick_path_number_selects_entry(tui, monkeypatch):
    _feed(monkeypatch, ['2'])
    assert tui._pick_path('Файл', ['D:\\a.cf', 'D:\\b.cfe'], []) == 'D:\\b.cfe'


def test_pick_path_typed_string_is_a_path(tui, monkeypatch):
    # путь можно набрать сразу, без предварительного «p»
    _feed(monkeypatch, ['D:\\cf\\Новая.cf'])
    assert tui._pick_path('Файл', ['D:\\a.cf'], []) == 'D:\\cf\\Новая.cf'


def test_pick_path_typed_path_is_unquoted(tui, monkeypatch):
    _feed(monkeypatch, ['"D:\\cf\\с пробелом.epf"'])
    assert tui._pick_path('Файл', [], []) == 'D:\\cf\\с пробелом.epf'


def test_pick_path_out_of_range_number_is_not_a_path(tui, monkeypatch):
    _feed(monkeypatch, ['9', '0'])
    assert tui._pick_path('Файл', ['D:\\a.cf'], []) is None


def test_pick_path_empty_input_reprompts(tui, monkeypatch):
    # случайный Enter не должен возвращать пустой путь
    _feed(monkeypatch, ['', '1'])
    assert tui._pick_path('Файл', ['D:\\a.cf'], []) == 'D:\\a.cf'


def test_pick_path_commands_still_work(tui, monkeypatch):
    _feed(monkeypatch, ['p', 'D:\\cf\\ЧерезP.cf'])
    assert tui._pick_path('Файл', [], []) == 'D:\\cf\\ЧерезP.cf'


def test_pick_path_empty_option(tui, monkeypatch):
    _feed(monkeypatch, ['e'])
    assert tui._pick_path('Дамп', [], [], allow_empty=True,
                          empty_hint='не сохранять') == ''


def test_pick_db_typed_path_may_not_exist_yet(monkeypatch):
    tui = _Tui(['D:\\a.db', 'D:\\b.db'])
    _feed(monkeypatch, ['D:\\_out\\новая.sqlite'])
    # сюда же задают базу, которую извлечение только создаст
    assert tui._pick_db('База') == 'D:\\_out\\новая.sqlite'


def test_pick_db_number_selects_from_candidates_and_recents(monkeypatch):
    tui = _Tui(['D:\\a.db', 'D:\\b.db'])
    _feed(monkeypatch, ['2'])
    assert tui._pick_db('База') == 'D:\\b.db'


def test_pick_db_empty_input_reprompts(monkeypatch):
    tui = _Tui(['D:\\a.db'])
    _feed(monkeypatch, ['', '1'])
    assert tui._pick_db('База') == 'D:\\a.db'


def test_pick_db_empty_option(monkeypatch):
    tui = _Tui(['D:\\a.db'])
    _feed(monkeypatch, ['e'])
    assert tui._pick_db('База', allow_empty=True, empty_hint='не писать') == ''
