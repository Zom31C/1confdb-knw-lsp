r"""Тесты разбора прав роли (confdb.rights) на синтетической структуре формата.

Форма данных повторяет снятую на УНФ (страница state3 `role-rights-format`):
`brace_file_read` возвращает список, чей первый элемент — корень записи роли.
"""
from confdb.rights import (VALUE_DENIED, VALUE_GRANTED, VALUE_UNSET,  # noqa: F401
                           RIGHT_NAMES, parse_role_rights)

R_READ = 'aa6448f2-be0f-42ea-ba26-1af7f52b5b65'
R_WRITE = '287b74b8-3a66-4a76-ba27-4f1f6a93770e'
OBJ = '6a3c1b05-ee48-4aa9-b2c1-4b66e3aeab70'
COLLECTION = '03f171e8-326f-41c6-9fa5-932a0b12cddf'


def _role(*groups, templates=('0',), version='10'):
    """Скобкоструктура роли: корень из версии, целей, шаблонов RLS и хвоста."""
    return [[version, [str(len(groups))] + list(groups), list(templates),
             '4294967295', '1', '0', '4294967295']]


def test_object_target_and_its_rights():
    role = _role([['1', OBJ.upper(), '0', '1'],
                  ['0', R_READ, '1', R_WRITE, '-1']])
    parsed = parse_role_rights(role)
    assert parsed.version == '10'
    assert len(parsed.targets) == 1
    target = parsed.targets[0]
    assert target.uuid == OBJ            # uuid приводится к нижнему регистру
    assert not target.is_subitem
    assert [(e.right_uuid, e.value) for e in target.rights] == [
        (R_READ, '1'), (R_WRITE, '-1')]
    assert target.rights[0].granted is True
    assert target.rights[1].granted is None   # -1 = не задано, не «запрещено»


def test_subitem_target_is_addressed_by_index():
    role = _role([['1', OBJ, '1', ['-2', COLLECTION], '1'],
                  ['0', R_READ, '1']])
    target = parse_role_rights(role).targets[0]
    assert target.is_subitem
    assert target.sub_index == -2
    assert target.collection_uuid == COLLECTION
    assert [e.value for e in target.rights] == ['1']


def test_rls_text_of_a_right_is_unwrapped():
    # реальная раскладка: счётчик пар, пары, счётчик RLS и блоки RLS,
    # где uuid права лежит ВНУТРИ блока
    rls = [R_WRITE, ['1', ['1', '"ГДЕ Пользователь = &ТекущийПользователь"', '0']]]
    role = _role([['1', OBJ, '0', '1'],
                  ['1', '2', R_READ, '1', R_WRITE, '-1', '1', rls]])
    target = parse_role_rights(role).targets[0]
    assert [(e.right_uuid, e.value) for e in target.rights] == [
        (R_READ, '1'), (R_WRITE, '-1')]
    entry = target.rights[1]
    assert entry.rls_text == 'ГДЕ Пользователь = &ТекущийПользователь'
    assert entry.granted is None
    assert target.rights[0].rls_text is None


def test_several_rls_blocks_reach_their_own_rights():
    rls_a = [R_READ, ['1', ['1', '"ГДЕ A = 1"', '0']]]
    rls_b = [R_WRITE, ['1', ['1', '"ГДЕ B = 2"', '0']]]
    role = _role([['1', OBJ, '0', '1'],
                  ['1', '2', R_READ, '1', R_WRITE, '1', '2', rls_a, rls_b]])
    entries = parse_role_rights(role).targets[0].rights
    assert [(e.right_uuid, e.rls_text) for e in entries] == [
        (R_READ, 'ГДЕ A = 1'), (R_WRITE, 'ГДЕ B = 2')]


def test_counted_rights_record_skips_the_counter():
    role = _role([['1', OBJ, '0', '1'],
                  ['1', '2', R_READ, '1', R_WRITE, '0']])
    entries = parse_role_rights(role).targets[0].rights
    assert [(e.right_uuid, e.value) for e in entries] == [
        (R_READ, '1'), (R_WRITE, '0')]
    assert entries[0].granted is True
    assert entries[1].granted is False


def test_rights_record_with_flag_one_is_not_a_target():
    # флаг '1' бывает и у записи прав — различать нужно по позиции, не по флагу
    role = _role([['1', OBJ, '0', '1'], ['1', '1', R_READ, '1']])
    parsed = parse_role_rights(role)
    assert len(parsed.targets) == 1
    assert [e.right_uuid for e in parsed.targets[0].rights] == [R_READ]


def test_role_rls_templates_keep_name_and_text():
    role = _role([['1', OBJ, '0', '1'], ['0', R_READ, '1']],
                 templates=['1', ['"ДляОбъекта(ПолеОбъекта)"',
                                  '"ГДЕ Ссылка = &ПолеОбъекта"']])
    assert parse_role_rights(role).rls_templates == (
        ('ДляОбъекта(ПолеОбъекта)', 'ГДЕ Ссылка = &ПолеОбъекта'),)


def test_doubled_quotes_in_template_text_are_unescaped():
    role = _role(templates=['1', ['"Шаблон"', '"Он сказал ""да"""']])
    assert parse_role_rights(role).rls_templates == (('Шаблон', 'Он сказал "да"'),)


def test_target_without_rights_keeps_no_entries():
    # 1490 групп УНФ: вторая запись — не права, а следующая цель
    role = _role([['1', OBJ, '0', '0'], ['1', R_WRITE, '0', '1']])
    parsed = parse_role_rights(role)
    assert len(parsed.targets) == 2
    assert all(not t.rights for t in parsed.targets)


def test_old_format_role_without_rights_is_not_an_error():
    role = _role(version='9')
    parsed = parse_role_rights(role)
    assert parsed.version == '9'
    assert parsed.targets == []
    assert parsed.rls_templates == ()
    assert parsed.flags == ('4294967295', '1', '0', '4294967295')


def test_entries_are_flat_pairs_for_the_writer():
    role = _role([['1', OBJ, '0', '1'], ['0', R_READ, '1', R_WRITE, '0']])
    rows = [(t.uuid, e.right_uuid, e.value)
            for t, e in parse_role_rights(role).entries()]
    assert rows == [(OBJ, R_READ, '1'), (OBJ, R_WRITE, '0')]


def test_right_name_is_none_until_the_dictionary_is_confirmed():
    # имена прав — платформенные константы, в конфигурации их нет:
    # до сверки с конфигуратором словарь пуст и инструмент обязан показать uuid
    assert RIGHT_NAMES == {}
    entry = parse_role_rights(
        _role([['1', OBJ, '0', '1'], ['0', R_READ, '1']])).targets[0].rights[0]
    assert entry.right_name is None
