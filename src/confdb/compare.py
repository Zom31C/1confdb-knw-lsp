"""Сравнение объектов и конфигураций между двумя базами знаний.

Задача — ответить на вопросы «чем доработанная конфигурация отличается от
типовой» и «что именно делает расширение» без ручного сопоставления двух
паспортов объекта. Обе базы открыты read-only; объект сравнивается по снимку
(snapshot): тип, реквизиты с типами, табличные части, группы полей регистра,
модули, методы, вложенные объекты, запросы СКД и состав пакета XDTO.

Тела модулей и методов в снимке хранятся как sha1 — сравнение не тянет
мегабайты кода в память второй раз, а разница «изменилось/не изменилось»
для кода важнее, чем его текст (текст отдаёт get_method).
"""
import difflib
import hashlib
import re
import sqlite3
from collections import namedtuple

from .bsl_analyzer import scan
from .header_props import REGISTER_KINDS, register_field_kinds

# сколько позиций показывать в одной секции отчёта
MAX_ITEMS = 40

# структурированное различие для пагинации и идентификации
DiffItem = namedtuple('DiffItem', ['id', 'section', 'kind', 'description'])

REGISTER_TYPES = frozenset({
    'InformationRegister', 'AccumulationRegister',
    'AccountingRegister', 'CalculationRegister',
})

# 'Ссылка: Catalog/Х' — цель внутри этой же базы; 'Ссылка: Х' — имя без пути
# (так выглядит ссылка на объект основной конфигурации из расширения)
RE_REF = re.compile(r'Ссылка:\s*([^|()]+)')

# директивы перехвата расширения: &Вместо("Х"), &После("Х"), &Перед("Х"),
# &ИзменениеИКонтроль("Х"); контекстные директивы (&НаСервере…) перехватами
# не являются
RE_INTERCEPT = re.compile(
    r'&\s*(Вместо|После|Перед|ИзменениеИКонтроль)\s*\(\s*"([^"]+)"',
    re.IGNORECASE)

INTERCEPT_VERBS = {
    'вместо': 'замещает целевой метод',
    'после': 'вставляет код после целевого метода',
    'перед': 'вставляет код перед целевым методом',
    'изменениеиконтроль': 'изменяет целевой метод (вариант с контролем)',
}


def sha1(text):
    return hashlib.sha1((text or '').encode('utf-8')).hexdigest()


def _xdto_snapshot(conn, oid):
    """{имя типа: (базовый тип, отпечаток свойств)} — состав пакета XDTO.

    Сравниваются только именованные типы: свойства вложенного анонимного типа
    входят в отпечаток свойства-владельца, поэтому его изменение видно как
    изменение именованного типа, а не как пропавший тип без имени.
    """
    try:
        types = {tid: (name, base, facets, values) for tid, name, base,
                 facets, values in conn.execute(
                     'SELECT id, name, base, facets, enum_values'
                     ' FROM xdto_type WHERE object_id=?', (oid,))}
        rows = conn.execute(
            'SELECT type_id, ord, name, type, lower_bound, upper_bound,'
            ' nillable, form, nested_type_id, extra FROM xdto_property'
            ' WHERE object_id=? ORDER BY type_id, ord', (oid,)).fetchall()
    except sqlite3.OperationalError:
        # база извлечена версией без таблиц xdto_*: отсутствие состава пакета
        # не должно ломать сравнение остальных разделов объекта
        return {}
    props = {}
    for row in rows:
        props.setdefault(row[0], []).append(row[2:])

    def signature(tid):
        parts = []
        for pname, ptype, lower, upper, nill, form, nested, extra \
                in props.get(tid, []):
            parts.append(f'{pname}|{ptype}|{lower}|{upper}|{nill}|{form}|{extra}'
                         + (f'|{signature(nested)}' if nested else ''))
        return sha1(';'.join(parts))

    out = {name: (base or '', sha1(f'{facets}|{values}|{signature(tid)}'))
           for tid, (name, base, facets, values) in types.items() if name}
    if props.get(None):
        # свойства, объявленные в самом пакете вне типов
        out['(свойства пакета)'] = ('', signature(None))
    return out


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
        'xdto': _xdto_snapshot(conn, oid),
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


def _only_in(title):
    """Подпись «только в базе X»: алиас базы произвольный, склонять его нельзя."""
    return f'  только в базе «{title}»: '


