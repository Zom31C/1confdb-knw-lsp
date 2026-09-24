"""Статический анализ кода 1С (BSL) по тексту и базе знаний конфигурации.

Основа инструментов method_dependencies и method_result_schema MCP-сервера:
понять, что использует фрагмент кода, не запуская платформу.

Анализ лексический, а не полноценный парсер BSL: текст разбирается на строковые
литералы и комментарии, точечные вызовы, обращения к менеджерам метаданных и
тексты запросов (которые проверяются уже настоящим парсером query_lang). Всё,
что достоверно проверить нельзя, попадает в список «не разрешено», а не
выдаётся за ошибку.

Синтаксис текста модулей здесь НЕ проверяется и проверяться не должен: его
разбирает BSL Language Server, который поднимает вариант 1confdb-knw-lsp.
"""
import bisect
import re

from .query_lang import QueryError, check_query_full, parse_query

# Менеджеры метаданных в BSL пишутся во множественном числе:
# 'Справочники.Номенклатура'. Значение — англ. stem типа объекта (TYPE_RU).
BSL_MANAGERS = {
    'справочники': 'Catalog',
    'документы': 'Document',
    'перечисления': 'Enum',
    'регистрысведений': 'InformationRegister',
    'регистрынакопления': 'AccumulationRegister',
    'регистрыбухгалтерии': 'AccountingRegister',
    'регистрырасчета': 'CalculationRegister',
    'планысчетов': 'ChartOfAccounts',
    'планывидовхарактеристик': 'ChartOfCharacteristicTypes',
    'планывидоврасчета': 'ChartOfCalculationTypes',
    'планыобмена': 'ExchangePlan',
    'обработки': 'DataProcessor',
    'отчеты': 'Report',
    'бизнеспроцессы': 'BusinessProcess',
    'задачи': 'Task',
    'константы': 'Constant',
    'критерииотбора': 'FilterCriterion',
    'хранилищанастроек': 'SettingsStorage',
    'журналыдокументов': 'DocumentJournal',
    'внешниеисточникиданных': 'ExternalDataSource',
}

ID = r'[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*'

# Точечный вызов: 'Модуль.Метод('. Смотрим назад, чтобы не принимать хвост
# цепочки 'Таблица.Колонки.Добавить(' за обращение к общему модулю 'Колонки'.
RE_DOTTED_CALL = re.compile(rf'(?<![\w.])({ID})\s*\.\s*({ID})\s*\(')
# Обращение к менеджеру метаданных: 'Справочники.Номенклатура'
RE_MANAGER = re.compile(rf'\b({"|".join(BSL_MANAGERS)})\s*\.\s*({ID})',
                        re.IGNORECASE)
# Одиночный вызов (локальный метод модуля или глобальный метод платформы)
RE_PLAIN_CALL = re.compile(rf'(?<![\w.])(?<!\.)\b({ID})\s*\(')
# Объявление метода: его имя — не вызов
RE_DECL = re.compile(rf'\b(?:Процедура|Функция)\s+({ID})\s*\(', re.IGNORECASE)
# Параметры объявленных в тексте методов
RE_DECL_PARAMS = re.compile(
    rf'\b(?:Процедура|Функция)\s+{ID}\s*\(([^()]*)\)', re.IGNORECASE)
# ключевое слово 'Знач' перед именем параметра (обязательно с разделителем,
# иначе пострадают имена вроде 'Значение')
RE_PARAM_ZNACH = re.compile(r'^знач\s+', re.IGNORECASE)

# Объекты глобального контекста платформы: общие модули с такими именами
# не встречаются, а обращение к ним — не ошибка
PLATFORM_GLOBALS = frozenset({
    'метаданные', 'параметрысеанса', 'библиотекакартинок', 'webцвета',
    'символы', 'библиотекастилей', 'webшрифты', 'глобальныепеременные',
})
RE_ASSIGN = re.compile(rf'(?:^|[;\s(,])({ID})\s*=(?!=)')
RE_FOR_EACH = re.compile(rf'\bДля\s+Каждого\s+({ID})', re.IGNORECASE)
RE_FOR_TO = re.compile(rf'\bДля\s+({ID})\s*=', re.IGNORECASE)
RE_VAR = re.compile(rf'^\s*Перем\s+({ID}(?:\s*,\s*{ID})*)', re.IGNORECASE | re.MULTILINE)
RE_QUERY_HEAD = re.compile(r'^\s*(?:ВЫБРАТЬ|ВЫРАЗИТЬ|SELECT)\b', re.IGNORECASE)
RE_HAS_SOURCE = re.compile(r'\b(?:ИЗ|FROM)\b', re.IGNORECASE)
# '+' сразу за литералом — запрос собран конкатенацией с переменной, текст неполный
RE_CONCAT_TAIL = re.compile(r'\s*\+')
RE_COLUMN_ADD = re.compile(rf'\.\s*Колонки\s*\.\s*Добавить\s*\(\s*"([^"]+)"',
                           re.IGNORECASE)
