"""1confdb-knw — MCP-сервер знаний по конфигурации 1С и BSL.

Рассчитан на использование любой LLM без контекста проекта: инструкции
протокола и описания инструментов содержат справочник по базе данных,
глоссарий 1С и рекомендуемые рабочие процессы.

Транспорты:
- stdio (по умолчанию): JSON-RPC 2.0, сообщения по одному на строку stdin/stdout;
  запуск, в т.ч. через SSH со стороны MCP-клиента:
    1confdb-knw <путь-к-базе.sqlite>
    python -m confdb.mcp_server <путь-к-базе.sqlite>
- HTTP (опция --port): сервер слушает порт, клиент подключается по URL
  (Streamable HTTP: POST /mcp; legacy SSE: GET /sse + POST /messages).
  Для доступа с другой машины — SSH-туннель:
    ssh -L 8765:127.0.0.1:8765 user@host
  и в конфиге клиента {"url": "http://127.0.0.1:8765/mcp"}.

Путь к базе можно не указывать — тогда берётся last_db из
~/.confdb/config.json, а при его отсутствии база ищется сама:
*.db/*.sqlite в текущем каталоге, db/ и _out/ (и в корне установки,
если запуск из venv). Свежая установка с привезённой базой работает
без ручной правки конфига.

Баз можно открыть несколько одновременно (например, основная
конфигурация + расширения/обработки): перечислите несколько путей
при запуске либо открывайте базы инструментом db_open уже на ходу;
активная база переключается инструментом db_use.
"""
import argparse
import glob
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import compare
from . import header_props
from .config import load_config
from .db.writer import FTS_TABLE, TYPE_RU, fts_index_info, fts_shard_paths, \
    tabular_field_counts

PROTOCOL_VERSION = '2024-11-05'

# пути объектов наружу — «как в конфигураторе»: Справочник.Имя[.Подобъект];
# на вход принимается и старый слэш-формат Catalog/Имя
_RU2TYPES = {}
for _stem, _ru in TYPE_RU.items():
    _RU2TYPES.setdefault(_ru, []).append(_stem)
    _RU2TYPES.setdefault(_ru.replace(' ', ''), []).append(_stem)
# Русское имя типа принимается в любом регистре и без пробелов: сервер печатает
# 'Регистр накопления.Имя', а язык запросов и BSL пишут 'РегистрНакопления.Имя'
# — это один и тот же тип.
_RU2TYPES_LOW = {}
for _key, _stems in _RU2TYPES.items():
    for _stem in _stems:
        if _stem not in _RU2TYPES_LOW.setdefault(_key.lower(), []):
            _RU2TYPES_LOW[_key.lower()].append(_stem)
_TYPE_SLASH_RE = re.compile(
    '(?:' + '|'.join(map(re.escape, sorted(TYPE_RU, key=len, reverse=True))) + ')/')
# 'Справочник.Имя' в строковых литералах sql -> внутренний 'Catalog/Имя'
_RU_PATH_LIT_RE = re.compile(
    "'(" + '|'.join(map(re.escape, sorted(_RU2TYPES, key=len, reverse=True))) +
    r")\.([^'.]+)'", re.IGNORECASE)


def _type_stems(name):
    """Англ. stem'ы типа метаданных по его русскому имени (регистр не важен)."""
    return _RU2TYPES_LOW.get((name or '').strip().lower())


def _sql_rewrite(query):
    def sub(match):
        stems = _type_stems(match.group(1))
        return f"'{stems[0]}/{match.group(2)}'" if stems else match.group(0)
    return _RU_PATH_LIT_RE.sub(sub, query)


def ru_path(path):
    """'Catalog/Х/CatalogForm/У' -> 'Справочник.Х.У'."""
    parts = str(path).split('/')
    if len(parts) % 2 or parts[0] not in TYPE_RU:
        return str(path)
    return '.'.join([TYPE_RU[parts[0]]] + parts[1::2])


_REGISTER_TYPES = frozenset({
    'InformationRegister', 'AccumulationRegister',
    'AccountingRegister', 'CalculationRegister',
})

_PERIODICITY = header_props.PERIODICITY


def _register_card_info(obj_type, header_json):
    """Свойства регистра из header_json: периодичность, режим записи."""
    return header_props.register_props(obj_type, header_json)


def _build_event_index(conn):
    """Подписки на события одной базы: by_path, by_source, by_handler.

    Ключ by_handler — (путь общего модуля, имя метода в нижнем регистре): по
    нему get_method отвечает, что метод вызывается ещё и подпиской, которую
    лексический анализ кода не видит.
    """
    q = conn.execute
    index = {'by_path': {}, 'by_source': {}, 'by_handler': {}}
    if not q("SELECT 1 FROM meta_object WHERE type='EventSubscription'"
             ' LIMIT 1').fetchone():
        return index
    refs, by_uuid = {}, {}
    for path, uuid, header in q('SELECT path, uuid, header_json FROM meta_object'):
        if uuid:
            by_uuid.setdefault(uuid, path)
        ref = header_props.self_ref_uuid(header)
        if ref:
            refs.setdefault(ref, path)
    for path, header in q("SELECT path, header_json FROM meta_object"
                          " WHERE type='EventSubscription' ORDER BY ord, path"):
        props = header_props.event_subscription('EventSubscription', header)
        if not props:
            continue
        sources = [refs[u] for u in props['sources'] if u in refs]
        entry = dict(props, path=path, sources=sources,
                     handler=by_uuid.get(props['handler_uuid'] or ''),
                     unresolved=len(props['sources']) - len(sources))
        index['by_path'][path] = entry
        for src in sources:
            index['by_source'].setdefault(src, []).append(entry)
        if entry['handler'] and entry['handler_method']:
            key = (entry['handler'], entry['handler_method'].lower())
            index['by_handler'].setdefault(key, []).append(entry)
    return index


def _handler_name(entry):
    """«Общий модуль.Имя.Метод» — обработчик подписки, или почему его нет."""
    if entry['handler'] and entry['handler_method']:
        return f'{ru_path(entry["handler"])}.{entry["handler_method"]}'
    if entry['handler']:
        return f'{ru_path(entry["handler"])} (имя метода не извлечено)'
    return 'общий модуль не найден в базе'


def _subscription_lines(entry, method_found=True):
    """Строки подписки для паспорта: событие, обработчик, источники."""
    handler = _handler_name(entry)
    if entry['handler'] and entry['handler_method'] and not method_found:
        # подписка объявлена, а метода в модуле нет: правило не работает,
        # и это важнее, чем его отсутствие в списке вызовов
        handler += ' (метод не найден в модуле)'
    total = len(entry['sources']) + entry['unresolved']
    shown = entry['sources'][:8]
    text = ', '.join(ru_path(s) for s in shown)
    if len(entry['sources']) > len(shown):
        text += f', … ещё {len(entry["sources"]) - len(shown)}'
    if entry['unresolved']:
        text = ((text + '; ') if text else '') + \
            f'{entry["unresolved"]} из {total} не распознано'
    return [f'Событие: {entry["event"]}', f'Обработчик: {handler}',
            f'Источники ({total}): ' + (text or 'не указаны')]


def _source_subscriptions_line(entries):
    """Строка «Подписки на события» в паспорте объекта-источника."""
    parts = [f'{e["event"]} → {_handler_name(e)}' for e in entries[:6]]
    if len(entries) > 6:
        parts.append(f'… ещё {len(entries) - 6}')
    return 'Подписки на события: ' + '; '.join(parts)


def _method_subscriptions_line(entries, limit=6):
    """Строка «вызывается подписками на события» в ответе get_method."""
    parts = [f'{e["event"]} — {ru_path(e["path"])} '
             f'(источников: {len(e["sources"]) + e["unresolved"]})'
             for e in entries[:limit]]
    if len(entries) > limit:
        parts.append(f'… ещё {len(entries) - limit}')
    return 'вызывается подписками на события: ' + '; '.join(parts)


def _exchange_plan_lines(uuids, known):
    """Строка состава плана обмена: какие объекты он синхронизирует."""
    total = len(uuids)
    groups = {}
    missing = 0
    for u in uuids:
        hit = known.get(u)
        if hit is None:
            missing += 1
        else:
            groups.setdefault(hit[1], []).append(hit[0])
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    if total <= 12:
        text = ', '.join(ru_path(p) for _, paths in ordered for p in paths)
    else:
        # состав бывает на тысячи объектов (у плана ОбновлениеИнформационнойБазы
        # УНФ их 1889): перечень имён утопил бы паспорт, поэтому крупные планы
        # показываются разбивкой по типам
        text = ', '.join(f'{TYPE_RU.get(t, t)} {len(paths)}'
                         for t, paths in ordered)
    if missing:
        text = ((text + '; ') if text else '') + \
            f'{missing} из {total} не найдено в базе'
    return [f'Состав плана обмена ({total}): ' + (text or 'пуст')]


def _predefined_lines(q, oid, rows, obj_type, limit=60):
    """Дерево предопределённых элементов с видами субконто счёта.

    rows — (ord, parent_ord, name, code, display) в порядке обхода в глубину.
    Корневой узел («Счета», «Элементы») элементом не является: в паспорт он не
    печатается, но остаётся в таблице строкой с parent_ord IS NULL.
    """
    subconto = {}
    for owner, kind, flags in q(
            "SELECT p.ord, COALESCE(NULLIF(k.display, ''), k.name, s.uuid), s.flags"
            ' FROM predefined_subconto s'
            ' JOIN predefined p ON p.id = s.predefined_id'
            ' LEFT JOIN predefined k ON k.id = s.kind_id'
            ' WHERE p.object_id = ? ORDER BY s.predefined_id, s.ord', (oid,)):
        subconto.setdefault(owner, []).append(kind + (f' [{flags}]' if flags else ''))
    children = {}
    for ord_no, parent, name, code, display in rows:
        children.setdefault(parent, []).append((ord_no, name, code, display))
    total = sum(1 for row in rows if row[1] is not None)
    noun = 'счета' if obj_type == 'ChartOfAccounts' else 'элементы'
    lines = [f'Предопределённые {noun} ({total}):']
    shown = 0

    def walk(parent, depth):
        nonlocal shown
        for ord_no, name, code, display in children.get(parent, []):
            if shown >= limit:
                return
            code = (code or '').strip()
            label = name if not display or display == name else f'{name} — {display}'
            lines.append('  ' * (depth + 1) + (f'{code} {label}' if code else label))
            kinds = subconto.get(ord_no)
            if kinds:
                lines.append('  ' * (depth + 2) + 'субконто: ' + '; '.join(kinds))
            shown += 1
            walk(ord_no, depth + 1)

    for row in rows:
        if row[1] is None:
            walk(row[0], 0)
    if total > shown:
        lines.append(f'  … и ещё {total - shown} (таблица predefined)')
    return lines


def ru_text(text):
    """Внутренние слэш-пути 'Catalog/Х' -> 'Справочник.Х' в произвольном тексте.

    Применяется и к сообщениям валидатора запросов: наружу всё отдаётся в форме
    «как в конфигураторе», внутренний формат пути не должен попадать в ответ.
    """
    if not text:
        return text
    return _TYPE_SLASH_RE.sub(lambda m: TYPE_RU[m.group(0)[:-1]] + '.', text)


def ru_type_str(text):
    """'Ссылка: Catalog/Валюты' -> 'Ссылка: Справочник.Валюты'.

    Голая 'Ссылка' — обобщённый тип платформы (ЛюбаяСсылка, Характеристика):
    его uuid не соответствует ни одному объекту конфигурации, поэтому подпись
    говорит о неконкретизированном типе, а не о ненайденной цели. Отдельный
    случай «цель известна по имени, но объекта в базе нет» writer помечает сам.
    """
    if not text:
        return text
    text = ru_text(text)
    parts = [p.strip() for p in text.split(' | ')]
    annotated = []
    for part in parts:
        if part == 'Ссылка':
            annotated.append('Ссылка (тип не конкретизирован)')
        else:
            annotated.append(part)
    return ' | '.join(annotated)


def body_hits(body, needle, line_start=1, limit=3, width=150):
    """Строки тела метода, содержащие подстроку: ['строка N: <текст>', …].

    Номер строки — в модуле, а не в теле (line_start метода уже учтён), поэтому
    по нему работают get_method и find_method_context. Сравнение без учёта
    регистра: LIKE в SQLite сворачивает регистр только для ASCII, а имена 1С
    кириллические, поэтому здесь сворачиваем сами.
    """
    low = needle.lower()
    hits = []
    for i, line in enumerate(body.split('\n')):
        if low in line.lower():
            hits.append(f'строка {line_start + i}: {line.strip()[:width]}')
            if len(hits) >= limit:
                break
    return hits


MAX_PAGE_LIMIT = 200    # потолок страницы поисковых инструментов (как LIMIT у sql)
COUNT_CACHE_MAX = 128   # сколько итогов COUNT(*) помнить на одну базу


def page_limit(limit):
    """Размер страницы поиска: целое в границах 1..MAX_PAGE_LIMIT.

    Потолок общий для всех поисковых инструментов: раз выдачу можно листать
    параметром offset, раздувать limit незачем, а большой ответ съедает
    контекст вызывающей модели.
    """
    return max(1, min(int(limit), MAX_PAGE_LIMIT))


def page_offset(offset):
    """Сколько первых совпадений того же поиска пропустить: целое >= 0."""
    return max(0, int(offset))


def page_note(total, offset, shown, detail=''):
    """Хвост поискового ответа: total_count, диапазон и offset следующей порции.

    Модель обязана видеть, что список обрезан, и как взять продолжение: без
    этой строки «первые 20» выглядят как все найденные, и вместо листания
    страниц вызывающая сторона начинает уточнять маску наугад.
    """
    if not shown:
        if not total:
            return f'всего найдено 0{detail}'
        return (f'всего найдено {total}{detail}; offset={offset} за пределами '
                'списка — повторите с offset=0')
    first, last = offset + 1, offset + shown
    if last < total:
        return (f'всего найдено {total}{detail}; показано {first}–{last}; '
                f'есть ещё {total - last} — следующий вызов с offset={last}')
    return f'всего найдено {total}{detail}; показано {first}–{last}; это все результаты'


def _source_row(conn):
    """Строка `source` базы как словарь: file, created, root_uuid, file_sha256, file_size.

    Отпечаток исходника (`extract.file_sha256`) пишется в базу с 2026-10-05, поэтому
    в старых базах колонок file_sha256/file_size просто нет: их отсутствие — не
    ошибка схемы, а «отпечаток неизвестен».
    """
    cols = {row[1] for row in conn.execute('PRAGMA table_info(source)')}
    names = [c for c in ('file', 'created', 'root_uuid', 'file_sha256',
                         'file_size') if c in cols]
    if not names:
        return {}
    row = conn.execute(
        f'SELECT {", ".join(names)} FROM source ORDER BY id LIMIT 1').fetchone()
    if not row:
        return {}
    info = dict(zip(names, row))
    info.setdefault('file_sha256', None)
    info.setdefault('file_size', None)
    return info


def sha_line(sha256, size=None):
    """'SHA-256 исходника: <отпечаток> (<размер> байт)' либо почему его нет."""
    if not sha256:
        return ('SHA-256 исходника: не сохранён (база собрана раньше, чем '
                'появился отпечаток; пересобрать — confdb extract --force)')
    tail = f' ({size} байт)' if size else ''
    return f'SHA-256 исходника: {sha256}{tail}'


