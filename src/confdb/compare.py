"""Сравнение объектов и конфигураций между двумя базами знаний.

Задача — ответить на вопросы «чем доработанная конфигурация отличается от
типовой» и «что именно делает расширение» без ручного сопоставления двух
паспортов объекта. Обе базы открыты read-only; объект сравнивается по снимку
(snapshot): тип, реквизиты с типами, табличные части, группы полей регистра,
модули, методы, вложенные объекты и запросы СКД.

Тела модулей и методов в снимке хранятся как sha1 — сравнение не тянет
мегабайты кода в память второй раз, а разница «изменилось/не изменилось»
для кода важнее, чем его текст (текст отдаёт get_method).
"""
import hashlib
import re

from .header_props import REGISTER_KINDS, register_field_kinds

# сколько позиций показывать в одной секции отчёта
MAX_ITEMS = 40

REGISTER_TYPES = frozenset({
    'InformationRegister', 'AccumulationRegister',
    'AccountingRegister', 'CalculationRegister',
})

# 'Ссылка: Catalog/Х' — цель внутри этой же базы; 'Ссылка: Х' — имя без пути
# (так выглядит ссылка на объект основной конфигурации из расширения)
RE_REF = re.compile(r'Ссылка:\s*([^|()]+)')


def sha1(text):
    return hashlib.sha1((text or '').encode('utf-8')).hexdigest()


def object_snapshot(conn, path):
    """Снимок объекта для сравнения; None, если объекта в базе нет."""
    row = conn.execute(
        'SELECT id, type, type_ru, name, comment, header_json '
        'FROM meta_object WHERE path=?', (path,)).fetchone()
    if not row:
        return None
    oid, obj_type, type_ru, name, comment, header_json = row
    snap = {
        'type': obj_type, 'type_ru': type_ru, 'name': name,
        'comment': comment or '',
        'kinds': register_field_kinds(header_json)
        if obj_type in REGISTER_TYPES else {},
        'attrs': {n: (t or '', tab or '') for n, t, tab in conn.execute(
            'SELECT name, type_str, tabular FROM meta_attribute '
            'WHERE object_id=?', (oid,))},
        'tabular': [n for n, in conn.execute(
            'SELECT name FROM meta_tabular WHERE object_id=? ORDER BY ord',
            (oid,))],
        'modules': {code: (ctx or '', sha1(body), len(body or ''))
                    for code, ctx, body in conn.execute(
                        'SELECT code_name, context, body FROM module '
                        'WHERE object_id=?', (oid,))},
        'methods': {},
        'children': {n: t for n, t in conn.execute(
            'SELECT name, type FROM meta_object WHERE parent_id=?', (oid,))},
        'skd': [sha1(text) for text, in conn.execute(
            'SELECT query FROM skd_query WHERE object_id=? ORDER BY ord',
            (oid,))],
    }
    for code, mname, kind, sig, exp, dirs, body in conn.execute(
            'SELECT m.code_name, t.name, t.kind, t.signature, t.is_export, '
            't.directives, t.body FROM method t '
            'JOIN module m ON m.id=t.module_id WHERE m.object_id=?', (oid,)):
        key = (code, mname.lower())
        snap['methods'][key] = (mname, kind, sig or '', int(exp or 0),
                                dirs or '', sha1(body))
    return snap


def _split(left, right):
    """(только слева, только справа, общие с отличиями) по ключам словарей."""
    only_left = sorted(k for k in left if k not in right)
    only_right = sorted(k for k in right if k not in left)
    changed = sorted(k for k in left if k in right and left[k] != right[k])
    return only_left, only_right, changed


def _list(items, fmt, limit=MAX_ITEMS):
    """Список строк с обрезкой: '…' + сколько ещё не показано."""
    lines = [fmt(k) for k in items[:limit]]
    if len(items) > limit:
        lines.append(f'… и ещё {len(items) - limit}')
    return lines


def _only_in(title):
    """Подпись «только в базе X»: алиас базы произвольный, склонять его нельзя."""
    return f'  только в базе «{title}»: '