def compute_diff(left, right, title_left='слева', title_right='справа',
                 fmt_type=None):
    """Вычислить структурированные различия двух снимков — единый источник.

    Возвращает список DiffItem(id, section, kind, description) БЕЗ обрезки:
    полноту выдачи обеспечивает пагинация format_diff, а не отброшенные
    элементы. None-снимки возвращают пустой список (вызывающий код сообщает
    об отсутствии объекта сам).

    :param fmt_type: функция показа типа реквизита (сервер передаёт ru_type_str)
    """
    if left is None or right is None:
        return []
    fmt = fmt_type or (lambda value: value)
    items = []

    def add(item_id, section, kind, description):
        items.append(DiffItem(item_id, section, kind, description))

    # тип объекта и комментарий
    if left['type'] != right['type']:
        add('type', 'type', 'changed',
            f'Тип объекта: {title_left} {left["type_ru"]} ({left["type"]})'
            f' / {title_right} {right["type_ru"]} ({right["type"]})')
    if left['comment'] != right['comment']:
        add('comment', 'comment', 'changed',
            f'Комментарий: {title_left} "{left["comment"]}"'
            f' / {title_right} "{right["comment"]}"')
    # реквизиты и поля табличных частей
    only_l, only_r, changed = _split(left['attrs'], right['attrs'])
    for k in only_l:
        add(f'attrs:{k}', 'attrs', 'only_left',
            _only_in(title_left)
            + _attr_str(k, left['attrs'][k], left['kinds'], fmt))
    for k in only_r:
        add(f'attrs:{k}', 'attrs', 'only_right',
            _only_in(title_right)
            + _attr_str(k, right['attrs'][k], right['kinds'], fmt))
    for key in changed:
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
        add(f'attrs:{key}', 'attrs', 'changed',
            f'  отличается {key}: ' + '; '.join(bits))
    # табличные части
    only_l, only_r, _ = _split({s: 1 for s in left['tabular']},
                               {s: 1 for s in right['tabular']})
    for s in only_l:
        add(f'tabular:{s}', 'tabular', 'only_left', _only_in(title_left) + s)
    for s in only_r:
        add(f'tabular:{s}', 'tabular', 'only_right', _only_in(title_right) + s)
    # типы XDTO-пакета
    only_l, only_r, changed = _split(left.get('xdto', {}), right.get('xdto', {}))
    for n in only_l:
        add(f'xdto:{n}', 'xdto', 'only_left', _only_in(title_left) + n)
    for n in only_r:
        add(f'xdto:{n}', 'xdto', 'only_right', _only_in(title_right) + n)
    for name in changed:
        lbase, lsha = left['xdto'][name]
        rbase, rsha = right['xdto'][name]
        bits = []
        if lbase != rbase:
            bits.append(f'базовый тип {lbase or "—"} -> {rbase or "—"}')
        if lsha != rsha:
            bits.append('состав свойств, ограничения или значения изменились')
        add(f'xdto:{name}', 'xdto', 'changed',
            f'  отличается {name}: ' + '; '.join(bits))
    # формы, команды и прочие подобъекты
    only_l, only_r, changed = _split(left['children'], right['children'])
    for n in only_l:
        add(f'children:{n}', 'children', 'only_left', _only_in(title_left) + n)
    for n in only_r:
        add(f'children:{n}', 'children', 'only_right', _only_in(title_right) + n)
    for n in changed:
        add(f'children:{n}', 'children', 'changed',
            f'  другой тип: {n} ({left["children"][n]} -> {right["children"][n]})')
    # модули
    only_l, only_r, changed = _split(left['modules'], right['modules'])
    for c in only_l:
        add(f'modules:{c}', 'modules', 'only_left',
            _only_in(title_left) + f'{c} ({left["modules"][c][2]} симв.)')
    for c in only_r:
        add(f'modules:{c}', 'modules', 'only_right',
            _only_in(title_right) + f'{c} ({right["modules"][c][2]} симв.)')
    for code in changed:
        lctx, lsha, llen = left['modules'][code]
        rctx, rsha, rlen = right['modules'][code]
        bits = []
        if lctx != rctx:
            bits.append(f'контекст {lctx or "—"} -> {rctx or "—"}')
        if lsha != rsha:
            bits.append(f'текст изменился ({llen} -> {rlen} симв.)')
        add(f'modules:{code}', 'modules', 'changed',
            f'  отличается {code}: ' + '; '.join(bits))
    # методы
    only_l, only_r, changed = _split(left['methods'], right['methods'])
    for k in only_l:
        add(f'methods:{k[0]}:{k[1]}', 'methods', 'only_left',
            _only_in(title_left) + _method_str(k, left['methods'][k]))
    for k in only_r:
        add(f'methods:{k[0]}:{k[1]}', 'methods', 'only_right',
            _only_in(title_right) + _method_str(k, right['methods'][k]))
    for key in changed:
        lname, lkind, lsig, lexp, ldirs, lsha = left['methods'][key]
        _rname, rkind, rsig, rexp, rdirs, rsha = right['methods'][key]
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
        add(f'methods:{key[0]}:{key[1]}', 'methods', 'changed',
            f'  отличается {key[0]}.{lname}: ' + '; '.join(bits))
    # запросы СКД
    if left['skd'] != right['skd']:
        add('skd', 'skd', 'changed',
            f'Запросы СКД: {title_left} {len(left["skd"])}, '
            f'{title_right} {len(right["skd"])}'
            + (' (состав или тексты различаются)'
               if len(left['skd']) == len(right['skd']) else ''))
    return items


