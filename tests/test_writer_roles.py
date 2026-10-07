r"""Права ролей при записи базы: role_right, role_rls_template, role_rights_state.

Фикстура — текст в формате скобкофайла (как настоящий `Role.0.c1brace`: числа без
кавычек, строки в кавычках), поэтому проверяется весь путь от файла дампа до строк
в базе, а не только парсер.
"""
import os
import sqlite3

from confdb.db.writer import _target_flags, write_db
from confdb.rights import _parse_target
from test_writer import ATTR_UUID, CAT_UUID, TAB_UUID, _json, _write, make_dump

ROLE_UUID = '7e7e7e7e-0000-0000-0000-0000000000a1'
RIGHT_READ = 'aaaaaaa1-0000-0000-0000-000000000001'
RIGHT_WRITE = 'aaaaaaa2-0000-0000-0000-000000000002'
# uuid табличной части «Товары» из фикстуры make_dump: не объект метаданных,
# поэтому цель с ним разрешается в meta_tabular (и в объект-владелец)
SUB_UUID = TAB_UUID
COLLECTION_UUID = '03f171e8-326f-41c6-9fa5-932a0b12cddf'

# цель-объект с двумя правами, цель-табличная часть с правом и текстом RLS,
# цель-реквизит с одним правом и ТА ЖЕ цель-объект с другими флагами записи:
# флаги хранятся дословно, иначе две цели с одним uuid слились бы в одну.
# Разметка один в один как в настоящем Role.0.c1brace: uuid и числа без кавычек,
# каждый блок со своей строки, кавычки только у строк (декодер построчный),
# перед блоками RLS идёт их счётчик, а uuid права лежит внутри блока.
ROLE_RIGHTS = (
    '{10,\n'
    '{4,\n'
    '{\n'
    '{1,%s,0,0},\n'
    '{0,%s,1,%s,-1}\n'
    '},\n'
    '{\n'
    '{1,%s,1,\n'
    '{-2,%s},1},\n'
    '{1,1,%s,1,1,\n'
    '{%s,\n'
    '{1,\n'
    '{1,"ГДЕ Пользователь = &ТекущийПользователь",0}\n'
    '}\n'
    '}\n'
    '}\n'
    '},\n'
    '{\n'
    '{1,%s,0,0},\n'
    '{0,%s,1}\n'
    '},\n'
    '{\n'
    '{1,%s,1,1},\n'
    '{0,%s,-1}\n'
    '}\n'
    '},\n'
    '{1,\n'
    '{"ДляОбъекта(ПолеОбъекта)","ГДЕ Ссылка = &ПолеОбъекта"}\n'
    '},4294967295,1,0,4294967295}'
) % (CAT_UUID, RIGHT_READ, RIGHT_WRITE, SUB_UUID, COLLECTION_UUID,
     RIGHT_READ, RIGHT_READ, ATTR_UUID, RIGHT_READ, CAT_UUID, RIGHT_WRITE)

ROLE_HEADER = {
    'name': 'ТестоваяРоль', 'comment': '', 'obj_version': '803',
    'name2': {'ru': 'Тестовая роль'},
    'header': [['1', ['6', ['3', ['1', '0', 'в отдельном файле'],
                              '"ТестоваяРоль"', ['1', '"ru"', '"Тестовая роль"'],
                              '""', '0', '0',
                              '00000000-0000-0000-0000-000000000000', '0'],
                       '1', '0', '0'], '0']],
}


def _role_dir(base, name='ТестоваяРоль'):
    role_dir = os.path.join(base, 'Role', name)
    _json(os.path.join(role_dir, 'Role.json'), ROLE_HEADER)
    _json(os.path.join(role_dir, 'Role.id.json'), {'uuid': ROLE_UUID})
    return role_dir


def _build(base, db):
    stats = write_db(base, db, source_file='role.cf', build_fts=False)
    return stats, sqlite3.connect(db)


