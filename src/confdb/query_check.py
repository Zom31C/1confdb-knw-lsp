"""Поиск текстов запросов в модулях и их проверка для LSP-диагностики.

Извлекает строковые литералы из тел модулей (таблица ``module``), отбирает
похожие на запросы языка 1С (начинаются с ВЫБРАТЬ и содержат ИЗ), прогоняет
через валидатор ``query_lang`` и сохраняет нарушения в таблицу
``query_violation``. Её читает диагностика форка bsl-language-server
(bsl-language-server-confdb), подсвечивая ошибки прямо в модулях.
"""
import re
import sqlite3

from .query_lang import MetaContext, check_query

__all__ = ['extract_literals', 'check_modules', 'check_db', 'ensure_violation_table']

VIOLATION_DDL = (
    'CREATE TABLE IF NOT EXISTS query_violation ('
    ' object_id INTEGER REFERENCES meta_object (id),'
    ' path TEXT NOT NULL,'
    ' line INTEGER NOT NULL,'
    ' col INTEGER NOT NULL,'
    ' line_end INTEGER NOT NULL,'
    ' col_end INTEGER NOT NULL,'
    ' message TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS ix_query_violation_path'
    ' ON query_violation (path)',
)

RE_QUERY_START = re.compile(r'^\s*ВЫБРАТЬ', re.I)
RE_HAS_FROM = re.compile(r'\bИЗ\b', re.I)


def extract_literals(body):
    """Строковые литералы BSL: список (start, end, text).

    ``end`` — смещение сразу за закрывающей кавычкой, ``text`` — содержимое
    (без кавычек, многострочные продолжения через ``|`` склеиваются, ``""``
    превращается в ``"``). Комментарии ``//`` пропускаются. Строки, не закрытые
    до конца строки без продолжения ``|``, не возвращаются.
    """
    literals = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == '/' and i + 1 < n and body[i + 1] == '/':
            nl = body.find('\n', i)
            i = n if nl < 0 else nl + 1
            continue
        if ch != '"':
            i += 1
            continue
        start = i
        buf = []
        ok = False
        i += 1
        while i < n:
            c = body[i]
            if c == '"':
                if i + 1 < n and body[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                i += 1
                ok = True
                break
            if c == '\r':
                i += 1
                continue
            if c == '\n':
                # многострочное продолжение: следующая строка начинается с |
                j = i + 1
                while j < n and body[j] in ' \t':
                    j += 1
                if j < n and body[j] == '|':
                    buf.append('\n')
                    i = j + 1
                    continue
                break  # литерал не закрыт
            buf.append(c)
            i += 1
        if ok:
            literals.append((start, i, ''.join(buf)))
        else:
            i = max(i, start + 1)
    return literals


def offset_to_linecol(text, offset):
    """Позиция (line, col) в координатах LSP (с нуля)."""
    line = text.count('\n', 0, offset)
    col = offset - (text.rfind('\n', 0, offset) + 1)
    return line, col


def ensure_violation_table(conn):
    for stmt in VIOLATION_DDL:
        conn.execute(stmt)


def check_modules(conn):
    """Проверяет запросы в модулях; возвращает нарушения.

    Нарушение — кортеж (object_id, path, line, col, line_end, col_end, message),
    path — относительный путь файла в дампе (как в таблице ``file``),
    позиции — в координатах LSP (с нуля) относительно файла.

    Сканируются и тело модуля вне методов (``module.body``), и тела методов
    (``method.body``); для методов строки пересчитываются в координаты файла
    через ``method.line_start`` (1-нумерация).
    """
    ctx = MetaContext(conn)
    violations = []

    def scan(body, object_id, path, line_offset):
        for start, end, text in extract_literals(body):
            if not RE_QUERY_START.match(text) or not RE_HAS_FROM.search(text):
                continue
            errors = check_query(text, ctx)
            if not errors:
                continue
            line, col = offset_to_linecol(body, start)
            line_end, col_end = offset_to_linecol(body, end)
            for err in errors:
                violations.append((object_id, path, line_offset + line, col,
                                   line_offset + line_end, col_end, err))

    rows = conn.execute(
        "SELECT m.object_id, f.path, m.body "
        "FROM module m JOIN file f ON f.object_id = m.object_id "
        "WHERE f.kind = 'bsl' AND f.path LIKE '%.' || m.code_name || '.bsl' "
        "AND m.body LIKE '%ВЫБРАТЬ%'").fetchall()
    for object_id, path, body in rows:
        scan(body, object_id, path, 0)

    rows = conn.execute(
        "SELECT m.object_id, f.path, me.line_start, me.body "
        "FROM method me "
        "JOIN module m ON m.id = me.module_id "
        "JOIN file f ON f.object_id = m.object_id "
        "WHERE f.kind = 'bsl' AND f.path LIKE '%.' || m.code_name || '.bsl' "
        "AND me.body LIKE '%ВЫБРАТЬ%'").fetchall()
    for object_id, path, line_start, body in rows:
        scan(body, object_id, path, line_start - 1)
    return violations


def check_db(db_path):
    """Проверяет запросы модулей и сохраняет нарушения в базу.

    Возвращает статистику {'bodies': …, 'violations': …}: ``bodies`` — число
    тел модулей/методов, содержащих слово ВЫБРАТЬ. Таблица query_violation
    перезаписывается.
    """
    conn = sqlite3.connect(db_path)
    try:
        violations = check_modules(conn)
        conn.execute('DROP TABLE IF EXISTS query_violation')
        ensure_violation_table(conn)
        conn.executemany(
            'INSERT INTO query_violation (object_id, path, line, col,'
            ' line_end, col_end, message) VALUES (?, ?, ?, ?, ?, ?, ?)',
            violations)
        conn.commit()
        bodies = conn.execute(
            "SELECT (SELECT COUNT(*) FROM module WHERE body LIKE '%ВЫБРАТЬ%') + "
            "(SELECT COUNT(*) FROM method WHERE body LIKE '%ВЫБРАТЬ%')").fetchone()[0]
    finally:
        conn.close()
    return {
        'bodies': bodies,
        'violations': len(violations),
    }