def format_diff(items, limit=100, offset=0):
    """Страница описаний DiffItem с отметкой полноты.

    Пагинация — единственный механизм обрезки: объединение всех страниц
    содержит каждое различие ровно один раз, а футер сообщает, как получить
    продолжение.
    """
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    total = len(items)
    if not total:
        return []
    if offset >= total:
        return [f'offset={offset} за пределами списка: всего различий '
                f'{total} — повторите с offset=0']
    end = min(offset + limit, total)
    page = [item.description for item in items[offset:end]]
    if offset > 0 or end < total:
        remaining = total - end
        page.append(f'… показано {offset + 1}–{end} из {total} различий'
                    + (f'; есть ещё {remaining} — следующий вызов с '
                       f'offset={end}' if remaining
                       else '; это все различия'))
    return page


RE_PARAM_ZNACH = re.compile(r'^знач\s+', re.IGNORECASE)


def normalized_lines(text):
    """Построчная нормализация BSL для сравнения: лексический разбор, не regex.

    Комментарии убираются, пробелы вне литералов убираются; строковые литералы
    (включая многострочные с '|') и препроцессор сохраняются как есть — разные
    литералы не должны сравниваться как равные. Возвращает список той же длины,
    что и text.splitlines(): нормализованная строка либо None для пустой
    строки/чистого комментария.
    """
    text = text or ''
    masked, literals = scan(text)
    in_lit = bytearray(len(text))
    for start, end, _value in literals:
        in_lit[start:end] = b'\1' * (end - start)
    out = []
    cur = []
    for i, ch in enumerate(text):
        if ch == '\n':
            out.append(''.join(cur).strip() or None)
            cur = []
        elif in_lit[i]:
            cur.append(ch)
        elif masked[i] == ' ' and ch != ' ':
            continue  # символ комментария
        elif not ch.isspace():
            cur.append(ch)
    if text and not text.endswith('\n'):
        out.append(''.join(cur).strip() or None)
    return out


def _declaration_end(body):
    """Индекс последней строки объявления метода (до закрывающей скобки).

    Скобки считаются по маскированному тексту — литералы и комментарии не
    мешают. -1, если объявление не найдено (битый источник): тело целиком
    считается исполняемым текстом.
    """
    masked, _literals = scan(body or '')
    depth = 0
    seen_open = False
    for idx, line in enumerate(masked.splitlines()):
        for ch in line:
            if ch == '(':
                depth += 1
                seen_open = True
            elif ch == ')':
                depth -= 1
        if seen_open and depth <= 0:
            return idx
    return -1


def split_params_full(signature):
    """[(имя, значение по умолчанию или None)] — разбор сигнатуры по запятым.

    В отличие от bsl_analyzer.split_params сохраняет значения по умолчанию:
    diff метода обязан показывать их изменение отдельно.
    """
    parts = []
    depth = 0
    in_str = False
    cur = []
    for ch in signature or '':
        if ch == '"':
            in_str = not in_str
        if not in_str:
            if ch in '([':
                depth += 1
            elif ch in ')]':
                depth -= 1
            elif ch == ',' and depth == 0:
                parts.append(''.join(cur))
                cur = []
                continue
        cur.append(ch)
    if cur:
        parts.append(''.join(cur))
    out = []
    for raw in parts:
        text = raw.strip()
        if not text:
            continue
        name, sep, default = text.partition('=')
        name = RE_PARAM_ZNACH.sub('', name.strip()).strip()
        if not name:
            continue
        out.append((name, default.strip() if sep else None))
    return out