def test_role_rights_are_written(tmp_path_factory):
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    _write(os.path.join(_role_dir(dump), 'Role.0.c1brace'), ROLE_RIGHTS)
    db = str(tmp_path_factory.mktemp('db') / 'r.sqlite')
    stats, conn = _build(dump, db)

    assert stats['role_rights'] == 5
    assert stats['role_rls_templates'] == 1
    assert stats['role_rights_failed'] == 0

    rows = conn.execute(
        'SELECT target_uuid, target_object_id, target_attr_id,'
        ' target_tabular_id, sub_index, collection_uuid,'
        ' right_uuid, value, rls_text FROM role_right ORDER BY id').fetchall()
    assert len(rows) == 5
    # цель-объект: объект-владелец — он сам, подобъекта нет
    cat_id = conn.execute('SELECT id FROM meta_object WHERE uuid=?',
                          (CAT_UUID,)).fetchone()[0]
    assert rows[0][:6] == (CAT_UUID, cat_id, None, None, None, None)
    assert (rows[0][6], rows[0][7], rows[0][8]) == (RIGHT_READ, '1', None)
    assert (rows[1][6], rows[1][7]) == (RIGHT_WRITE, '-1')
    # цель-табличная часть: её uuid лежит в meta_tabular.uuid, поэтому
    # разрешаются и ТЧ, и её объект; индекс и uuid коллекции сохраняются
    doc_id, tab_id = conn.execute(
        'SELECT object_id, id FROM meta_tabular WHERE uuid=?', (SUB_UUID,)).fetchone()
    assert rows[2][:6] == (SUB_UUID, doc_id, None, tab_id, -2, COLLECTION_UUID)
    assert rows[2][8] == 'ГДЕ Пользователь = &ТекущийПользователь'
    # цель-реквизит: её uuid лежит в meta_attribute.uuid
    attr_owner, attr_id = conn.execute(
        'SELECT object_id, id FROM meta_attribute WHERE uuid=?', (ATTR_UUID,)).fetchone()
    assert rows[3][:6] == (ATTR_UUID, attr_owner, attr_id, None, None, None)
    assert (rows[3][6], rows[3][7]) == (RIGHT_READ, '1')
    # тот же объект второй раз, но с другими флагами записи цели: это отдельная
    # цель, и флаги сохранены дословно (их смысл не подтверждён — не толкуем)
    assert rows[4][:6] == (CAT_UUID, cat_id, None, None, None, None)
    assert (rows[4][6], rows[4][7]) == (RIGHT_WRITE, '-1')
    assert [r[0] for r in conn.execute(
        'SELECT target_flags FROM role_right ORDER BY id')] == \
        ['0/0', '0/0', '1/1', '0/0', '1/1']
    conn.close()


def test_role_rls_template_and_state(tmp_path_factory):
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    _write(os.path.join(_role_dir(dump), 'Role.0.c1brace'), ROLE_RIGHTS)
    db = str(tmp_path_factory.mktemp('db') / 'r.sqlite')
    _stats, conn = _build(dump, db)

    assert conn.execute('SELECT ord, name, text FROM role_rls_template'
                        ).fetchall() == [
        (0, 'ДляОбъекта(ПолеОбъекта)', 'ГДЕ Ссылка = &ПолеОбъекта')]
    role_id = conn.execute('SELECT id FROM meta_object WHERE uuid=?',
                           (ROLE_UUID,)).fetchone()[0]
    assert conn.execute(
        'SELECT role_id, version, parsed, targets, rights, rls_templates, error'
        ' FROM role_rights_state').fetchall() == [
        (role_id, '10', 1, 4, 5, 1, None)]
    conn.close()


def test_role_without_rights_file_reads_as_unavailable(tmp_path_factory):
    """Нет файла прав — это «данные недоступны», а не «доступ запрещён»."""
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    _role_dir(dump)                      # роль есть, Role.0.c1brace — нет
    db = str(tmp_path_factory.mktemp('db') / 'r.sqlite')
    stats, conn = _build(dump, db)

    assert stats['role_rights'] == 0
    assert stats['role_rights_failed'] == 1
    parsed, error = conn.execute(
        'SELECT parsed, error FROM role_rights_state').fetchone()
    assert parsed == 0
    assert error
    assert conn.execute('SELECT count(*) FROM role_right').fetchone()[0] == 0
    conn.close()


def test_broken_rights_file_does_not_stop_the_write(tmp_path_factory):
    """Сбой разбора одной роли не роняет запись всей базы."""
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    _write(os.path.join(_role_dir(dump), 'Role.0.c1brace'), '{10,{1,{{{')
    db = str(tmp_path_factory.mktemp('db') / 'r.sqlite')
    stats, conn = _build(dump, db)

    assert stats['role_rights_failed'] == 1
    assert stats['objects'] > 0
    assert conn.execute('SELECT parsed FROM role_rights_state').fetchone()[0] == 0
    conn.close()


def test_target_flags_keep_every_form_of_the_target_record():
    """Флаги записи цели: скаляры и вложенный блок, без repr списка в базе.

    У УНФ найдены записи цели с ДВУМЯ вложенными блоками —
    ['1', uuid, '2', ['-12', uuid1], ['-13', uuid2]] (8 строк в роли
    ДобавлениеИзменениеВозвратовПоставщикам): вторая ступень адресации попадает
    в flag_b, и без неё четыре цели этой роли сливались в одну.
    """
    assert _target_flags(_parse_target(['1', SUB_UUID, '0', '1'])) == '0/1'
    nested = _parse_target(['1', SUB_UUID, '1', ['-2', COLLECTION_UUID], '1'])
    assert _target_flags(nested) == '1/1'
    assert nested.sub_index == -2
    assert nested.collection_uuid == COLLECTION_UUID
    two_blocks = _parse_target(
        ['1', SUB_UUID, '2', ['-12', CAT_UUID], ['-13', COLLECTION_UUID]])
    flags = _target_flags(two_blocks)
    assert flags == f'2/-13:{COLLECTION_UUID}'
    assert '[' not in flags and "'" not in flags
    # первая ступень адресации по-прежнему в sub_index/collection_uuid
    assert two_blocks.sub_index == -12
    assert two_blocks.collection_uuid == CAT_UUID