RE_COLUMN_ADD_VAR = re.compile(r'\.\s*Колонки\s*\.\s*Добавить\s*\(\s*([^,)\s"]+)',
                               re.IGNORECASE)
RE_STRUCTURE = re.compile(r'Нов(?:ый|ая)\s+Структура\s*\(\s*"([^"]+)"',
                          re.IGNORECASE)
RE_NEW_TABLE = re.compile(r'Нов(?:ый|ая)\s+(?:Фиксированная)?ТаблицаЗначений',
                          re.IGNORECASE)
RE_QUERY_RESULT = re.compile(r'Выполнить\s*\(\s*\)\s*\.\s*Выгрузить', re.IGNORECASE)

# Слова языка, которые регулярное выражение вызова принимает за имя метода.
# Синтаксис текста модулей здесь НЕ проверяется: его разбирает BSL Language
# Server (вариант 1confdb-knw-lsp), дублировать проверку в mainline не нужно.
LANGUAGE_WORDS = frozenset({
    'процедура', 'функция', 'конецпроцедуры', 'конецфункции', 'если', 'тогда',
    'иначе', 'иначеесли', 'конецесли', 'для', 'пока', 'цикл', 'конеццикла',
    'каждого', 'из', 'по', 'попытка', 'исключение', 'конецпопытки', 'возврат',
    'перем', 'экспорт', 'знач', 'и', 'или', 'не', 'выполнить', 'новый', 'новая',
})

# контекст вызывающего кода: директивы метода -> что разрешено
CLIENT_DIRECTIVES = ('&наклиенте', '&наклиентенасервере',
                     '&наклиентенасерверебезконтекста')
SERVER_DIRECTIVES = ('&насервере', '&наклиентенасервере',
                     '&наклиентенасерверебезконтекста')


def _blank(text):
    """Замена содержимого на пробелы с сохранением переводов строк и длины."""
    return ''.join(c if c == '\n' else ' ' for c in text)


def scan(text):
    """(текст без литералов и комментариев, список литералов).

    Литерал — (start, end, значение): строка в кавычках с '' как экранировкой
    и с поддержкой многострочных строк 1С, где продолжение начинается с '|'.
    Маска сохраняет длину и переводы строк, поэтому номера строк по ней
    совпадают с исходными.
    """
    masked = []
    literals = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            buf = []
            while j < n:
                c = text[j]
                if c == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    j += 1
                    break
                if c == '\n':
                    # многострочный литерал: следующая непустая позиция — '|'
                    k = j + 1
                    while k < n and text[k] in ' \t\r':
                        k += 1
                    if k < n and text[k] == '|':
                        buf.append('\n')
                        j = k + 1
                        continue
                    break  # незакрытая строка — оставляем как есть
                buf.append(c)
                j += 1
            literals.append((i, j, ''.join(buf)))
            masked.append(_blank(text[i:j]))
            i = j
        elif ch == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            j = n if j < 0 else j
            masked.append(' ' * (j - i))
            i = j
        else:
            masked.append(ch)
            i += 1
    return ''.join(masked), literals


class LineIndex:
    """Номер строки (от 1) по позиции в тексте."""

    def __init__(self, text):
        self.starts = [0]
        for i, ch in enumerate(text):
            if ch == '\n':
                self.starts.append(i + 1)

    def __call__(self, pos):
        return bisect.bisect_right(self.starts, pos)


def local_names(masked, params=()):
    """Имена локальных переменных и параметров — чтобы не принимать их за модули."""
    names = {str(p).strip().lower() for p in params if str(p).strip()}
    names |= {m.group(1).lower() for m in RE_ASSIGN.finditer(masked)}
    names |= {m.group(1).lower() for m in RE_FOR_EACH.finditer(masked)}
    names |= {m.group(1).lower() for m in RE_FOR_TO.finditer(masked)}
    for m in RE_VAR.finditer(masked):
        names |= {part.strip().lower() for part in m.group(1).split(',')}
    # параметры методов, объявленных в этом же тексте: проверка произвольного
    # кода или целого модуля приходит без сигнатуры
    for m in RE_DECL_PARAMS.finditer(masked):
        names |= {name.lower() for name in split_params(m.group(1))}
    names.discard('')
    return names