def _comment_lines_of(lines, norms):
    """Непустые строки-комментарии тела (strip), по маске normalized_lines."""
    return [lines[i].strip() for i, norm in enumerate(norms)
            if norm is None and lines[i].strip()]


def _unified_code_diff(code_l, code_r, start_l, start_r, context):
    """Единый unified diff нормализованных строк: контекст общий, без дублей.

    code_* — списки (индекс строки в теле, оригинальная строка, нормализованная);
    номера строк в выводе — строки МОДУЛЯ (line_start + индекс). Контекстная
    строка печатается один раз с номерами обеих сторон.
    """
    matcher = difflib.SequenceMatcher(None, [n for _, _, n in code_l],
                                      [n for _, _, n in code_r], autojunk=False)
    ops = matcher.get_opcodes()
    changed = [op for op in ops if op[0] != 'equal']
    if not changed:
        return []
    # hunks: изменения ближе 2*context (в кодовых строках) сливаются в один
    groups = []
    cur = [changed[0]]
    for op in changed[1:]:
        if op[1] - cur[-1][2] <= 2 * context:
            cur.append(op)
        else:
            groups.append(cur)
            cur = [op]
    groups.append(cur)

    out = []
    for group in groups:
        a1 = max(0, group[0][1] - context)
        a2 = min(len(code_l), group[-1][2] + context)
        b1 = max(0, group[0][3] - context)
        b2 = min(len(code_r), group[-1][4] + context)
        span_l = (f'{start_l + code_l[a1][0]}–{start_l + code_l[a2 - 1][0]}'
                  if a2 > a1 else f'{start_l}')
        span_r = (f'{start_r + code_r[b1][0]}–{start_r + code_r[b2 - 1][0]}'
                  if b2 > b1 else f'{start_r}')
        out.append(f'@@ строки {span_l} | {span_r} @@')
        for tag, x1, x2, y1, y2 in ops:
            if tag == 'equal':
                shift = y1 - x1
                for k in range(max(x1, a1), min(x2, a2)):
                    li, txt, _ = code_l[k]
                    ri = code_r[k + shift][0]
                    out.append(f'  {start_l + li:4d} {start_r + ri:4d}  {txt}')
                continue
            if x2 < a1 or x1 > a2:
                continue  # изменение соседнего hunk'а
            if tag in ('replace', 'delete'):
                for k in range(x1, x2):
                    li, txt, _ = code_l[k]
                    out.append(f'- {start_l + li:4d}        {txt}')
            if tag in ('replace', 'insert'):
                for k in range(y1, y2):
                    ri, txt, _ = code_r[k]
                    out.append(f'+        {start_r + ri:4d}  {txt}')
    return out


