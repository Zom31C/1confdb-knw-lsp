"""Тесты проверки запросов в модулях (query_check) для LSP-диагностики."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.__main__ import main  # noqa: E402
from confdb.db.writer import write_db  # noqa: E402
from confdb.query_check import check_db, extract_literals  # noqa: E402

from test_writer import make_dump  # noqa: E402


def test_extract_literals():
    body = ('А = "один"; // коммент "не литерал"\n'
            'Б = "два""три";\n'
            'В = "много\n'
            '|строк";\n'
            'Г = "незакрыт\n'
            'Д = 1;')
    lits = extract_literals(body)
    assert [text for _, _, text in lits] == ['один', 'два"три', 'много\nстрок']
    assert lits[0][0] == body.index('"один"')
    assert lits[0][1] == body.index('"один"') + len('"один"')


def test_check_db(tmp_path):
    dump = tmp_path / 'dump'
    make_dump(str(dump))
    module_text = (
        'Процедура Тест()\n'                                       # 0
        '\tЗапрос = Новый Запрос;\n'                               # 1
        '\tЗапрос.Текст = "ВЫБРАТЬ\n'                              # 2
        '\t|Ссылка\n'                                              # 3
        '\t|ИЗ Справочник.Справочник1";\n'                         # 4  (валидный)
        '\tЗапрос.Текст = "ВЫБРАТЬ 1 ИЗ Справочник.Несуществует";\n'  # 5
        '\tЗапрос.Текст = "ВЫБРАТЬ 1 ИЗ";\n'                          # 6
        'КонецПроцедуры\n')                                        # 7
    with open(os.path.join(dump, 'Catalog', 'Справочник1', 'Catalog.obj.bsl'),
              'w', encoding='utf-8') as f:
        f.write(module_text)
    db = str(tmp_path / 'test.db')
    write_db(str(dump), db)
    stats = check_db(db)
    assert stats['bodies'] >= 1 and stats['violations'] == 2
    conn = sqlite3.connect(db)
    rows = conn.execute('SELECT path, line, col, message FROM query_violation '
                        'ORDER BY line').fetchall()
    conn.close()
    assert len(rows) == 2
    assert rows[0][0].endswith('Catalog.obj.bsl')
    assert rows[0][1] == 5  # запрос с несуществующей таблицей
    assert rows[1][1] == 6  # синтаксическая ошибка
    assert 'синтаксис' in rows[1][3]


def test_cli_check_queries(tmp_path, capsys):
    dump = tmp_path / 'dump'
    make_dump(str(dump))
    db = str(tmp_path / 'test.db')
    write_db(str(dump), db)
    assert main(['check-queries', db]) == 0
    assert 'нарушений сохранено' in capsys.readouterr().out
    assert main(['check-queries', str(tmp_path / 'нет.db')]) == 2