PRIMER = """1confdb-knw: MCP server over one or several knowledge bases of a 1C:Enterprise 8 configuration — metadata, BSL code and SKD queries, extracted from binary .cf/.cfe/.epf files into SQLite. 1C is a Russian business-automation platform; a configuration contains metadata objects, their fields, modules of 1C-language code (Russian keywords) and SKD report queries. All object/field names are in Russian.

GLOSSARY: Catalog=справочник (directory), Document=документ, InformationRegister/AccumulationRegister=регистры, Enum=перечисление, DataProcessor=обработка, Report=отчет, DefinedType=определяемый тип, CommonAttribute=общий реквизит, CommonModule=общий модуль. Tabular section (табличная часть) = row table of an object (e.g. Документ.ЗаказПокупателя has section Запасы with fields Номенклатура, Цена…).

OBJECT PATHS: tools return and accept configurator-style Russian dotted paths: 'Справочник.Номенклатура', nested 'Справочник.Х.ФормаЭлемента' (legacy 'Catalog/Х/…' slash form is also accepted as input). In the 1C query language the table name for an object is exactly this dotted form: 'Справочник.Имя', 'Документ.Имя', 'РегистрСведений.Имя'…

COMMON MODULES: in BSL code a common module is called by its bare name: 'ИмяМодуля.Функция(...)'. Prefixes like 'ОбщийМодуль.', 'Общий модуль.', 'ОбщМодуль.' are NOT valid code — never write them. The dotted 'Общий модуль.Имя' form only identifies the object in this knowledge base.

DATABASE FILE: the SQLite file is internal to the server. Do NOT search for it, open it, read it from disk, or ask the user for its location — you have no filesystem access to it. Everything is available through the tools below; the sql tool runs arbitrary read-only SELECTs.

MULTIPLE DATABASES: the server can hold several knowledge bases at once — typically the MAIN configuration plus extensions/data processors (.cfe/.epf extracted into their own .db files). Each open base has an alias. All tools query the ACTIVE base; to query a specific base without switching, pass its alias as the db parameter (e.g. find_objects(mask=…, db='расш_интеграция')). Management tools: db_list (what is open, which is active), db_open (open another base file while the server runs — the path comes from the user), db_use (switch the active base), db_close. An extension usually adds/overrides objects of the main configuration — if something is not found in one base, check the other. Special db value '*': run a tool on every open base at once (the answer is sectioned per base) — one call to compare the main configuration with all extensions.

CONFIGURATION GROUPS: a group bundles related databases (main configuration + extensions + data processors) as a single unit. SEVERAL groups can be open at the same time — typically two different configurations, or two releases of the same one; group_list shows them all and marks the active one. To query ONE group separately, pass group=<name> to any data tool: it runs on every database of that group and sections the answer per database. group='*' runs the tool on every group at once, keeping the groups apart in the answer. WITHOUT the group parameter only the ACTIVE base is queried — NOT the whole active group — so when several groups are open always name the group you mean; db='*' ignores the division and mixes every open base, so prefer group='*' for a per-group picture. Priority: explicit group > explicit db > the active base. group_use switches the active group and makes its first base the active one. Management tools: group_create (create an empty group), group_add_db (add an open database to a group), group_remove_db (remove a database from a group — the database itself stays open), group_list (all groups with their databases), group_use (switch the active group), group_close (delete a group — databases are NOT closed). compare_object and extension_diff take explicit aliases (db_left/db_right, extension_db/base_db) and know nothing about groups — pick the aliases with db_list/group_list.

DATABASE IDENTIFIER: every tool response includes a header line identifying the source database and, when the base belongs to a group, the group: '=== группа <имя> / база <алиас> (<путь>) ===' (a base in several groups prints all of them: '=== группы A, B / база …'). Read it before trusting a result: it says which configuration the answer came from. This lets you compare configurations (e.g. standard vs customized) or understand which base contains a method (main configuration vs extension). Use db='*' to query all bases at once and compare results side-by-side.

PAGING SEARCH RESULTS: find_objects, find_field, find_methods, find_skd, find_xdto, refs_of, role_rights and object_rights return ONE PAGE of their hits — limit is the page size (1..200), offset is how many hits of the SAME search to skip. Every answer ends with a paging line: '… всего найдено N; показано a–b; есть ещё K — следующий вызов с offset=b' (total hit count, the range shown, how many are left and the offset to pass for the next page); when the page holds everything it says 'это все результаты' instead. The order of hits is stable, so consecutive pages never overlap or skip — to read further, call the same tool with the printed offset. Do NOT raise limit to 200 and do not narrow the mask just to fit one answer: page. find_xdto counts matching types and properties as one list (types first, then properties) and names both counts; refs_of pages each of its two lists with the same offset; role_rights pages the targets of a role and object_rights pages the roles touching an object.

COMPARING BASES: compare_object(path, db_left, db_right) diffs ONE object between two open bases in a single call — attributes and their types, tabular sections, register dimensions/resources, forms and commands, modules, methods (signature, directives, body), SKD queries. Use it for standard-vs-customized or release-to-release analysis instead of fetching two passports and diffing them by hand. extension_diff(extension_db, base_db) answers the task-level question 'what does this extension do': new objects (carrying the extension name prefix), borrowed objects, the extension methods and whether one REPLACES a stock method (&Вместо) or inserts code around it (&После/&Перед), the attributes it adds, and its external dependencies. configuration_info says WHICH configuration and release a base holds and WHAT KIND of file it came from — .cf configuration, .cfe extension, .epf external data processor, .erf external report (name, version, compatibility mode, source file, build date); db_list repeats the kind and the version in one line per open base. These three take explicit base aliases (db_left/db_right, extension_db/base_db), not the db parameter, and db='*' does not apply to them.

REGISTERS: object_card of a РегистрСведений/РегистрНакопления lists Измерения (dimensions — they form the record key), Ресурсы (resources — the stored values) and Реквизиты (attributes) as SEPARATE groups, plus Периодичность and Режим записи (независимый / подчинение регистратору). Before writing СрезПоследних or joining a register, check whether the field you rely on is a dimension: only dimensions guarantee one row per key. A periodicity code that could not be decoded is shown as the raw code, never as a guessed name.

ROLE RIGHTS: role_rights(role) lists the EXPLICIT rights one role carries — a line per target (the object itself, one of its attributes / tabular-section fields, or one of its tabular sections) with the rights set on it and the record-level restriction (RLS) text; object_rights(path) turns it around and lists the roles that carry explicit rights on that object, its fields and sections included (pass role to see one role-object pair in full). Storage is SPARSE: a target or a role absent from the answer means the right is NOT SET, never 'denied' — and roles whose rights file could not be read are counted out loud instead of being silently missing. A right is printed as the first 8 hex chars of its platform uuid plus the value exactly as stored (1 or -1): the configuration holds NO right names and no value dictionary, so never render them as 'Чтение'/'Запись' or as allowed/forbidden — say that the name is not confirmed. Bases built before 2026-10-07 have no rights tables at all and both tools say so instead of failing.

DATABASE SCHEMA (for the sql tool; path columns store the legacy slash form 'Catalog/Имя', but string literals in the Russian dotted form ('Справочник.Имя') are auto-converted — either form works in WHERE path = …):
- meta_object(id, path, type, type_ru, name, uuid, comment, parent_id, ord). path like 'Catalog/Номенклатура'; type = English stem (Catalog, Document, InformationRegister, Enum, CommonModule, DefinedType…); type_ru = Russian label as in the configurator.
- meta_attribute(object_id, ord, name, type_str, tabular, uuid). Object fields; tabular NULL = header attribute, else the tabular section the field belongs to; uuid = the field's own metadata id (what a role right on a field points at), NULL when the header carries none. type_str examples: 'Строка(50)', 'Число', 'Ссылка: Справочник.Валюты', 'ОпределяемыйТип: … (Ссылка: …)', composites joined with ' | '. Read the unresolved forms as follows: 'Ссылка' alone = a generic platform type (any reference), NOT an extraction failure; 'Ссылка: Имя (объект не найден в базе)' = the target name is known but no such object exists in THIS base — check the other open bases; type_str NULL = the metadata header carries no type description for this field, i.e. not extracted.
- meta_tabular(object_id, ord, name, uuid) — tabular sections in declaration order; uuid = the section's own metadata id.
- module(object_id, code_name, context, body). code_name: 'obj' (object module), 'mgr' (manager module), form/common modules etc.; context = execution context for common modules (Сервер/Клиент/…); body = module text WITHOUT method bodies (signatures, comments, #Если regions) — a table of contents.
- method(id, module_id, ord, kind, name, signature, is_export, directives, description, line_start, line_end, body). Procedures/functions of the 1C code; directives like '&НаСервере'/'&НаКлиенте'; description = comment block above the method.
- attribute_ref(attribute_id, ord, uuid, object_id) — which metadata objects a field's type references (one row per member; NULL object = a generic platform type, whose uuid matches no object of this configuration). Use for joins and impact analysis ('who references X').
- xdto_type(object_id, ord, name, kind, base, base_ns, facets, enum_values) — the types an XDTO package declares: kind is objectType, valueType (a simple/enumeration type) or typeDef (an anonymous type nested in a property); base/base_ns name the base type and the namespace it comes from; facets holds the remaining XML attributes as 'name=value; …' (maxLength, totalDigits, localName…); enum_values lists the allowed values of an enumeration type. xdto_property(type_id, object_id, ord, name, type, type_ns, lower_bound, upper_bound, nillable, form, extra, nested_type_id) — properties: lower_bound=1 = obligatory, upper_bound=-1 = a list, form = Attribute|Element, nested_type_id → an anonymous nested type, extra = the remaining attributes; type_id NULL = a property declared by the package itself, outside any type. xdto_import(object_id, ord, namespace) — the namespaces the package imports. Prefer xdto_of/find_xdto over querying these directly.
- skd_query(object_id, ord, query) — report queries in the 1C query language (Russian keywords ВЫБРАТЬ/ИЗ/ГДЕ/СОЕДИНЕНИЕ/ОБЪЕДИНИТЬ).
- enum_value(object_id, ord, name) — enum values; predefined(object_id, ord, parent_ord, uuid, name, code, display) — predefined elements (catalog items, chart-of-accounts accounts, characteristic-chart values) in depth-first order: parent_ord is the ord of the parent, and NULL only for the root node ('Счета'/'Элементы'), which is not an element; uuid identifies the element and is what a subconto kind points at; predefined_subconto(predefined_id, ord, uuid, kind_id, flags) — the subconto kinds of a predefined account: kind_id → the predefined element of the characteristic chart that names the kind, flags = 'Суммовой;Валютный;Количественный'; common_target(common_id, target_id) — objects a common attribute is attached to; subsystem_content — subsystem composition; source(file, created, root_type/root_name/root_uuid, file_size, file_sha256) — which .cf/.cfe/.epf the base was built from, when, and the SHA-256 of that file: equal digests in two bases mean the very same source file, so nothing has to be re-extracted (NULL in bases built before the digest was added); file.
- role_right(role_id, target_uuid, target_object_id, target_attr_id, target_tabular_id, sub_index, collection_uuid, target_flags, right_uuid, value, rls_text) — explicit role rights, SPARSE (no row = the right is not set); target_flags keeps the flags of the target record verbatim (their meaning is NOT confirmed — they only tell two targets sharing one uuid apart); role_rls_template(role_id, ord, name, text) — the role RLS templates; role_rights_state(role_id, version, parsed, targets, rights, rls_templates, error) — parsed=0 means the rights file could NOT be read (the reason is in error), which is not the same as a role without rights. Absent in bases built before 2026-10-07. Prefer role_rights / object_rights over querying these.

1C QUERY LANGUAGE: Russian keywords, dotted paths, table names 'Справочник.Имя', 'Документ.Имя', 'РегистрСведений.Имя', 'РегистрНакопления.Имя.Обороты' (virtual tables: Остатки, Обороты, СрезПоследних…). Grouping clause is 'СГРУППИРОВАТЬ ПО' — the form 'СГРУППИРОВАНО' does NOT exist in the 1C query language. Example: ВЫБРАТЬ Т.Запасы.Номенклатура.Наименование ИЗ Документ.ЗаказПокупателя КАК Т ГДЕ Т.Сумма > 0.

RECOMMENDED WORKFLOW to write a query or 1C code: 1) configuration_info to know which configuration and release you are in, find_objects to locate objects; 2) object_card for its fields, sections, references and the event subscriptions fired on it (the platform calls those handlers, so there is no call site to find in code); 3) skd_of / find_skd to see how THIS configuration queries the same tables (best examples); 4) find_methods — by mask for a name/signature/description, or by text to search INSIDE method bodies: that is how you find EVERY place touching something (all writes to a register, all calls of a common module, all uses of a field) without falling back to a full-text sql query, and every hit carries its module line number; find_methods(text='"Имя"') finds all places where a string literal is mentioned (useful for dynamic calls); then find_method_context for a window around the call you need (it also gives stable insertion markers) and get_method for the full body — reuse existing code instead of inventing; 5) check_query to validate your query before use; 6) method_dependencies before porting code to another configuration (it lists everything the code needs there — including dynamic calls via Вычислить/Выполнить with string literals, resolved against the configuration's modules and objects), compare_object / extension_diff to see how two configurations differ; 7) method_result_schema when a stock function returns a temporary table and you need its columns; 8) role_rights / object_rights for access analysis — what a role is given and which roles carry explicit rights on an object (its fields and tabular sections included). This server does NOT check 1C code syntax — for that use the 1confdb-knw-lsp variant (BSL Language Server).

All tools are read-only. Prefer the dedicated tools over raw sql; use sql only for what is not covered. ANTI-LOOP: never issue more than two sql calls in a row — if sql did not answer the question, switch to the dedicated tools (find_objects, object_card, find_field, skd_of, refs_of). The schema is EXACTLY as documented above — never waste calls on PRAGMA / sqlite_master / schema guessing."""


def _group_items(groups):
    """Группы к открытию -> [(имя, [пути])]: словарь либо список пар."""
    if not groups:
        return []
    items = groups.items() if hasattr(groups, 'items') else groups
    result = []
    for item in items:
        name, paths = (item if isinstance(item, (tuple, list)) and len(item) == 2
                       else (None, None))
        if not name or not str(name).strip():
            raise ValueError(f'имя группы не может быть пустым: {item!r}')
        if isinstance(paths, str):
            paths = [paths]
        result.append((str(name).strip(), list(paths or ())))
    return result


# -- права ролей ----------------------------------------------------------
# Право печатается первыми знаками его uuid: словаря «uuid -> русское имя права»
# в конфигурации нет (66 uuid прав не встречаются ни в одном .json дампа),
# поэтому выдуманного имени в выдаче быть не должно. Значение тоже идёт как
# записано в роли: его смысл платформенным словарём не подтверждён.
RIGHT_UUID_PREFIX = 8
RLS_TEXT_LIMIT = 600      # ограничение доступа длинное — страница не должна тонуть
TARGETS_PER_ROLE = 12     # сколько целей объекта печатать в строке роли

RIGHTS_NOTE = ('право = первые 8 знаков его uuid (имени права в конфигурации нет),'
               ' значение — как в файле роли (1 или -1), смысл значения'
               ' платформенным словарём не подтверждён')

NO_RIGHTS_TABLES = ('в базе нет прав ролей: она собрана раньше, чем появились'
                    ' таблицы role_right/role_rights_state (2026-10-07) —'
                    ' пересобрать: confdb extract <файл> --db <база> --force')


def _rights_tables(conn):
    """Есть ли в базе таблицы прав ролей (пишутся с 2026-10-07)."""
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name IN ('role_right','role_rights_state')").fetchone()[0] == 2


def _right_labels(rows):
    """'287b74b8=1; aa6448f2=-1' — из записей (uuid права, значение, RLS)."""
    return '; '.join(f'{right[:RIGHT_UUID_PREFIX]}={value}'
                     for right, value, _rls in rows)


def _target_label(obj_path, attr_name, attr_tabular, tab_name, target_uuid,
                  sub_index, collection_uuid, flags=None):
    """Кем приходится цель права: объект, его реквизит/поле ТЧ или его ТЧ.

    Неразрешённый uuid печатается как есть: ни объектом, ни реквизитом, ни
    табличной частью этой базы он не является, а выдумывать сущность нельзя —
    смысл адресации «объект + отрицательный номер внутри коллекции» и у
    специального маркера `de29c81d-…` не подтверждён. `flags` — флаги записи
    цели из файла роли: они добавляются только когда без них две цели
    неотличимы, потому что их смысл тоже не подтверждён.
    """
    if attr_name:
        base = ru_path(obj_path)
        label = (f'{base}, поле {attr_tabular}.{attr_name}' if attr_tabular
                 else f'{base}, реквизит {attr_name}')
    elif tab_name:
        label = f'{ru_path(obj_path)}, табличная часть {tab_name}'
    elif obj_path:
        label = ru_path(obj_path)
    else:
        label = f'цель не распознана (uuid {target_uuid})'
    if sub_index is not None:
        tail = f'подобъект №{sub_index}'
        if collection_uuid:
            tail += f' коллекции {collection_uuid[:RIGHT_UUID_PREFIX]}'
        label = f'{label} — {tail}'
    return f'{label} (флаги цели {flags})' if flags else label


def _rls_lines(rows):
    """Тексты ограничений доступа (RLS) из записей права — по строке на право."""
    out = []
    for right, _value, rls in rows:
        if not rls:
            continue
        text = ' '.join(str(rls).split())
        if len(text) > RLS_TEXT_LIMIT:
            text = text[:RLS_TEXT_LIMIT] + '… (обрезано)'
        out.append(f'    RLS для права {right[:RIGHT_UUID_PREFIX]}: {text}')
    return out