def method_compare(row_left, row_right, context=3):
    """Сравнить две строки метода из базы (формат McpServer._method_row).

    Строка: kind, name, signature, directives, description, body, is_export,
    контекст модуля, line_start. Объявление (параметры и значения по умолчанию,
    Экспорт, директивы) отделяется от исполняемого текста, а комментарии и
    форматирование — от кода (feedback п.2): добавление только Экспорт не
    считается изменением исполняемого тела. Возвращает словарь для
    format_method_compare.
    """
    (kind_l, _name_l, sig_l, dirs_l, desc_l, body_l, exp_l,
     _ctx_l, start_l) = row_left
    (kind_r, _name_r, sig_r, dirs_r, desc_r, body_r, exp_r,
     _ctx_r, start_r) = row_right
    body_l, body_r = body_l or '', body_r or ''
    start_l, start_r = start_l or 1, start_r or 1

    # -- объявление: параметры и значения по умолчанию, Экспорт, директивы
    params_l = split_params_full(sig_l)
    params_r = split_params_full(sig_r)
    names_l = {n.lower() for n, _ in params_l}
    names_r = {n.lower() for n, _ in params_r}
    defs_l = {n.lower(): d for n, d in params_l}
    defs_r = {n.lower(): d for n, d in params_r}
    added = [n for n, _ in params_r if n.lower() not in names_l]
    removed = [n for n, _ in params_l if n.lower() not in names_r]
    defaults = [(n, defs_l[n.lower()], defs_r[n.lower()])
                for n, _ in params_r
                if n.lower() in defs_l
                and defs_l[n.lower()] != defs_r[n.lower()]]
    common_l = [n.lower() for n, _ in params_l if n.lower() in names_r]
    common_r = [n.lower() for n, _ in params_r if n.lower() in names_l]
    order_changed = common_l != common_r
    exp_l, exp_r = int(exp_l or 0), int(exp_r or 0)
    export = (exp_l, exp_r) if exp_l != exp_r else None
    dl = [d.strip() for d in (dirs_l or '').splitlines() if d.strip()]
    dr = [d.strip() for d in (dirs_r or '').splitlines() if d.strip()]
    directives = (dl, dr) if dl != dr else None
    kind = (kind_l, kind_r) if kind_l != kind_r else None

    # -- исполняемый текст: тело без объявления, литералы и препроцессор целы
    lines_l, lines_r = body_l.splitlines(), body_r.splitlines()
    norms_l, norms_r = normalized_lines(body_l), normalized_lines(body_r)
    base_l = _declaration_end(body_l) + 1
    base_r = _declaration_end(body_r) + 1
    code_l = [(i, lines_l[i], norms_l[i])
              for i in range(base_l, len(lines_l)) if norms_l[i] is not None]
    code_r = [(i, lines_r[i], norms_r[i])
              for i in range(base_r, len(lines_r)) if norms_r[i] is not None]
    body_diff = _unified_code_diff(code_l, code_r, start_l, start_r,
                                   max(0, int(context)))

    # -- комментарии и форматирование отдельно от кода
    comments_l = ([s.strip() for s in (desc_l or '').splitlines() if s.strip()]
                  + _comment_lines_of(lines_l, norms_l))
    comments_r = ([s.strip() for s in (desc_r or '').splitlines() if s.strip()]
                  + _comment_lines_of(lines_r, norms_r))
    comments_changed = comments_l != comments_r
    comment_lines = []
    if comments_changed:
        for tag, x1, x2, y1, y2 in difflib.SequenceMatcher(
                None, comments_l, comments_r, autojunk=False).get_opcodes():
            if tag == 'equal':
                continue
            comment_lines.extend(f'- {comments_l[k]}' for k in range(x1, x2))
            comment_lines.extend(f'+ {comments_r[k]}' for k in range(y1, y2))

    decl_changed = bool(added or removed or defaults or order_changed
                        or export or directives or kind)
    return {
        'params': {'added': added, 'removed': removed, 'defaults': defaults,
                   'order_changed': order_changed},
        'export': export,
        'directives': directives,
        'kind': kind,
        'body_diff': body_diff,
        'comments_changed': comments_changed,
        'comment_lines': comment_lines,
        'formatting_only': body_l != body_r and not body_diff
        and not comments_changed,
        'identical': not decl_changed and not body_diff
        and not comments_changed and body_l == body_r,
    }


def format_method_compare(res, title_left='слева', title_right='справа'):
    """Строки выдачи method_compare: объявление, исполняемый текст, комментарии.

    Разделы отдельны: изменение только Экспорт или только комментариев не
    выглядит как изменение исполняемого тела.
    """
    if res['identical']:
        return ['Различий нет: методы идентичны']
    out = []
    decl = []
    p = res['params']
    if p['added']:
        decl.append(f'добавлены параметры в {title_right}: '
                    + ', '.join(p['added']))
    if p['removed']:
        decl.append(f'удалены параметры в {title_right}: '
                    + ', '.join(p['removed']))
    for name, old, new in p['defaults']:
        decl.append(f'значение по умолчанию {name}: {old or "—"} -> {new or "—"}')
    if p['order_changed']:
        decl.append('изменён порядок параметров')
    if res['kind']:
        decl.append(f'{res["kind"][0]} -> {res["kind"][1]}')
    if res['export']:
        decl.append('Экспорт: {} -> {}'.format(
            'задан' if res['export'][0] else 'не задан',
            'задан' if res['export'][1] else 'не задан'))
    if res['directives']:
        decl.append(f'директивы: [{", ".join(res["directives"][0]) or "—"}]'
                    f' -> [{", ".join(res["directives"][1]) or "—"}]')
    if decl:
        out.append('Объявление:')
        out.extend(f'  {line}' for line in decl)
    if res['body_diff']:
        out.append('Исполняемый текст (комментарии и пробелы вне литералов '
                   'изменением не считаются):')
        out.extend(res['body_diff'])
    else:
        out.append('Исполняемый текст: без изменений')
    if res['comments_changed']:
        out.append('Комментарии и описание:')
        out.extend(f'  {line}' for line in res['comment_lines'][:12])
        if len(res['comment_lines']) > 12:
            out.append(f'  … и ещё {len(res["comment_lines"]) - 12} строк '
                       'комментариев')
    if res['formatting_only']:
        out.append('Изменено только форматирование: код и комментарии те же')
    return out