def diff_snapshots(left, right, title_left='слева', title_right='справа',
                   fmt_type=None):
    """Строки различий двух снимков одного объекта (пусто — объекты равны).

    :param fmt_type: функция показа типа реквизита (сервер передаёт ru_type_str)
    """
    fmt = fmt_type or (lambda value: value)
    out = []
    if left['type'] != right['type']:
        out.append(f'Тип объекта: {title_left} {left["type_ru"]} ({left["type"]})'
                   f' / {title_right} {right["type_ru"]} ({right["type"]})')
    if left['comment'] != right['comment']:
        out.append(f'Комментарий: {title_left} "{left["comment"]}"'
                   f' / {title_right} "{right["comment"]}"')

    only_l, only_r, changed = _split(left['attrs'], right['attrs'])
    if only_l or only_r or changed:
        out.append(f'Реквизиты и поля табличных частей ({title_left} '
                   f'{len(left["attrs"])}, {title_right} {len(right["attrs"])}):')
        out.extend(_list(only_l, lambda k: _only_in(title_left)
                         + _attr_str(k, left['attrs'][k], left['kinds'], fmt)))
        out.extend(_list(only_r, lambda k: _only_in(title_right)
                         + _attr_str(k, right['attrs'][k], right['kinds'], fmt)))
        for key in changed[:MAX_ITEMS]:
            lt, lb = left['attrs'][key]
            rt, rb = right['attrs'][key]
            bits = []
            if lt != rt:
                bits.append(f'тип {fmt(lt) or "?"} -> {fmt(rt) or "?"}')
            if lb != rb:
                bits.append(f'секция {lb or "—"} -> {rb or "—"}')
            lk, rk = left['kinds'].get(key), right['kinds'].get(key)
            if lk != rk:
                bits.append(f'группа {lk or "?"} -> {rk or "?"}')
            out.append(f'  отличается {key}: ' + '; '.join(bits))
        if len(changed) > MAX_ITEMS:
            out.append(f'  … и ещё {len(changed) - MAX_ITEMS}')

    only_l, only_r, _ = _split({s: 1 for s in left['tabular']},
                               {s: 1 for s in right['tabular']})
    if only_l or only_r:
        out.append('Табличные части:')
        out.extend(_list(only_l, lambda s: _only_in(title_left) + s))
        out.extend(_list(only_r, lambda s: _only_in(title_right) + s))

    only_l, only_r, changed = _split(left['children'], right['children'])
    if only_l or only_r or changed:
        out.append('Формы, команды и прочие подобъекты:')
        out.extend(_list(only_l, lambda n: _only_in(title_left) + n))
        out.extend(_list(only_r, lambda n: _only_in(title_right) + n))
        out.extend(_list(changed, lambda n: f'  другой тип: {n} '
                                            f'({left["children"][n]} -> '
                                            f'{right["children"][n]})'))

    only_l, only_r, changed = _split(left['modules'], right['modules'])
    if only_l or only_r or changed:
        out.append('Модули:')
        out.extend(_list(only_l, lambda c: _only_in(title_left) + f'{c} '
                                           f'({left["modules"][c][2]} симв.)'))
        out.extend(_list(only_r, lambda c: _only_in(title_right) + f'{c} '
                                           f'({right["modules"][c][2]} симв.)'))
        for code in changed[:MAX_ITEMS]:
            lctx, lsha, llen = left['modules'][code]
            rctx, rsha, rlen = right['modules'][code]
            bits = []
            if lctx != rctx:
                bits.append(f'контекст {lctx or "—"} -> {rctx or "—"}')
            if lsha != rsha:
                bits.append(f'текст изменился ({llen} -> {rlen} симв.)')
            out.append(f'  отличается {code}: ' + '; '.join(bits))

    out.extend(_methods_diff(left, right, title_left, title_right))

    if left['skd'] != right['skd']:
        out.append(f'Запросы СКД: {title_left} {len(left["skd"])}, '
                   f'{title_right} {len(right["skd"])}'
                   + (' (состав или тексты различаются)'
                      if len(left['skd']) == len(right['skd']) else ''))
    return out