class McpServer:
    """Обработчик JSON-RPC сообщений MCP поверх баз SQLite (read-only).

    Держит несколько баз одновременно (например, основная конфигурация
    плюс расширения/обработки): каждая видна под алиасом, инструменты
    работают с активной базой либо с явно указанной параметром db.
    """

    def __init__(self, db_paths=None, groups=None):
        self.dbs = {}      # алиас -> {'path':…, 'conn':…, 'ctx':…}
        self.active = None
        self.groups = {}   # имя_группы -> [алиас1, алиас2, ...]
        self.active_group = None
        if isinstance(db_paths, str):
            db_paths = [db_paths]
        # Группы открываются первыми: активными становятся первая группа и её
        # первая база, а не «одиночная» база из db_paths (CLI --group, TUI).
        for name, paths in _group_items(groups):
            self.add_group_paths(name, paths)
        for path in db_paths or ():
            # активна первая указанная база, а не последняя
            self.open_db(path, activate=False)

    # -- реестр баз ----------------------------------------------------------
    def _make_alias(self, path):
        base = os.path.splitext(os.path.basename(path))[0] or 'db'
        alias, num = base, 1
        while alias in self.dbs:
            num += 1
            alias = f'{base}_{num}'
        return alias

    def open_db(self, path, alias=None, activate=True):
        """Открывает базу и возвращает её алиас.

        Файл проверяется: должен существовать и содержать таблицу
        meta_object (база знаний confdb). Повторное открытие того же
        файла без явного алиаса возвращает прежний алиас. Если alias
        указан явно и отличается от существующего — создаётся новое
        подключение (позволяет работать с одной базой под разными именами).
        """
        path = os.path.abspath(path)
        known = next((a for a, d in self.dbs.items()
                      if os.path.abspath(d['path']) == path), None)
        if known is not None and alias is None:
            # Повторное открытие без явного алиаса — возвращаем существующий
            if activate:
                self.active = known
            return known
        if not os.path.isfile(path):
            raise ValueError(f'файл базы не найден: {path}')
        if alias is not None:
            if not str(alias).strip():
                raise ValueError('алиас не может быть пустым')
            alias = str(alias).strip()
            if alias.lower() in (a.lower() for a in self.dbs):
                raise ValueError(f'алиас уже занят: {alias}')
        conn = sqlite3.connect(
            f'file:{path}?mode=ro', uri=True,
            check_same_thread=False)  # HTTP-транспорт: потоки под блокировкой
        # sqlite-LOWER не знает кириллицу — регистрируем питоний lower
        conn.create_function(
            'lower_ru', 1, lambda v: v.lower() if isinstance(v, str) else v)
        # регистронезависимый поиск подстроки в больших текстах (тела методов):
        # LIKE сворачивает регистр только для ASCII, поэтому кириллическая игла
        # в нижнем регистре не нашла бы текст в смешанном. Своя функция ещё и
        # в полтора раза быстрее lower_ru(body) LIKE — замер на базе УНФ
        # (241566 методов): 3,6 с против 5,5 с. Регистр сворачиваем у обоих
        # аргументов: функция видна модели через инструмент sql, и требовать
        # от вызывающего заранее переведённой в нижний регистр иглы нельзя —
        # иначе она молча вернула бы ноль
        conn.create_function(
            'body_has', 2,
            lambda body, needle: isinstance(body, str)
            and isinstance(needle, str) and needle.lower() in body.lower())
        try:
            conn.execute('SELECT COUNT(*) FROM meta_object').fetchone()
        except sqlite3.Error:
            conn.close()
            raise ValueError(
                f'это не база знаний confdb (нет таблицы meta_object): {path}')
        # FTS5-индекс по телам методов. Неполный не считается: сборку могли
        # прервать (или база собрана с --no-fts), и тогда поиск по телам должен
        # уйти на body_has, а не молча вернуть ноль. Штатная схема — приставные
        # шарды `<база>.fts\0.db…` (writer.build_fts_index); таблица внутри базы
        # поддерживается как прежняя схема. Число методов сверяется с манифестом:
        # чужой/устаревший индекс хуже отсутствующего
        fts = None
        fts_shards = None
        manifest = fts_index_info(path)
        if manifest and int(manifest.get('methods', -1)) == conn.execute(
                'SELECT COUNT(*) FROM method').fetchone()[0]:
            fts_shards = fts_shard_paths(path)
        else:
            try:
                if conn.execute(f'SELECT rowid FROM {FTS_TABLE} LIMIT 1').fetchone():
                    fts = FTS_TABLE
            except sqlite3.Error:
                pass  # индекса нет — будет fallback на body_has
        if alias is None:
            alias = self._make_alias(path)
        self.dbs[alias] = {'path': path, 'conn': conn, 'ctx': None, 'fts': fts,
                           'fts_shards': fts_shards, 'fts_conn': None}
        if activate or self.active is None:
            self.active = alias
        return alias

    def close_db(self, alias=None):
        """Закрывает базу (по умолчанию активную); возвращает её алиас."""
        alias = self._alias(alias)
        info = self.dbs.pop(alias)
        info['conn'].close()
        for shard in info.get('fts_conn') or ():
            shard.close()
        if self.active == alias:
            self.active = next(iter(self.dbs), None)
        # База уходит и из групп: иначе повторное открытие того же файла
        # (алиас совпадёт) молча вернуло бы её в прежнюю группу
        for aliases in self.groups.values():
            while alias in aliases:
                aliases.remove(alias)
        return alias

    def _alias(self, alias=None):
        """Разрешает алиас (None = активная база); ValueError, если не найден."""
        if not alias:
            if self.active is None:
                raise ValueError('нет открытых баз — укажите путь в db_open')
            return self.active
        for key in self.dbs:
            if key.lower() == str(alias).strip().lower():
                return key
        raise ValueError(
            f'база не открыта: {alias}' +
            ('; открыты: ' + ', '.join(self.dbs) if self.dbs
             else ' — откройте через db_open'))

    def db_stats(self, alias=None):
        """(объекты, модули, методы) базы — для отчётов пользователю."""
        return self.conn(alias).execute(
            'SELECT (SELECT COUNT(*) FROM meta_object), '
            '(SELECT COUNT(*) FROM module), '
            '(SELECT COUNT(*) FROM method)').fetchone()

    # -- группы баз ----------------------------------------------------------
    def create_group(self, name):
        """Создаёт пустую группу баз. Имя группы уникально (регистр не важен)."""
        if not name or not str(name).strip():
            raise ValueError('имя группы не может быть пустым')
        name = str(name).strip()
        if name.lower() in (g.lower() for g in self.groups):
            raise ValueError(f'группа уже существует: {name}')
        self.groups[name] = []
        if self.active_group is None:
            self.active_group = name
        return name

    def add_group_paths(self, name, paths, activate=False):
        """Открывает базы и регистрирует их как группу с указанным составом.

        Так группа появляется при запуске сервера (CLI --group, TUI) и так же
        в работающий сервер применяется группа из конфига: состав становится
        равным `paths`, прежние базы группы остаются открытыми, но выходят из
        неё. Недоступный файл — ошибка в третьем элементе результата, а не
        обрыв: группу из конфига можно открыть и частично.
        Возвращает (имя, [алиасы], [ошибки]).
        """
        name = str(name).strip() if name else ''
        if not name:
            raise ValueError('имя группы не может быть пустым')
        group = next((g for g in self.groups if g.lower() == name.lower()), None)
        if group is None:
            group = self.create_group(name)
        aliases, errors = [], []
        for path in paths or ():
            try:
                alias = self.open_db(path, activate=False)
            except ValueError as err:
                errors.append(str(err))
                continue
            if alias not in aliases:
                aliases.append(alias)
        self.groups[group] = aliases
        if activate or self.active_group is None:
            self.active_group = group
        if activate and aliases:
            self.active = aliases[0]
        elif self.active is None and aliases:
            self.active = aliases[0]
        return group, aliases, errors

    def drop_group(self, group_name, close_dbs=True):
        """Удаляет группу; по умолчанию закрывает базы, которые больше никому не нужны.

        База, входящая ещё в одну группу, остаётся открытой — удаление одной
        группы не должно рвать другую. Возвращает (имя, [закрытые алиасы]).
        """
        group_name = str(group_name).strip() if group_name else ''
        if not group_name:
            raise ValueError('имя группы не может быть пустым')
        group = next((g for g in self.groups if g.lower() == group_name.lower()), None)
        if group is None:
            raise ValueError(f'группа не найдена: {group_name}')
        aliases = list(self.groups[group])
        self.close_group(group)
        closed = []
        if close_dbs:
            for alias in aliases:
                if alias in self.dbs and not any(
                        alias in others for others in self.groups.values()):
                    self.close_db(alias)
                    closed.append(alias)
        return group, closed

    def groups_of(self, alias):
        """Имена групп, в которые входит база (для заголовков ответов)."""
        return [name for name, aliases in self.groups.items() if alias in aliases]

    def add_db_to_group(self, group_name, db_alias):
        """Добавляет базу в группу. База должна быть открыта."""
        if not group_name or not str(group_name).strip():
            raise ValueError('имя группы не может быть пустым')
        group_name = str(group_name).strip()
        # Поиск группы без учёта регистра
        actual_group = next((g for g in self.groups if g.lower() == group_name.lower()), None)
        if actual_group is None:
            raise ValueError(f'группа не найдена: {group_name}')
        alias = self._alias(db_alias)  # Проверит, что база открыта
        if alias not in self.groups[actual_group]:
            self.groups[actual_group].append(alias)
        return actual_group, alias

    def remove_db_from_group(self, group_name, db_alias):
        """Убирает базу из группы. База не закрывается."""
        if not group_name or not str(group_name).strip():
            raise ValueError('имя группы не может быть пустым')
        group_name = str(group_name).strip()
        actual_group = next((g for g in self.groups if g.lower() == group_name.lower()), None)
        if actual_group is None:
            raise ValueError(f'группа не найдена: {group_name}')
        alias = self._alias(db_alias)
        if alias in self.groups[actual_group]:
            self.groups[actual_group].remove(alias)
        return actual_group, alias

    def list_groups(self):
        """Возвращает список групп с содержимым."""
        result = []
        for group_name, db_aliases in self.groups.items():
            mark = '*' if group_name == self.active_group else ' '
            dbs_info = []
            for alias in db_aliases:
                if alias in self.dbs:
                    path = self.dbs[alias]['path']
                    dbs_info.append(f'{alias} ({path})')
            result.append({
                'name': group_name,
                'active': group_name == self.active_group,
                'databases': dbs_info,
                'mark': mark
            })
        return result

    def use_group(self, group_name):
        """Делает группу активной; активной базой становится её первая база.

        Без параметра group инструменты работают с активной БАЗОЙ, поэтому
        переключение группы обязано переключать и её: иначе запросы «по
        умолчанию» после group_use продолжали бы ходить в прежнюю конфигурацию.
        """
        if not group_name or not str(group_name).strip():
            raise ValueError('имя группы не может быть пустым')
        group_name = str(group_name).strip()
        actual_group = next((g for g in self.groups if g.lower() == group_name.lower()), None)
        if actual_group is None:
            raise ValueError(f'группа не найдена: {group_name}')
        self.active_group = actual_group
        opened = [a for a in self.groups[actual_group] if a in self.dbs]
        if opened:
            self.active = opened[0]
        return actual_group

    def close_group(self, group_name):
        """Удаляет группу. Базы не закрываются."""
        if not group_name or not str(group_name).strip():
            raise ValueError('имя группы не может быть пустым')
        group_name = str(group_name).strip()
        actual_group = next((g for g in self.groups if g.lower() == group_name.lower()), None)
        if actual_group is None:
            raise ValueError(f'группа не найдена: {group_name}')
        del self.groups[actual_group]
        if self.active_group == actual_group:
            self.active_group = next(iter(self.groups), None)
        return actual_group

    def get_group_dbs(self, group_name=None):
        """Возвращает список алиасов баз в группе. None = активная группа."""
        if group_name is None:
            if self.active_group is None:
                raise ValueError('нет активной группы — укажите имя группы')
            group_name = self.active_group
        actual_group = next((g for g in self.groups if g.lower() == str(group_name).lower()), None)
        if actual_group is None:
            raise ValueError(f'группа не найдена: {group_name}')
        return self.groups[actual_group]

    # -- инфраструктура ----------------------------------------------------
    def conn(self, db=None):
        return self.dbs[self._alias(db)]['conn']

    def count_of(self, sql, params=(), db=None, extra=()):
        """Сколько всего строк набирает поиск (total_count ответа).

        Итог кэшируется на базу: она открыта read-only, поэтому значение не
        устареет, а листание страниц не платит повторным полным проходом —
        для find_methods(text=…) без FTS-индекса это секунды на каждую страницу.
        `extra` входит в ключ: часть условий поиска живёт не в params
        (временная таблица совпадений FTS5 строится из иглы).
        """
        cache = self.dbs[self._alias(db)].setdefault('counts', {})
        key = (sql, tuple(params), tuple(extra))
        if key not in cache:
            if len(cache) >= COUNT_CACHE_MAX:
                cache.clear()
            cache[key] = self.conn(db).execute(sql, list(params)).fetchone()[0]
        return cache[key]

    def ctx(self, db=None):
        alias = self._alias(db)
        info = self.dbs[alias]
        if info['ctx'] is None:
            from .query_lang import MetaContext
            info['ctx'] = MetaContext(info['conn'])
        return info['ctx']

    def resolve_path(self, value, db=None):
        """Русский точечный путь ('Справочник.Х.Форма') -> внутренний слэш-путь."""
        value = (value or '').strip()
        if not value or '/' in value or '.' not in value:
            return value
        parts = value.split('.')
        stems = _type_stems(parts[0])
        if not stems:
            return value
        conn = self.conn(db)
        row = conn.execute(
            'SELECT id, path FROM meta_object WHERE name=? AND type IN (%s)'
            % ','.join('?' * len(stems)), [parts[1]] + stems).fetchone()
        if not row:
            return value
        oid, path = row
        for name in parts[2:]:
            row = conn.execute(
                'SELECT id, path FROM meta_object '
                'WHERE parent_id=? AND name=? ORDER BY ord', (oid, name)).fetchone()
            if not row:
                return value
            oid, path = row
        return path

    def handle(self, msg):
        method = msg.get('method')
        msg_id = msg.get('id')
        if method == 'initialize':
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': '1confdb-knw', 'version': '1.0'},
                'instructions': PRIMER}}
        if msg_id is None or (method or '').startswith('notifications/'):
            return None  # уведомления
        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': {}}
        if method == 'tools/list':
            return {'jsonrpc': '2.0', 'id': msg_id,
                    'result': {'tools': [t.spec() for t in TOOLS]}}
        if method == 'tools/call':
            params = msg.get('params', {})
            name = params.get('name')
            args = params.get('arguments', {}) or {}
            tool = next((t for t in TOOLS if t.name == name), None)
            if tool is None:
                return {'jsonrpc': '2.0', 'id': msg_id, 'error': {
                    'code': -32602, 'message': f'unknown tool: {name}'}}
            try:
                text = tool.run(self, **args)
                return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                    'content': [{'type': 'text', 'text': text}]}}
            except Exception as err:  # noqa: BLE001 — ошибка инструмента, не сервера
                return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                    'content': [{'type': 'text', 'text': error_text(err)}],
                    'isError': True}}
        return {'jsonrpc': '2.0', 'id': msg_id, 'error': {
            'code': -32601, 'message': f'method not found: {method}'}}

    # -- инструменты ---------------------------------------------------------
    def find_objects(self, mask, type=None, limit=20, offset=0,
                     db=None):  # noqa: A002
        like = f'%{mask}%'
        # имена в 1С пишутся Слитно, а маски часто приходят с пробелами
        # и в другой раскладке регистра
        like_ns = f'%{mask.replace(" ", "").lower()}%'
        where = ('(name LIKE ? OR path LIKE ? '
                 "OR lower_ru(REPLACE(name, ' ', '')) LIKE ? "
                 "OR lower_ru(REPLACE(path, ' ', '')) LIKE ?)")
        params = [like, like, like_ns, like_ns]
        if type:
            where += ' AND (type = ? OR type_ru = ?)'
            params += [type, type]
        limit, offset = page_limit(limit), page_offset(offset)
        total = self.count_of(f'SELECT COUNT(*) FROM meta_object WHERE {where}',
                              params, db)
        if not total:
            return 'ничего не найдено'
        # порядок стабилен: id разрывает равенство путей, иначе страница
        # с offset повторяет и теряет строки на границе
        rows = self.conn(db).execute(
            'SELECT path, type, type_ru, name FROM meta_object '
            f'WHERE {where} ORDER BY length(path), path, id LIMIT ? OFFSET ?',
            params + [limit, offset]).fetchall()
        out = [f'{ru_path(p)} — {ru} ({t})' for p, t, ru, _ in rows]
        out.append('… ' + page_note(total, offset, len(rows)))
        return '\n'.join(out)

    def _subscription_card(self, q, path, db=None):
        """Строки подписки на событие; [] — заголовок не распознан."""
        entry = self.event_index(db)['by_path'].get(path)
        if not entry:
            return []
        found = 0
        if entry['handler'] and entry['handler_method']:
            found = q('SELECT COUNT(*) FROM method mt '
                      'JOIN module m ON m.id=mt.module_id '
                      'JOIN meta_object o ON o.id=m.object_id '
                      'WHERE o.path=? AND LOWER(mt.name)=LOWER(?)',
                      (entry['handler'], entry['handler_method'])).fetchone()[0]
        return _subscription_lines(entry, bool(found))

    def object_card(self, path, db=None):
        path = self.resolve_path(path, db)
        q = self.conn(db).execute
        row = q('SELECT type, type_ru, name, comment, header_json '
                'FROM meta_object WHERE path=?', (path,)).fetchone()
        if not row:
            return f'объект не найден: {path}'
        oid = q('SELECT id FROM meta_object WHERE path=?', (path,)).fetchone()[0]
        # тип — русским именем, как в конфигураторе: английский stem остаётся
        # в колонке type и в списках find_objects, но не в паспорте. TYPE_RU
        # важнее сохранённого type_ru — в базах, собранных до исправления ключа
        # ChartOfCharacteristicType, в type_ru лежит английский stem
        kind = TYPE_RU.get(row[0]) or row[1] or row[0]
        out = [f'{ru_path(path)} — {kind}, имя {row[2]}' +
               (f'; комментарий: {row[3]}' if row[3] else '')]
        # свойства регистра (периодичность, режим записи)
        out.extend(_register_card_info(row[0], row[4]))
        # целевое пространство имён пакета XDTO
        out.extend(header_props.xdto_props(row[0], row[4]))
        if row[0] == 'EventSubscription':
            out.extend(self._subscription_card(q, path, db))
        if row[0] == 'ExchangePlan':
            uuids = header_props.exchange_plan_content(row[0], row[4])
            if uuids:
                out.extend(_exchange_plan_lines(uuids, self.uuid_map(db)))
        attrs = q('SELECT name, type_str FROM meta_attribute '
                  'WHERE object_id=? AND tabular IS NULL ORDER BY ord',
                  (oid,)).fetchall()
        kinds = (header_props.register_field_kinds(row[4])
                 if row[0] in _REGISTER_TYPES else {})
        if kinds:
            # у регистра измерения/ресурсы/реквизиты показываются раздельно:
            # по плоскому списку не понять, что входит в ключ записи
            groups = {}
            for name, tstr in attrs:
                groups.setdefault(kinds.get(name, 'Прочие поля'), []).append(
                    f'{name}: {ru_type_str(tstr) or "тип не извлечён"}')
            for kind in header_props.REGISTER_KINDS + ('Прочие поля',):
                if kind in groups:
                    out.append(f'{kind}: ' + '; '.join(groups[kind]))
            if not attrs:
                out.append('Реквизиты: нет')
        else:
            out.append('Реквизиты: ' + ('; '.join(
                f'{n}: {ru_type_str(t) or "тип не извлечён"}' for n, t in attrs)
                if attrs else 'нет'))
        tabs = q('SELECT t.name, a.name, a.type_str FROM meta_tabular t '
                 'LEFT JOIN meta_attribute a ON a.object_id=t.object_id '
                 'AND a.tabular=t.name WHERE t.object_id=? '
                 'ORDER BY t.ord, a.ord', (oid,)).fetchall()
        sections = {}
        for sec, fname, ftype in tabs:
            # у секции без извлечённых полей LEFT JOIN даёт одну строку с NULL:
            # это не поле с именем None, а отсутствующий состав
            sections.setdefault(sec, [])
            if fname is not None:
                sections[sec].append(
                    f'{fname}: {ru_type_str(ftype) or "тип не извлечён"}')
        # сколько полей объявляет сама конфигурация — без этого пустую секцию
        # не отличить от пробела извлечения
        declared = tabular_field_counts(row[4])
        for sec, fields in sections.items():
            if fields:
                out.append(f'Табличная часть {sec}: ' + '; '.join(fields))
            elif declared.get(sec) == 0:
                # в конфигурации у секции не объявлено ни одного поля: это факт,
                # а не пробел извлечения
                out.append(f'Табличная часть {sec}: полей не объявлено')
            else:
                out.append(f'Табличная часть {sec}: полей не извлечено')
        predef = q('SELECT ord, parent_ord, name, code, display FROM predefined '
                   'WHERE object_id=? ORDER BY ord', (oid,)).fetchall()
        if predef:
            out.extend(_predefined_lines(q, oid, predef, row[0]))
        subs = self.event_index(db)['by_source'].get(path)
        if subs:
            # платформа вызывает эти обработчики сама: в коде объекта
            # таких вызовов нет, и без этой строки их не найти
            out.append(_source_subscriptions_line(subs))
        mods = q('SELECT code_name, context FROM module WHERE object_id=?',
                 (oid,)).fetchall()
        if mods:
            out.append('Модули: ' + ', '.join(
                c + (f' [{x}]' if x else '') for c, x in mods))
        out.extend(self._children_lines(q, oid))
        nskd = q('SELECT COUNT(*) FROM skd_query WHERE object_id=?',
                 (oid,)).fetchone()[0]
        if nskd:
            out.append(f'Запросов СКД: {nskd} (см. skd_of)')
        if row[0] == 'XDTOPackage':
            # состав пакета в паспорт не печатается: типов бывает сотни
            ntypes, nprops = q(
                'SELECT (SELECT COUNT(*) FROM xdto_type WHERE object_id=?),'
                ' (SELECT COUNT(*) FROM xdto_property WHERE object_id=?)',
                (oid, oid)).fetchone()
            if ntypes or nprops:
                # пакет может не содержать типов, но объявлять свойства сам
                out.append(f'Типов XDTO: {ntypes}, свойств: {nprops} (см. xdto_of)')
        fwd = [r[0] for r in q(
            'SELECT DISTINCT t.path FROM attribute_ref r '
            'JOIN meta_attribute a ON a.id=r.attribute_id '
            'JOIN meta_object v ON v.id=a.object_id '
            'JOIN meta_object t ON t.id=r.object_id '
            'WHERE v.path=? AND t.path IS NOT NULL LIMIT 12', (path,))]
        if fwd:
            out.append('Ссылается на: ' + ', '.join(ru_path(p) for p in fwd))
        rev = [r[0] for r in q(
            'SELECT DISTINCT v.path FROM attribute_ref r '
            'JOIN meta_attribute a ON a.id=r.attribute_id '
            'JOIN meta_object v ON v.id=a.object_id '
            'JOIN meta_object t ON t.id=r.object_id '
            'WHERE t.path=? LIMIT 12', (path,))]
        if rev:
            out.append('На него ссылаются: ' + ', '.join(ru_path(p) for p in rev))
        return '\n'.join(out)

    @staticmethod
    def _children_lines(q, oid):
        """Строки вложенных объектов: формы, команды, макеты.

        Путь к ним — '<путь родителя>.<имя>', поэтому имена достаточно
        перечислить: object_card/get_method принимают его целиком.
        """
        buckets = {}
        for name, ktype in q('SELECT name, type FROM meta_object '
                             'WHERE parent_id=? ORDER BY ord', (oid,)):
            if ktype.endswith('Form'):
                key = 'Формы'
            elif ktype.endswith('Command'):
                key = 'Команды'
            elif ktype.endswith('Template'):
                key = 'Макеты'
            else:
                key = 'Прочие подобъекты'
            buckets.setdefault(key, []).append(name)
        lines = []
        for key in ('Формы', 'Команды', 'Макеты', 'Прочие подобъекты'):
            if key in buckets:
                lines.append(f'{key}: ' + ', '.join(buckets[key]))
        return lines

    def configuration_info(self, db=None):
        """Паспорт базы: тип файла, имя, версия, режим совместимости."""
        alias = self._alias(db)
        props = self.cfg_props(alias)
        if not props:
            return 'корневой объект конфигурации не найден'
        root_type = props.get('root_type')
        kind, _ext = header_props.kind_of(root_type, props.get('root_type_ru'),
                                          props.get('source_file'))
        head = f'{kind or "?"}: {props.get("name") or "?"}'
        if props.get('synonym') and props['synonym'] != props.get('name'):
            head += f' ({props["synonym"]})'
        out = [head]
        root_ru = props.get('root_type_ru')
        # type_ru в meta_object допускает NULL — без запасного варианта
        # получилось бы «Тип корня: None (Configuration)»
        out.append('Тип корня: '
                   + (f'{root_ru} ({root_type})' if root_ru and root_type
                      else (root_ru or root_type or '?')))
        out.append(f'Версия {header_props.version_noun(kind)}: '
                   + (props.get('version') or 'в файле не указана'))
        if props.get('name_prefix'):
            out.append(f'Префикс имён расширения: {props["name_prefix"]}')
        out.append('Режим совместимости: '
                   + header_props.compatibility_line(
                       root_type, props.get('compatibility')))
        out.append('Версия платформы: в файле конфигурации не хранится '
                   '(см. режим совместимости)')
        if props.get('obj_version'):
            out.append(f'Формат метаданных (obj_version): {props["obj_version"]}')
        src = _source_row(self.conn(db))
        if src:
            if src.get('file'):
                out.append(f'Источник выгрузки: {src["file"]}')
            # отпечаток исходника — способ убедиться, что две базы собраны из
            # одного и того же файла (и что повторное извлечение не нужно)
            out.append(sha_line(src.get('file_sha256'), src.get('file_size')))
            if src.get('created'):
                out.append(f'База знаний собрана: {src["created"]}')
            if src.get('root_uuid'):
                out.append(f'UUID корня: {src["root_uuid"]}')
        nobj, nmod, nmeth = self.db_stats(db)
        nskd = self.conn(db).execute(
            'SELECT COUNT(*) FROM skd_query').fetchone()[0]
        out.append(f'Состав: объектов {nobj}, модулей {nmod}, методов {nmeth}, '
                   f'запросов СКД {nskd}')
        return '\n'.join(out)

    # -- сравнение двух баз --------------------------------------------------
    def compare_object(self, path, db_left, db_right, path_right=None):
        """Различия одного объекта в двух базах знаний."""
        left_alias = self._alias(db_left)
        right_alias = self._alias(db_right)
        left_path = self.resolve_path(path, left_alias)
        right_path = (self.resolve_path(path_right, right_alias)
                      if path_right else left_path)
        left = compare.object_snapshot(self.conn(left_alias), left_path)
        right = compare.object_snapshot(self.conn(right_alias), right_path)
        title = ru_path(left_path if left else right_path) or path
        head = [f'Сравнение объекта: {title}',
                f'  {left_alias}: {self.dbs[left_alias]["path"]} '
                f'(путь {left_path})',
                f'  {right_alias}: {self.dbs[right_alias]["path"]} '
                f'(путь {right_path})']
        if left is None and right is None:
            head.append(f'объект не найден ни в базе «{left_alias}», '
                        f'ни в базе «{right_alias}»')
            return '\n'.join(head)
        if left is None:
            head.append(f'объект есть только в базе «{right_alias}»; '
                        f'в базе «{left_alias}» его нет')
            return '\n'.join(head)
        if right is None:
            head.append(f'объект есть только в базе «{left_alias}»; '
                        f'в базе «{right_alias}» его нет')
            return '\n'.join(head)
        head.append(
            f'Состав ({left_alias} / {right_alias}): реквизитов и полей '
            f'{len(left["attrs"])} / {len(right["attrs"])}, методов '
            f'{len(left["methods"])} / {len(right["methods"])}, модулей '
            f'{len(left["modules"])} / {len(right["modules"])}, запросов СКД '
            f'{len(left["skd"])} / {len(right["skd"])}')
        lines = compare.diff_snapshots(left, right, left_alias, right_alias,
                                       fmt_type=ru_type_str)
        head.append('Различий нет: метаданные и код объекта совпадают'
                    if not lines else 'Различия:')
        return '\n'.join(head + lines)

    def extension_diff(self, extension_db, base_db, limit=30):
        """Что делает расширение относительно основной конфигурации."""
        ext_alias = self._alias(extension_db)
        base_alias = self._alias(base_db)
        ext = self.cfg_props(ext_alias)
        base = self.cfg_props(base_alias)
        out = []
        name = ext.get('name') or ext_alias
        extra = [text for text in (
            f'версия {ext["version"]}' if ext.get('version') else None,
            f'режим совместимости {ext["compatibility"]}'
            if ext.get('compatibility') else None) if text]
        out.append(f'Расширение: {name}'
                   + (' (' + '; '.join(extra) + ')' if extra else ''))
        if ext.get('name_prefix'):
            out.append(f'Префикс имён новых объектов: {ext["name_prefix"]}')
        out.append(f'  база {ext_alias}: {self.dbs[ext_alias]["path"]}')
        base_name = base.get('name') or base_alias
        base_ver = f' {base["version"]}' if base.get('version') else ''
        out.append(f'Основная конфигурация: {base_name}{base_ver}')
        out.append(f'  база {base_alias}: {self.dbs[base_alias]["path"]}')
        if ext.get('root_type') != 'ConfigurationExtension':
            out.append(f'Внимание: корень базы {ext_alias} — '
                       f'{ext.get("root_type_ru") or ext.get("root_type")}, '
                       'а не расширение конфигурации; отчёт показывает '
                       'различия двух баз как есть')
        out.append('')
        out.extend(compare.extension_report(
            self.conn(ext_alias), self.conn(base_alias), fmt=ru_path,
            prefix=ext.get('name_prefix'), limit=int(limit),
            fmt_type=ru_type_str))
        return '\n'.join(out)

    def object_tree(self, path='', depth=2, db=None):
        path = self.resolve_path(path, db)
        rows = self.conn(db).execute(
            'SELECT id, parent_id, path, type_ru FROM meta_object '
            'ORDER BY ord').fetchall()
        children = {}
        ids = {}
        for oid, pid, p, ru in rows:
            children.setdefault(pid, []).append((p, ru, oid))
            ids[oid] = (p, ru)
        root_id = None
        for oid, (p, _) in ids.items():
            if p == path:
                root_id = oid
                break
        if root_id is None:
            return f'объект не найден: {path}'
        out = []

        def walk(oid, lvl):
            if lvl > depth:
                return
            for p, ru, cid in children.get(oid, []):
                out.append('  ' * lvl + f'{ru_path(p)} — {ru}')
                walk(cid, lvl + 1)

        out.append(f'{ru_path(path) or "(корень)"} — {ids[root_id][1]}')
        walk(root_id, 1)
        return '\n'.join(out)

    def find_field(self, name, limit=20, offset=0, db=None):
        like = f'%{name}%'
        like_ns = f'%{name.replace(" ", "").lower()}%'
        where = ("(a.name LIKE ? OR lower_ru(REPLACE(a.name, ' ', '')) LIKE ?)")
        params = [like, like_ns]
        limit, offset = page_limit(limit), page_offset(offset)
        total = self.count_of(
            'SELECT COUNT(*) FROM meta_attribute a '
            'JOIN meta_object o ON o.id=a.object_id '
            f'WHERE {where}', params, db)
        if not total:
            return 'ничего не найдено'
        # одно имя поля встречается у многих объектов и во многих табличных
        # частях, поэтому порядок заканчивается уникальным a.id
        rows = self.conn(db).execute(
            'SELECT o.path, a.name, a.tabular, a.type_str FROM meta_attribute a '
            'JOIN meta_object o ON o.id=a.object_id '
            f'WHERE {where} '
            'ORDER BY o.path, a.tabular, a.ord, a.id LIMIT ? OFFSET ?',
            params + [limit, offset]).fetchall()
        out = [f'{ru_path(p)} :: поле {n} ({ru_type_str(t) or "?"})' +
               (f' [табчасть {s}]' if s else '')
               for p, n, s, t in rows]
        out.append('… ' + page_note(total, offset, len(out)))
        return '\n'.join(out)

    def refs_of(self, path, direction='both', limit=30, offset=0, db=None):
        path = self.resolve_path(path, db)
        limit, offset = page_limit(limit), page_offset(offset)
        joined = (' FROM attribute_ref r '
                  'JOIN meta_attribute a ON a.id=r.attribute_id '
                  'JOIN meta_object v ON v.id=a.object_id '
                  'JOIN meta_object t ON t.id=r.object_id ')
        # (заголовок, колонка результата, условие): обе секции листаются одним
        # offset, поэтому хвост пагинации печатается у каждой свой
        sections = []
        if direction in ('both', 'forward'):
            sections.append(('Ссылается на', 't.path',
                             'v.path=? AND t.path IS NOT NULL'))
        if direction in ('both', 'reverse'):
            sections.append(('На него ссылаются', 'v.path', 't.path=?'))
        out = []
        for title, column, cond in sections:
            where = f'WHERE {cond} '
            total = self.count_of(
                f'SELECT COUNT(DISTINCT {column}){joined}{where}', (path,), db)
            rows = self.conn(db).execute(
                f'SELECT DISTINCT {column}{joined}{where}'
                f'ORDER BY {column} LIMIT ? OFFSET ?',
                (path, limit, offset)).fetchall()
            out.append(title + ': ' + (', '.join(ru_path(r[0]) for r in rows)
                                       if rows else '—'))
            out.append('… ' + page_note(total, offset, len(rows)))
        return '\n'.join(out)

    # -- права ролей -------------------------------------------------------
    def _role_path(self, role, db=None):
        """Путь роли по имени ('ПолныеПрава') или пути ('Role/…', 'Роль.…')."""
        value = (role or '').strip()
        if not value:
            return None
        q = self.conn(db).execute
        row = q("SELECT path FROM meta_object WHERE type='Role' AND path=?",
                (self.resolve_path(value, db),)).fetchone()
        if row:
            return row[0]
        row = q("SELECT path FROM meta_object WHERE type='Role' AND name=?"
                ' ORDER BY path LIMIT 1', (value,)).fetchone()
        return row[0] if row else None

    def _role_state(self, db, role_id):
        """Строка role_rights_state роли или None — состояние не сохранено."""
        return self.conn(db).execute(
            'SELECT version, parsed, targets, rights, rls_templates, error'
            ' FROM role_rights_state WHERE role_id=?', (role_id,)).fetchone()

    def _rights_of_targets(self, db, keys):
        """{(role_id, uuid цели, sub_index, collection_uuid, флаги): [(право, значение, RLS)]}.

        Один запрос на страницу вместо запроса на цель: фильтр по двум спискам
        (роли и uuid целей) даёт надмножество нужного, а ключ отбирает ровно те
        записи, по которым сгруппирована выдача. Адресация подобъекта И флаги
        записи входят в ключ: один и тот же uuid объекта встречается в роли
        несколько раз с разными sub_index («подобъект №-K») или флагами, и это
        РАЗНЫЕ цели — слить их значит показать право дважды и занизить счётчик.
        """
        if not keys:
            return {}
        role_ids = sorted({key[0] for key in keys})
        uuids = sorted({key[1] for key in keys})
        wanted = set(keys)
        marks = ','.join('?' * len(role_ids))
        marks_uuid = ','.join('?' * len(uuids))
        out = {}
        for row in self.conn(db).execute(
                'SELECT role_id, target_uuid, sub_index, collection_uuid,'
                ' target_flags, right_uuid, value, rls_text FROM role_right'
                f' WHERE role_id IN ({marks}) AND target_uuid IN ({marks_uuid})'
                ' ORDER BY id', role_ids + uuids):
            key = tuple(row[:5])
            if key in wanted:
                out.setdefault(key, []).append(tuple(row[5:]))
        return out

    def _flag_ambiguous(self, db, where, params):
        """Ключи (uuid цели, sub_index, collection_uuid), различимые только флагами.

        Флаги записи цели хранятся дословно, а их смысл не подтверждён, поэтому
        в выдаче они появляются лишь там, где без них две цели выглядели бы
        одинаково: иначе одна и та же строка встретилась бы дважды без объяснения.
        """
        rows = self.conn(db).execute(
            'SELECT target_uuid, sub_index, collection_uuid FROM role_right'
            f' WHERE {where}'
            ' GROUP BY target_uuid, sub_index, collection_uuid'
            " HAVING COUNT(DISTINCT COALESCE(target_flags, '-')) > 1",
            params).fetchall()
        return {tuple(row) for row in rows}

    def role_rights(self, role, limit=50, offset=0, db=None):
        """Явные права роли: строка на цель (объект, его реквизит/поле или ТЧ).

        Хранятся только ЯВНЫЕ записи, поэтому цели нет в списке — значит право
        на неё ролью не задано, а не запрещено. `parsed=0` — файл прав не
        прочитан: ответ обязан сказать «данные недоступны», а не показать пустой
        список, который читается как «роли ничего не разрешено».
        """
        conn = self.conn(db)
        if not _rights_tables(conn):
            return NO_RIGHTS_TABLES
        path = self._role_path(role, db)
        if not path:
            return (f'роль не найдена: {role} (список ролей:'
                    ' find_objects(mask="", type="Role"))')
        q = conn.execute
        role_id = q('SELECT id FROM meta_object WHERE path=?', (path,)).fetchone()[0]
        state = self._role_state(db, role_id)
        out = [ru_path(path)]
        if state is None:
            out.append('состояние извлечения прав этой роли не сохранено —'
                       ' список ниже может быть неполным')
        else:
            version, parsed, targets, rights, templates, error = state
            out[0] += (f': формат версии {version or "?"}, целей {targets},'
                       f' явных записей прав {rights}, шаблонов RLS {templates}')
            if not parsed:
                out.append(f'данные недоступны: {error or "причина не сохранена"}'
                           ' — это НЕ «права не заданы»: файл прав не прочитан')
                return '\n'.join(out)
            if not rights:
                out.append('явных записей прав нет: права роли не заданы'
                           ' (факт конфигурации, а не сбой извлечения)')
                return '\n'.join(out)
            if templates:
                names = [r[0] for r in q(
                    'SELECT name FROM role_rls_template WHERE role_id=?'
                    ' ORDER BY ord', (role_id,)) if r[0]]
                out.append('Шаблоны RLS: ' + ', '.join(names) +
                           ' (тексты — sql: SELECT name, text FROM'
                           f' role_rls_template WHERE role_id={role_id})')
        out.append(RIGHTS_NOTE)
        limit, offset = page_limit(limit), page_offset(offset)
        groups = (
            'SELECT r.target_uuid, o.path, a.name, a.tabular, t.name,'
            ' r.sub_index, r.collection_uuid, r.target_flags, COUNT(*)'
            ' FROM role_right r'
            ' LEFT JOIN meta_object o ON o.id=r.target_object_id'
            ' LEFT JOIN meta_attribute a ON a.id=r.target_attr_id'
            ' LEFT JOIN meta_tabular t ON t.id=r.target_tabular_id'
            ' WHERE r.role_id=?'
            ' GROUP BY r.target_uuid, r.target_object_id, r.target_attr_id,'
            ' r.target_tabular_id, r.sub_index, r.collection_uuid, r.target_flags'
            ' ORDER BY o.path IS NULL, o.path, t.name, a.tabular, a.name,'
            ' r.sub_index, r.target_uuid, r.target_flags')
        total = self.count_of(f'SELECT COUNT(*) FROM ({groups})', (role_id,), db)
        rows = q(groups + ' LIMIT ? OFFSET ?',
                 [role_id, limit, offset]).fetchall()
        dup = self._flag_ambiguous(db, 'role_id=?', [role_id])
        by_target = self._rights_of_targets(
            db, [(role_id, r[0], r[5], r[6], r[7]) for r in rows])
        for (target_uuid, obj_path, attr_name, attr_tabular, tab_name,
             sub_index, collection_uuid, flags, count) in rows:
            rights = by_target.get((role_id, target_uuid, sub_index,
                                    collection_uuid, flags), [])
            label = _target_label(
                obj_path, attr_name, attr_tabular, tab_name, target_uuid,
                sub_index, collection_uuid,
                flags if (target_uuid, sub_index, collection_uuid) in dup
                else None)
            out.append(f'{label} — записей {count}: ' + _right_labels(rights))
            out.extend(_rls_lines(rights))
        out.append('… ' + page_note(total, offset, len(rows), ' (целей)'))
        return '\n'.join(out)

    def object_rights(self, path, role=None, limit=50, offset=0, db=None):
        """Явные права ролей на объект, его реквизиты/поля ТЧ и табличные части.

        У права на подобъект `target_object_id` — объект-владелец, поэтому одно
        условие охватывает и сам объект, и его поля: «кто работает с этим
        справочником» видно одним запросом. Роли с `parsed=0` в список не
        попадают (их файл прав не прочитан) — ответ обязан назвать их число,
        иначе пустой список читается как «доступ запрещён всем».
        """
        conn = self.conn(db)
        if not _rights_tables(conn):
            return NO_RIGHTS_TABLES
        path = self.resolve_path(path, db)
        q = conn.execute
        row = q('SELECT id, path FROM meta_object WHERE path=?', (path,)).fetchone()
        if not row:
            return f'объект не найден: {path}'
        oid, path = row
        out = [ru_path(path)]
        where = ' WHERE r.target_object_id=?'
        params = [oid]
        only_role = None
        role_path = None
        if role:
            role_path = self._role_path(role, db)
            if not role_path:
                return f'роль не найдена: {role}'
            only_role = q('SELECT id FROM meta_object WHERE path=?',
                          (role_path,)).fetchone()[0]
            where += ' AND r.role_id=?'
            params.append(only_role)
        limit, offset = page_limit(limit), page_offset(offset)
        total = self.count_of(
            'SELECT COUNT(DISTINCT r.role_id) FROM role_right r' + where,
            params, db)
        role_rows = q(
            'SELECT DISTINCT r.role_id, ro.path FROM role_right r'
            ' JOIN meta_object ro ON ro.id=r.role_id' + where +
            ' ORDER BY ro.path LIMIT ? OFFSET ?',
            params + [limit, offset]).fetchall()
        if not role_rows:
            tail = (f' у роли {ru_path(role_path)}' if role_path
                    else ' ни у одной роли')
            out.append('явных записей прав на этот объект нет' + tail)
        by_role = {}
        ids = [rid for rid, _ in role_rows]
        if ids:
            marks = ','.join('?' * len(ids))
            for row in q(
                    'SELECT r.role_id, r.target_uuid, o.path, a.name, a.tabular,'
                    ' t.name, r.sub_index, r.collection_uuid, r.target_flags,'
                    ' r.right_uuid, r.value, r.rls_text'
                    ' FROM role_right r'
                    ' LEFT JOIN meta_object o ON o.id=r.target_object_id'
                    ' LEFT JOIN meta_attribute a ON a.id=r.target_attr_id'
                    ' LEFT JOIN meta_tabular t ON t.id=r.target_tabular_id'
                    f' WHERE r.target_object_id=? AND r.role_id IN ({marks})'
                    ' ORDER BY r.role_id, o.path, t.name, a.tabular, a.name,'
                    ' r.sub_index, r.target_uuid, r.target_flags, r.id',
                    [oid] + ids):
                by_role.setdefault(row[0], []).append(row[1:])
        dup = self._flag_ambiguous(db, 'target_object_id=?', [oid])
        out.append(RIGHTS_NOTE)
        cap = None if only_role else TARGETS_PER_ROLE
        for rid, rpath in role_rows:
            targets = []
            index = {}
            for (target_uuid, obj_path, attr_name, attr_tabular, tab_name,
                 sub_index, collection_uuid, flags, right, value,
                 rls) in by_role.get(rid, ()):
                # тот же uuid объекта с иным sub_index или флагами — другая цель
                key = (target_uuid, sub_index, collection_uuid, flags)
                if key not in index:
                    index[key] = len(targets)
                    targets.append([_target_label(
                        obj_path, attr_name, attr_tabular, tab_name, target_uuid,
                        sub_index, collection_uuid,
                        flags if (target_uuid, sub_index,
                                  collection_uuid) in dup else None), []])
                targets[index[key]][1].append((right, value, rls))
            shown = targets if cap is None else targets[:cap]
            body = '; '.join(f'{label} ({len(rights)}) — {_right_labels(rights)}'
                             for label, rights in shown)
            line = f'{ru_path(rpath)}: ' + (body or '—')
            if cap is not None and len(targets) > cap:
                line += f'; … и ещё {len(targets) - cap} целей'
            out.append(line)
            for _label, rights in shown:
                out.extend(_rls_lines(rights))
        bad = q('SELECT COUNT(*) FROM role_rights_state WHERE parsed=0').fetchone()[0]
        if bad:
            roles = q("SELECT COUNT(*) FROM meta_object WHERE type='Role'"
                      ).fetchone()[0]
            out.append(f'у {bad} из {roles} ролей права недоступны (файл прав не'
                       ' прочитан, причина в role_rights_state.error) — их нет'
                       ' в списке не потому, что они ничего не разрешают')
        out.append('… ' + page_note(total, offset, len(role_rows), ' (ролей)'))
        return '\n'.join(out)

    def module_outline(self, path, code_name='obj', db=None):
        path = self.resolve_path(path, db)
        row = self.conn(db).execute(
            'SELECT m.body FROM module m JOIN meta_object o ON o.id=m.object_id '
            'WHERE o.path=? AND m.code_name=?', (path, code_name)).fetchone()
        if not row or not row[0]:
            return f'модуль не найден: {path} ({code_name})'
        return row[0]

    def _method_row(self, path, code_name, name, db=None):
        """Строка метода: kind, name, signature, directives, description, body,
        is_export, контекст модуля, line_start — либо None."""
        path = self.resolve_path(path, db)
        return self.conn(db).execute(
            'SELECT mt.kind, mt.name, mt.signature, mt.directives, '
            'mt.description, mt.body, mt.is_export, m.context, mt.line_start '
            'FROM method mt JOIN module m ON m.id=mt.module_id '
            'JOIN meta_object o ON o.id=m.object_id '
            'WHERE o.path=? AND m.code_name=? AND LOWER(mt.name)=LOWER(?)',
            (path, code_name, name)).fetchone()

    def bsl_ctx(self, db=None):
        """Контекст анализа BSL базы (кэш: строится секунды на большой базе)."""
        alias = self._alias(db)
        info = self.dbs[alias]
        if info.get('bsl') is None:
            from .bsl_analyzer import BslContext
            info['bsl'] = BslContext(info['conn'])
        return info['bsl']

    def event_index(self, db=None):
        """Подписки на события базы (кэш): по пути, по источнику, по обработчику.

        Источник подписки хранится в заголовке «источниковым» uuid объекта
        (header_props.self_ref_uuid), а обработчик — обычным meta_object.uuid
        общего модуля, поэтому карта uuid -> путь строится обходом
        meta_object.header_json. На УНФ это 1.1 с на 23513 объектов: обход
        делается один раз на базу и только если подписки в ней вообще есть.
        """
        alias = self._alias(db)
        info = self.dbs[alias]
        if info.get('events') is None:
            info['events'] = _build_event_index(info['conn'])
        return info['events']

    def uuid_map(self, db=None):
        """{meta_object.uuid: (путь, тип)} одной базы (кэш).

        Состав плана обмена перечисляет объекты их обычными uuid — их ещё надо
        превратить в пути; на УНФ это 23513 строк одним запросом.
        """
        alias = self._alias(db)
        info = self.dbs[alias]
        if info.get('uuids') is None:
            info['uuids'] = {u: (p, t) for p, t, u in info['conn'].execute(
                'SELECT path, type, uuid FROM meta_object'
                ' WHERE uuid IS NOT NULL')}
        return info['uuids']

    def other_aliases(self, db=None):
        """Алиасы остальных открытых баз, кроме указанной/активной.

        Расширение и внешняя обработка обращаются к объектам основной
        конфигурации, которых в их собственной базе нет. Когда основная база
        открыта рядом, искать надо и в ней — иначе ответ «объект не найден»
        уводит модель в ручную проверку.
        """
        alias = self._alias(db)
        return [a for a in self.dbs if a != alias]

    def foreign_manager(self, db, manager, name):
        """(алиас, путь) — объект метаданных, найденный в другой открытой базе."""
        for alias in self.other_aliases(db):
            target = self.bsl_ctx(alias).resolve_manager(manager, name)
            if target is not None:
                return alias, target
        return None, None

    def foreign_module(self, db, module, method):
        """Пометка о общем модуле, который живёт в другой открытой базе."""
        for alias in self.other_aliases(db):
            ctx = self.bsl_ctx(alias)
            found = ctx.common_module(module)
            if found is None:
                continue
            row = ctx.common_method(found['name'], method)
            if row is None:
                return (f'общий модуль есть в базе «{alias}», но метода '
                        f'{method} в нём нет')
            state = ('Экспорт' if row[0]
                     else 'не Экспорт — вызов извне не работает')
            return f'общий модуль в базе «{alias}»: {method} — {state}'
        return None

    def get_method(self, path, code_name, name, db=None):
        row = self._method_row(path, code_name, name, db)
        if not row:
            path = self.resolve_path(path, db)
            return f'метод не найден: {path} ({code_name}) :: {name}'
        head = f'{row[0]} {row[1]}({row[2]})' + (' Экспорт' if row[6] else '')
        parts = [head]
        if row[3]:
            parts.append('директивы: ' + row[3])
        if row[4]:
            parts.append('описание:\n' + row[4])
        subs = self.event_index(db)['by_handler'].get(
            (self.resolve_path(path, db), str(name).lower()))
        if subs:
            # обработчик подписки вызывает платформа: в коде конфигурации
            # вызова нет, и лексический анализ его не показывает
            parts.append(_method_subscriptions_line(subs))
        parts.append('тело:\n' + row[5])
        return '\n'.join(parts)

    def find_method_context(self, path, code_name, name, match='', before=20,
                            after=20, db=None):
        """Фрагмент тела метода вокруг вхождения match + маркеры точки вставки."""
        row = self._method_row(path, code_name, name, db)
        if not row:
            return (f'метод не найден: {self.resolve_path(path, db)} '
                    f'({code_name}) :: {name}')
        lines = (row[5] or '').splitlines()
        start = row[8] or 1
        before, after = max(0, int(before)), max(0, int(after))
        out = [f'{row[0]} {row[1]}({row[2]}) — строки модуля '
               f'{start}..{start + len(lines) - 1}'
               + (f'; директивы {row[3]}' if row[3] else '')]
        needle = (match or '').strip()
        if needle:
            hits = [i for i, line in enumerate(lines)
                    if needle.lower() in line.lower()]
            if not hits:
                out.append(f'в теле метода не найдено: {needle}')
                out.append('Полное тело — get_method; поиск по другим методам '
                           '— find_methods')
                return '\n'.join(out)
        else:
            out.append('match не задан — показано начало тела; передайте match '
                       '(строку или вызов), чтобы получить точку вставки')
            hits = [0]
        for i in hits[:3]:
            low, high = max(0, i - before), min(len(lines), i + after + 1)
            out.append(f'--- вхождение: строка {start + i} '
                       f'(показано {start + low}..{start + high - 1}) ---')
            for j in range(low, high):
                out.append(f'{">>" if j == i else "  "} {start + j}: {lines[j]}')
            prev_op = next((lines[k].strip() for k in range(i - 1, -1, -1)
                            if lines[k].strip()), '')
            next_op = next((lines[k].strip() for k in range(i + 1, len(lines))
                            if lines[k].strip()), '')
            out.append(f'Маркеры вставки у строки {start + i}:')
            out.append(f'  предыдущий оператор: {prev_op or "(начало метода)"}')
            out.append(f'  следующий оператор:  {next_op or "(конец метода)"}')
        if len(hits) > 3:
            out.append(f'… и ещё {len(hits) - 3} вхождений (уточните match)')
        return '\n'.join(out)

    def method_dependencies(self, path, code_name, name, db=None):
        """Что использует метод: общие модули, метаданные, запросы, параметры."""
        from .bsl_analyzer import analyze, caller_context, split_params
        row = self._method_row(path, code_name, name, db)
        if not row:
            return (f'метод не найден: {self.resolve_path(path, db)} '
                    f'({code_name}) :: {name}')
        kind, mname, sig, dirs, _desc, body, exp, mod_ctx, start = row
        params = split_params(sig)
        ctx = self.bsl_ctx(db)
        report = analyze(body or '', ctx, params=params,
                         caller_context=caller_context(dirs, mod_ctx),
                         self_name=mname)
        out = [f'{kind} {mname}({sig})' + (' Экспорт' if exp else '')]
        out.append(f'Контекст: {dirs or "директив нет"}'
                   + (f'; контекст модуля: {mod_ctx}' if mod_ctx else ''))
        out.append(f'Параметры: {", ".join(params) if params else "нет"}')

        out.append('\nОбщие модули и их методы:')
        if not report['modules']:
            out.append('  вызовов общих модулей не найдено')
        for module, method, line, state in report['modules']:
            out.append(f'  строка {start + line - 1}: {module}.{method} — {state}')

        out.append('\nМетаданные в коде:')
        if not report['metadata']:
            out.append('  обращений к менеджерам метаданных не найдено')
        for manager, obj, line, target in report['metadata']:
            if target:
                shown = ru_path(target)
            else:
                alias, other = self.foreign_manager(db, manager, obj)
                if other:
                    shown = ('в этой базе НЕТ — есть в базе '
                             f'«{alias}»: ' + ru_path(other))
                else:
                    shown = 'НЕ НАЙДЕНО в этой базе' + (
                        ' и в открытых рядом' if self.other_aliases(db) else '')
            out.append(f'  строка {start + line - 1}: {manager}.{obj} -> {shown}')

        out.append('\nЗапросы в коде:')
        if not report['queries']:
            out.append('  текстов запросов не найдено')
        had_errors = False
        for line, query, complete, errors, unverified in report['queries']:
            head = f'  строка {start + line - 1}'
            if not complete:
                out.append(f'{head}: запрос собирается по частям — '
                           'проверка парсером пропущена')
                continue
            had_errors = had_errors or bool(errors)
            out.append(f'{head}: ошибок {len(errors)}, '
                       f'непроверенных полей {len(unverified)}')
            for err in errors[:6]:
                out.append(f'    ! {ru_text(err)}')
            for ref in unverified[:6]:
                out.append(f'    ? {ref} (схема параметра-таблицы неизвестна)')
        others = self.other_aliases(db)
        if had_errors and others:
            # запрос расширения может обращаться к таблицам основной конфигурации;
            # контексты двух баз не сливаются, поэтому подсказываем явную проверку
            names = ', '.join(others)
            out.append('  ошибки запросов получены по метаданным ЭТОЙ базы; если '
                       f'запрос про таблицы другой открытой базы ({names}), '
                       'проверьте тот же текст в её контексте: check_query(text, '
                       'db=<алиас>)')
        if report['tables']:
            out.append('  таблицы запросов: ' + ', '.join(report['tables'][:20]))
        if report['fields']:
            out.append('  поля запросов: ' + ', '.join(report['fields'][:30]))

        if report['context_warnings']:
            out.append('\nКонтекст клиент/сервер:')
            out.extend(f'  ! {w}' for w in report['context_warnings'])
        if report['unknown']:
            foreign, unresolved = [], []
            for left, right, line in report['unknown'][:20]:
                note = self.foreign_module(db, left, right)
                item = f'  строка {start + line - 1}: {left}.{right}'
                if note:
                    foreign.append(f'{item} — {note}')
                else:
                    unresolved.append(item)
            if foreign:
                out.append('\nЗависимости из другой открытой базы (в этой их нет '
                           '— так расширение или внешняя обработка обращается '
                           'к основной конфигурации):')
                out.extend(foreign)
            if unresolved:
                out.append('\nНе разрешено (не общий модуль, не менеджер '
                           'метаданных и не локальная переменная — вероятно, '
                           'реквизит формы/объекта или глобальный контекст):')
                out.extend(unresolved)
        if report['plain_calls']:
            out.append('\nВызовы без точки (методы этого модуля или глобальные '
                       'методы платформы — не проверялись): '
                       + ', '.join(report['plain_calls'][:25]))
        if report['dynamic_calls']:
            out.append('\nДинамические вызовы (строковые литералы как имена):')
            for line, func, literal, resolution in report['dynamic_calls']:
                out.append(f'  строка {start + line - 1}: '
                           f'{func}("{literal}") — {resolution}')
        return '\n'.join(out)

    def method_result_schema(self, path, code_name, name, db=None):
        """Из чего состоит таблица/структура, которую возвращает метод."""
        from .bsl_analyzer import result_schema
        row = self._method_row(path, code_name, name, db)
        if not row:
            return (f'метод не найден: {self.resolve_path(path, db)} '
                    f'({code_name}) :: {name}')
        kind, mname, sig, _dirs, _desc, body, _exp, _ctx, start = row
        found, notes = result_schema(body or '')
        out = [f'{kind} {mname}({sig})']
        if not found and not notes:
            out.append('Признаков создания таблицы значений, структуры или '
                       'запроса в теле не найдено — состав результата по коду '
                       'не определяется (возможно, он возвращается из другого '
                       'метода или это объект метаданных).')
            return '\n'.join(out)
        by_kind = {}
        for line_kind, column, line in found:
            by_kind.setdefault(line_kind, []).append((column, line))
        for line_kind, items in by_kind.items():
            out.append(f'{line_kind.capitalize()} '
                       f'({len(items)}): ' + '; '.join(
                           f'{c} [стр. {start + ln - 1}]' for c, ln in items[:40]))
        for note in notes:
            out.append(note)
        out.append('Внимание: это эвристика по тексту — имена из переменных и '
                   'колонки, добавленные в цикле или другом методе, сюда не '
                   'попадают. Точный состав даёт только выполнение кода.')
        return '\n'.join(out)

    def _fts_shard_conns(self, info):
        """Соединения приставных шардов индекса (read-only) — лениво и с кэшем."""
        conns = info.get('fts_conn')
        if conns is None:
            conns = []
            for path in info.get('fts_shards') or ():
                shard = sqlite3.connect(
                    'file:' + path.replace(os.sep, '/') + '?mode=ro', uri=True)
                shard.execute('PRAGMA cache_size=-8192')
                conns.append(shard)
            info['fts_conn'] = conns
        return conns

    def _fts_filter(self, info, conn, text):
        """Условие «тело содержит text» по FTS-индексу: (фрагмент SQL, параметры).

        None — индекса нет, и вызывающий уходит на body_has. Игла оборачивается в
        кавычки (phrase query), чтобы спецсимволы FTS5 не ломали запрос; trigram
        ищет от трёх символов, поэтому более короткие иглы сюда не доходят.

        Шарды лежат в ОТДЕЛЬНЫХ файлах, и ATTACH не подходит: лимит
        SQLITE_MAX_ATTACHED (10 по умолчанию) считается на соединение, а в
        мульти-базовом режиме их набирается больше. Поэтому выдачи шардов
        объединяются во временной таблице основного соединения — TEMP-схема
        доступна и при mode=ro, а rowid во всех шардах это method.id.
        """
        needle = f'"{text}"'
        table = info.get('fts')
        if table:
            return (f' AND mt.id IN (SELECT rowid FROM {table} WHERE {table} MATCH ?)',
                    [needle])
        shards = self._fts_shard_conns(info)
        if not shards:
            return None
        hits = set()
        for shard in shards:
            hits.update(r[0] for r in shard.execute(
                f'SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?', (needle,)))
        conn.execute('DROP TABLE IF EXISTS temp._fts_hits')
        conn.execute('CREATE TEMP TABLE _fts_hits (id INTEGER PRIMARY KEY)')
        conn.executemany('INSERT INTO temp._fts_hits(id) VALUES (?)',
                         ((hit,) for hit in sorted(hits)))
        return ' AND mt.id IN (SELECT id FROM temp._fts_hits)', []

    def find_methods(self, mask='', text='', path=None, limit=20, offset=0,
                     db=None):
        """Поиск методов: mask — имя/сигнатура/описание, text — подстрока в теле.

        text отвечает на вопрос «где в коде это упоминается» — все обращения к
        объекту, все точки записи регистра, все вызовы общего модуля. По каждому
        совпадению выдаётся номер строки модуля и сама строка, поэтому искать
        дальше инструментом sql не нужно. Совпадений обычно больше страницы —
        выдача листается параметром offset; порядок строгий (по id метода,
        а он растёт вдоль объектов и модулей), поэтому страницы не пересекаются.
        """
        path = self.resolve_path(path, db) if path else None
        limit, offset = page_limit(limit), page_offset(offset)
        like = f'%{mask}%'
        like_ns = f'%{mask.replace(" ", "").lower()}%'
        # OR-группа обязана быть в скобках: AND связывается сильнее OR, и без
        # скобок фильтр по объекту применялся только к последней ветке — поиск
        # «в одном объекте» молча возвращал методы всей конфигурации
        cond = ('(mt.name LIKE ? OR mt.signature LIKE ? '
                'OR mt.description LIKE ? '
                "OR lower_ru(REPLACE(mt.name, ' ', '')) LIKE ?)")
        params = [like, like, like, like_ns]
        conn = self.conn(db)
        db_info = self.dbs[self._alias(db)]
        # trigram ищет от трёх символов, поэтому короткие иглы идут мимо индекса
        fts_filter = self._fts_filter(db_info, conn, text) \
            if text and len(text) >= 3 else None
        if text:
            if fts_filter:
                cond += fts_filter[0]
                params.extend(fts_filter[1])
            else:
                # body_has — своя SQL-функция, регистронезависимая в обе стороны:
                # LIKE сворачивает регистр только для ASCII, а пара LIKE через OR
                # ловила бы лишь тот регистр, в котором игла передана
                cond += ' AND body_has(mt.body, ?)'
                params.append(text.lower())
        if path:
            cond += ' AND o.path=?'
            params.append(path)
        tables = (' FROM method mt '
                  'JOIN module m ON m.id=mt.module_id '
                  'JOIN meta_object o ON o.id=m.object_id WHERE ')
        # extra=(text,): при поиске по FTS-индексу игла попадает не в params,
        # а во временную таблицу совпадений, и без неё ключ кэша совпал бы
        # у двух разных поисков
        total = self.count_of(f'SELECT COUNT(*){tables}{cond}', params, db,
                              extra=(text,))
        if not total:
            return f'в телах методов ничего не найдено: {text}' if text \
                else 'ничего не найдено'
        rows = conn.execute(
            'SELECT o.path, m.code_name, mt.kind, mt.name, mt.signature, '
            'mt.directives, mt.description'
            + (', mt.body, mt.line_start' if text else '')
            + tables + cond
            # порядок по первичному ключу: writer вставляет методы вдоль
            # объектов и модулей, поэтому выдача читается так же, как
            # «путь, модуль, строка», но планировщик снимает её прямо с
            # обхода таблицы (без temp B-tree) и обрывает на LIMIT —
            # сортировка всех совпадений на короткой игле стоила 3,3 с
            + ' ORDER BY mt.id LIMIT ? OFFSET ?',
            params + [limit, offset]).fetchall()
        out = []
        for row in rows:
            p, code, kind, name, sig, dirs, desc = row[:7]
            line = f'{ru_path(p)} ({code}) — {kind} {name}({sig})'
            if dirs:
                line += f' [{dirs}]'
            if text:
                hits = body_hits(row[7] or '', text, row[8] or 1)
                if hits:
                    line += '\n    ' + '\n    '.join(hits)
            elif desc:
                line += ' | ' + desc.splitlines()[0][:80]
            out.append(line)
        out.append('… ' + page_note(total, offset, len(rows)))
        if text:
            out.append('тело метода целиком — get_method, окно строк вокруг '
                       'нужного вызова — find_method_context')
        return '\n'.join(out)

    def skd_of(self, path, db=None):
        path = self.resolve_path(path, db)
        rows = self.conn(db).execute(
            'SELECT q.query FROM skd_query q JOIN meta_object o '
            'ON o.id=q.object_id WHERE o.path=? ORDER BY q.ord',
            (path,)).fetchall()
        if not rows:
            return f'у объекта нет запросов СКД: {path}'
        return ('\n;\n'.join(r[0] for r in rows))[:20000]

    def find_skd(self, mask, limit=10, offset=0, db=None):
        like = f'%{mask}%'
        limit, offset = page_limit(limit), page_offset(offset)
        tables = (' FROM skd_query q JOIN meta_object o ON o.id=q.object_id '
                  'WHERE q.query LIKE ? ')
        total = self.count_of(f'SELECT COUNT(*){tables}', (like,), db)
        if not total:
            return 'ничего не найдено'
        rows = self.conn(db).execute(
            f'SELECT q.id, o.path, q.query{tables}'
            'ORDER BY o.path, q.ord, q.id LIMIT ? OFFSET ?',
            (like, limit, offset)).fetchall()
        out = []
        for rid, path, text in rows:
            pos = text.lower().find(mask.lower())
            snippet = text[max(0, pos - 120):pos + 240].replace('\n', ' ')
            out.append(f'[{rid}] {ru_path(path)} … {snippet} …')
        out.append('… ' + page_note(total, offset, len(rows)))
        return '\n'.join(out)

    def xdto_of(self, path, type=None, db=None):
        """Состав пакета XDTO: пространство имён, импорты, типы и их свойства.

        :param type: имя типа — показать только его (вместе с вложенными)
        """
        path = self.resolve_path(path, db)
        q = self.conn(db).execute
        row = q('SELECT id, type, header_json FROM meta_object WHERE path=?',
                (path,)).fetchone()
        if not row:
            return f'объект не найден: {path}'
        oid = row[0]
        out = [f'{ru_path(path)} — {row[1]}']
        out.extend(header_props.xdto_props(row[1], row[2]))
        imports = [r[0] for r in q(
            'SELECT namespace FROM xdto_import WHERE object_id=? ORDER BY ord',
            (oid,))]
        if imports:
            out.append('Импортирует пространства имён: ' + ', '.join(imports))
        types = q('SELECT id, ord, name, kind, base, base_ns, facets,'
                  ' enum_values FROM xdto_type WHERE object_id=? '
                  'ORDER BY ord, id', (oid,)).fetchall()
        by_id = {t[0]: t for t in types}
        props = {}
        for r in q('SELECT type_id, ord, name, type, lower_bound, upper_bound, '
                   'form, nested_type_id, extra FROM xdto_property '
                   'WHERE object_id=? ORDER BY ord', (oid,)):
            props.setdefault(r[0], []).append(r[1:])
        out.append(f'Типов в пакете: {len(types)}')
        shown = types
        if type is not None:
            wanted = str(type).lower()
            shown = [t for t in types if (t[2] or '').lower() == wanted]
            if not shown:
                out.append(f'тип не найден в пакете: {type}')
        for item in shown:
            out.extend(self._xdto_type_lines(item, props, by_id, ''))
        # свойства, объявленные в самом пакете, вне типов (type_id IS NULL)
        if props.get(None):
            out.append('Свойства пакета (вне типов):')
            out.extend(self._xdto_prop_lines(props[None], props, by_id, '  '))
        text = '\n'.join(out)
        if len(text) > 20000:
            # обрезанный состав пакета не должен выглядеть полным
            text = text[:20000] + ('\n… состав пакета обрезан: уточните тип '
                                   'параметром type= или спросите find_xdto')
        return text

    @staticmethod
    def _xdto_type_lines(item, props, by_id, indent):
        """Строки типа XDTO: заголовок, ограничения, значения, свойства."""
        _tid, _ord, name, kind, base, base_ns, facets, values = item
        head = f'{indent}Тип {name or "(без имени)"} ({kind})'
        if base:
            head += f', базовый {base}' + (f' [{base_ns}]' if base_ns else '')
        out = [head]
        if facets:
            out.append(f'{indent}  ограничения: {facets}')
        if values:
            out.append(f'{indent}  значения: {values}')
        out.extend(McpServer._xdto_prop_lines(props.get(_tid, []), props,
                                              by_id, indent))
        return out

    @staticmethod
    def _xdto_prop_lines(rows, props, by_id, indent):
        """Строки свойств; вложенный анонимный тип — большим отступом."""
        out = []
        for _ord, pname, ptype, lower, upper, form, nested, extra in rows:
            bits = [ptype or ('вложенный тип' if nested is not None
                              else 'тип не указан')]
            if lower:
                bits.append('обязательное')
            if upper == -1:
                bits.append('список')
            if form:
                bits.append('атрибут' if form == 'Attribute' else 'элемент')
            line = f'{indent}  {pname or "(без имени)"}: ' + ', '.join(bits)
            if extra:
                line += f' [{extra}]'
            out.append(line)
            if nested is not None and nested in by_id:
                out.extend(McpServer._xdto_type_lines(by_id[nested], props,
                                                      by_id, indent + '    '))
        return out

    def find_xdto(self, mask, limit=20, offset=0, db=None):
        """Типы и свойства пакетов XDTO, чьё имя содержит mask.

        Список виртуально склеен: сначала все совпавшие типы, потом все
        свойства, поэтому total_count и offset считаются по склейке, а
        страница может начинаться уже со свойств.
        """
        like = f'%{mask}%'
        limit, offset = page_limit(limit), page_offset(offset)
        type_from = (' FROM xdto_type t JOIN meta_object o ON o.id=t.object_id '
                     'WHERE t.name LIKE ? ')
        prop_from = (' FROM xdto_property p JOIN xdto_type t ON t.id=p.type_id '
                     'JOIN meta_object o ON o.id=p.object_id '
                     'WHERE p.name LIKE ? ')
        types_total = self.count_of(f'SELECT COUNT(*){type_from}', (like,), db)
        props_total = self.count_of(f'SELECT COUNT(*){prop_from}', (like,), db)
        total = types_total + props_total
        if not total:
            return f'в пакетах XDTO ничего не найдено по «{mask}»'
        out = []
        if offset < types_total:
            for path, name, base in self.conn(db).execute(
                    f'SELECT o.path, t.name, t.base{type_from}'
                    'ORDER BY o.path, t.ord, t.id LIMIT ? OFFSET ?',
                    (like, limit, offset)):
                out.append(f'{ru_path(path)} — тип {name}'
                           + (f' (базовый {base})' if base else ''))
        if len(out) < limit:
            for path, tname, pname, ptype in self.conn(db).execute(
                    f'SELECT o.path, t.name, p.name, p.type{prop_from}'
                    'ORDER BY o.path, t.ord, p.ord, p.id LIMIT ? OFFSET ?',
                    (like, limit - len(out), max(0, offset - types_total))):
                out.append(f'{ru_path(path)} — {tname}.{pname}'
                           + (f': {ptype}' if ptype else ''))
        out.append('… ' + page_note(total, offset, len(out),
                                    f' (типов {types_total}, '
                                    f'свойств {props_total})'))
        return '\n'.join(out)

    def check_query(self, text, db=None):
        from .query_lang import check_query_full
        errs, unverified = check_query_full(text, self.ctx(db))
        parts = []
        if errs:
            parts.append('Ошибки:\n' + '\n'.join(ru_text(e) for e in errs))
        else:
            msg = 'OK: синтаксис корректен, таблицы/поля/цепочки существуют'
            if unverified:
                msg += ('\n\nНепроверенные поля параметров-таблиц '
                        '(схема неизвестна):\n' +
                        '\n'.join(f'  {ref}' for ref in unverified))
            parts.append(msg)
        return '\n'.join(parts)

    def sql(self, query, db=None):
        stripped = _sql_rewrite(query).strip().rstrip(';')
        head = stripped.upper()
        if not (head.startswith('SELECT') or head.startswith('WITH')):
            raise ValueError('разрешены только SELECT/WITH (read-only)')
        if ' LIMIT ' not in head:
            stripped += ' LIMIT 200'
        cur = self.conn(db).execute(stripped)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchall()
        if not rows:
            return '(пусто)'
        out = [' | '.join(cols)]
        for row in rows:
            cells = []
            for v in row:
                s = str(v)
                cells.append(s[:120] + ('…' if len(s) > 120 else ''))
            out.append(' | '.join(cells))
        return '\n'.join(out)

    # -- управление базами ---------------------------------------------------
    def cfg_props(self, alias):
        """Свойства конфигурации базы; разбираются один раз и кэшируются.

        Заголовок корня большой (у УНФ ~4 МБ из-за списка versions), поэтому
        повторный разбор на каждый db_list/configuration_info не делается.
        """
        info = self.dbs[alias]
        if info.get('cfg') is None:
            row = info['conn'].execute(
                'SELECT type, type_ru, name, header_json FROM meta_object '
                'WHERE parent_id IS NULL ORDER BY id LIMIT 1').fetchone()
            props = header_props.config_props(row[3]) if row else {}
            if row:
                props.setdefault('name', row[2])
                props['root_type'] = row[0]
                props['root_type_ru'] = row[1]
            src = _source_row(info['conn'])
            # путь исходника нужен для типа файла: .erf и .epf дают один
            # и тот же root_type, различает их только расширение
            props['source_file'] = src.get('file')
            props['source_sha256'] = src.get('file_sha256')
            props['source_size'] = src.get('file_size')
            info['cfg'] = props
        return info['cfg']

    def cfg_summary(self, alias):
        """(имя конфигурации, строка свойств) базы — коротко для db_list."""
        props = self.cfg_props(alias)
        kind, ext = header_props.kind_of(props.get('root_type'),
                                         props.get('root_type_ru'),
                                         props.get('source_file'))
        bits = []
        if kind:
            bits.append(f'{kind} ({ext})' if ext else kind)
        bits.append(props.get('version') or 'версия не указана')
        compat = header_props.compatibility_short(props.get('root_type'),
                                                  props.get('compatibility'))
        if compat:
            bits.append(f'режим совместимости {compat}')
        if props.get('name_prefix'):
            bits.append(f'префикс имён {props["name_prefix"]}')
        if props.get('source_sha256'):
            # короткий отпечаток исходника: по нему видно, что две открытые базы
            # собраны из одного и того же файла; полный — в configuration_info
            bits.append(f'SHA-256 {props["source_sha256"][:16]}…')
        return props.get('name') or '?', '; '.join(bits)

    def db_list(self):
        if not self.dbs:
            return 'нет открытых баз — откройте через db_open'
        out = []
        for alias, info in self.dbs.items():
            nobj, nmod, nmeth = self.db_stats(alias)
            mark = '*' if alias == self.active else ' '
            groups = self.groups_of(alias)
            tail = f' [в группах: {", ".join(groups)}]' if groups else ''
            out.append(f'{mark} {alias} — {info["path"]} '
                       f'(объектов: {nobj}, модулей: {nmod}, '
                       f'методов: {nmeth}){tail}')
            name, meta = self.cfg_summary(alias)
            out.append(f'    {name}: {meta} (configuration_info — подробно)')
        return 'Открытые базы (* — активная):\n' + '\n'.join(out)

    def db_open(self, path, alias=None):
        alias = self.open_db(path, alias)
        nobj, nmod, nmeth = self.db_stats(alias)
        return (f'база открыта: {alias} — {self.dbs[alias]["path"]} '
                f'(объектов: {nobj}, модулей: {nmod}, методов: {nmeth}); '
                'сделана активной')

    def db_use(self, alias):
        alias = self._alias(alias)
        self.active = alias
        return f'активная база: {alias} — {self.dbs[alias]["path"]}'

    def db_close(self, alias=None):
        alias = self.close_db(alias)
        if not self.dbs:
            return f'база {alias} закрыта; открытых баз не осталось'
        return f'база {alias} закрыта; активная: {self.active}'

    # -- инструменты групп ---------------------------------------------------
    def group_create(self, name):
        name = self.create_group(name)
        return f'группа создана: {name}'

    def group_add_db(self, group, db):
        group_name, alias = self.add_db_to_group(group, db)
        return f'база {alias} добавлена в группу {group_name}'

    def group_remove_db(self, group, db):
        group_name, alias = self.remove_db_from_group(group, db)
        return f'база {alias} удалена из группы {group_name}'

    def group_list(self):
        groups = self.list_groups()
        if not groups:
            return ('нет созданных групп — создайте через group_create; '
                    'открытые базы перечисляет db_list')
        out = []
        for g in groups:
            mark = g['mark']
            out.append(f'{mark} {g["name"]} ({len(g["databases"])} баз):')
            for db_info in g['databases']:
                out.append(f'    {db_info}')
        out.append('Запрос по одной группе — параметр group=<имя> '
                   '(инструмент выполнится по всем её базам), по всем группам '
                   'сразу — group=\'*\'. Без group работает только активная '
                   'база (* в db_list), а не вся активная группа.')
        return 'Группы баз (* — активная):\n' + '\n'.join(out)

    def group_use(self, group):
        group_name = self.use_group(group)
        dbs = [a for a in self.groups[group_name] if a in self.dbs]
        line = f'активная группа: {group_name} (баз: {len(dbs)})'
        if self.active:
            line += f'; активная база: {self.active}'
        return line + (f'. Без group инструменты работают только с активной '
                       f'базой; запрос по всей группе — group={group_name}')

    def group_close(self, group):
        group_name = self.close_group(group)
        if not self.groups:
            return f'группа {group_name} удалена; групп не осталось'
        return f'группа {group_name} удалена; активная: {self.active_group}'