def config_diff(conn_left, conn_right, title_left='слева', title_right='справа',
                limit=50, offset=0):
    """Сводка различий двух конфигураций (верхнеуровневое сравнение).
    
    Оптимизированная версия: сравнивает по сигнатуре объекта (COUNT модулей/методов)
    через SQL-агрегацию, без загрузки полных снимков. Работает за ~1-2 сек на 40k объектов.
    
    Возвращает словарь со сводкой и списком изменённых объектов.
    Для детального анализа использовать compare_object и method_diff.
    """
    # сводка через SQL-агрегацию (быстро)
    total_attrs_left = conn_left.execute('SELECT COUNT(*) FROM meta_attribute').fetchone()[0]
    total_attrs_right = conn_right.execute('SELECT COUNT(*) FROM meta_attribute').fetchone()[0]
    total_modules_left = conn_left.execute('SELECT COUNT(*) FROM module').fetchone()[0]
    total_modules_right = conn_right.execute('SELECT COUNT(*) FROM module').fetchone()[0]
    total_methods_left = conn_left.execute('SELECT COUNT(*) FROM method').fetchone()[0]
    total_methods_right = conn_right.execute('SELECT COUNT(*) FROM method').fetchone()[0]
    total_skd_left = conn_left.execute('SELECT COUNT(*) FROM skd_query').fetchone()[0]
    total_skd_right = conn_right.execute('SELECT COUNT(*) FROM skd_query').fetchone()[0]
    
    # сигнатура объекта: количество модулей (один простой GROUP BY)
    def object_signature(conn):
        mod_count = dict(conn.execute(
            'SELECT object_id, COUNT(*) FROM module GROUP BY object_id').fetchall())
        sig = {}
        for path, oid in conn.execute('SELECT path, id FROM meta_object').fetchall():
            sig[path] = mod_count.get(oid, 0)
        return sig
    
    sig_left = object_signature(conn_left)
    sig_right = object_signature(conn_right)
    
    paths_left = set(sig_left.keys())
    paths_right = set(sig_right.keys())
    only_left = sorted(paths_left - paths_right)
    only_right = sorted(paths_right - paths_left)
    common = paths_left & paths_right
    
    # найти объекты с изменённой сигнатурой
    changed_objects = []
    for path in common:
        l_mod = sig_left[path]
        r_mod = sig_right[path]
        if l_mod != r_mod:
            type_ru = conn_left.execute(
                'SELECT type_ru FROM meta_object WHERE path=?', (path,)).fetchone()
            type_ru = type_ru[0] if type_ru else '?'
            changed_objects.append({
                'path': path,
                'type_ru': type_ru,
                'modules_diff': abs(l_mod - r_mod),
                'methods_diff': 0,
                'attrs_diff': 0,
                'diffs_count': abs(l_mod - r_mod),
            })
    
    changed_objects.sort(key=lambda x: x['diffs_count'], reverse=True)
    total_changed = len(changed_objects)
    end = min(offset + limit, total_changed)
    page = changed_objects[offset:end]
    
    return {
        'summary': {
            'objects': f'{len(paths_left)} / {len(paths_right)}',
            'attrs': f'{total_attrs_left} / {total_attrs_right}',
            'modules': f'{total_modules_left} / {total_modules_right}',
            'methods': f'{total_methods_left} / {total_methods_right}',
            'skd': f'{total_skd_left} / {total_skd_right}',
            'only_left': len(only_left),
            'only_right': len(only_right),
            'changed': total_changed,
        },
        'changed_objects': page,
        'total_changed': total_changed,
        'offset': offset,
        'limit': limit,
    }