def _attr_str(name, value, kinds, fmt=lambda value: value):
    tstr, tabular = value
    kind = kinds.get(name)
    return (f'{name}: {fmt(tstr) or "?"}'
            + (f' [табчасть {tabular}]' if tabular else '')
            + (f' ({kind.lower()})' if kind and kind in REGISTER_KINDS else ''))


def _methods_diff(left, right, title_left, title_right):
    only_l, only_r, changed = _split(left['methods'], right['methods'])
    if not (only_l or only_r or changed):
        return []
    out = [f'Методы ({title_left} {len(left["methods"])}, '
           f'{title_right} {len(right["methods"])}):']
    out.extend(_list(only_l, lambda k: _only_in(title_left)
                     + _method_str(k, left['methods'][k])))
    out.extend(_list(only_r, lambda k: _only_in(title_right)
                     + _method_str(k, right['methods'][k])))
    for key in changed[:MAX_ITEMS]:
        lname, lkind, lsig, lexp, ldirs, lsha = left['methods'][key]
        rname, rkind, rsig, rexp, rdirs, rsha = right['methods'][key]
        bits = []
        if lsig != rsig:
            bits.append(f'сигнатура ({lsig}) -> ({rsig})')
        if lkind != rkind:
            bits.append(f'{lkind} -> {rkind}')
        if lexp != rexp:
            bits.append('Экспорт' if rexp else 'без Экспорт')
        if ldirs != rdirs:
            bits.append(f'директивы [{ldirs or "—"}] -> [{rdirs or "—"}]')
        if lsha != rsha:
            bits.append('тело изменилось')
        out.append(f'  отличается {key[0]}.{lname}: ' + '; '.join(bits))
    if len(changed) > MAX_ITEMS:
        out.append(f'  … и ещё {len(changed) - MAX_ITEMS}')
    return out


def _method_str(key, value):
    name, kind, sig, exp, dirs, _ = value
    return (f'{key[0]}::{kind} {name}({sig})'
            + (' Экспорт' if exp else '')
            + (f' [{dirs}]' if dirs else ''))


def object_paths(conn):
    """{path: (type, type_ru, name)} всех объектов базы, кроме корневого."""
    return {path: (typ, ru, name) for path, typ, ru, name in conn.execute(
        "SELECT path, type, type_ru, name FROM meta_object WHERE path <> ''")}


def path_set(conn):
    """Множество путей всех объектов базы, кроме корневого.

    Для основной конфигурации в split_extension нужны только сами пути —
    незачем тянуть type/type_ru/name и строить из них словарь на сотни
    тысяч записей, который потом используется лишь как `p in base`.
    """
    return {path for path, in conn.execute(
        "SELECT path FROM meta_object WHERE path <> ''")}


def external_refs(conn):
    """Имена объектов, на которые ссылается база, но не разрешает в себе.

    В расширении типы реквизитов, указывающие на основную конфигурацию,
    хранятся как 'Ссылка: Имя' без пути: самого объекта в базе расширения нет.
    """
    names = {}
    for tstr, in conn.execute(
            'SELECT DISTINCT type_str FROM meta_attribute '
            "WHERE type_str LIKE '%Ссылка%'"):
        for ref in RE_REF.findall(tstr or ''):
            ref = ref.strip()
            if ref and '/' not in ref and '.' not in ref:
                names.setdefault(ref, 0)
                names[ref] += 1
    return names


def resolve_in_base(conn_base, names, limit=MAX_ITEMS):
    """Куда в основной базе ведут неразрешённые имена: [(имя, путь, тип_ru)]."""
    found = []
    for name in sorted(names):
        row = conn_base.execute(
            'SELECT path, type_ru FROM meta_object WHERE name=? '
            "AND path <> '' AND path NOT LIKE '%/%/%' LIMIT 1",
            (name,)).fetchone()
        found.append((name, row[0] if row else None, row[1] if row else None))
        if len(found) >= limit:
            break
    return found