# Категории ошибок инструмента: клиенту нужен понятный код, а не «Unknown».
LOCKED_RE = re.compile(r'lock|busy', re.I)
RETRY_DELAY = 0.05


def error_text(err):
    """'ошибка [КАТЕГОРИЯ]: сообщение' — категория по типу исключения.

    Отдельно выделена занятая база (SQLITE_BUSY/locked): она транзитна, и
    вызывающая сторона может повторить запрос.
    """
    if isinstance(err, sqlite3.OperationalError):
        low = str(err).lower()
        if LOCKED_RE.search(low):
            code = 'DB_LOCKED'
        elif 'no such' in low:
            code = 'DB_SCHEMA'
        else:
            code = 'DB_ERROR'
    elif isinstance(err, sqlite3.Error):
        code = 'DB_ERROR'
    elif isinstance(err, TypeError):
        code = 'BAD_ARGS'
    elif isinstance(err, ValueError):
        code = 'BAD_REQUEST'
    elif isinstance(err, OSError):
        code = 'IO_ERROR'
    else:
        code = 'INTERNAL'
    detail = f'{type(err).__name__}: {err}' if code == 'INTERNAL' else str(err)
    return f'ошибка [{code}]: {detail}'


def call_with_retry(fn, *args, **kwargs):
    """Вызов инструмента с повтором, если база оказалась занята."""
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as err:
            if attempt == 2 or not LOCKED_RE.search(str(err)):
                raise
            time.sleep(RETRY_DELAY * (attempt + 1))