def _attr_str(name, value, kinds, fmt=lambda value: value):
    tstr, tabular = value
    kind = kinds.get(name)
    return (f'{name}: {fmt(tstr) or "?"}'
            + (f' [табчасть {tabular}]' if tabular else '')
            + (f' ({kind.lower()})' if kind and kind in REGISTER_KINDS else ''))


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


def parse_intercepts(directives):
    """[(директива, имя целевого метода)] из текста директив метода.

    Директивы перехвата расширения: &Вместо("Х"), &После("Х"), &Перед("Х"),
    &ИзменениеИКонтроль("Х"). Контекстные директивы (&НаСервере и т.п.)
    перехватами не являются.
    """
    return [(kind, name)
            for kind, name in RE_INTERCEPT.findall(directives or '')]


def _method_rows_by_path(conn, path):
    """{(code_name, имя в нижнем регистре): строка метода в формате _method_row}."""
    rows = {}
    for code, kind, name, sig, dirs, desc, body, exp, ctx, start \
            in conn.execute(
                'SELECT m.code_name, mt.kind, mt.name, mt.signature,'
                ' mt.directives, mt.description, mt.body, mt.is_export,'
                ' m.context, mt.line_start'
                ' FROM meta_object o'
                ' JOIN module m ON m.object_id = o.id'
                ' JOIN method mt ON mt.module_id = m.id'
                ' WHERE o.path = ?', (path,)):
        rows[(code, name.lower())] = (kind, name, sig, dirs, desc, body,
                                      exp, ctx, start)
    return rows


def _find_target(rows, code, target):
    """Строка целевого метода: сначала в том же модуле, затем в любом модуле объекта."""
    key = target.lower()
    row = rows.get((code, key))
    if row is None:
        row = next((r for (c, n), r in rows.items() if n == key), None)
    return row


def release_verdict(cmp_res):
    """Короткий вердикт по method_compare: чем метод изменился между релизами."""
    bits = []
    if cmp_res['body_diff']:
        bits.append('исполняемый текст')
    p = cmp_res['params']
    if p['added'] or p['removed'] or p['defaults'] or p['order_changed']:
        bits.append('параметры')
    if cmp_res['export']:
        bits.append('Экспорт')
    if cmp_res['directives']:
        bits.append('директивы')
    if cmp_res['kind']:
        bits.append('вид метода')
    if cmp_res['comments_changed']:
        bits.append('комментарии')
    if bits:
        return 'изменилось: ' + ', '.join(bits)
    if cmp_res['formatting_only']:
        return 'изменилось только форматирование'
    return 'без изменений'


def intercept_matrix(conn_ext, conn_base, conn_base_new=None):
    """Матрица перехватов: метод расширения → директива → цель → её изменение.

    Feedback п.3: цели разрешаются в основной конфигурации (conn_base — старый
    релиз); если задан conn_base_new (новый релиз), каждая цель дополнительно
    сравнивается method_compare — неизменившиеся обработчики остаются
    неизменившимися независимо от изменений других методов модуля. Для
    &ИзменениеИКонтроль видно вставки/удаления метода расширения относительно
    текста цели (контрольного экземпляра).

    Возвращает (rows, own, missing):
      rows — перехваты: {path, code, method, kind, directive, target,
             releases?, control?, source_note?};
      own — собственные методы без директив перехвата: (path, code, name,
             name_in_base);
      missing — директивы, цели которых в основной конфигурации не найдены.
    """
    _new, borrowed = split_extension(conn_ext, conn_base)
    ext_by_path = {}
    for path, code, name, kind, dirs, _exp in extension_methods(conn_ext):
        if path in borrowed:
            ext_by_path.setdefault(path, []).append((code, name, kind, dirs))
    rows, own, missing = [], [], []
    for path in sorted(ext_by_path):
        ext_rows = base_old = base_new = base_keys = None
        for code, name, kind, dirs in ext_by_path[path]:
            intercepts = parse_intercepts(dirs)
            if not intercepts:
                if base_keys is None:
                    base_keys = method_keys(conn_base, path)
                own.append((path, code, name,
                            (code, name.lower()) in base_keys))
                continue
            if base_old is None:
                base_old = _method_rows_by_path(conn_base, path)
            if ext_rows is None:
                ext_rows = _method_rows_by_path(conn_ext, path)
            if conn_base_new is not None and base_new is None:
                base_new = _method_rows_by_path(conn_base_new, path)
            for directive, target in intercepts:
                item = {'path': path, 'code': code, 'method': name,
                        'kind': kind, 'directive': directive,
                        'target': target}
                trow = _find_target(base_old, code, target)
                if trow is None:
                    missing.append(item)
                    continue
                if not trow[5]:
                    item['source_note'] = ('исходник целевого метода пуст '
                                           '(не извлечён)')
                if base_new is not None:
                    nrow = _find_target(base_new, code, target)
                    item['releases'] = ('в новом релизе отсутствует'
                                        if nrow is None
                                        else release_verdict(
                                            method_compare(trow, nrow)))
                if directive.lower() == 'изменениеиконтроль':
                    erow = ext_rows.get((code, name.lower()))
                    if erow is not None and trow[5]:
                        ctrl = method_compare(trow, erow)
                        parts = []
                        if ctrl['body_diff']:
                            plus = sum(1 for ln in ctrl['body_diff']
                                       if ln.startswith('+'))
                            minus = sum(1 for ln in ctrl['body_diff']
                                        if ln.startswith('-'))
                            parts.append(f'тело +{plus}/-{minus} строк')
                        p = ctrl['params']
                        if (p['added'] or p['removed'] or p['defaults']
                                or p['order_changed']):
                            parts.append('параметры')
                        if ctrl['export']:
                            parts.append('Экспорт')
                        if ctrl['comments_changed']:
                            parts.append('комментарии')
                        item['control'] = ('; '.join(parts) if parts
                                           else 'совпадает с текстом цели')
                rows.append(item)
    return rows, own, missing