def extension_methods(conn, path=None):
    """Методы базы: (path, code_name, имя, kind, директивы, Экспорт).

    С path запрос ведётся от meta_object через CROSS JOIN — в SQLite это
    подсказка порядку соединения. Без неё планировщик сканирует всю таблицу
    method и применяет фильтр по пути последним: на базе УНФ (241566 методов)
    это 325 мс на запрос вместо 2,6 мс.
    """
    if path:
        sql = ('SELECT o.path, m.code_name, t.name, t.kind, t.directives, '
               't.is_export FROM meta_object o '
               'CROSS JOIN module m ON m.object_id=o.id '
               'CROSS JOIN method t ON t.module_id=m.id '
               'WHERE o.path=? ORDER BY m.code_name, t.ord')
        return list(conn.execute(sql, (path,)))
    sql = ('SELECT o.path, m.code_name, t.name, t.kind, t.directives, '
           't.is_export FROM method t JOIN module m ON m.id=t.module_id '
           'JOIN meta_object o ON o.id=m.object_id '
           'ORDER BY o.path, m.code_name, t.ord')
    return list(conn.execute(sql))


def method_keys(conn, path):
    """{(code_name, имя в нижнем регистре)} методов объекта.

    CROSS JOIN — см. extension_methods: без него каждый вызов сканирует
    всю таблицу method.
    """
    return {(code, name.lower()) for code, name in conn.execute(
        'SELECT m.code_name, t.name FROM meta_object o '
        'CROSS JOIN module m ON m.object_id=o.id '
        'CROSS JOIN method t ON t.module_id=m.id WHERE o.path=?', (path,))}


def attribute_names(conn, path):
    """Имена реквизитов и полей табличных частей объекта."""
    return {name for name, in conn.execute(
        'SELECT a.name FROM meta_attribute a '
        'JOIN meta_object o ON o.id=a.object_id WHERE o.path=?', (path,))}


def attribute_lines(conn, path):
    """[(имя, тип, табличная часть)] реквизитов объекта в порядке объявления."""
    return list(conn.execute(
        'SELECT a.name, a.type_str, a.tabular FROM meta_attribute a '
        'JOIN meta_object o ON o.id=a.object_id WHERE o.path=? ORDER BY a.ord',
        (path,)))


def split_extension(conn_ext, conn_base):
    """(новые объекты расширения, заимствованные из основной конфигурации).

    Оба списка — словари {path: (type, type_ru, name)}; заимствованным считается
    объект, который есть в основной базе под тем же путём (расширение сохраняет
    имя замещаемого объекта, префикс получают только новые).
    """
    ext = object_paths(conn_ext)
    base = path_set(conn_base)
    new = {p: v for p, v in ext.items() if p not in base}
    borrowed = {p: v for p, v in ext.items() if p in base}
    return new, borrowed