def split_params(signature):
    """Имена параметров метода из сигнатуры ('Знач А, Б = Неопределено')."""
    params = []
    depth = 0
    in_str = False
    current = []
    for ch in signature or '':
        if ch == '"':
            in_str = not in_str
        if not in_str:
            if ch in '([':
                depth += 1
            elif ch in ')]':
                depth -= 1
            elif ch == ',' and depth == 0:
                params.append(''.join(current))
                current = []
                continue
        current.append(ch)
    if current:
        params.append(''.join(current))
    names = []
    for raw in params:
        text = raw.split('=')[0].strip()
        # 'Знач' — ключевое слово перед именем параметра, а не начало имени:
        # срезать префикс у 'Значение' значило бы потерять параметр
        text = RE_PARAM_ZNACH.sub('', text).strip()
        if text:
            names.append(text)
    return names


def extract_queries(text):
    """Тексты запросов из строковых литералов: [(строка, текст, полный)].

    Соседние литералы, разделённые только пробелами и '+', склеиваются: запрос
    часто собирают по частям. «Полный» — есть секция ИЗ/FROM и текст не обрывается
    на конкатенации с переменной: неполный запрос проверять парсером бессмысленно,
    его ошибки синтаксиса были бы ложными.
    """
    masked, literals = scan(text)
    lines = LineIndex(text)
    groups = []
    current = None
    prev_end = None
    for start, end, value in literals:
        # промежуток между литералами смотрим по маске: комментарий между частями
        # запроса не должен разрывать группу
        gap = masked[prev_end:start] if prev_end is not None else ''
        if current is not None and re.fullmatch(r'[\s+]*', gap):
            current[2].append(value)
        else:
            current = [start, end, [value]]
            groups.append(current)
        current[1] = end
        prev_end = end
    out = []
    for start, end, values in groups:
        query = '\n'.join(values)
        if not RE_QUERY_HEAD.match(query):
            continue
        # '+' сразу за литералом — продолжение переменной или параметром, а не
        # другим литералом (тот попал бы в эту же группу): текст неполный
        truncated = RE_CONCAT_TAIL.match(masked, end) is not None
        out.append((lines(start), query,
                    bool(RE_HAS_SOURCE.search(query)) and not truncated))
    return out


def query_refs(query_text):
    """(таблицы, поля) из текста запроса; пустые списки, если разобрать не вышло."""
    try:
        ast = parse_query(query_text)
    except (QueryError, ValueError, IndexError, KeyError, TypeError):
        return [], []
    tables, fields = [], []
    _walk(ast, tables, fields)
    return tables, fields


def _walk(node, tables, fields):
    if isinstance(node, tuple) and node:
        if node[0] == 'table' and isinstance(node[1], list):
            tables.append('.'.join(str(s) for s in node[1]))
        elif node[0] == 'field' and isinstance(node[1], list):
            fields.append('.'.join(str(s) for s in node[1]))
    if isinstance(node, (list, tuple)):
        for child in node:
            _walk(child, tables, fields)
    elif isinstance(node, dict):
        for child in node.values():
            _walk(child, tables, fields)


def query_result_columns(query_text):
    """Имена колонок результата запроса: алиасы КАК либо последний сегмент поля."""
    try:
        ast = parse_query(query_text)
    except (QueryError, ValueError, IndexError, KeyError, TypeError):
        return []
    columns = []
    for node in _selects(ast):
        for item in node.get('items') or []:
            if not isinstance(item, tuple) or not item:
                continue
            if item[0] == 'star':
                columns.append('*')
            elif item[0] == 'expr' and len(item) >= 3:
                alias = item[2]
                if isinstance(alias, str) and alias:
                    columns.append(alias)
                else:
                    expr = item[1]
                    if isinstance(expr, tuple) and expr and expr[0] == 'field' \
                            and isinstance(expr[1], list) and expr[1]:
                        columns.append(str(expr[1][-1]))
                    else:
                        columns.append('<выражение>')
    return columns


