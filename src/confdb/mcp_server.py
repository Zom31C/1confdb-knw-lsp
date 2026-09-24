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
import base64
import glob
import hashlib
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
from urllib.parse import parse_qs, quote as urllib_quote, urlparse

from . import compare
from . import header_props
from .config import load_config, save_config
from .db.writer import TYPE_RU

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


def ru_text(text):
    """Внутренние слэш-пути 'Catalog/Х' -> 'Справочник.Х' в произвольном тексте.

    Применяется и к сообщениям валидатора запросов: наружу всё отдаётся в форме
    «как в конфигураторе», внутренний формат пути не должен попадать в ответ.
    """
    if not text:
        return text
    return _TYPE_SLASH_RE.sub(lambda m: TYPE_RU[m.group(0)[:-1]] + '.', text)


# страница пагинации (строк) для очень длинных тел методов и оглавлений модулей
_METHOD_PAGE = 250
_OUTLINE_PAGE = 300

# суффиксы в пути модуля, которые модели передают вместо code_name:
# 'Документ.Х.mgr', 'Документ.Х.МодульМенеджера', 'Документ.Х.obj.bsl'
_CODE_ALIASES = {
    'obj': 'obj', 'mgr': 'mgr', 'val': 'val', 'recordset': 'val',
    'seance': 'seance', 'app': 'app', '802': '802', 'con': 'con',
    'модульобъекта': 'obj', 'модульменеджера': 'mgr',
    'модульнаборазаписей': 'val', 'модуль': 'obj',
}


def split_module_path(path, code_name):
    """Нормализует путь модуля к виду (путь объекта, code_name).

    Понимает: 'Документ.Х.mgr', 'Документ.Х.obj.bsl', 'Документ.Х.МодульМенеджера'
    (суффикс переносится в code_name) и полные пути файлов дампа
    'Document/Х/Document.mgr.bsl' (маппятся на объект и code_name).
    """
    path = (path or '').strip()
    code_name = (code_name or 'obj').strip() or 'obj'
    norm = path.replace('\\', '/')
    if norm.endswith('.bsl') and '/' in norm:
        # путь файла дампа: объект и code_name возьмём из таблиц file/module
        return path, code_name, True
    parts = path.split('.')
    if len(parts) >= 2:
        with_ext = parts[-1].lower() == 'bsl' and len(parts) >= 3
        tail = parts[-2].lower() if with_ext else parts[-1].lower()
        if tail in _CODE_ALIASES:
            cut = 2 if with_ext else 1
            return '.'.join(parts[:-cut]), _CODE_ALIASES[tail], False
    return path, code_name, False


def _paginate_lines(text, offset, limit, default_page, header=''):
    """Постраничный вывод текста: строки offset..offset+limit (0-нумерация).

    limit=0 — страница по умолчанию, если текст длиннее неё, иначе весь текст.
    Всегда видно, сколько строк показано и как получить продолжение —
    содержимое не обрезается молча.
    """
    lines = text.split('\n')
    total = len(lines)
    offset = max(0, offset)
    page = limit if limit > 0 else (default_page if total > default_page else total)
    chunk = lines[offset:offset + page]
    shown_end = offset + len(chunk)
    prefix = f'{header}: ' if header else ''
    out = [f'{prefix}строки {offset + 1}-{shown_end} из {total}',
           '\n'.join(chunk)]
    if shown_end < total:
        out.append(f'… продолжение: offset={shown_end}' +
                   (f', limit={page}' if limit > 0 else ''))
    return '\n'.join(out)



def ru_type_str(text):
    """'Ссылка: Catalog/Валюты' -> 'Ссылка: Справочник.Валюты'.

    Голая 'Ссылка' без целевого объекта помечается '(цель не определена)'.
    """
    if not text:
        return text
    text = ru_text(text)
    parts = [p.strip() for p in text.split(' | ')]
    annotated = []
    for part in parts:
        if part == 'Ссылка':
            annotated.append('Ссылка (цель не определена)')
        else:
            annotated.append(part)
    return ' | '.join(annotated)