def extension_report(conn_ext, conn_base, fmt=str, prefix=None,
                     limit=MAX_ITEMS, fmt_type=None):
    """Разделы отчёта «что делает расширение»: объекты, методы, реквизиты.

    :param fmt: функция показа пути (сервер передаёт ru_path)
    :param prefix: префикс имён расширения из его заголовка ('Расш1_')
    :param fmt_type: функция показа типа реквизита (сервер передаёт ru_type_str)
    """
    show_type = fmt_type or (lambda value: value)
    # limit уходит в срезы списков: отрицательный отрезал бы последний элемент
    # и одновременно напечатал «… и ещё N»
    limit = max(1, int(limit))
    new, borrowed = split_extension(conn_ext, conn_base)
    out = [f'Новые объекты расширения ({len(new)}):']
    if not new:
        out.append('  нет')
    # считаем по всем новым объектам, а не по показанной странице: иначе
    # «N из M» относится только к первым limit объектам и вводит в заблуждение
    prefixed = sum(1 for _typ, _ru, name in new.values()
                   if prefix and name.startswith(prefix))
    for path in sorted(new)[:limit]:
        typ, type_ru, name = new[path]
        mark = f' [префикс {prefix}]' if prefix and name.startswith(prefix) \
            else ''
        out.append(f'  {fmt(path)} — {type_ru} ({typ}){mark}')
    if len(new) > limit:
        out.append(f'  … и ещё {len(new) - limit}')
    if prefix and new:
        out.append(f'  с префиксом расширения: {prefixed} из {len(new)}')

    out.append(f'\nЗаимствованные объекты ({len(borrowed)}):')
    if not borrowed:
        out.append('  нет')
    for path in sorted(borrowed)[:limit]:
        typ, type_ru, name = borrowed[path]
        out.append(f'  {fmt(path)} — {type_ru} ({typ})')
    if len(borrowed) > limit:
        out.append(f'  … и ещё {len(borrowed) - limit}')

    out.append('\nМетоды расширения (что замещается и куда вставляются вставки):')
    # один проход по методам расширения вместо запроса на каждый объект
    ext_methods = {}
    for row in extension_methods(conn_ext):
        ext_methods.setdefault(row[0], []).append(row)
    changed_objects = 0
    for path in sorted(borrowed):
        rows = ext_methods.get(path) or []
        if not rows:
            continue
        changed_objects += 1
        if changed_objects > limit:
            # потолок по числу объектов: без него отчёт о расширении с тысячами
            # заимствований уходит в контекст модели десятками тысяч строк
            continue
        base_keys = method_keys(conn_base, path)
        out.append(f'  {fmt(path)}:')
        for _, code, name, kind, dirs, exp in rows[:limit]:
            key = (code, name.lower())
            verdict = 'замещает штатный метод' if key in base_keys \
                else 'новый метод (в основной базе такого нет)'
            tail = f' [{dirs}]' if dirs else ''
            out.append(f'    {code}::{kind} {name}(){tail} — {verdict}'
                       + (' Экспорт' if exp else ''))
        if len(rows) > limit:
            out.append(f'    … и ещё {len(rows) - limit}')
    if not changed_objects:
        out.append('  в заимствованных объектах методов нет')
    elif changed_objects > limit:
        out.append(f'  … и ещё {changed_objects - limit} объектов с методами '
                   '(увеличьте limit)')

    out.append('\nРеквизиты и поля, добавленные расширением:')
    added_total = 0
    objects_with_added = 0
    for path in sorted(new) + sorted(borrowed):
        base_attrs = set() if path in new else attribute_names(conn_base, path)
        added = [(n, t, tab) for n, t, tab in attribute_lines(conn_ext, path)
                 if n not in base_attrs]
        if not added:
            continue
        added_total += len(added)
        objects_with_added += 1
        if objects_with_added > limit:
            continue
        out.append(f'  {fmt(path)}:')
        for name, tstr, tab in added[:limit]:
            out.append(f'    + {name}: {show_type(tstr) or "?"}'
                       + (f' [табчасть {tab}]' if tab else ''))
        if len(added) > limit:
            out.append(f'    … и ещё {len(added) - limit}')
    if not added_total:
        out.append('  нет')
    elif objects_with_added > limit:
        out.append(f'  … и ещё {objects_with_added - limit} объектов '
                   f'(всего добавленных полей: {added_total}; увеличьте limit)')

    out.append('\nВнешние зависимости (ссылки на объекты вне расширения):')
    refs = external_refs(conn_ext)
    if not refs:
        out.append('  по типам реквизитов не обнаружены')
    resolved = resolve_in_base(conn_base, refs, limit=limit)
    for name, path, type_ru in resolved:
        if path:
            out.append(f'  {name} -> {fmt(path)} ({type_ru})')
        else:
            out.append(f'  {name} -> в основной базе не найден')
    if len(refs) > len(resolved):
        # без этой строки обрезанный список внешних имён выглядит полным
        out.append(f'  … и ещё {len(refs) - len(resolved)} имён '
                   '(увеличьте limit)')
    return out
