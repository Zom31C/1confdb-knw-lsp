"""Тесты записи дампа стадии 3 в SQLite на синтетическом дереве."""
import json
import os
import sqlite3

from confdb.db.writer import VT_FIELDS_KEY, write_db

ROOT_UUID = 'aaaaaaaa-0000-0000-0000-000000000001'
CAT_UUID = 'bbbbbbbb-0000-0000-0000-000000000002'
FORM_UUID = 'cccccccc-0000-0000-0000-000000000003'
DT_UUID = 'dddddddd-0000-0000-0000-000000000004'
ENUM_UUID = 'eeeeeeee-0000-0000-0000-000000000005'
COMMON_UUID = 'ffffffff-0000-0000-0000-000000000006'
DOC_UUID = '0d0d0d0d-0000-0000-0000-000000000007'
REF_CAT = '11111111-1111-1111-1111-111111111111'  # ссылочный uuid справочника в .10
REF_DT = '22222222-2222-2222-2222-222222222222'   # собственный ссылочный uuid DT


def _core(name):
    return ['3', ['1', '0', 'в отдельном файле'], f'"{name}"',
            ['1', '"ru"', f'"{name}"'], '""', '0', '0',
            '00000000-0000-0000-0000-000000000000', '0']


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, str):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(data)
    else:
        with open(path, 'wb') as f:
            f.write(data)


def _json(path, data):
    _write(path, json.dumps(data, ensure_ascii=False))