PRIMER = """1confdb-knw: MCP server over one or several knowledge bases of a 1C:Enterprise 8 configuration — metadata, BSL code and SKD queries, extracted from binary .cf/.cfe/.epf files into SQLite. 1C is a Russian business-automation platform; a configuration contains metadata objects, their fields, modules of 1C-language code (Russian keywords) and SKD report queries. All object/field names are in Russian.

GLOSSARY: Catalog=справочник (directory), Document=документ, InformationRegister/AccumulationRegister=регистры, Enum=перечисление, DataProcessor=обработка, Report=отчет, DefinedType=определяемый тип, CommonAttribute=общий реквизит, CommonModule=общий модуль. Tabular section (табличная часть) = row table of an object (e.g. Документ.ЗаказПокупателя has section Запасы with fields Номенклатура, Цена…).

OBJECT PATHS: tools return and accept configurator-style Russian dotted paths: 'Справочник.Номенклатура', nested 'Справочник.Х.ФормаЭлемента' (legacy 'Catalog/Х/…' slash form is also accepted as input). In the 1C query language the table name for an object is exactly this dotted form: 'Справочник.Имя', 'Документ.Имя', 'РегистрСведений.Имя'…

COMMON MODULES: in BSL code a common module is called by its bare name: 'ИмяМодуля.Функция(...)'. Prefixes like 'ОбщийМодуль.', 'Общий модуль.', 'ОбщМодуль.' are NOT valid code — never write them. The dotted 'Общий модуль.Имя' form only identifies the object in this knowledge base.

DATABASE FILE: the SQLite file is internal to the server. Do NOT search for it, open it, read it from disk, or ask the user for its location — you have no filesystem access to it. Everything is available through the tools below; the sql tool runs arbitrary read-only SELECTs.

MULTIPLE DATABASES: the server can hold several knowledge bases at once — typically the MAIN configuration plus extensions/data processors (.cfe/.epf extracted into their own .db files). Each open base has an alias. All tools query the ACTIVE base; to query a specific base without switching, pass its alias as the db parameter (e.g. find_objects(mask=…, db='расш_интеграция')). Management tools: db_list (what is open, which is active), db_open (open another base file while the server runs — the path comes from the user), db_use (switch the active base), db_close. An extension usually adds/overrides objects of the main configuration — if something is not found in one base, check the other. Special db value '*': run a tool on every open base at once (the answer is sectioned per base) — one call to compare the main configuration with all extensions.

DATABASE IDENTIFIER: every tool response includes a header line identifying the source database: '=== база <алиас> (<путь>) ==='. This lets you compare configurations (e.g. standard vs customized) or understand which base contains a method (main configuration vs extension). Use db='*' to query all bases at once and compare results side-by-side.

COMPARING BASES: compare_object(path, db_left, db_right) diffs ONE object between two open bases in a single call — attributes and their types, tabular sections, register dimensions/resources, forms and commands, modules, methods (signature, directives, body), SKD queries. Use it for standard-vs-customized or release-to-release analysis instead of fetching two passports and diffing them by hand. extension_diff(extension_db, base_db) answers the task-level question 'what does this extension do': new objects (carrying the extension name prefix), borrowed objects, the extension methods and whether one REPLACES a stock method (&Вместо) or inserts code around it (&После/&Перед), the attributes it adds, and its external dependencies. configuration_info says WHICH configuration and release a base holds (name, version, compatibility mode, source file, build date). These three take explicit base aliases (db_left/db_right, extension_db/base_db), not the db parameter, and db='*' does not apply to them.

REGISTERS: object_card of a РегистрСведений/РегистрНакопления lists Измерения (dimensions — they form the record key), Ресурсы (resources — the stored values) and Реквизиты (attributes) as SEPARATE groups, plus Периодичность and Режим записи (независимый / подчинение регистратору). Before writing СрезПоследних or joining a register, check whether the field you rely on is a dimension: only dimensions guarantee one row per key. A periodicity code that could not be decoded is shown as the raw code, never as a guessed name.

DATABASE SCHEMA (for the sql tool; path columns store the legacy slash form 'Catalog/Имя', but string literals in the Russian dotted form ('Справочник.Имя') are auto-converted — either form works in WHERE path = …):
- meta_object(id, path, type, type_ru, name, uuid, comment, parent_id, ord). path like 'Catalog/Номенклатура'; type = English stem (Catalog, Document, InformationRegister, Enum, CommonModule, DefinedType…); type_ru = Russian label as in the configurator.
- meta_attribute(object_id, ord, name, type_str, tabular). Object fields; tabular NULL = header attribute, else the tabular section the field belongs to. type_str examples: 'Строка(50)', 'Число', 'Ссылка: Справочник.Валюты', 'ОпределяемыйТип: … (Ссылка: …)', composites joined with ' | '; 'Ссылка' alone = abstract/any reference.
- meta_tabular(object_id, ord, name) — tabular sections in declaration order.
- module(object_id, code_name, context, body). code_name: 'obj' (object module), 'mgr' (manager module), form/common modules etc.; context = execution context for common modules (Сервер/Клиент/…); body = module text WITHOUT method bodies (signatures, comments, #Если regions) — a table of contents.
- method(id, module_id, ord, kind, name, signature, is_export, directives, description, line_start, line_end, body). Procedures/functions of the 1C code; directives like '&НаСервере'/'&НаКлиенте'; description = comment block above the method.
- attribute_ref(attribute_id, ord, uuid, object_id) — which metadata objects a field's type references (one row per member; NULL object = abstract). Use for joins and impact analysis ('who references X').
- skd_query(object_id, ord, query) — report queries in the 1C query language (Russian keywords ВЫБРАТЬ/ИЗ/ГДЕ/СОЕДИНЕНИЕ/ОБЪЕДИНИТЬ).
- enum_value(object_id, ord, name) — enum values; predefined(object_id, ord, name, code, display) — predefined elements; common_target(common_id, target_id) — objects a common attribute is attached to; subsystem_content — subsystem composition; source, file.

1C QUERY LANGUAGE: Russian keywords, dotted paths, table names 'Справочник.Имя', 'Документ.Имя', 'РегистрСведений.Имя', 'РегистрНакопления.Имя.Обороты' (virtual tables: Остатки, Обороты, СрезПоследних…). Grouping clause is 'СГРУППИРОВАТЬ ПО' — the form 'СГРУППИРОВАНО' does NOT exist in the 1C query language. Example: ВЫБРАТЬ Т.Запасы.Номенклатура.Наименование ИЗ Документ.ЗаказПокупателя КАК Т ГДЕ Т.Сумма > 0.

RECOMMENDED WORKFLOW to write a query or 1C code: 1) configuration_info to know which configuration and release you are in, find_objects to locate objects; 2) object_card for its fields, sections and references; 3) skd_of / find_skd to see how THIS configuration queries the same tables (best examples); 4) find_methods, then find_method_context for a window around the call you need (it also gives stable insertion markers) and get_method for the full body — reuse existing code instead of inventing; 5) check_query to validate your query before use; 6) method_dependencies before porting code to another configuration (it lists everything the code needs there), compare_object / extension_diff to see how two configurations differ; 7) method_result_schema when a stock function returns a temporary table and you need its columns. This server does NOT check 1C code syntax — for that use the 1confdb-knw-lsp variant (BSL Language Server).

All tools are read-only. Prefer the dedicated tools over raw sql; use sql only for what is not covered. ANTI-LOOP: never issue more than two sql calls in a row — if sql did not answer the question, switch to the dedicated tools (find_objects, object_card, find_field, skd_of, refs_of). The schema is EXACTLY as documented above — never waste calls on PRAGMA / sqlite_master / schema guessing."""