def _selects(node, out=None):
    if out is None:
        out = []
    if isinstance(node, dict):
        if node.get('select'):
            out.append(node)
        for child in node.values():
            _selects(child, out)
    elif isinstance(node, (list, tuple)):
        for child in node:
            _selects(child, out)
    return out


def result_schema(text):
    """Эвристика схемы временной таблицы/результата: [(раздел, имя, строка)].

    Источники имён колонок: Колонки.Добавить("Имя"), Новая Структура("А, Б"),
    алиасы и поля запросов (КАК / последний сегмент). Имена, заданные
    переменной, помечаются как динамические — их состав из кода не достать.
    """
    masked, _ = scan(text)
    lines = LineIndex(text)
    found = []
    seen = set()

    def add(kind, name, line):
        key = (kind, name.lower())
        if key not in seen:
            seen.add(key)
            found.append((kind, name, line))

    for m in RE_COLUMN_ADD.finditer(text):
        add('колонка', m.group(1), lines(m.start()))
    # имя колонки переменной — по маске: строковые литералы уже учтены выше
    for m in RE_COLUMN_ADD_VAR.finditer(masked):
        add('колонка (имя из переменной)', m.group(1), lines(m.start()))
    for m in RE_STRUCTURE.finditer(text):
        for key in m.group(1).split(','):
            key = key.strip()
            if key:
                add('ключ структуры', key, lines(m.start()))
    for line, query, complete in extract_queries(text):
        for name in (query_result_columns(query) if complete else []):
            add('колонка запроса', name, line)
    tables = [m.start() for m in RE_NEW_TABLE.finditer(masked)]
    notes = []
    if tables:
        notes.append(f'создано таблиц значений: {len(tables)} '
                     f'(строки {", ".join(str(lines(p)) for p in tables[:8])})')
    if RE_QUERY_RESULT.search(masked):
        notes.append('результат запроса выгружается в таблицу значений '
                     '(Выполнить().Выгрузить()) — состав колонок даёт запрос')
    return found, notes


class BslContext:
    """Справочник общих модулей, их методов и объектов метаданных по базе."""

    def __init__(self, conn):
        from .query_lang import MetaContext
        self.meta = MetaContext(conn)
        self.common = {}
        for name, path, context in conn.execute(
                'SELECT o.name, o.path, m.context FROM module m '
                'JOIN meta_object o ON o.id=m.object_id '
                "WHERE o.type='CommonModule'"):
            self.common[name.lower()] = {'name': name, 'path': path,
                                         'context': context or ''}
        self.common_methods = {}
        for module, name, is_export, kind, signature in conn.execute(
                'SELECT o.name, t.name, t.is_export, t.kind, t.signature '
                'FROM method t JOIN module m ON m.id=t.module_id '
                'JOIN meta_object o ON o.id=m.object_id '
                "WHERE o.type='CommonModule'"):
            self.common_methods[(module.lower(), name.lower())] = (
                int(is_export or 0), kind, signature or '')
        self.objects = {}
        for name, typ, path in conn.execute(
                'SELECT name, type, path FROM meta_object '
                "WHERE parent_id IS NOT NULL AND path NOT LIKE '%/%/%'"):
            self.objects.setdefault((typ, name.lower()), path)

    def resolve_manager(self, manager, name):
        """Путь объекта метаданных по обращению 'Справочники.Номенклатура'."""
        stem = BSL_MANAGERS.get(manager.lower())
        if stem is None:
            return None
        return self.objects.get((stem, name.lower()))

    def common_module(self, name):
        return self.common.get(name.lower())

    def common_method(self, module, name):
        return self.common_methods.get((module.lower(), name.lower()))


