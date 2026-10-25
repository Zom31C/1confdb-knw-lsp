"""Тесты compare.py: снимки, структурированные различия, пагинация, config_diff.

Закрывают приёмку feedback п.1 (P0): объединение страниц содержит все
различия без пропусков и дублей; контракт чтения XDTO не падает на распаковке.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb import compare  # noqa: E402
from confdb.db.writer import write_db  # noqa: E402

from test_writer import make_dump  # noqa: E402


def _db(tmp_path_factory, name):
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / name)
    write_db(dump, db, source_file=name + '.cf')
    return db


def _oid(conn, path):
    return conn.execute('SELECT id FROM meta_object WHERE path=?',
                        (path,)).fetchone()[0]


def _add_attrs(conn, path, count, prefix='лст_Реквизит'):
    """Добавить объекту count новых реквизитов — для тестов пагинации."""
    oid = _oid(conn, path)
    for i in range(count):
        conn.execute(
            'INSERT INTO meta_attribute (object_id, ord, name, type_str)'
            ' VALUES (?, ?, ?, ?)',
            (oid, 100 + i, f'{prefix}{i:02d}', 'Строка'))
    conn.commit()


def _snapshots(tmp_path_factory, mutate_right):
    """Две базы из одного дампа; правая изменена mutate_right(conn)."""
    left_db = _db(tmp_path_factory, 'left.sqlite')
    right_db = _db(tmp_path_factory, 'right.sqlite')
    conn_l = sqlite3.connect(left_db)
    conn_r = sqlite3.connect(right_db)
    mutate_right(conn_r)
    left = compare.object_snapshot(conn_l, 'Catalog/Справочник1')
    right = compare.object_snapshot(conn_r, 'Catalog/Справочник1')
    return left, right


def test_compute_diff_items_are_structured_and_rich(tmp_path_factory):
    def mutate(conn):
        _add_attrs(conn, 'Catalog/Справочник1', 1)
        conn.execute("UPDATE meta_attribute SET type_str='Число' "
                     "WHERE name='СсылкаАтрибут'")
        conn.commit()

    left, right = _snapshots(tmp_path_factory, mutate)
    items = compare.compute_diff(left, right, 'старая', 'новая')
    by_id = {it.id: it for it in items}
    # стабильные идентификаторы «секция:ключ» и вид различия
    added = by_id['attrs:лст_Реквизит00']
    assert added.section == 'attrs' and added.kind == 'only_right'
    assert ('только в базе «новая»: лст_Реквизит00: Строка'
            in added.description)
    changed = by_id['attrs:СсылкаАтрибут']
    assert changed.kind == 'changed'
    assert changed.description.startswith('  отличается СсылкаАтрибут: тип ')
    assert '-> Число' in changed.description


def test_format_diff_pages_cover_every_item_once(tmp_path_factory):
    def mutate(conn):
        _add_attrs(conn, 'Catalog/Справочник1', 45)

    left, right = _snapshots(tmp_path_factory, mutate)
    items = compare.compute_diff(left, right, 'старая', 'новая')
    assert len(items) == 45  # старый потолок MAX_ITEMS=40 больше не режет
    descs = [it.description for it in items]

    collected = []
    offset = 0
    while offset < len(items):
        page = compare.format_diff(items, limit=10, offset=offset)
        body = [line for line in page
                if not line.startswith('…') and not line.startswith('offset=')]
        collected.extend(body)
        offset += 10
    # приёмка: объединение страниц = все различия, без пропусков и дублей
    assert collected == descs

    first = compare.format_diff(items, limit=10)
    assert first[-1] == ('… показано 1–10 из 45 различий; есть ещё 35 — '
                         'следующий вызов с offset=10')
    last = compare.format_diff(items, limit=10, offset=40)
    assert last[-1] == '… показано 41–45 из 45 различий; это все различия'
    beyond = compare.format_diff(items, limit=10, offset=100)
    assert beyond == ['offset=100 за пределами списка: всего различий 45 — '
                      'повторите с offset=0']
    assert compare.format_diff([], limit=10) == []


def test_xdto_snapshot_reads_types_properties_and_nesting(tmp_path_factory):
    """Регрессия P0-1: чтение состава пакета не падает на распаковке строк."""
    db = _db(tmp_path_factory, 'xdto.sqlite')
    conn = sqlite3.connect(db)
    oid = _oid(conn, 'Catalog/Справочник1')
    # id не задаём: в дампе уже есть XDTO-пакет со своими строками
    cur = conn.execute(
        'INSERT INTO xdto_type (object_id, ord, name, kind, base,'
        " facets, enum_values) VALUES (?, 0, 'Тип1', 'С', 'xs:string',"
        " 'Длина=10', NULL)", (oid,))
    tid1 = cur.lastrowid
    cur = conn.execute(
        'INSERT INTO xdto_type (object_id, ord, name, kind)'
        " VALUES (?, 1, NULL, 'С')", (oid,))  # вложенный анонимный тип
    tid2 = cur.lastrowid
    conn.execute(
        'INSERT INTO xdto_property (type_id, object_id, ord, name, type,'
        ' lower_bound, upper_bound, nillable, form, nested_type_id, extra)'
        " VALUES (?, ?, 0, 'Свойство1', 'xs:string', 1, 1, 0, 'Элемент',"
        " NULL, 'key=value')", (tid1, oid))
    conn.execute(
        'INSERT INTO xdto_property (type_id, object_id, ord, name, type,'
        ' lower_bound, upper_bound, nillable, form, nested_type_id, extra)'
        " VALUES (?, ?, 1, 'Вложенное', NULL, 1, 1, 0, 'Элемент', ?,"
        " 'nested')", (tid1, oid, tid2))
    conn.execute(
        'INSERT INTO xdto_property (type_id, object_id, ord, name, type,'
        ' lower_bound, upper_bound, nillable, form, nested_type_id, extra)'
        " VALUES (NULL, ?, 0, 'СвойствоПакета', 'xs:int', 1, 1, 0,"
        " 'Атрибут', NULL, '')", (oid,))
    conn.commit()

    snap = compare.object_snapshot(conn, 'Catalog/Справочник1')
    assert 'Тип1' in snap['xdto']
    assert '(свойства пакета)' in snap['xdto']
    base, sig = snap['xdto']['Тип1']
    assert base == 'xs:string' and len(sig) == 40

    # изменение свойства видно как изменение именованного типа
    conn.execute("UPDATE xdto_property SET lower_bound=0 WHERE name='Свойство1'")
    conn.commit()
    snap2 = compare.object_snapshot(conn, 'Catalog/Справочник1')
    items = compare.compute_diff(snap, snap2, 'до', 'после')
    assert any(it.id == 'xdto:Тип1' and it.kind == 'changed' for it in items)


def test_config_diff_summary_and_changed_objects(tmp_path_factory):
    left_db = _db(tmp_path_factory, 'cl.sqlite')
    right_db = _db(tmp_path_factory, 'cr.sqlite')
    conn_r = sqlite3.connect(right_db)
    oid = _oid(conn_r, 'Catalog/Справочник1')
    conn_r.execute("INSERT INTO module (object_id, code_name, context, body)"
                   " VALUES (?, 'mgr', '', '')", (oid,))
    conn_r.execute(
        'INSERT INTO meta_object (source_id, ord, path, type, name, type_ru,'
        " header_json) VALUES (1, 99, 'Catalog/Новый', 'Catalog', 'Новый',"
        " 'Справочник', '{}')")
    conn_r.commit()
    conn_l = sqlite3.connect(left_db)

    res = compare.config_diff(conn_l, conn_r, 'левая', 'правая')
    assert res['summary']['only_right'] == 1
    assert res['summary']['only_left'] == 0
    assert res['summary']['changed'] == 1
    assert res['changed_objects'][0]['path'] == 'Catalog/Справочник1'
    assert res['changed_objects'][0]['modules_diff'] == 1
    assert res['total_changed'] == 1


# -- точный diff метода (feedback п.2) ---------------------------------------

def _row(kind='процедура', name='М', sig='', dirs=None, desc='', body='',
         exp=0, ctx='', start=1):
    """Строка метода в формате McpServer._method_row."""
    return (kind, name, sig, dirs, desc, body, exp, ctx, start)


def test_normalized_lines_preserves_literals():
    lines = compare.normalized_lines(
        '    Сообщить("http://a"); // x\n'
        '    С = "а б";\n'
        '    // чистый комментарий\n'
        '\n'
        '    #Если Сервер\n')
    assert lines[0] == 'Сообщить("http://a");'
    assert lines[1] == 'С="а б";'
    assert lines[2] is None and lines[3] is None
    assert lines[4] == '#ЕслиСервер'
    # разные литералы не равны — старая нормализация резала их по '//'
    # и стирала пробелы внутри строк
    assert compare.normalized_lines('A = "http://a";') != \
        compare.normalized_lines('A = "http://b";')
    assert compare.normalized_lines('С = "а б";') != \
        compare.normalized_lines('С = "аб";')
    # пробелы и концевой комментарий вне литерала изменением не считаются
    assert compare.normalized_lines('  А = 1;  // пояснение') == \
        compare.normalized_lines('А=1;')


def test_method_compare_export_only_is_not_a_body_change():
    """Приёмка feedback п.2: добавление только Экспорт не меняет тело."""
    body_l = 'Процедура М()\n    А = 1;\nКонецПроцедуры'
    body_r = 'Процедура М() Экспорт\n    А = 1;\nКонецПроцедуры'
    res = compare.method_compare(_row(body=body_l, exp=0),
                                 _row(body=body_r, exp=1))
    assert res['export'] == (0, 1)
    assert res['body_diff'] == []
    assert not res['identical']
    text = '\n'.join(compare.format_method_compare(res))
    assert 'Экспорт: не задан -> задан' in text
    assert 'Исполняемый текст: без изменений' in text


def test_method_compare_body_diff_numbers_and_single_context():
    body_l = '\n'.join(['Процедура М()', '    // комментарий слева',
                        '    А = 1;', '    Б = 2;', '    В = 3;',
                        'КонецПроцедуры'])
    body_r = '\n'.join(['Процедура М()', '    // комментарий справа',
                        '    А = 1;', '    Б = 200;', '    В = 3;',
                        'КонецПроцедуры'])
    res = compare.method_compare(_row(body=body_l, start=10),
                                 _row(body=body_r, start=20))
    diff = res['body_diff']
    minus = [ln for ln in diff if ln.startswith('-')]
    plus = [ln for ln in diff if ln.startswith('+')]
    ctx = [ln for ln in diff if ln.startswith('  ')]
    assert len(minus) == 1 and 'Б = 2;' in minus[0]
    assert len(plus) == 1 and 'Б = 200;' in plus[0]
    # номера строк МОДУЛЯ каждой стороны: 10+3 слева, 20+3 справа
    assert '13' in minus[0] and '23' in plus[0]
    # контекст общий: каждая строка один раз (старая реализация дублировала)
    assert ctx and len(ctx) == len(set(ctx))
    # комментарий ушёл в свой раздел, а не в исполняемый diff
    assert res['comments_changed']
    assert not any('комментарий' in ln for ln in diff)


def test_method_compare_literals_are_compared():
    body_l = 'Функция М()\n    Возврат "http://a";\nКонецФункции'
    body_r = 'Функция М()\n    Возврат "http://b";\nКонецФункции'
    res = compare.method_compare(_row(kind='функция', body=body_l),
                                 _row(kind='функция', body=body_r))
    assert res['body_diff']
    assert any('"http://a"' in ln for ln in res['body_diff'])
    assert any('"http://b"' in ln for ln in res['body_diff'])


def test_method_compare_comment_only_and_formatting_only():
    body_l = 'Процедура М()\n    // старый\n    А = 1;\nКонецПроцедуры'
    body_r = 'Процедура М()\n    // новый\n    А = 1;\nКонецПроцедуры'
    res = compare.method_compare(_row(body=body_l), _row(body=body_r))
    assert res['body_diff'] == [] and res['comments_changed']
    assert not res['identical']
    text = '\n'.join(compare.format_method_compare(res))
    assert 'Комментарии и описание:' in text and '- // старый' in text

    fmt_l = 'Процедура М()\n    А = 1;\nКонецПроцедуры'
    fmt_r = 'Процедура М()\n    А  =  1;\nКонецПроцедуры'
    res2 = compare.method_compare(_row(body=fmt_l), _row(body=fmt_r))
    assert res2['body_diff'] == [] and res2['formatting_only']
    assert 'только форматирование' in '\n'.join(
        compare.format_method_compare(res2))


def test_method_compare_params_defaults_and_directives():
    res = compare.method_compare(
        _row(sig='А, Знач Б = Неопределено', dirs='&НаСервере'),
        _row(sig='А = 1, В', dirs='&НаКлиенте'))
    p = res['params']
    assert p['added'] == ['В'] and p['removed'] == ['Б']
    assert p['defaults'] == [('А', None, '1')]
    assert res['directives'] == (['&НаСервере'], ['&НаКлиенте'])
    text = '\n'.join(compare.format_method_compare(res))
    assert 'значение по умолчанию А: — -> 1' in text
    assert 'директивы: [&НаСервере] -> [&НаКлиенте]' in text


def test_method_compare_identical():
    body = 'Процедура М()\n    А = 1;\nКонецПроцедуры'
    res = compare.method_compare(_row(body=body), _row(body=body))
    assert res['identical']
    assert compare.format_method_compare(res) == \
        ['Различий нет: методы идентичны']


# -- матрица перехватов расширения (feedback п.3) -----------------------------

def _add_method(conn, path, code_name, name, body, ord_, dirs=None,
                kind='процедура', sig='', exp=0, start=1):
    """Добавить метод в модуль объекта (модуль создаётся при отсутствии)."""
    row = conn.execute(
        'SELECT id FROM module WHERE object_id=(SELECT id FROM meta_object'
        ' WHERE path=?) AND code_name=?', (path, code_name)).fetchone()
    if row:
        mid = row[0]
    else:
        mid = conn.execute(
            'INSERT INTO module (object_id, code_name, context, body)'
            " VALUES ((SELECT id FROM meta_object WHERE path=?), ?, '', '')",
            (path, code_name)).lastrowid
    conn.execute(
        'INSERT INTO method (module_id, ord, kind, name, signature,'
        ' is_export, directives, description, line_start, line_end, body)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (mid, ord_, kind, name, sig, exp, dirs, '', start,
         start + body.count('\n'), body))
    conn.commit()


def test_parse_intercepts():
    assert compare.parse_intercepts('&После("ОбработкаПроведения")') == \
        [('После', 'ОбработкаПроведения')]
    assert compare.parse_intercepts('&НаСервере\n&Вместо("А")') == \
        [('Вместо', 'А')]
    assert compare.parse_intercepts('&ИзменениеИКонтроль("Б")') == \
        [('ИзменениеИКонтроль', 'Б')]
    assert compare.parse_intercepts('&НаКлиенте') == []
    assert compare.parse_intercepts(None) == []


def test_intercept_matrix_unchanged_targets(tmp_path_factory):
    """Приёмка feedback п.3: два неизменившихся обработчика показаны как
    неизменившиеся цели, независимо от изменений остальных методов модуля."""
    old_db = _db(tmp_path_factory, 'old.sqlite')
    new_db = _db(tmp_path_factory, 'new.sqlite')
    ext_db = _db(tmp_path_factory, 'ext.sqlite')
    for db in (old_db, new_db):
        conn = sqlite3.connect(db)
        _add_method(conn, 'Catalog/Справочник1', 'obj', 'ОбработатьРезервы1',
                    'Процедура ОбработатьРезервы1()\n    А = 1;\n'
                    'КонецПроцедуры', 10)
        _add_method(conn, 'Catalog/Справочник1', 'obj', 'ОбработатьРезервы2',
                    'Процедура ОбработатьРезервы2()\n    Б = 2;\n'
                    'КонецПроцедуры', 11)
        _add_method(conn, 'Catalog/Справочник1', 'obj', 'Прочее',
                    'Процедура Прочее()\n    В = 3;\nКонецПроцедуры', 12)
        conn.close()
    # новый релиз: изменился только третий метод модуля
    conn = sqlite3.connect(new_db)
    conn.execute("UPDATE method SET body='Процедура Прочее()\n    В = 300;\n"
                 "КонецПроцедуры' WHERE name='Прочее'")
    conn.commit()
    conn.close()
    # расширение перехватывает оба обработчика
    conn = sqlite3.connect(ext_db)
    _add_method(conn, 'Catalog/Справочник1', 'obj', 'ОбработатьРезервы1',
                'Процедура ОбработатьРезервы1()\n    // вставка\n'
                'КонецПроцедуры', 20, dirs='&После("ОбработатьРезервы1")')
    _add_method(conn, 'Catalog/Справочник1', 'obj', 'ОбработатьРезервы2',
                'Процедура ОбработатьРезервы2()\n    // вставка\n'
                'КонецПроцедуры', 21, dirs='&После("ОбработатьРезервы2")')
    conn.close()

    rows, own, missing = compare.intercept_matrix(
        sqlite3.connect(ext_db), sqlite3.connect(old_db),
        sqlite3.connect(new_db))
    assert len(rows) == 2 and not missing
    assert {r['target'] for r in rows} == {'ОбработатьРезервы1',
                                           'ОбработатьРезервы2'}
    assert all(r['releases'] == 'без изменений' for r in rows)
    # штатный метод без директив — собственный метод расширения
    assert ('Catalog/Справочник1', 'obj', 'Тест', True) in own


def test_intercept_matrix_changed_missing_and_control(tmp_path_factory):
    old_db = _db(tmp_path_factory, 'o.sqlite')
    new_db = _db(tmp_path_factory, 'n.sqlite')
    ext_db = _db(tmp_path_factory, 'e.sqlite')
    for db in (old_db, new_db):
        conn = sqlite3.connect(db)
        _add_method(conn, 'Catalog/Справочник1', 'obj', 'Цель',
                    'Процедура Цель()\n    А = 1;\nКонецПроцедуры', 10)
        _add_method(conn, 'Catalog/Справочник1', 'obj', 'ЦельКонтроль',
                    'Процедура ЦельКонтроль()\n    Х = 1;\n    У = 2;\n'
                    'КонецПроцедуры', 11)
        conn.close()
    conn = sqlite3.connect(new_db)
    conn.execute("UPDATE method SET body='Процедура Цель()\n    А = 100;\n"
                 "КонецПроцедуры' WHERE name='Цель'")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(ext_db)
    _add_method(conn, 'Catalog/Справочник1', 'obj', 'Цель',
                'Процедура Цель()\n    А = 1;\nКонецПроцедуры', 20,
                dirs='&Вместо("Цель")')
    _add_method(conn, 'Catalog/Справочник1', 'obj', 'ЧегоНет',
                'Процедура ЧегоНет()\nКонецПроцедуры', 21,
                dirs='&Перед("НетТакого")')
    _add_method(conn, 'Catalog/Справочник1', 'obj', 'ЦельКонтроль',
                'Процедура ЦельКонтроль()\n    Х = 1;\n    У = 200;\n'
                '    З = 3;\nКонецПроцедуры', 22,
                dirs='&ИзменениеИКонтроль("ЦельКонтроль")')
    conn.close()

    rows, _own, missing = compare.intercept_matrix(
        sqlite3.connect(ext_db), sqlite3.connect(old_db),
        sqlite3.connect(new_db))
    by_target = {r['target']: r for r in rows}
    assert by_target['Цель']['releases'] == 'изменилось: исполняемый текст'
    ctrl = by_target['ЦельКонтроль']
    assert ctrl['releases'] == 'без изменений'
    # вставки/удаления относительно контрольного текста цели
    assert ctrl['control'] == 'тело +2/-1 строк'
    assert [m['target'] for m in missing] == ['НетТакого']
    assert missing[0]['directive'] == 'Перед'