def make_dump(base):
    """Минимальное дерево в стиле вывода декодера (стадия 3)."""
    _json(os.path.join(base, 'Configuration.json'), {
        'uuid': ROOT_UUID, 'name': 'ТестКонф', 'comment': 'комментарий',
        'obj_version': '803', 'header': {},
    })
    _write(os.path.join(base, 'Configuration.con.bsl'), 'Перем Тест;')
    _write(os.path.join(base, 'help.html'), '<html></html>')
    _json(os.path.join(base, 'Configuration.4.json'), {'info': True})  # инфо-файл, не объект
    # таблица ссылочных uuid корневого потока .10: {REF_CAT: Справочник1}
    _json(os.path.join(base, 'Configuration.10.json'),
          [['2', 'a', 'b', [['1', [REF_CAT, '"Справочник1"']]], '0']])

    cat_dir = os.path.join(base, 'Catalog', 'Справочник1')
    _json(os.path.join(cat_dir, 'Catalog.json'), {
        'name': 'Справочник1', 'comment': '', 'obj_version': '803',
        'header': [
            # простая ссылка через таблицу .10
            ['2', _core('СсылкаАтрибут'), ['"Pattern"', ['"#"', REF_CAT]]],
            # ссылка на определяемый тип (раскрывается состав)
            ['2', _core('ТипАтрибут'), ['"Pattern"', ['"#"', REF_DT]]],
            # составной тип: собственный uuid объекта вложен в дескриптор
            ['2', _core('СоставнойАтрибут'), '99999999-0000-0000-0000-000000000000',
             ['0', '2',
              ['"#"', '3ea29ea5-0000-0000-0000-000000000000',
               ['0', ['"#"', '157fa490-0000-0000-0000-000000000000', ['1', CAT_UUID]]]],
              ['"#"', '3ea29ea5-0000-0000-0000-000000000000',
               ['0', ['"#"', '157fa490-0000-0000-0000-000000000000',
                      ['1', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee']]]],
              ], '1'],
        ],
    })
    _json(os.path.join(cat_dir, 'Catalog.id.json'), {'uuid': CAT_UUID})
    _write(os.path.join(cat_dir, 'Catalog.obj.bsl'), 'Процедура Тест() КонецПроцедуры')

    doc_dir = os.path.join(base, 'Document', 'ЗаказПокупателя')
    _json(os.path.join(doc_dir, 'Document.json'), {
        'name': 'ЗаказПокупателя', 'comment': '', 'obj_version': '803',
        'header': [
            ['2', _core('НомерЗаказа'), ['"Pattern"', ['"S"', '11', '1']]],
            # запись секции, 1, блок полей табличной части
            ['1', ['11', 'aaaaaaaa-0000-0000-0000-000000000010',
                   ['0', ['3', ['1', '0', 'aaaaaaaa-0000-0000-0000-000000000011'],
                    '"Товары"', ['1', '"ru"', '"Товары"'], '""', '0', '0',
                    '00000000-0000-0000-0000-000000000000', '0']]],
             '0', ['0'], ['1', '"ru"', '"Товары"']],
            '1',
            [VT_FIELDS_KEY, '2',
             ['8', ['27', ['2', _core('ТоварыНоменклатура'),
                     ['"Pattern"', ['"#"', REF_CAT]]]], '0'],
             ['8', ['27', ['2', _core('ТоварыКоличество'),
                     ['"Pattern"', ['"N"', '15', '3', '1']]]], '0']],
        ],
    })
    _json(os.path.join(doc_dir, 'Document.id.json'), {'uuid': DOC_UUID})

    enum_dir = os.path.join(base, 'Enum', 'ТестПеречисление')
    _json(os.path.join(enum_dir, 'Enum.json'), {
        'name': 'ТестПеречисление', 'comment': '', 'obj_version': '803',
        'header': [[[[0, _core('Значение1')], 0], [[0, _core('Значение2')], 0]]],
    })
    _json(os.path.join(enum_dir, 'Enum.id.json'), {'uuid': ENUM_UUID})

    common_dir = os.path.join(base, 'CommonAttribute', 'ОбщийТест')
    _json(os.path.join(common_dir, 'CommonAttribute.json'), {
        'name': 'ОбщийТест', 'comment': '', 'obj_version': '803',
        'header': [['3', '1', CAT_UUID,
                    ['2', '1', '00000000-0000-0000-0000-000000000000']]],
    })
    _json(os.path.join(common_dir, 'CommonAttribute.id.json'), {'uuid': COMMON_UUID})

    _write(os.path.join(cat_dir, 'Предустановленные данные.bin'),
           '{0,\n{1,\n{6,\n{0,"",\n{"Pattern",\n{"S"}\n},"",0}\n},\n'
           '{2,1,7,\n{"#",00000000-0000-0000-0000-000000000001,\n'
           '{1,00000000-0000-0000-0000-000000000000}\n},\n{"B",0},\n'
           '{"#",00000000-0000-0000-0000-000000000001,\n'
           '{1,00000000-0000-0000-0000-000000000000}\n},\n'
           '{"S","ПредЗначение"},\n{"S","001"},\n{"S","Предопределенное значение"},\n'
           '{"N",0},0}\n}\n')

    dt_dir = os.path.join(base, 'DefinedType', 'ТипТест')
    # запись header[0][1] определяемого типа: собственный ссылочный uuid + состав
    _json(os.path.join(dt_dir, 'DefinedType.json'), {
        'name': 'ТипТест', 'comment': '', 'obj_version': '803',
        'header': [['0', ['0', REF_DT, '33333333-0000-0000-0000-000000000000',
                          _core('ТипТест'), ['"Pattern"', ['"#"', REF_CAT]]]]],
    })
    _json(os.path.join(dt_dir, 'DefinedType.id.json'), {'uuid': DT_UUID})

    form_dir = os.path.join(cat_dir, 'Form', 'ФормаЭлемента')
    _json(os.path.join(form_dir, 'CatalogForm.json'), {
        'name': 'ФормаЭлемента', 'comment': '', 'obj_version': '803', 'header': {},
    })
    _json(os.path.join(form_dir, 'CatalogForm.id.json'), {'uuid': FORM_UUID})
    _write(os.path.join(form_dir, 'CatalogForm.mod.bsl'), '// модуль формы')


def test_write_db(tmp_path):
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    db_path = str(tmp_path / 'out.sqlite')

    stats = write_db(dump, db_path, source_file='test.cf')
    assert stats == {'objects': 7, 'modules': 3, 'methods': 1, 'files': 4, 'files_content': 3,
                     'skd': 0, 'attributes': 6, 'refs': 6, 'enum_values': 2,
                     'predefined': 1, 'common_targets': 1, 'tabular': 1}

    conn = sqlite3.connect(db_path)
    q = conn.execute

    src = q('SELECT file, root_type, root_name, root_uuid FROM source').fetchone()
    assert src == ('test.cf', 'Configuration', 'ТестКонф', ROOT_UUID)

    # type_ru — русские имена типов «как в конфигураторе»
    assert q("SELECT type_ru FROM meta_object WHERE type='Catalog'").fetchone()[0] == 'Справочник'
    assert q("SELECT type_ru FROM meta_object WHERE type='CatalogForm'").fetchone()[0] == 'Форма справочника'

    # типы реквизитов: простая ссылка, определяемый тип с составом, составной
    attrs = {r[0]: r[1] for r in q(
        'SELECT a.name, a.type_str FROM meta_attribute a JOIN meta_object o ON o.id=a.object_id'
        " WHERE o.path='Catalog/Справочник1' ORDER BY a.ord")}
    assert attrs['СсылкаАтрибут'] == 'Ссылка: Catalog/Справочник1'
    assert attrs['ТипАтрибут'] == \
        'ОпределяемыйТип: DefinedType/ТипТест (Ссылка: Catalog/Справочник1)'
    assert attrs['СоставнойАтрибут'] == 'Ссылка: Catalog/Справочник1 | Ссылка'

    # attribute_ref: uuid ссылки → объект (включая состав определяемого типа)
    refs = {(r[0], r[1], r[2]) for r in q(
        'SELECT a.name, r.uuid, o.path FROM attribute_ref r'
        ' JOIN meta_attribute a ON a.id=r.attribute_id'
        ' LEFT JOIN meta_object o ON o.id=r.object_id')}
    assert refs == {
        ('СсылкаАтрибут', REF_CAT, 'Catalog/Справочник1'),
        ('ТипАтрибут', REF_DT, 'DefinedType/ТипТест'),
        ('ТипАтрибут', REF_CAT, 'Catalog/Справочник1'),
        ('СоставнойАтрибут', CAT_UUID, 'Catalog/Справочник1'),
        ('СоставнойАтрибут', '3ea29ea5-0000-0000-0000-000000000000', None),
        ('ТоварыНоменклатура', REF_CAT, 'Catalog/Справочник1'),
    }

    # методы: только процедуры/функции, без «преамбул»
    methods = {(r[0], r[1], r[2]) for r in q(
        'SELECT o.path, mt.kind, mt.name FROM method mt'
        ' JOIN module mo ON mo.id = mt.module_id'
        ' JOIN meta_object o ON o.id = mo.object_id')}
    assert methods == {('Catalog/Справочник1', 'процедура', 'Тест')}

    objects = {r[1]: r for r in q(
        'SELECT id, path, type, name, uuid, parent_id FROM meta_object ORDER BY path')}
    assert set(objects) == {'', 'Catalog/Справочник1',
                            'Catalog/Справочник1/Form/ФормаЭлемента', 'DefinedType/ТипТест',
                            'Enum/ТестПеречисление', 'CommonAttribute/ОбщийТест',
                            'Document/ЗаказПокупателя'}

    # табличная часть и её поля
    assert [r[0] for r in q(
        'SELECT t.name FROM meta_tabular t JOIN meta_object o ON o.id=t.object_id'
        " WHERE o.path='Document/ЗаказПокупателя'")] == ['Товары']
    assert [r[0] for r in q(
        'SELECT a.name FROM meta_attribute a JOIN meta_object o ON o.id=a.object_id'
        " WHERE o.path='Document/ЗаказПокупателя' AND a.tabular='Товары' "
        'ORDER BY a.ord')] == ['ТоварыНоменклатура', 'ТоварыКоличество']

    # значения перечислений, предопределённые, привязки общих реквизитов
    assert [r[0] for r in q(
        'SELECT e.name FROM enum_value e JOIN meta_object o ON o.id=e.object_id'
        " WHERE o.path='Enum/ТестПеречисление' ORDER BY e.ord")] == ['Значение1', 'Значение2']
    assert q("SELECT name, code, display FROM predefined WHERE name='ПредЗначение'"
             ).fetchone() == ('ПредЗначение', '001', 'Предопределенное значение')
    assert [r[0] for r in q(
        'SELECT t.path FROM common_target ct JOIN meta_object t ON t.id=ct.target_id'
        ' JOIN meta_object c ON c.id=ct.common_id WHERE c.name=\'ОбщийТест\'')] == \
        ['Catalog/Справочник1']
    root, cat, form = objects[''], objects['Catalog/Справочник1'], \
        objects['Catalog/Справочник1/Form/ФормаЭлемента']
    assert root[2] == 'Configuration' and root[5] is None
    assert cat[2] == 'Catalog' and cat[4] == CAT_UUID and cat[5] == root[0]
    assert form[4] == FORM_UUID and form[2] == 'CatalogForm'

    # цепочка родителей: форма → справочник → корень
    chain = q('WITH RECURSIVE up(id) AS ('
              '  SELECT parent_id FROM meta_object WHERE id=?'
              '  UNION ALL'
              '  SELECT m.parent_id FROM meta_object m JOIN up ON m.id=up.id WHERE up.id IS NOT NULL'
              ') SELECT id FROM up', (form[0],)).fetchall()
    assert [c[0] for c in chain][:2] == [cat[0], root[0]]

    # модуль: паспорт + текст как есть без кода методов
    rows = q('SELECT m.code_name, m.body FROM module m '
             'JOIN meta_object o ON o.id=m.object_id '
             'WHERE o.path=?', ('Catalog/Справочник1',)).fetchall()
    assert rows and rows[0][0] == 'obj'
    assert rows[0][1] == 'Процедура Тест() КонецПроцедуры'
    # модуль без методов хранит весь текст
    conf = q('SELECT m.body FROM module m JOIN meta_object o ON o.id=m.object_id '
             "WHERE o.path='' AND m.code_name='con'").fetchone()
    assert conf[0] == 'Перем Тест;'

    files = {r[0]: (r[1], r[2]) for r in q('SELECT path, kind, data FROM file')}
    assert files['help.html'][0] == 'html'
    assert files['help.html'][1] == b'<html></html>'
    assert 'Configuration.4.json' in files
    conn.close()


def test_write_db_no_root(tmp_path):
    dump = str(tmp_path / 'empty')
    os.makedirs(dump)
    db_path = str(tmp_path / 'out.sqlite')
    try:
        write_db(dump, db_path)
        raise AssertionError('ожидалась ошибка отсутствия корневого объекта')
    except ValueError:
        pass


def test_write_db_rejects_dir_as_db(tmp_path):
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    try:
        write_db(dump, str(tmp_path))
        raise AssertionError('ожидалась ошибка: путь БД — каталог')
    except ValueError:
        pass


def test_extract_skd_queries(tmp_path):
    from confdb.db.writer import _extract_skd_queries
    xml = ('<?xml version="1.0"?>'
           '<DataCompositionSchema xmlns="http://v8.1c.ru/2008/10/data-composition-schema">'
           '<dataSets><dataSet><query>ВЫБРАТЬ 1 КАК А</query></dataSet></dataSets>'
           '</DataCompositionSchema>')
    raw = b'\x00\x00\x00\x00\x01\x00\x00\x00' + xml.encode('utf-8')
    (tmp_path / 'Template.bin').write_bytes(raw)
    assert _extract_skd_queries(str(tmp_path / 'Template.bin')) == ['ВЫБРАТЬ 1 КАК А']
    assert _extract_skd_queries(str(tmp_path / 'нет.bin')) is None