def _db_header(server, alias, groups=None):
    """Заголовок секции ответа: '=== группа X / база Y (путь) ==='.

    `groups=None` — назвать группы, в которые база входит: деление на группы
    должно быть видно и в ответе на запрос к одной базе. `groups=[]` — не
    называть (секция уже идёт под заголовком группы).
    """
    names = server.groups_of(alias) if groups is None else list(groups)
    if len(names) == 1:
        prefix = f'группа {names[0]} / '
    elif names:
        prefix = f'группы {", ".join(names)} / '
    else:
        prefix = ''
    return f'=== {prefix}база {alias} ({server.dbs[alias]["path"]}) ==='


class Tool:
    def __init__(self, name, description, schema, fn):
        self.name = name
        self.description = description
        self.schema = schema
        self.fn = fn

    def spec(self):
        return {'name': self.name, 'description': self.description,
                'inputSchema': self.schema}

    def run(self, server, **args):
        # Поддержка групп: приоритет group > db > active_group > active
        group = args.get('group')
        db = args.get('db')

        # Инструменты управления группами не используют fan-out
        is_group_management = self.name.startswith('group_')

        # group='*' — fan-out по всем группам
        if group == '*' and 'group' in self.schema.get('properties', {}) and not is_group_management:
            if not server.groups:
                raise ValueError('нет созданных групп — создайте через group_create')
            parts = []
            for group_name, db_aliases in server.groups.items():
                group_parts = []
                for alias in db_aliases:
                    if alias in server.dbs:
                        # Убираем group из args, т.к. методы не принимают этот параметр
                        call_args = {k: v for k, v in args.items() if k != 'group'}
                        call_args['db'] = alias
                        part = call_with_retry(self.fn, server, **call_args)
                        group_parts.append(
                            _db_header(server, alias, []) + '\n' + part)
                if group_parts:
                    parts.append(f'=== группа {group_name} ===\n' + '\n\n'.join(group_parts))
            if not parts:
                raise ValueError('группы не содержат открытых баз')
            return '\n\n'.join(parts)

        # Конкретная группа — fan-out по её базам
        if group and 'group' in self.schema.get('properties', {}) and not is_group_management:
            db_aliases = server.get_group_dbs(group)
            if not db_aliases:
                raise ValueError(f'группа {group} пуста — добавьте базы через group_add_db')
            parts = []
            for alias in db_aliases:
                if alias in server.dbs:
                    # Убираем group из args, т.к. методы не принимают этот параметр
                    call_args = {k: v for k, v in args.items() if k != 'group'}
                    call_args['db'] = alias
                    part = call_with_retry(self.fn, server, **call_args)
                    parts.append(
                        _db_header(server, alias, [group]) + '\n' + part)
            if not parts:
                raise ValueError(f'группа {group} не содержит открытых баз')
            return '\n\n'.join(parts)

        # db='*' — выполнить инструмент по всем открытым базам сразу
        if db == '*' and 'db' in self.schema.get('properties', {}):
            parts = []
            for alias in server.dbs:
                part = call_with_retry(self.fn, server, **dict(args, db=alias))
                parts.append(_db_header(server, alias) + '\n' + part)
            if not parts:
                raise ValueError('нет открытых баз — укажите путь в db_open')
            return '\n\n'.join(parts)

        # Обычный запрос к одной базе
        result = call_with_retry(self.fn, server, **args)

        # Если у инструмента есть параметр db — добавляем заголовок с идентификатором базы
        if 'db' in self.schema.get('properties', {}):
            alias = server._alias(args.get('db'))  # разрешает None → активная база
            return _db_header(server, alias) + '\n' + result

        return result


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required)}


