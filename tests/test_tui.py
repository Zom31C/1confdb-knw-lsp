"""Тесты вспомогательных функций консольного интерфейса."""
import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.tui import (Tui, _unquote, first_db,  # noqa: E402
                        group_cli_args, load_groups, replace_server_dbs,
                        resolve_launch_groups)


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


# ---------- запуск сервера с несколькими группами ----------

def test_group_cli_args_one_flag_per_base():
    # по флагу на базу: ';' в имени файла Windows допустим, а повтор --group
    # сервер складывает в ту же группу
    assert group_cli_args([('УНФ', ['D:\\a.db', 'D:\\b.db']),
                           ('БП', ['D:\\c.db'])]) == [
        '--group', 'УНФ=D:\\a.db', '--group', 'УНФ=D:\\b.db',
        '--group', 'БП=D:\\c.db']
    assert group_cli_args([]) == [] and group_cli_args(None) == []


def test_first_db_prefers_the_first_group():
    assert first_db(['D:\\отдельная.db'],
                    [('УНФ', ['D:\\a.db', 'D:\\b.db'])]) == 'D:\\a.db'
    assert first_db(['D:\\отдельная.db'], []) == 'D:\\отдельная.db'
    assert first_db([], [('УНФ', [])]) is None


def test_resolve_launch_groups_keeps_order_and_reports_missing(tmp_path):
    good = tmp_path / 'a.db'
    good.write_text('')
    config = {'УНФ': [str(good), str(tmp_path / 'нет.db')],
              'БП': [str(tmp_path / 'тоже-нет.db')]}
    groups, missing = resolve_launch_groups(config, ['УНФ', 'БП'])
    # группа, в которой нет ни одной доступной базы, не запускается
    assert groups == [('УНФ', [str(good)])]
    assert len(missing) == 2 and str(good) not in missing
    assert resolve_launch_groups(config, []) == ([], [])


def _pick(monkeypatch, groups_config, answers, entries=()):
    """Прогоняет _pick_launch_set на файлах-заглушках и заданных ответах."""
    tui = _Tui(list(entries))
    tui.groups = dict(groups_config)
    monkeypatch.setattr('confdb.tui._cls', lambda: None)
    _feed(monkeypatch, answers)
    return tui._pick_launch_set()


def test_pick_launch_set_several_groups(monkeypatch, tmp_path):
    a, b = tmp_path / 'a.db', tmp_path / 'b.db'
    a.write_text(''), b.write_text('')
    dbs, groups, title, missing = _pick(
        monkeypatch, {'УНФ': [str(a)], 'БП': [str(b)]}, ['2', '1', ''])
    assert dbs == [] and missing == []
    # порядок выбора, а не порядок в конфиге: первая группа станет активной
    assert [name for name, _ in groups] == ['БП', 'УНФ']
    assert title == 'группы «БП», «УНФ»'


def test_pick_launch_set_reorders_groups_and_takes_loose_dbs(monkeypatch,
                                                             tmp_path):
    a, b, c = tmp_path / 'a.db', tmp_path / 'b.db', tmp_path / 'c.db'
    for p in (a, b, c):
        p.write_text('')
    # 1 и 2 — отметили обе группы, *2 — сделали второй выбор первым,
    # m + 1 + Enter — добавили отдельную базу, Enter — запустить
    dbs, groups, title, missing = _pick(
        monkeypatch, {'УНФ': [str(a)], 'БП': [str(b), str(c)]},
        ['1', '2', '*2', 'm', '1', '', ''], entries=[str(c)])
    assert [name for name, _ in groups] == ['БП', 'УНФ']
    assert groups[0][1] == [str(b), str(c)]
    assert dbs == [str(c)] and missing == []
    assert title == 'группы «БП», «УНФ»; отдельные базы: c.db'


def test_pick_launch_set_reports_missing_bases(monkeypatch, tmp_path):
    a = tmp_path / 'a.db'
    a.write_text('')
    dbs, groups, title, missing = _pick(
        monkeypatch,
        {'УНФ': [str(a), str(tmp_path / 'нет.db')],
         'Пустая': [str(tmp_path / 'тоже-нет.db')]},
        ['1', '2', ''])
    assert [name for name, _ in groups] == ['УНФ']
    assert groups[0][1] == [str(a)] and len(missing) == 2
    assert title == 'группы «УНФ»'


def test_pick_launch_set_cancel_and_empty_choice(monkeypatch):
    assert _pick(monkeypatch, {}, ['0']) == ([], [], '', [])
    # Enter без выбора не запускает пустой сервер — спрашивает снова
    assert _pick(monkeypatch, {'УНФ': ['D:\\a.db']}, ['', '0']) == (
        [], [], '', [])


def _real_db(tmp_path_factory, name):
    from confdb.db.writer import write_db

    from test_writer import make_dump
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / name)
    write_db(dump, db, source_file=name + '.cf')
    return db


def test_apply_config_group_adds_without_touching_other_groups(tmp_path_factory):
    from confdb.mcp_server import McpServer
    first = _real_db(tmp_path_factory, 'первая.sqlite')
    second = _real_db(tmp_path_factory, 'вторая.sqlite')
    server = McpServer([first])
    tui = _Tui()
    name, aliases, errors = tui._apply_config_group(server, 'БП', [second])
    assert (name, aliases, errors) == ('БП', ['вторая'], [])
    # прежняя база осталась открытой и активной
    assert list(server.dbs) == ['первая', 'вторая']
    assert server.active == 'первая' and server.active_group == 'БП'
    # недоступный файл — ошибка, а не обрыв
    name, aliases, errors = tui._apply_config_group(
        server, 'Битая', ['D:\\нет\\такой.db'])
    assert (name, aliases) == ('Битая', []) and len(errors) == 1


def test_apply_config_group_replace_drops_the_rest(tmp_path_factory):
    from confdb.mcp_server import McpServer
    first = _real_db(tmp_path_factory, 'первая.sqlite')
    second = _real_db(tmp_path_factory, 'вторая.sqlite')
    server = McpServer(groups=[('УНФ', [first]), ('БП', [second])])
    tui = _Tui()
    name, aliases, errors = tui._apply_config_group(
        server, 'БП', [second], replace=True)
    assert (name, aliases, errors) == ('БП', ['вторая'], [])
    assert list(server.groups) == ['БП'] and list(server.dbs) == ['вторая']
    assert server.active == 'вторая' and server.active_group == 'БП'