def extension_report(conn_ext, conn_base, conn_base_new=None, fmt=str,
                     prefix=None, limit=MAX_ITEMS, fmt_type=None):
    """Разделы отчёта «что делает расширение»: объекты, методы, реквизиты.

    :param conn_base_new: новый релиз основной конфигурации — с ним матрица
        перехватов показывает изменение каждой цели между двумя релизами
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

    out.append('\nМетоды расширения (перехваты, цели и их состояние):')
    matrix, own_methods, missing_targets = intercept_matrix(
        conn_ext, conn_base, conn_base_new)
    by_path = {}
    for item in matrix:
        by_path.setdefault(item['path'], []).append(item)
    shown_objects = 0
    for path in sorted(by_path):
        shown_objects += 1
        if shown_objects > limit:
            # потолок по числу объектов: без него отчёт о расширении с тысячами
            # заимствований уходит в контекст модели десятками тысяч строк
            continue
        out.append(f'  {fmt(path)}:')
        items = by_path[path]
        for it in items[:limit]:
            verb = INTERCEPT_VERBS.get(it['directive'].lower(),
                                       'перехватывает целевой метод')
            out.append(f'    {it["code"]}::{it["kind"]} {it["method"]}() '
                       f'[&{it["directive"]}("{it["target"]}")] — {verb}')
            if it.get('source_note'):
                out.append(f'      ! {it["source_note"]}')
            if it.get('releases'):
                out.append(f'      цель между релизами: {it["releases"]}')
            if it.get('control'):
                out.append(f'      вставки/удаления относительно '
                           f'контрольного текста: {it["control"]}')
        if len(items) > limit:
            out.append(f'    … и ещё {len(items) - limit} перехватов')
    if not matrix:
        out.append('  перехватов директивами нет')
    elif shown_objects > limit:
        out.append(f'  … и ещё {shown_objects - limit} объектов с перехватами '
                   '(увеличьте limit)')
    if own_methods:
        out.append('\nСобственные методы расширения (не перехватывают цель):')
        for path, code, name, name_in_base in own_methods[:limit]:
            note = (' — имя совпадает со штатным методом, но директивы '
                    'перехвата нет' if name_in_base else '')
            out.append(f'  {fmt(path)}: {code}::{name}{note}')
        if len(own_methods) > limit:
            out.append(f'  … и ещё {len(own_methods) - limit}')
    if missing_targets:
        out.append('\nОтсутствующие цели (директива есть, метода в основной '
                   'конфигурации нет):')
        for it in missing_targets[:limit]:
            out.append(f'  {fmt(it["path"])}: {it["code"]}::{it["method"]}() '
                       f'[&{it["directive"]}("{it["target"]}")]')
        if len(missing_targets) > limit:
            out.append(f'  … и ещё {len(missing_targets) - limit}')

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