_STR = {'type': 'string'}
_INT = {'type': 'integer'}
_DB = {'type': 'string',
       'description': 'Alias of the knowledge base to query INSTEAD of the '
                      'active one (see db_list). Omit to use the active base. '
                      "Special value '*': run the tool on EVERY open base at "
                      'once; the answer comes back sectioned per base — note '
                      'that it ignores the group division, so use group=* '
                      'when the bases must stay separated by group.'}
_GROUP = {'type': 'string',
          'description': 'Name of the configuration group to query SEPARATELY '
                         'from the other open groups (group_list names them). '
                         'A group bundles related databases (main config + '
                         'extensions + processors). When specified, the tool '
                         'runs on ALL databases of that group and the answer '
                         'is sectioned per database, each section headed with '
                         "the group and the base. Special value '*': run on "
                         'every group at once, keeping the groups apart. Takes '
                         'priority over the db parameter; when omitted, only '
                         'the ACTIVE base is queried (not the whole group).'}
_LIMIT = {'type': 'integer',
          'description': 'Page size, 1..200. When the search has more hits '
                         'than one page, read the next page with offset '
                         'instead of raising limit.'}
_OFFSET = {'type': 'integer',
           'description': 'Hits of the SAME search to skip, i.e. the start of '
                          'the page (default 0 = the first page). The answer '
                          'ends with the total hit count, the range shown and '
                          'the offset to pass for the next page; the order of '
                          'hits is stable, so pages never overlap or skip.'}