class McpServer:
    """Обработчик JSON-RPC сообщений MCP поверх баз SQLite (read-only).

    Держит несколько баз одновременно (например, основная конфигурация
    плюс расширения/обработки): каждая видна под алиасом, инструменты
    работают с активной базой либо с явно указанной параметром db.

    :param bsl: запущенный :class:`BslMcpProxy` (необязательно) — добавляет
        к инструментам базы инструменты ``bsl_*`` (BSL Language Server).
    """

    def __init__(self, db_paths=None, bsl=None):
        self.bsl = bsl
        self.dbs = {}      # алиас -> {'path':…, 'conn':…, 'ctx':…}
        self.active = None
        if isinstance(db_paths, str):
            db_paths = [db_paths]
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
        файла просто возвращает прежний алиас.
        """
        path = os.path.abspath(path)
        known = next((a for a, d in self.dbs.items()
                      if os.path.abspath(d['path']) == path), None)
        if known is not None:
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
        try:
            conn.execute('SELECT COUNT(*) FROM meta_object').fetchone()
        except sqlite3.Error:
            conn.close()
            raise ValueError(
                f'это не база знаний confdb (нет таблицы meta_object): {path}')
        if alias is None:
            alias = self._make_alias(path)
        self.dbs[alias] = {'path': path, 'conn': conn, 'ctx': None}
        if activate or self.active is None:
            self.active = alias
        return alias

    def close_db(self, alias=None):
        """Закрывает базу (по умолчанию активную); возвращает её алиас."""
        alias = self._alias(alias)
        info = self.dbs.pop(alias)
        info['conn'].close()
        if self.active == alias:
            self.active = next(iter(self.dbs), None)
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

    # -- инфраструктура ----------------------------------------------------
    def conn(self, db=None):
        return self.dbs[self._alias(db)]['conn']

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
            instructions = PRIMER
            if self.bsl is not None:
                instructions += (
                    '\n\nBSL CODE ANALYSIS (bsl_* tools): AST-level analysis of '
                    'configuration modules in the dump workspace — '
                    'bsl_analyze_file (diagnostics/metrics), bsl_document_symbols, '
                    'bsl_find_references, bsl_call_hierarchy, bsl_hover, bsl_definition, '
                    'bsl_type_info, bsl_type_at_position, bsl_global_member_info, '
                    'bsl_global_member_search. File paths — relative to the dump root '
                    '(e.g. Catalog/Товары/Товары.obj.bsl). Metadata (types, fields, '
                    'references) comes from the same confdb database.')
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': '1confdb-knw', 'version': '1.0'},
                'instructions': instructions}}
        if msg_id is None or (method or '').startswith('notifications/'):
            return None  # уведомления
        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': {}}
        if method == 'tools/list':
            tools = [t.spec() for t in TOOLS]
            if self.bsl is not None:
                tools.extend(self.bsl.prefixed_tools())
            return {'jsonrpc': '2.0', 'id': msg_id,
                    'result': {'tools': tools}}
        if method == 'tools/call':
            params = msg.get('params', {})
            name = params.get('name')
            args = params.get('arguments', {}) or {}
            if isinstance(name, str) and name.startswith('bsl_'):
                if self.bsl is None:
                    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {
                        'code': -32602,
                        'message': 'инструменты bsl_* недоступны: запустите '
                                   'с --lsp-workspace <каталог дампа>'}}
                try:
                    text = self.bsl.call(name[len('bsl_'):], args)
                    return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                        'content': [{'type': 'text', 'text': text}]}}
                except Exception as err:  # noqa: BLE001 — ошибка инструмента
                    return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                        'content': [{'type': 'text', 'text': f'ошибка: {err}'}],
                        'isError': True}}
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
    def find_objects(self, mask='', type=None, limit=20, db=None):  # noqa: A002
        like = f'%{mask}%'
        # имена в 1С пишутся Слитно, а маски часто приходят с пробелами
        # и в другой раскладке регистра
        like_ns = f'%{mask.replace(" ", "").lower()}%'
        sql = ('SELECT path, type, type_ru, name FROM meta_object '
               'WHERE name LIKE ? OR path LIKE ? '
               "OR lower_ru(REPLACE(name, ' ', '')) LIKE ? "
               "OR lower_ru(REPLACE(path, ' ', '')) LIKE ?")
        params = [like, like, like_ns, like_ns]
        if type:
            sql += ' AND (type = ? OR type_ru = ?)'
            params += [type, type]
        sql += ' ORDER BY length(path), path LIMIT ?'
        params.append(int(limit))
        rows = self.conn(db).execute(sql, params).fetchall()
        if not rows:
            return 'ничего не найдено'
        return '\n'.join(f'{ru_path(p)} — {ru} ({t})' for p, t, ru, _ in rows)

    def object_card(self, path, db=None):
        path = self.resolve_path(path, db)
        q = self.conn(db).execute
        row = q('SELECT type, type_ru, name, comment, header_json '
                'FROM meta_object WHERE path=?', (path,)).fetchone()
        if not row:
            return f'объект не найден: {path}'
        oid = q('SELECT id FROM meta_object WHERE path=?', (path,)).fetchone()[0]
        out = [f'{ru_path(path)} — {row[1]} ({row[0]}), имя {row[2]}' +
               (f'; комментарий: {row[3]}' if row[3] else '')]
        # свойства регистра (периодичность, режим записи)
        out.extend(_register_card_info(row[0], row[4]))
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
                    f'{name}: {ru_type_str(tstr) or "?"}')
            for kind in header_props.REGISTER_KINDS + ('Прочие поля',):
                if kind in groups:
                    out.append(f'{kind}: ' + '; '.join(groups[kind]))
            if not attrs:
                out.append('Реквизиты: нет')
        else:
            out.append('Реквизиты: ' + ('; '.join(
                f'{n}: {ru_type_str(t) or "?"}' for n, t in attrs)
                if attrs else 'нет'))
        tabs = q('SELECT t.name, a.name, a.type_str FROM meta_tabular t '
                 'LEFT JOIN meta_attribute a ON a.object_id=t.object_id '
                 'AND a.tabular=t.name WHERE t.object_id=? '
                 'ORDER BY t.ord, a.ord', (oid,)).fetchall()
        sections = {}
        for sec, fname, ftype in tabs:
            sections.setdefault(sec, []).append(
                f'{fname}: {ru_type_str(ftype) or "?"}')
        for sec, fields in sections.items():
            out.append(f'Табличная часть {sec}: ' + '; '.join(fields))
        mods = q('SELECT code_name, context FROM module WHERE object_id=?',
                 (oid,)).fetchall()
        if mods:
            out.append('Модули: ' + ', '.join(
                c + (f' [{x}]' if x else '') for c, x in mods))
        if row[0] == 'Enum':
            vals = [v[0] for v in q(
                'SELECT name FROM enum_value WHERE object_id=? '
                'ORDER BY ord LIMIT 60', (oid,))]
            cnt = q('SELECT COUNT(*) FROM enum_value WHERE object_id=?',
                    (oid,)).fetchone()[0]
            if vals:
                extra = f' (всего {cnt})' if cnt > len(vals) else ''
                out.append('Значения перечисления' + extra + ': ' +
                           ', '.join(vals))
        elif row[0] == 'Catalog':
            pre = q('SELECT name, code FROM predefined WHERE object_id=? '
                    'ORDER BY ord LIMIT 60', (oid,)).fetchall()
            cnt = q('SELECT COUNT(*) FROM predefined WHERE object_id=?',
                    (oid,)).fetchone()[0]
            if pre:
                extra = f' (всего {cnt})' if cnt > len(pre) else ''
                out.append('Предопределённые элементы' + extra + ': ' +
                           ', '.join(n + (f' [{c}]' if c else '')
                                     for n, c in pre))
        out.extend(self._children_lines(q, oid))
        nskd = q('SELECT COUNT(*) FROM skd_query WHERE object_id=?',
                 (oid,)).fetchone()[0]
        if nskd:
            out.append(f'Запросов СКД: {nskd} (см. skd_of)')
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
        """Паспорт конфигурации/расширения: имя, версия, режим совместимости."""
        alias = self._alias(db)
        props = self.cfg_props(alias)
        if not props:
            return 'корневой объект конфигурации не найден'
        label = {'Configuration': 'Конфигурация',
                 'ConfigurationExtension': 'Расширение конфигурации',
                 'ExternalDataProcessor': 'Внешняя обработка'}.get(
                     props.get('root_type'),
                     props.get('root_type_ru') or props.get('root_type') or '?')
        head = f'{label}: {props.get("name") or "?"}'
        if props.get('synonym') and props['synonym'] != props.get('name'):
            head += f' ({props["synonym"]})'
        out = [head]
        root_ru = props.get('root_type_ru')
        root_type = props.get('root_type')
        # type_ru в meta_object допускает NULL — без запасного варианта
        # получилось бы «Тип корня: None (Configuration)»
        out.append('Тип корня: '
                   + (f'{root_ru} ({root_type})' if root_ru and root_type
                      else (root_ru or root_type or '?')))
        out.append('Версия '
                   + ('расширения'
                      if props.get('root_type') == 'ConfigurationExtension'
                      else 'конфигурации') + ': '
                   + (props.get('version') or 'в файле не указана'))
        if props.get('name_prefix'):
            out.append(f'Префикс имён расширения: {props["name_prefix"]}')
        out.append('Режим совместимости: '
                   + (props.get('compatibility') or 'не определён'))
        out.append('Версия платформы: в файле конфигурации не хранится '
                   '(см. режим совместимости)')
        if props.get('obj_version'):
            out.append(f'Формат метаданных (obj_version): {props["obj_version"]}')
        src = self.conn(db).execute(
            'SELECT file, created, root_uuid FROM source '
            'ORDER BY id LIMIT 1').fetchone()
        if src:
            if src[0]:
                out.append(f'Источник выгрузки: {src[0]}')
            if src[1]:
                out.append(f'База знаний собрана: {src[1]}')
            if src[2]:
                out.append(f'UUID корня: {src[2]}')
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

    def find_field(self, name, limit=20, db=None):
        like = f'%{name}%'
        like_ns = f'%{name.replace(" ", "").lower()}%'
        rows = self.conn(db).execute(
            'SELECT o.path, a.name, a.tabular, a.type_str FROM meta_attribute a '
            'JOIN meta_object o ON o.id=a.object_id '
            "WHERE a.name LIKE ? OR lower_ru(REPLACE(a.name, ' ', '')) LIKE ? "
            'ORDER BY o.path LIMIT ?',
            (like, like_ns, int(limit))).fetchall()
        if not rows:
            return 'ничего не найдено'
        return '\n'.join(
            f'{ru_path(p)} :: поле {n} ({ru_type_str(t) or "?"})' +
            (f' [табчасть {s}]' if s else '')
            for p, n, s, t in rows)

    def refs_of(self, path, direction='both', limit=30, db=None):
        path = self.resolve_path(path, db)
        q = self.conn(db).execute
        out = []
        if direction in ('both', 'forward'):
            rows = q(
                'SELECT DISTINCT t.path FROM attribute_ref r '
                'JOIN meta_attribute a ON a.id=r.attribute_id '
                'JOIN meta_object v ON v.id=a.object_id '
                'JOIN meta_object t ON t.id=r.object_id '
                'WHERE v.path=? AND t.path IS NOT NULL LIMIT ?',
                (path, int(limit))).fetchall()
            out.append('Ссылается на: ' + (', '.join(ru_path(r[0]) for r in rows)
                       if rows else '—'))
        if direction in ('both', 'reverse'):
            rows = q(
                'SELECT DISTINCT v.path FROM attribute_ref r '
                'JOIN meta_attribute a ON a.id=r.attribute_id '
                'JOIN meta_object v ON v.id=a.object_id '
                'JOIN meta_object t ON t.id=r.object_id '
                'WHERE t.path=? LIMIT ?', (path, int(limit))).fetchall()
            out.append('На него ссылаются: ' + (', '.join(ru_path(r[0]) for r in rows)
                       if rows else '—'))
        return '\n'.join(out)

    def _module_target(self, path, code_name, db=None):
        """(путь объекта, code_name) из разных форм записи пути модуля:
        'Объект.mgr', 'Объект.obj.bsl', 'Объект.МодульМенеджера' или путь
        файла дампа 'Document/Х/Document.mgr.bsl'."""
        p, cn, is_file = split_module_path(path, code_name)
        if is_file:
            row = self.conn(db).execute(
                "SELECT o.path, m.code_name FROM file f "
                "JOIN meta_object o ON o.id=f.object_id "
                "JOIN module m ON m.object_id=f.object_id "
                "WHERE f.kind='bsl' AND f.path=? "
                "AND f.path LIKE '%.' || m.code_name || '.bsl'",
                (p.replace('\\', '/'),)).fetchone()
            if row:
                return row[0], row[1]
            return p, cn
        return self.resolve_path(p, db), cn

    def module_outline(self, path, code_name='obj', offset=0, limit=0, db=None):
        path, code_name = self._module_target(path, code_name, db)
        row = self.conn(db).execute(
            'SELECT m.body FROM module m JOIN meta_object o ON o.id=m.object_id '
            'WHERE o.path=? AND m.code_name=?', (path, code_name)).fetchone()
        if not row or not row[0]:
            return f'модуль не найден: {path} ({code_name})'
        return _paginate_lines(row[0], int(offset or 0), int(limit or 0),
                               _OUTLINE_PAGE)

    def _method_row(self, path, code_name, name, db=None):
        """Строка метода: kind, name, signature, directives, description, body,
        is_export, контекст модуля, line_start — либо None."""
        path, code_name = self._module_target(path, code_name, db)
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

    def get_method(self, path, code_name, name, offset=0, limit=0, db=None):
        row = self._method_row(path, code_name, name, db)
        if not row:
            path, code_name = self._module_target(path, code_name, db)
            return f'метод не найден: {path} ({code_name}) :: {name}'
        head = f'{row[0]} {row[1]}({row[2]})' + (' Экспорт' if row[6] else '')
        parts = [head]
        if row[3]:
            parts.append('директивы: ' + row[3])
        if row[4]:
            parts.append('описание:\n' + row[4])
        body = row[5] or ''
        total = body.count('\n') + 1
        parts.append(_paginate_lines(body, int(offset or 0), int(limit or 0),
                                     _METHOD_PAGE, header='тело'))
        if total > _METHOD_PAGE and int(offset or 0) + _METHOD_PAGE < total:
            parts.append('метод длинный — листай: offset/limit')
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
            shown = ru_path(target) if target else 'НЕ НАЙДЕНО в этой базе'
            out.append(f'  строка {start + line - 1}: {manager}.{obj} -> {shown}')

        out.append('\nЗапросы в коде:')
        if not report['queries']:
            out.append('  текстов запросов не найдено')
        for line, query, complete, errors, unverified in report['queries']:
            head = f'  строка {start + line - 1}'
            if not complete:
                out.append(f'{head}: запрос собирается по частям — '
                           'проверка парсером пропущена')
                continue
            out.append(f'{head}: ошибок {len(errors)}, '
                       f'непроверенных полей {len(unverified)}')
            for err in errors[:6]:
                out.append(f'    ! {ru_text(err)}')
            for ref in unverified[:6]:
                out.append(f'    ? {ref} (схема параметра-таблицы неизвестна)')
        if report['tables']:
            out.append('  таблицы запросов: ' + ', '.join(report['tables'][:20]))
        if report['fields']:
            out.append('  поля запросов: ' + ', '.join(report['fields'][:30]))

        if report['context_warnings']:
            out.append('\nКонтекст клиент/сервер:')
            out.extend(f'  ! {w}' for w in report['context_warnings'])
        if report['unknown']:
            out.append('\nНе разрешено (не общий модуль, не менеджер '
                       'метаданных и не локальная переменная — вероятно, '
                       'реквизит формы/объекта или глобальный контекст):')
            out.extend(f'  строка {start + line - 1}: {left}.{right}'
                       for left, right, line in report['unknown'][:20])
        if report['plain_calls']:
            out.append('\nВызовы без точки (методы этого модуля или глобальные '
                       'методы платформы — не проверялись): '
                       + ', '.join(report['plain_calls'][:25]))
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

    def find_methods(self, mask='', path=None, limit=20, db=None):
        path = self.resolve_path(path, db) if path else None
        like = f'%{mask}%'
        like_ns = f'%{mask.replace(" ", "").lower()}%'
        sql = ('SELECT o.path, m.code_name, mt.kind, mt.name, mt.signature, '
               'mt.directives, mt.description FROM method mt '
               'JOIN module m ON m.id=mt.module_id '
               'JOIN meta_object o ON o.id=m.object_id '
               'WHERE mt.name LIKE ? OR mt.signature LIKE ? '
               'OR mt.description LIKE ? '
               "OR lower_ru(REPLACE(mt.name, ' ', '')) LIKE ?")
        params = [like, like, like, like_ns]
        if path:
            sql += ' AND o.path=?'
            params.append(path)
        sql += ' LIMIT ?'
        params.append(int(limit))
        rows = self.conn(db).execute(sql, params).fetchall()
        if not rows:
            return 'ничего не найдено'
        out = []
        for p, code, kind, name, sig, dirs, desc in rows:
            line = f'{ru_path(p)} ({code}) — {kind} {name}({sig})'
            if dirs:
                line += f' [{dirs}]'
            if desc:
                line += ' | ' + desc.splitlines()[0][:80]
            out.append(line)
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

    def find_skd(self, mask, limit=10, db=None):
        rows = self.conn(db).execute(
            'SELECT q.id, o.path, q.query FROM skd_query q '
            'JOIN meta_object o ON o.id=q.object_id '
            'WHERE q.query LIKE ? LIMIT ?',
            (f'%{mask}%', int(limit))).fetchall()
        if not rows:
            return 'ничего не найдено'
        out = []
        for rid, path, text in rows:
            pos = text.lower().find(mask.lower())
            snippet = text[max(0, pos - 120):pos + 240].replace('\n', ' ')
            out.append(f'[{rid}] {ru_path(path)} … {snippet} …')
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

    def db_schema(self, db=None):
        conn = self.conn(db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = []
        for name in tables:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
            cnt = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            out.append(f'{name} ({cnt} строк): ' + ', '.join(cols))
        return ('Таблицы базы знаний (используй в sql; повторно схему не '
                'запрашивай):\n' + '\n'.join(out))

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
            info['cfg'] = props
        return info['cfg']

    def cfg_summary(self, alias):
        """(имя конфигурации, строка версий) базы — коротко для db_list."""
        props = self.cfg_props(alias)
        bits = [props.get('version') or 'версия не указана']
        if props.get('compatibility'):
            bits.append(f'режим совместимости {props["compatibility"]}')
        if props.get('name_prefix'):
            bits.append(f'префикс имён {props["name_prefix"]}')
        return props.get('name') or '?', '; '.join(bits)

    def db_list(self):
        if not self.dbs:
            return 'нет открытых баз — откройте через db_open'
        out = []
        for alias, info in self.dbs.items():
            nobj, nmod, nmeth = self.db_stats(alias)
            mark = '*' if alias == self.active else ' '
            out.append(f'{mark} {alias} — {info["path"]} '
                       f'(объектов: {nobj}, модулей: {nmod}, методов: {nmeth})')
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
        if args.get('db') == '*' and 'db' in self.schema.get('properties', {}):
            # db='*' — выполнить инструмент по всем открытым базам сразу
            parts = []
            for alias, info in server.dbs.items():
                part = call_with_retry(self.fn, server, **dict(args, db=alias))
                parts.append(f'=== база {alias} ({info["path"]}) ===\n{part}')
            if not parts:
                raise ValueError('нет открытых баз — укажите путь в db_open')
            return '\n\n'.join(parts)

        # Обычный запрос к одной базе
        result = call_with_retry(self.fn, server, **args)

        # Если у инструмента есть параметр db — добавляем заголовок с идентификатором базы
        if 'db' in self.schema.get('properties', {}):
            alias = server._alias(args.get('db'))  # разрешает None → активная база
            info = server.dbs[alias]
            return f'=== база {alias} ({info["path"]}) ===\n{result}'

        return result


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required)}


_STR = {'type': 'string'}
_INT = {'type': 'integer'}
_DB = {'type': 'string',
       'description': 'Alias of the knowledge base to query INSTEAD of the '
                      'active one (see db_list). Omit to use the active base. '
                      "Special value '*': run the tool on EVERY open base at "
                      'once; the answer comes back sectioned per base.'}

TOOLS = [
    Tool('find_objects',
         'Search metadata objects by name or path substring. Returns '
         "configurator-style dotted paths ('Справочник.Имя') with Russian and "
         'English type labels. First step for anything: locate '
         'справочник/документ/регистр by its Russian name. mask is optional: '
         'omit it to browse all objects of a given type.',
         _schema({'mask': _STR, 'type': _STR,
                  'limit': _INT, 'db': _DB}),
         McpServer.find_objects),
    Tool('object_card',
         "Full 'passport' of one object in a single call: type, header "
         'attributes with types, tabular sections with their fields, modules, '
         'SKD query count, forward/reverse references. Use right after '
         'find_objects.',
         _schema({'path': _STR, 'db': _DB}, ('path',)),
         McpServer.object_card),
    Tool('object_tree',
         "Browse the metadata tree 'as in the configurator' (subsystems, "
         'nested forms/commands). path empty = configuration root.',
         _schema({'path': _STR, 'depth': _INT, 'db': _DB}),
         McpServer.object_tree),
    Tool('find_field',
         'Reverse search: which objects contain a field/tabular-section field '
         'with this name. Use to discover join paths between tables.',
         _schema({'name': _STR, 'limit': _INT, 'db': _DB}, ('name',)),
         McpServer.find_field),
    Tool('refs_of',
         "Reference links of an object via attribute types: forward ('on what "
         "it references') and reverse ('who references it') — impact analysis.",
         _schema({'path': _STR, 'direction': _STR, 'limit': _INT, 'db': _DB},
                 ('path',)),
         McpServer.refs_of),
    Tool('module_outline',
         'Table of contents of a 1C module: signatures, comments, #Если '
         "regions, WITHOUT method bodies. path — object path ('Документ.Х' "
         "or 'Document/Х'); code_name: 'obj' (object module), 'mgr' (manager "
         'module) etc. Path forms are also accepted: Документ.Х.mgr, '
         'Документ.Х.obj.bsl, Document/Х/Document.mgr.bsl. '
         'For very long modules use offset/limit (0-based lines) to page.',
         _schema({'path': _STR, 'code_name': _STR,
                  'offset': _INT, 'limit': _INT, 'db': _DB}, ('path',)),
         McpServer.module_outline),
    Tool('get_method',
         'Full source of one procedure/function: signature, directives '
         '(&НаСервере…), description comment and body. Use after '
         'find_methods/module_outline. Path forms are also accepted: '
         'Документ.Х.mgr, Документ.Х.obj.bsl, Document/Х/Document.mgr.bsl. '
         'Long methods are paginated: the '
         'response shows which lines are given and how to fetch the rest '
         '(offset/limit, 0-based lines) — nothing is silently truncated.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR,
                  'offset': _INT, 'limit': _INT, 'db': _DB},
                 ('path', 'code_name', 'name')),
         McpServer.get_method),
    Tool('find_method_context',
         'A WINDOW into a method body instead of the whole body: lines around '
         'each occurrence of match, with the module line numbers, plus stable '
         'insertion markers (the previous and the next statement). Cheaper '
         'than get_method on a big method and the right way to pick a place '
         'to insert code. before/after = how many lines to show (default 20).',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR,
                  'match': _STR, 'before': _INT, 'after': _INT, 'db': _DB},
                 ('path', 'code_name', 'name')),
         McpServer.find_method_context),
    Tool('method_dependencies',
         'Static analysis of ONE method: its parameters, the common modules it '
         'calls (and whether those methods exist and are Экспорт), the '
         'metadata it touches (Справочники.Х, Документы.Х…), the tables and '
         'fields of the queries inside it (each query is validated), and what '
         'could not be resolved. Use it before porting a customization to '
         'another configuration — it lists everything the code needs there.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR, 'db': _DB},
                 ('path', 'code_name', 'name')),
         McpServer.method_dependencies),
    Tool('method_result_schema',
         'Best-effort shape of the value a method RETURNS: columns added with '
         'Колонки.Добавить, keys of Новая Структура, and the result columns of '
         'the queries in the body (aliases / field names). Use it when a stock '
         'function returns a temporary table and you need to know its columns '
         'without guessing. HEURISTIC: names built at runtime are reported as '
         'dynamic, and the answer says what it could not see.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR, 'db': _DB},
                 ('path', 'code_name', 'name')),
         McpServer.method_result_schema),
    Tool('find_methods',
         'Search 1C methods by name/signature/description substring '
         "(e.g. 'ПриПроведении'). Reuse existing code instead of inventing. "
         'mask is optional: with only path it lists all methods of the object. '
         'Not sure about the exact name — give a partial mask (piece of the '
         'name), do not guess the full name.',
         _schema({'mask': _STR, 'path': _STR, 'limit': _INT, 'db': _DB}),
         McpServer.find_methods),
    Tool('skd_of',
         'All SKD (report) queries of an object — the best examples of how '
         'THIS configuration queries its own tables.',
         _schema({'path': _STR, 'db': _DB}, ('path',)),
         McpServer.skd_of),
    Tool('find_skd',
         'Search across all SKD query texts (e.g. a table name like '
         "'РегистрНакопления.Запасы'). Returns snippets around the match.",
         _schema({'mask': _STR, 'limit': _INT, 'db': _DB}, ('mask',)),
         McpServer.find_skd),
    Tool('check_query',
         'Validate a 1C query: syntax (Russian keywords) + existence of '
         'tables/fields/reference chains against this configuration. ALWAYS '
         'run it on a query you wrote before using it.',
         _schema({'text': _STR, 'db': _DB}, ('text',)),
         McpServer.check_query),
    Tool('schema',
         'Knowledge base schema reference: all tables, their columns and row '
         'counts. Call it ONCE before writing sql — do not guess column names, '
         'do not query sqlite_master/PRAGMA.',
         _schema({'db': _DB}),
         McpServer.db_schema),
    Tool('sql',
         'Read-only SELECT escape hatch for anything not covered by the '
         'dedicated tools. Call `schema` first if unsure about tables/columns. '
         'Non-SELECT is rejected; LIMIT 200 enforced.',
         _schema({'query': _STR, 'db': _DB}, ('query',)),
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
         'Passport of the knowledge base itself: configuration/extension name '
         'and synonym, its VERSION, compatibility mode (режим совместимости), '
         'extension name prefix, source .cf/.cfe/.epf file and the date the '
         'base was built, object/module/method counts. Call it first when you '
         'need to know WHICH configuration and which release you are looking '
         'at (e.g. before porting code between configurations).',
         _schema({'db': _DB}),
         McpServer.configuration_info),
    Tool('db_list',
         'List the knowledge bases open on this server: alias, file path, '
         'object/module/method counts; * marks the ACTIVE base that the other '
         'tools query by default.',
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
]


def make_handler(server):
    """HTTP-обработчик MCP: Streamable HTTP (POST /mcp), legacy SSE (/sse)
    и минимальный OAuth 2.1 для клиентов, которым он нужен (Claude Code).

    OAuth реализован по спецификации MCP (RFC 9728/8414/7591, authorization
    code + PKCE) с автоматическим одобрением: сервер локальный, база
    отдаётся read-only, реальная авторизация не требуется — поток нужен
    только чтобы клиенты, ожидающие OAuth, могли подключиться.
    """
    state = {'lock': threading.Lock(), 'sessions': {},
             'oauth_clients': {}, 'oauth_codes': {}, 'oauth_tokens': set()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = '1confdb-knw'

        def handle(self):
            # MCP-клиенты часто закрывают соединение сразу после ответа
            # (новый коннект на каждый запрос) — обрыв на keep-alive не ошибка
            try:
                super().handle()
            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError, TimeoutError):
                self.close_connection = True

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

        def _base_url(self):
            return 'http://' + (self.headers.get('Host') or 'localhost')

        def _read_body_bytes(self):
            if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
                return self._read_chunked()
            length = int(self.headers.get('Content-Length') or 0)
            return self.rfile.read(length) if length > 0 else b''

        def _drain_body(self):
            """Дочитать тело запроса, чтобы keep-alive соединение не съехало."""
            self._read_body_bytes()

        def _read_msg(self):
            if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
                body = self._read_chunked()
            else:
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length)
            try:
                return json.loads(body)
            except ValueError:
                return None

        def _read_chunked(self):
            """Тело запроса в chunked-кодировке (так шлют Node-клиенты,
            например Claude Code)."""
            parts = []
            while True:
                size_line = self.rfile.readline(65536)
                if not size_line:
                    break
                size_token = size_line.strip().split(b';')[0]
                if not size_token:
                    continue
                try:
                    size = int(size_token, 16)
                except ValueError:
                    break
                if size == 0:
                    # финальный блок: дочитать трейлеры до пустой строки
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b'\r\n', b'\n', b''):
                            break
                    break
                parts.append(self.rfile.read(size))
                self.rfile.readline(65536)  # CRLF после чанка
            return b''.join(parts)

        # -- OAuth 2.1 (авто-одобрение) -------------------------------------

        def _oauth_resource_metadata(self):
            base = self._base_url()
            return self._send(200, {
                'resource': base + '/mcp',
                'authorization_servers': [base]})

        def _oauth_server_metadata(self):
            base = self._base_url()
            return self._send(200, {
                'issuer': base,
                'authorization_endpoint': base + '/oauth/authorize',
                'token_endpoint': base + '/oauth/token',
                'registration_endpoint': base + '/oauth/register',
                'response_types_supported': ['code'],
                'grant_types_supported': ['authorization_code',
                                          'refresh_token'],
                'code_challenge_methods_supported': ['S256'],
                'token_endpoint_auth_methods_supported': ['none']})

        def _oauth_register(self):
            try:
                meta = json.loads(self._read_body_bytes() or b'{}')
            except ValueError:
                meta = {}
            client_id = uuid.uuid4().hex
            with state['lock']:
                state['oauth_clients'][client_id] = meta
            resp = {'client_id': client_id,
                    'token_endpoint_auth_method': 'none'}
            for key in ('client_name', 'redirect_uris', 'grant_types',
                        'response_types'):
                if meta.get(key) is not None:
                    resp[key] = meta[key]
            return self._send(201, resp)

        def _oauth_authorize(self, qs):
            params = parse_qs(qs)
            redirect_uri = params.get('redirect_uri', [''])[0]
            if not redirect_uri:
                self._drain_body()
                return self._send(400, {'error': 'redirect_uri required'})
            code = uuid.uuid4().hex
            with state['lock']:
                state['oauth_codes'][code] = {
                    'client_id': params.get('client_id', [''])[0],
                    'redirect_uri': redirect_uri,
                    'challenge': params.get('code_challenge', [''])[0]}
            location = redirect_uri + ('&' if '?' in redirect_uri else '?') \
                + 'code=' + code
            if params.get('state'):
                location += '&state=' + urllib_quote(params['state'][0])
            self.send_response(302)
            self.send_header('Location', location)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def _oauth_token(self):
            params = parse_qs(self._read_body_bytes().decode('utf-8', 'replace'))
            grant = params.get('grant_type', [''])[0]
            if grant == 'refresh_token':
                token = uuid.uuid4().hex
                with state['lock']:
                    state['oauth_tokens'].add(token)
                return self._send(200, {'access_token': token,
                                        'token_type': 'Bearer',
                                        'expires_in': 3600,
                                        'refresh_token': uuid.uuid4().hex})
            code = params.get('code', [''])[0]
            with state['lock']:
                stored = state['oauth_codes'].pop(code, None)
            if stored is None:
                return self._send(400, {'error': 'invalid_grant'})
            challenge = stored.get('challenge') or ''
            if challenge:
                verifier = params.get('code_verifier', [''])[0]
                digest = hashlib.sha256(verifier.encode('ascii', 'ignore')).digest()
                expect = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
                if expect != challenge:
                    return self._send(400, {'error': 'invalid_grant'})
            token = uuid.uuid4().hex
            with state['lock']:
                state['oauth_tokens'].add(token)
            return self._send(200, {'access_token': token,
                                    'token_type': 'Bearer',
                                    'expires_in': 3600,
                                    'refresh_token': uuid.uuid4().hex})

        # -- маршрутизация ----------------------------------------------------

        def do_OPTIONS(self):
            self._send(204, extra={
                'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
                'Access-Control-Allow-Headers':
                    'Content-Type, Mcp-Session-Id, Mcp-Protocol-Version, '
                    'Authorization'})

        def do_GET(self):
            path = urlparse(self.path)
            if path.path in ('/sse', '/mcp'):
                return self._sse_stream()
            if path.path in ('/.well-known/oauth-protected-resource',
                             '/.well-known/oauth-protected-resource/mcp'):
                return self._oauth_resource_metadata()
            if path.path in ('/.well-known/oauth-authorization-server',
                             '/.well-known/oauth-authorization-server/mcp'):
                return self._oauth_server_metadata()
            if path.path == '/oauth/authorize':
                return self._oauth_authorize(path.query)
            self._drain_body()
            return self._send(404, {'error': f'not found: {path.path}'})

        def do_DELETE(self):
            self._drain_body()
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
            if path.path in ('/oauth/register', '/register'):
                return self._oauth_register()
            if path.path == '/oauth/token':
                return self._oauth_token()
            if path.path not in ('/mcp', '/messages'):
                self._drain_body()
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


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Не печатает traceback на штатные обрывы связи со стороны клиента."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def start_http_server(server, host='127.0.0.1', port=0):
    """Поднимает ThreadingHTTPServer; возвращает (httpd, фактический порт)."""
    httpd = _QuietThreadingHTTPServer((host, port), make_handler(server))
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
    """Сообщает открытые базы и алиасы (в stderr — не в поток протокола)."""
    for alias, info in server.dbs.items():
        mark = '*' if alias == server.active else ' '
        print(f' {mark} база {alias}: {info["path"]}', file=sys.stderr)


def create_bsl_proxy(args, db):
    """Создаёт и запускает шлюз BSL Language Server.

    Возвращает запущенный :class:`BslMcpProxy` или None (нет workspace/jar,
    ошибка запуска) — сервер при этом продолжает работать без инструментов
    ``bsl_*``. Удачный workspace запоминается в ~/.confdb/config.json
    (ключ lsp_workspace) для следующих запусков без опции.
    """
    workspace = getattr(args, 'lsp_workspace', None) or load_config().get('lsp_workspace')
    if not workspace:
        return None
    if not os.path.isdir(workspace):
        print(f'1confdb-knw: каталог LSP-workspace не найден: {workspace} — '
              'инструменты bsl_* недоступны', file=sys.stderr)
        return None
    from .mcp_bsl_proxy import BslMcpProxy, find_jar
    jar = find_jar(getattr(args, 'bsl_jar', None))
    if not jar:
        print('1confdb-knw: jar BSL Language Server не найден — соберите его '
              '(build-lsp-jar.bat) или задайте CONFDB_BSL_JAR; '
              'инструменты bsl_* недоступны', file=sys.stderr)
        return None
    proxy = BslMcpProxy(jar, workspace, db, java=getattr(args, 'java', None))
    print(f'1confdb-knw: запускаю BSL Language Server '
          f'(workspace: {os.path.abspath(workspace)})...', file=sys.stderr)
    try:
        proxy.start()
    except Exception as err:  # noqa: BLE001 — шлюз не обязателен для работы
        print(f'1confdb-knw: BSL LS не запустился: {err} — '
              'инструменты bsl_* недоступны', file=sys.stderr)
        proxy.stop()
        return None
    print(f'1confdb-knw: BSL LS готов, инструментов: {len(proxy.tools)}',
          file=sys.stderr)
    config = load_config()
    if config.get('lsp_workspace') != os.path.abspath(workspace):
        config['lsp_workspace'] = os.path.abspath(workspace)
        save_config(config)
    return proxy


def serve_http(db_paths, host='127.0.0.1', port=8765, bsl=None):
    if isinstance(db_paths, str):
        db_paths = [db_paths]
    for path in db_paths:
        if not os.path.isfile(path):
            print(f'Файл базы не найден: {path}', file=sys.stderr)
            return 2
    server = McpServer(db_paths, bsl=bsl)
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
    parser = argparse.ArgumentParser(
        prog='1confdb-knw',
        description='MCP-сервер знаний по конфигурации 1С и BSL '
                    '(stdio по умолчанию; --port — HTTP для SSH-туннеля). '
                    'Можно открыть несколько баз сразу (основная конфигурация '
                    '+ расширения/обработки) — остальные через db_open на ходу.')
    parser.add_argument(
        'db', nargs='*', default=None,
        help='пути к базам SQLite (можно несколько); без путей — last_db из '
             '~/.confdb/config.json или автопоиск *.db/*.sqlite '
             '(текущий каталог, db/, _out/)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='адрес для HTTP-режима (по умолчанию 127.0.0.1)')
    parser.add_argument('--port', type=int, default=0,
                        help='порт HTTP-режима (без него — stdio)')
    parser.add_argument('--lsp-workspace', metavar='DIR', default=None,
                        help='каталог дампа для инструментов BSL Language Server '
                             '(bsl_*); запоминается в конфиге, при следующих '
                             'запусках можно не указывать')
    parser.add_argument('--bsl-jar', metavar='JAR', default=None,
                        help='путь к jar BSL Language Server '
                             '(по умолчанию ищется в bin/ дистрибутива)')
    parser.add_argument('--java', metavar='EXE', default=None,
                        help='путь к java (по умолчанию JAVA_HOME или PATH)')
    args = parser.parse_args(argv)
    dbs = resolve_dbs(args.db)
    bsl = create_bsl_proxy(args, dbs[0])
    if args.port:
        try:
            return serve_http(dbs, args.host, args.port, bsl=bsl)
        finally:
            if bsl is not None:
                bsl.stop()
    server = McpServer(dbs, bsl=bsl)
    _print_dbs(server)
    try:
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
    finally:
        if bsl is not None:
            bsl.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