def analyze(text, ctx=None, params=(), caller_context=None, self_name=None):
    """Разбор кода: общие модули, метаданные, запросы, неразрешённые обращения.

    Синтаксис текста модуля здесь не проверяется — этим занимается BSL Language
    Server в варианте 1confdb-knw-lsp.

    :param ctx: BslContext (None — проверки по базе пропускаются)
    :param params: имена параметров метода (считаются локальными)
    :param caller_context: 'client' | 'server' | None — контекст вызывающего кода
    :param self_name: имя разбираемого метода (его объявление — не вызов)
    """
    masked, _ = scan(text)
    lines = LineIndex(text)
    known = local_names(masked, params)
    if self_name:
        known.add(str(self_name).lower())
    known |= {m.group(1).lower() for m in RE_DECL.finditer(masked)}
    report = {
        'modules': [],      # (модуль, метод, строка, состояние)
        'metadata': [],     # (менеджер, имя, строка, путь или None)
        'queries': [],      # (строка, текст, полный, ошибки, непроверенные)
        'tables': [],       # таблицы запросов
        'fields': [],       # поля запросов
        'unknown': [],      # (левая часть, метод, строка)
        'plain_calls': [],  # одиночные вызовы (локальные/глобальные)
        'context_warnings': [],
    }

    # цепочка 'Справочники.Объект.Метод(' — вызов метода менеджера, а не
    # обращение к общему модулю: позицию имени объекта не считаем модулем
    manager_names = {m.start(2) for m in RE_MANAGER.finditer(masked)}

    seen_calls = set()
    for m in RE_DOTTED_CALL.finditer(masked):
        left, right = m.group(1), m.group(2)
        if left.lower() in known or m.start(1) in manager_names:
            continue
        line = lines(m.start())
        key = (left.lower(), right.lower())
        if key in seen_calls:
            continue
        seen_calls.add(key)
        if ctx is None:
            report['modules'].append((left, right, line, 'не проверено'))
            continue
        module = ctx.common_module(left)
        if module is not None:
            state = _check_common_method(ctx, module, right, caller_context,
                                         report)
            report['modules'].append((module['name'], right, line, state))
        elif left.lower() not in BSL_MANAGERS \
                and left.lower() not in PLATFORM_GLOBALS:
            report['unknown'].append((left, right, line))

    seen_meta = set()
    for m in RE_MANAGER.finditer(masked):
        manager, name = m.group(1), m.group(2)
        # 'Обработки', 'Отчеты', 'Константы', 'Задачи' — обычные имена переменных:
        # локальное имя не обращение к менеджеру метаданных (как и в цикле выше)
        if manager.lower() in known:
            continue
        key = (manager.lower(), name.lower())
        if key in seen_meta:
            continue
        seen_meta.add(key)
        path = ctx.resolve_manager(manager, name) if ctx else None
        report['metadata'].append((manager, name, lines(m.start()), path))

    for m in RE_PLAIN_CALL.finditer(masked):
        name = m.group(1)
        low = name.lower()
        if low in known or low in LANGUAGE_WORDS:
            continue
        if name not in report['plain_calls']:
            report['plain_calls'].append(name)

    for line, query, complete in extract_queries(text):
        errors, unverified = [], []
        if ctx is not None and complete:
            errors, unverified = check_query_full(query, ctx.meta)
        report['queries'].append((line, query, complete, errors, unverified))
        if complete:
            tables, fields = query_refs(query)
            report['tables'].extend(t for t in tables if t not in report['tables'])
            report['fields'].extend(f for f in fields if f not in report['fields'])
    return report


def _check_common_method(ctx, module, method_name, caller_context, report):
    """Состояние вызова метода общего модуля + предупреждения о контексте."""
    found = ctx.common_method(module['name'], method_name)
    if found is None:
        state = f'метод не найден в модуле {module["path"]}'
    elif not found[0]:
        state = f'метод {module["path"]} не Экспорт — вызов извне не работает'
    else:
        state = 'ok'
    conflict = context_conflict(caller_context, module['context'])
    if conflict:
        report['context_warnings'].append(
            f'{module["name"]}.{method_name}: {conflict} '
            f'(контекст модуля: {module["context"] or "не задан"})')
    return state


def context_conflict(caller_context, callee_context):
    """'вызов серверного модуля из клиентского контекста' либо None."""
    if caller_context != 'client' or not callee_context:
        return None
    low = callee_context.lower()
    if 'клиент' in low:
        return None
    if 'сервер' in low:
        return ('серверный общий модуль вызывается из клиентского контекста — '
                'нужен вызов через серверный метод')
    return None


def caller_context(directives, module_context=None):
    """'client' | 'server' | None по директивам метода и контексту модуля."""
    low = (directives or '').lower()
    client = any(d in low for d in CLIENT_DIRECTIVES)
    server = any(d in low for d in SERVER_DIRECTIVES)
    if client and not server:
        return 'client'
    if server and not client:
        return 'server'
    ctx = (module_context or '').lower()
    if 'клиент' in ctx and 'сервер' not in ctx:
        return 'client'
    if 'сервер' in ctx and 'клиент' not in ctx:
        return 'server'
    return None