TOOLS = [
    Tool('find_objects',
         'Search metadata objects by name or path substring. Returns '
         "configurator-style dotted paths ('Справочник.Имя') with Russian and "
         'English type labels. First step for anything: locate '
         'справочник/документ/регистр by its Russian name.',
         _schema({'mask': _STR, 'type': _STR, 'limit': _LIMIT,
                  'offset': _OFFSET, 'db': _DB, 'group': _GROUP}, ('mask',)),
         McpServer.find_objects),
    Tool('object_card',
         "Full 'passport' of one object in a single call: type, header "
         'attributes with types, tabular sections with their fields, the event '
         'subscriptions the platform fires on this object, modules, '
         'SKD query count, forward/reverse references. For an '
         'EventSubscription: its event, handler module.method and source '
         'objects; for an ExchangePlan: the objects it synchronizes. '
         'Use right after find_objects.',
         _schema({'path': _STR, 'db': _DB, 'group': _GROUP}, ('path',)),
         McpServer.object_card),
    Tool('object_tree',
         "Browse the metadata tree 'as in the configurator' (subsystems, "
         'nested forms/commands). path empty = configuration root.',
         _schema({'path': _STR, 'depth': _INT, 'db': _DB, 'group': _GROUP}),
         McpServer.object_tree),
    Tool('find_field',
         'Reverse search: which objects contain a field/tabular-section field '
         'with this name. Use to discover join paths between tables.',
         _schema({'name': _STR, 'limit': _LIMIT, 'offset': _OFFSET,
                  'db': _DB, 'group': _GROUP}, ('name',)),
         McpServer.find_field),
    Tool('refs_of',
         "Reference links of an object via attribute types: forward ('on what "
         "it references') and reverse ('who references it') — impact analysis. "
         'limit/offset page each of the two lists separately.',
         _schema({'path': _STR, 'direction': _STR, 'limit': _LIMIT,
                  'offset': _OFFSET, 'db': _DB, 'group': _GROUP}, ('path',)),
         McpServer.refs_of),
    Tool('role_rights',
         'Explicit rights of ONE role (from Role.0.c1brace): a line per target — '
         'the object itself, one of its attributes / tabular-section fields, or '
         'one of its tabular sections — with the rights set on it and their '
         'record-level restriction (RLS) text, plus the role RLS templates. '
         'Storage is SPARSE: a target absent from the answer means the right is '
         'NOT SET for this role, never "denied". A right is named by the first 8 '
         'hex chars of its platform uuid and its value is printed exactly as '
         'stored (1 or -1): the configuration holds no right names, so do not '
         'present them as "Чтение"/"Запись" — the dictionary is not confirmed. '
         'Says "данные недоступны" (with the reason) when the rights file could '
         'not be read, which is NOT the same as an empty list. Pages targets.',
         _schema({'role': _STR, 'limit': _LIMIT, 'offset': _OFFSET,
                  'db': _DB, 'group': _GROUP}, ('role',)),
         McpServer.role_rights),
    Tool('object_rights',
         'Which roles carry explicit rights on an OBJECT — its attributes, '
         'tabular-section fields and tabular sections included, because they '
         'belong to the same object. One line per role with its targets and '
         'rights (same sparse rule, same uuid/value caveat as role_rights). Pass '
         'role to see one role-object pair in full. The answer also names how '
         'many roles have UNREADABLE rights files: they are missing from the '
         'list for that reason, not because they deny access. Use it for access '
         'analysis ("who can work with this catalog"). Pages roles.',
         _schema({'path': _STR, 'role': _STR, 'limit': _LIMIT, 'offset': _OFFSET,
                  'db': _DB, 'group': _GROUP}, ('path',)),
         McpServer.object_rights),
    Tool('module_outline',
         'Table of contents of a 1C module: signatures, comments, #Если '
         "regions, WITHOUT method bodies. code_name: 'obj' (object module), "
         "'mgr' (manager module) etc. Cheap way to inspect a module.",
         _schema({'path': _STR, 'code_name': _STR, 'db': _DB, 'group': _GROUP}, ('path',)),
         McpServer.module_outline),
    Tool('get_method',
         'Full source of one procedure/function: signature, directives '
         '(&НаСервере…), description comment and body. If the method is an '
         'event-subscription handler, the answer names the subscriptions that '
         'call it — the platform calls those, so no call site exists in code. '
         'Use after find_methods/module_outline.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR, 'db': _DB, 'group': _GROUP},
                 ('path', 'code_name', 'name')),
         McpServer.get_method),
    Tool('find_method_context',
         'A WINDOW into a method body instead of the whole body: lines around '
         'each occurrence of match, with the module line numbers, plus stable '
         'insertion markers (the previous and the next statement). Cheaper '
         'than get_method on a big method and the right way to pick a place '
         'to insert code. before/after = how many lines to show (default 20).',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR,
                  'match': _STR, 'before': _INT, 'after': _INT, 'db': _DB, 'group': _GROUP},
                 ('path', 'code_name', 'name')),
         McpServer.find_method_context),
    Tool('method_dependencies',
         'Static analysis of ONE method: its parameters, the common modules it '
         'calls (and whether those methods exist and are Экспорт), the '
         'metadata it touches (Справочники.Х, Документы.Х…), the tables and '
         'fields of the queries inside it (each query is validated), and what '
         'could not be resolved. When another knowledge base is open — a main '
         'configuration next to an extension or an external data processor — '
         'references missing from this base are looked up in the others too, '
         'and the answer names the base each one was found in instead of just '
         'saying "not found". Use it before porting a customization to '
         'another configuration — it lists everything the code needs there.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR, 'db': _DB, 'group': _GROUP},
                 ('path', 'code_name', 'name')),
         McpServer.method_dependencies),
    Tool('method_result_schema',
         'Best-effort shape of the value a method RETURNS: columns added with '
         'Колонки.Добавить, keys of Новая Структура, and the result columns of '
         'the queries in the body (aliases / field names). Use it when a stock '
         'function returns a temporary table and you need to know its columns '
         'without guessing. HEURISTIC: names built at runtime are reported as '
         'dynamic, and the answer says what it could not see.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR, 'db': _DB, 'group': _GROUP},
                 ('path', 'code_name', 'name')),
         McpServer.method_result_schema),
    Tool('find_methods',
         'Search 1C methods. mask = substring of name/signature/description '
         "(e.g. 'ПриПроведении') — reuse existing code instead of inventing. "
         'text = substring inside method BODIES: the way to find EVERY place '
         'that touches something (all writes to a register, all calls of a '
         'common module, all uses of a field) without falling back to sql — '
         'each hit comes with its module line number and the line itself. '
         'mask and text may be combined; path narrows the search to one object '
         '(it is a real filter now). Body search is case-insensitive and scans '
         'every method, so it takes seconds on a large base. Hits are paged: '
         'the answer ends with the total count and the offset of the next '
         'page, so page through a broad search instead of raising limit.',
         _schema({'mask': _STR, 'text': _STR, 'path': _STR, 'limit': _LIMIT,
                  'offset': _OFFSET, 'db': _DB, 'group': _GROUP}),
         McpServer.find_methods),
    Tool('skd_of',
         'All SKD (report) queries of an object — the best examples of how '
         'THIS configuration queries its own tables.',
         _schema({'path': _STR, 'db': _DB, 'group': _GROUP}, ('path',)),
         McpServer.skd_of),
    Tool('find_skd',
         'Search across all SKD query texts (e.g. a table name like '
         "'РегистрНакопления.Запасы'). Returns snippets around the match.",
         _schema({'mask': _STR, 'limit': _LIMIT, 'offset': _OFFSET,
                  'db': _DB, 'group': _GROUP}, ('mask',)),
         McpServer.find_skd),
    Tool('xdto_of',
         'Contents of an XDTO package: target namespace, imported namespaces, '
         'object types with their properties (type, obligatory, list, '
         'attribute/element form) and nested anonymous types. This is the '
         'contract of web/HTTP services and of message-based exchange. '
         'Optional type= shows a single type.',
         _schema({'path': _STR, 'type': _STR, 'db': _DB, 'group': _GROUP}, ('path',)),
         McpServer.xdto_of),
    Tool('find_xdto',
         'Search type and property NAMES inside every XDTO package of the base '
         '(e.g. the field of a message an exchange contract defines). Each hit '
         'names its package and type. Hits are one list — matching types '
         'first, then matching properties — paged by limit/offset; the answer '
         'names both counts.',
         _schema({'mask': _STR, 'limit': _LIMIT, 'offset': _OFFSET,
                  'db': _DB, 'group': _GROUP}, ('mask',)),
         McpServer.find_xdto),
    Tool('check_query',
         'Validate a 1C query: syntax (Russian keywords) + existence of '
         'tables/fields/reference chains against this configuration. ALWAYS '
         'run it on a query you wrote before using it.',
         _schema({'text': _STR, 'db': _DB, 'group': _GROUP}, ('text',)),
         McpServer.check_query),
    Tool('sql',
         'Read-only SELECT escape hatch for anything not covered by the '
         'dedicated tools. Non-SELECT is rejected; LIMIT 200 enforced.',
         _schema({'query': _STR, 'db': _DB, 'group': _GROUP}, ('query',)),
         McpServer.sql),
    Tool('compare_object',
         'Compare ONE metadata object between two open knowledge bases in a '
         'single call: presence, attributes and their types, tabular sections, '
         'register dimensions/resources, forms and commands, modules, methods '
         '(signature, directives, body), SKD queries. Use it for a standard vs '
         'a customized configuration, or for the same object in two releases. '
         'db_left/db_right are aliases from db_list (NOT the db parameter); '
         'path_right is needed only when the object is named differently in '
         'the right base.',
         _schema({'path': _STR, 'db_left': _STR, 'db_right': _STR,
                  'path_right': _STR}, ('path', 'db_left', 'db_right')),
         McpServer.compare_object),
    Tool('extension_diff',
         'What an EXTENSION does relative to the main configuration, in one '
         'call: new objects (marked with the extension name prefix), borrowed '
         'objects, the extension methods inside them with their directives '
         '(&Вместо replaces a stock method, &После/&Перед insert code around '
         'it), attributes the extension adds, and external dependencies '
         '(references to objects living outside the extension). Both arguments '
         'are aliases from db_list.',
         _schema({'extension_db': _STR, 'base_db': _STR, 'limit': _INT},
                 ('extension_db', 'base_db')),
         McpServer.extension_diff),
    Tool('configuration_info',
         'Passport of the knowledge base itself: WHAT KIND of file it was built '
         'from (.cf configuration / .cfe extension / .epf external data '
         'processor / .erf external report), its name and synonym, its VERSION, '
         'compatibility mode (режим совместимости — external reports and data '
         'processors have none, an extension inherits it from the main '
         'configuration), extension name prefix, source file and the date the '
         'base was built, the SHA-256 of the source file (the same digest in '
         'two bases means they were built from the very same file, so no '
         're-extraction is needed), object/module/method counts. Call it first when you '
         'need to know WHICH configuration and which release you are looking '
         'at (e.g. before porting code between configurations).',
         _schema({'db': _DB, 'group': _GROUP}),
         McpServer.configuration_info),
    Tool('db_list',
         'List the knowledge bases open on this server: alias, file path, '
         'object/module/method counts, and one line per base with the kind of '
         'the source file (.cf/.cfe/.epf/.erf), its version and compatibility '
         'mode, plus the configuration group(s) each base belongs to; * marks '
         'the ACTIVE base that the other tools query by default. Several '
         'groups can be open at once — group_list shows the division.',
         _schema({}),
         McpServer.db_list),
    Tool('db_open',
         'Open one more knowledge base file while the server is running '
         '(e.g. an extension or a data processor extracted next to the main '
         'configuration) and make it active. path = path to the .db/.sqlite '
         'file given by the user; alias = optional short name (default: the '
         'file name without extension).',
         _schema({'path': _STR, 'alias': _STR}, ('path',)),
         McpServer.db_open),
    Tool('db_use',
         'Switch the ACTIVE knowledge base — the one all other tools query '
         'when the db parameter is omitted.',
         _schema({'alias': _STR}, ('alias',)),
         McpServer.db_use),
    Tool('db_close',
         'Close a knowledge base. alias omitted = the active one. The other '
         'open bases keep working.',
         _schema({'alias': _STR}),
         McpServer.db_close),
    # -- инструменты групп ---------------------------------------------------
    Tool('group_create',
         'Create a new configuration group. A group bundles related databases '
         '(main configuration + extensions + data processors) as a single unit. '
         'Use groups to compare different configurations or their versions.',
         _schema({'name': _STR}, ('name',)),
         McpServer.group_create),
    Tool('group_add_db',
         'Add an open database to a group. The database must be opened first '
         'via db_open. A database can belong to multiple groups.',
         _schema({'group': _STR, 'db': _STR}, ('group', 'db')),
         McpServer.group_add_db),
    Tool('group_remove_db',
         'Remove a database from a group. The database itself is NOT closed.',
         _schema({'group': _STR, 'db': _STR}, ('group', 'db')),
         McpServer.group_remove_db),
    Tool('group_list',
         'List all configuration groups with their databases — several groups '
         'can be open at the same time. * marks the ACTIVE group. Pass '
         'group=<name> to a data tool to query ONE group separately, or '
         "group='*' for every group at once; with no group parameter only the "
         'active BASE is queried, not the whole group.',
         _schema({}),
         McpServer.group_list),
    Tool('group_use',
         'Switch the ACTIVE configuration group: its first base becomes the '
         'active base, so tools called without group query that group. To '
         'query a group WITHOUT switching, pass group=<name> to the tool.',
         _schema({'group': _STR}, ('group',)),
         McpServer.group_use),
    Tool('group_close',
         'Delete a configuration group. Databases in the group are NOT closed.',
         _schema({'group': _STR}, ('group',)),
         McpServer.group_close),
]


def make_handler(server):
    """HTTP-обработчик MCP: Streamable HTTP (POST /mcp) и legacy SSE (/sse)."""
    state = {'lock': threading.Lock(), 'sessions': {}}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = '1confdb-knw'

        def _send(self, code, body=None, extra=None):
            data = None if body is None else (
                json.dumps(body, ensure_ascii=False).encode('utf-8'))
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            for key, val in (extra or {}).items():
                self.send_header(key, val)
            self.send_header('Content-Length', str(len(data) if data else 0))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _read_msg(self):
            length = int(self.headers.get('Content-Length') or 0)
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                return None

        def do_OPTIONS(self):
            self._send(204, extra={
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Mcp-Session-Id'})

        def do_GET(self):
            path = urlparse(self.path)
            if path.path in ('/sse', '/mcp'):
                return self._sse_stream()
            return self._send(404, {'error': f'not found: {path.path}'})

        def do_DELETE(self):
            self._send(405, {'error': 'сессии не сохраняются'},
                       extra={'Allow': 'GET, POST'})

        def _sse_stream(self):
            # endpoint-событие нужно legacy SSE-клиентам; streamable-клиенты
            # (POST /mcp) по спецификации игнорируют неизвестные события
            sid = uuid.uuid4().hex
            events = queue.Queue()
            state['sessions'][sid] = events
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(
                    f'event: endpoint\n'
                    f'data: /messages?session_id={sid}\n\n'.encode('utf-8'))
                self.wfile.flush()
                while True:
                    try:
                        msg = events.get(timeout=20)
                    except queue.Empty:
                        self.wfile.write(b': keep-alive\n\n')
                        self.wfile.flush()
                        continue
                    self.wfile.write((
                        f'event: message\n'
                        f'data: {json.dumps(msg, ensure_ascii=False)}\n\n'
                    ).encode('utf-8'))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                state['sessions'].pop(sid, None)

        def do_POST(self):
            path = urlparse(self.path)
            if path.path not in ('/mcp', '/messages'):
                return self._send(404, {'error': f'not found: {path.path}'})
            msg = self._read_msg()
            if msg is None:
                return self._send(400, {'error': 'body must be a JSON-RPC message'})
            with state['lock']:
                resp = server.handle(msg)
            if path.path == '/mcp':
                if resp is None:
                    return self._send(202)
                return self._send(200, resp)
            sid = parse_qs(path.query).get('session_id', [''])[0]
            events = state['sessions'].get(sid)
            if events is None:
                return self._send(404, {'error': 'unknown session_id'})
            if resp is not None:
                events.put(resp)
            return self._send(202, {'status': 'accepted'})

    return Handler


def start_http_server(server, host='127.0.0.1', port=0):
    """Поднимает ThreadingHTTPServer; возвращает (httpd, фактический порт)."""
    httpd = ThreadingHTTPServer((host, port), make_handler(server))
    httpd.daemon_threads = True
    return httpd, httpd.server_address[1]


def _scan_roots():
    """Каталоги автопоиска базы: текущий и корень установки (при запуске из venv)."""
    roots = [os.getcwd()]
    if sys.prefix != getattr(sys, 'base_prefix', sys.prefix):
        # venv: два уровня вверх от python.exe — корень установки с bat-обёртками
        roots.append(os.path.dirname(os.path.dirname(
            os.path.dirname(sys.executable))))
    return list(dict.fromkeys(roots))


def find_db_candidates():
    """Базы .db/.sqlite в типовых местах: корень, db/, _out/ (без рекурсии)."""
    found = []
    for root in _scan_roots():
        for sub in ('', 'db', '_out'):
            directory = os.path.join(root, sub) if sub else root
            if not os.path.isdir(directory):
                continue
            for pattern in ('*.db', '*.sqlite'):
                for path in sorted(glob.glob(os.path.join(directory, pattern))):
                    path = os.path.abspath(path)
                    if os.path.isfile(path) and path not in found:
                        found.append(path)
    return found


def resolve_db(db):
    """Проверяет явный путь либо сам ищет базу; SystemExit(2), если не нашёл."""
    if db:
        if os.path.isfile(db):
            return db
        print(f'Файл базы не найден: {db}', file=sys.stderr)
        print('Укажите существующий путь к базе SQLite.', file=sys.stderr)
        raise SystemExit(2)
    return _find_single_db()


def resolve_dbs(dbs):
    """Список баз к открытию: проверяет явные пути; без путей — автопоиск одной."""
    if not dbs:
        return [_find_single_db()]
    resolved = []
    for path in dbs:
        if not os.path.isfile(path):
            print(f'Файл базы не найден: {path}', file=sys.stderr)
            print('Укажите существующий путь к базе SQLite.', file=sys.stderr)
            raise SystemExit(2)
        resolved.append(path)
    return resolved


def parse_group_spec(spec):
    """'ИМЯ=путь1;путь2' -> ('ИМЯ', ['путь1', 'путь2']).

    Разделитель имени — первый '=', поэтому путь может содержать '=' (в Windows
    это допустимый символ имени файла), а имя группы — нет. ';' разделяет пути
    внутри одного флага; повтор флага с тем же именем добавляет базы в ту же
    группу, так что ';' не обязателен.
    """
    text = str(spec)
    if '=' not in text:
        raise ValueError(
            f'нужен вид ИМЯ=ПУТЬ (несколько путей — через ;): {spec}')
    name, _, rest = text.partition('=')
    name = name.strip()
    if not name:
        raise ValueError(f'пустое имя группы: {spec}')
    paths = [p.strip().strip('"') for p in rest.split(';')]
    paths = [p for p in paths if p]
    if not paths:
        raise ValueError(f'в группе «{name}» нет ни одного пути к базе')
    return name, paths


def collect_groups(specs):
    """[флаги --group] -> [(имя, [пути])]; порядок первого вхождения сохраняется."""
    ordered, index = [], {}
    for spec in specs or ():
        name, paths = parse_group_spec(spec)
        key = name.lower()
        if key not in index:
            index[key] = len(ordered)
            ordered.append((name, []))
        bucket = ordered[index[key]][1]
        for path in paths:
            if path not in bucket:
                bucket.append(path)
    return ordered


def resolve_groups(specs):
    """То же, что collect_groups, но с проверкой файлов (SystemExit 2)."""
    return [(name, resolve_dbs(paths)) for name, paths in collect_groups(specs)]


def _find_single_db():
    """last_db из конфига либо автопоиск единственной базы; SystemExit(2)."""
    last = load_config().get('last_db') or ''
    if last and os.path.isfile(last):
        print(f'База из ~/.confdb/config.json (last_db): {last}', file=sys.stderr)
        return last
    found = find_db_candidates()
    if len(found) == 1:
        print(f'База найдена автоматически: {found[0]}', file=sys.stderr)
        return found[0]
    if found:
        print('Найдено несколько баз — укажите путь явно:', file=sys.stderr)
        for path in found:
            print(f'  {path}', file=sys.stderr)
    else:
        print('База SQLite не найдена. Положите файл .db/.sqlite в текущий '
              'каталог (или в db/, _out/) либо укажите путь явно:',
              file=sys.stderr)
    print('Пример: 1confdb-knw база.sqlite', file=sys.stderr)
    raise SystemExit(2)


def _print_dbs(server):
    """Сообщает открытые базы, алиасы и группы (в stderr — не в поток протокола)."""
    for alias, info in server.dbs.items():
        mark = '*' if alias == server.active else ' '
        groups = server.groups_of(alias)
        tail = f' [в группах: {", ".join(groups)}]' if groups else ''
        print(f' {mark} база {alias}: {info["path"]}{tail}', file=sys.stderr)
    for name, aliases in server.groups.items():
        mark = '*' if name == server.active_group else ' '
        print(f' {mark} группа {name}: {", ".join(aliases) or "(пусто)"}',
              file=sys.stderr)


def serve_http(db_paths, host='127.0.0.1', port=8765, groups=None):
    if isinstance(db_paths, str):
        db_paths = [db_paths]
    group_items = _group_items(groups)
    for name, paths in group_items:
        for path in paths:
            if not os.path.isfile(path):
                print(f'Файл базы не найден: {path} (группа {name})',
                      file=sys.stderr)
                return 2
    for path in db_paths:
        if not os.path.isfile(path):
            print(f'Файл базы не найден: {path}', file=sys.stderr)
            return 2
    server = McpServer(db_paths, groups=group_items)
    _print_dbs(server)
    httpd, real_port = start_http_server(server, host, port)
    print(f'1confdb-knw: слушаю http://{host}:{real_port}/mcp '
          f'(legacy SSE: /sse); остановка — Ctrl+C.')
    if host == '127.0.0.1':
        print('С другой машины — через SSH-туннель: '
              f'ssh -L {real_port}:127.0.0.1:{real_port} user@host')
        print('Конфигурация клиента: '
              f'{{"mcpServers": {{"1confdb-knw": '
              f'{{"url": "http://127.0.0.1:{real_port}/mcp"}}}}}}')
    else:
        print('Внимание: порт открыт для внешних подключений без аутентификации; '
              'база отдаётся read-only.')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('Сервер остановлен.')
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    # stdio-транспорт MCP обязан быть UTF-8; на Windows в пайпе stdout/stdin
    # по умолчанию cp1251 — клиенты (Claude Code и др.) получали кракозябры
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass
    # Список баз и групп при старте печатается в stderr, и клиент читает его из
    # пайпа тем же UTF-8; в живой консоли (запуск из TUI) оставляем кодовую
    # страницу терминала, иначе русский текст станет нечитаемым.
    if not sys.stderr.isatty():
        try:
            sys.stderr.reconfigure(encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(
        prog='1confdb-knw',
        description='MCP-сервер знаний по конфигурации 1С и BSL '
                    '(stdio по умолчанию; --port — HTTP для SSH-туннеля). '
                    'Можно открыть несколько баз сразу (основная конфигурация '
                    '+ расширения/обработки) — остальные через db_open на ходу. '
                    'Несколько групп конфигураций одновременно — повторяющимся '
                    '--group ИМЯ=ПУТЬ: группы остаются раздельными и '
                    'запрашиваются параметром group.')
    parser.add_argument(
        'db', nargs='*', default=None,
        help='пути к базам SQLite (можно несколько); без путей — last_db из '
             '~/.confdb/config.json или автопоиск *.db/*.sqlite '
             '(текущий каталог, db/, _out/)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='адрес для HTTP-режима (по умолчанию 127.0.0.1)')
    parser.add_argument('--port', type=int, default=0,
                        help='порт HTTP-режима (без него — stdio)')
    parser.add_argument('--group', action='append', default=None,
                        metavar='ИМЯ=ПУТЬ',
                        help='группа конфигураций: имя и путь к базе. Повтор '
                             'флага добавляет базу в ту же группу, несколько '
                             'путей можно перечислить через ";". Групп может '
                             'быть несколько — все откроются одновременно '
                             '(активна первая), а инструменты будут запрашивать '
                             'их раздельно параметром group')
    args = parser.parse_args(argv)
    try:
        groups = resolve_groups(args.group)
    except ValueError as err:
        parser.error(str(err))
    # Без --group пустой список баз означает автопоиск; с группами базы уже
    # названы в них, и искать что-то ещё не нужно
    dbs = resolve_dbs(args.db) if (args.db or not groups) else []
    if args.port:
        return serve_http(dbs, args.host, args.port, groups=groups)
    server = McpServer(dbs, groups=groups)
    _print_dbs(server)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = server.handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
            sys.stdout.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
