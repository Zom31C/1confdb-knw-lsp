"""Тесты MCP-сервера: протокол и инструменты на синтетической базе."""
import json
import os
import sqlite3
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import confdb.mcp_server as mcp_server  # noqa: E402
from confdb.db.writer import write_db  # noqa: E402
from confdb.mcp_server import McpServer, resolve_db, start_http_server  # noqa: E402

from test_writer import make_dump  # noqa: E402


def _make_db(tmp_path_factory):
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / 't.sqlite')
    write_db(dump, db, source_file='t.cf')
    return db


def _server(tmp_path_factory):
    return McpServer(_make_db(tmp_path_factory))


def _call(server, tool, **args):
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': tool, 'arguments': args}})
    return resp['result']


def test_initialize_has_primer(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    assert 'meta_object' in resp['result']['instructions']
    assert resp['result']['protocolVersion']


def test_tools_list(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    names = {t['name'] for t in resp['result']['tools']}
    assert names == {'find_objects', 'object_card', 'object_tree', 'find_field',
                     'refs_of', 'module_outline', 'get_method', 'find_methods',
                     'skd_of', 'find_skd', 'check_query', 'schema', 'sql',
                     'db_list', 'db_open', 'db_use', 'db_close'}


def test_db_schema(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': 'schema', 'arguments': {}}})
    text = resp['result']['content'][0]['text']
    assert 'meta_object' in text and 'module' in text and 'строк' in text


def test_get_method_pagination(tmp_path_factory):
    db = _make_db(tmp_path_factory)
    long_body = '\n'.join(['\tА = 1;'] +
                          [f'\t// строка {i}' for i in range(320)] +
                          ['\tБ = 2;'])
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE method SET body=? WHERE name='Тест'", (long_body,))
    server = McpServer(db)
    text = _call(server, 'get_method', path='Справочник.Справочник1',
                 code_name='obj', name='Тест')['content'][0]['text']
    assert 'строки 1-250 из 322' in text
    assert 'продолжение: offset=250' in text
    tail = _call(server, 'get_method', path='Справочник.Справочник1',
                 code_name='obj', name='Тест',
                 offset=250, limit=100)['content'][0]['text']
    assert 'строки 251-322 из 322' in tail
    assert 'продолжение' not in tail
    assert 'Б = 2;' in tail


def test_module_outline_pagination(tmp_path_factory):
    db = _make_db(tmp_path_factory)
    long_body = '\n'.join(f'// заголовок {i}' for i in range(400))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE module SET body=? WHERE code_name='obj'",
                     (long_body,))
    server = McpServer(db)
    text = _call(server, 'module_outline', path='Справочник.Справочник1',
                 code_name='obj')['content'][0]['text']
    assert 'строки 1-300 из 400' in text and 'продолжение: offset=300' in text


def test_object_card_enum_and_predefined(tmp_path_factory):
    db = _make_db(tmp_path_factory)
    with sqlite3.connect(db) as conn:
        oid = conn.execute("SELECT id FROM meta_object "
                           "WHERE path='Catalog/Справочник1'").fetchone()[0]
        # перечисление
        conn.execute("UPDATE meta_object SET type='Enum' WHERE id=?", (oid,))
        conn.execute("INSERT INTO enum_value (object_id, ord, name) "
                     "VALUES (?, 1, 'Значение1'), (?, 2, 'Значение2')",
                     (oid, oid))
    server = McpServer(db)
    text = _call(server, 'object_card',
                 path='Catalog/Справочник1')['content'][0]['text']
    assert 'Значения перечисления' in text and 'Значение1' in text
    # предопределённые элементы справочника
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta_object SET type='Catalog' WHERE id=?", (oid,))
        conn.execute("INSERT INTO predefined (object_id, ord, name, code) "
                     "VALUES (?, 1, 'Основной', '001')", (oid,))
    text = _call(server, 'object_card',
                 path='Catalog/Справочник1')['content'][0]['text']
    assert 'Предопределённые элементы' in text and 'Основной [001]' in text


def test_find_objects_and_card(tmp_path_factory):
    server = _server(tmp_path_factory)
    text = _call(server, 'find_objects', mask='Справочник1')['content'][0]['text']
    assert 'Справочник.Справочник1' in text
    # вход в русском точечном формате
    card = _call(server, 'object_card', path='Справочник.Справочник1')['content'][0]['text']
    assert 'СсылкаАтрибут' in card and 'Товары' not in card
    # и в старом слэш-формате
    card_doc = _call(server, 'object_card',
                     path='Document/ЗаказПокупателя')['content'][0]['text']
    assert 'Документ.ЗаказПокупателя' in card_doc
    assert 'Табличная часть Товары' in card_doc and 'ТоварыНоменклатура' in card_doc


def test_tree_and_field(tmp_path_factory):
    server = _server(tmp_path_factory)
    tree = _call(server, 'object_tree', path='', depth=3)['content'][0]['text']
    assert 'Справочник.Справочник1' in tree
    assert 'Справочник.Справочник1.ФормаЭлемента' in tree
    fields = _call(server, 'find_field', name='Товары')['content'][0]['text']
    assert 'Документ.ЗаказПокупателя' in fields and '[табчасть Товары]' in fields


def test_methods(tmp_path_factory):
    server = _server(tmp_path_factory)
    found = _call(server, 'find_methods', mask='Тест')['content'][0]['text']
    assert 'процедура Тест()' in found
    method = _call(server, 'get_method', path='Catalog/Справочник1',
                   code_name='obj', name='Тест')['content'][0]['text']
    assert 'КонецПроцедуры' in method
    outline = _call(server, 'module_outline',
                    path='Catalog/Справочник1')['content'][0]['text']
    assert 'Процедура Тест()' in outline


def test_check_and_sql(tmp_path_factory):
    server = _server(tmp_path_factory)
    ok = _call(server, 'check_query',
               text='ВЫБРАТЬ Т.СсылкаАтрибут ИЗ Справочник.Справочник1 КАК Т')
    assert ok['content'][0]['text'].startswith('OK')
    bad = _call(server, 'check_query',
                text='ВЫБРАТЬ Т.Х ИЗ Справочник.Нет КАК Т')['content'][0]['text']
    assert 'неизвестная таблица' in bad
    res = _call(server, 'sql', query='SELECT COUNT(*) AS n FROM meta_object')
    assert 'n' in res['content'][0]['text']
    # русский точечный путь в литерале sql конвертируется во внутренний формат
    res = _call(server, 'sql',
                query="SELECT path FROM meta_object WHERE path = 'Справочник.Справочник1'")
    assert 'Catalog/Справочник1' in res['content'][0]['text']
    denied = _call(server, 'sql', query='DELETE FROM meta_object')
    assert denied.get('isError')


def test_refs_and_skd_empty(tmp_path_factory):
    server = _server(tmp_path_factory)
    refs = _call(server, 'refs_of', path='Catalog/Справочник1')['content'][0]['text']
    assert 'Ссылается на' in refs
    skd = _call(server, 'skd_of', path='Catalog/Справочник1')['content'][0]['text']
    assert 'нет запросов' in skd


def test_jsonrpc_roundtrip(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle(json.loads('{"jsonrpc":"2.0","id":9,'
                                    '"method":"notifications/initialized"}'))
    assert resp is None
    resp = server.handle({'jsonrpc': '2.0', 'id': 10, 'method': 'nope'})
    assert resp['error']['code'] == -32601


def _post(port, msg, path='/mcp'):
    req = urllib.request.Request(
        f'http://127.0.0.1:{port}{path}',
        data=json.dumps(msg).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_http_transport(tmp_path_factory):
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = _post(port, {'jsonrpc': '2.0', 'id': 1,
                                    'method': 'initialize'})
        assert status == 200
        assert 'instructions' in json.loads(body)['result']
        status, _ = _post(port, {'jsonrpc': '2.0',
                                 'method': 'notifications/initialized'})
        assert status == 202
        status, body = _post(port, {'jsonrpc': '2.0', 'id': 2,
                                    'method': 'tools/call',
                                    'params': {'name': 'find_objects',
                                               'arguments': {'mask': 'Справочник1'}}})
        assert status == 200
        assert 'Справочник.Справочник1' in json.loads(body)['result']['content'][0]['text']
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_streamable_http_get_stream(tmp_path_factory):
    # клиенты Streamable HTTP (Claude) открывают SSE-поток через GET /mcp
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/mcp',
                                    timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get('Content-Type') == 'text/event-stream'
            # legacy SSE-клиенты получают endpoint; streamable игнорируют его
            assert resp.readline().decode().strip() == 'event: endpoint'
            assert 'session_id=' in resp.readline().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_sse_transport(tmp_path_factory):
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/sse',
                                    timeout=5) as sse:
            assert sse.readline().decode().strip() == 'event: endpoint'
            data = sse.readline().decode().strip()
            sid = data.split('session_id=')[1]
            assert sse.readline() == b'\n'  # разделитель события
            status, _ = _post(port, {'jsonrpc': '2.0', 'id': 7,
                                     'method': 'tools/list'},
                              path=f'/messages?session_id={sid}')
            assert status == 202
            assert sse.readline().decode().strip() == 'event: message'
            payload = json.loads(sse.readline().decode().split('data: ', 1)[1])
            assert payload['id'] == 7 and 'tools' in payload['result']
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- запуск без пути: проверка и автопоиск базы --------------------------

def test_resolve_db_explicit(tmp_path, capsys):
    db = tmp_path / 't.sqlite'
    db.write_bytes(b'x')
    assert resolve_db(str(db)) == str(db)
    with pytest.raises(SystemExit) as exc:
        resolve_db(str(tmp_path / 'нет.sqlite'))
    assert exc.value.code == 2
    assert 'не найден' in capsys.readouterr().err


def test_resolve_db_last_db(tmp_path, monkeypatch):
    # last_db из конфига имеет приоритет над автопоиском
    db = tmp_path / 'база.sqlite'
    db.write_bytes(b'x')
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'last_db': str(db)}), encoding='utf-8')
    monkeypatch.setattr('confdb.config.CONFIG_PATH', str(cfg))
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert resolve_db(None) == str(db)


def test_resolve_db_autofind(tmp_path, monkeypatch, capsys):
    # без конфига (свежая установка) база находится обходом каталогов
    monkeypatch.setattr('confdb.config.CONFIG_PATH', str(tmp_path / 'нет.json'))
    monkeypatch.setattr(mcp_server, '_scan_roots', lambda: [str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        resolve_db(None)
    assert exc.value.code == 2
    assert 'не найдена' in capsys.readouterr().err
    (tmp_path / 'db').mkdir()
    one = tmp_path / 'db' / 'одна.sqlite'
    one.write_bytes(b'x')
    assert resolve_db(None) == str(one)
    two = tmp_path / 'вторая.db'
    two.write_bytes(b'x')
    with pytest.raises(SystemExit) as exc:
        resolve_db(None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(one) in err and str(two) in err


def test_find_db_candidates_dedup(tmp_path, monkeypatch):
    # db/ и _out/ просматриваются, повторы и каталоги не попадают в список
    monkeypatch.setattr(mcp_server, '_scan_roots', lambda: [str(tmp_path)])
    (tmp_path / 'db').mkdir()
    (tmp_path / '_out').mkdir()
    (tmp_path / 'a.db').write_bytes(b'x')
    (tmp_path / 'db' / 'b.sqlite').write_bytes(b'x')
    (tmp_path / '_out' / 'c.db').write_bytes(b'x')
    (tmp_path / 'каталог.db').mkdir()
    found = mcp_server.find_db_candidates()
    assert found == [str(tmp_path / 'a.db'),
                     str(tmp_path / 'db' / 'b.sqlite'),
                     str(tmp_path / '_out' / 'c.db')]


# -- удобные формы путей модулей и опциональные маски -------------------

def test_module_path_forms(tmp_path_factory):
    server = _server(tmp_path_factory)
    base = _call(server, 'module_outline', path='Справочник.Справочник1',
                 code_name='obj')['content'][0]['text']
    assert 'не найден' not in base
    for form in ('Справочник.Справочник1.obj',
                 'Справочник.Справочник1.obj.bsl',
                 'Справочник.Справочник1.МодульОбъекта'):
        text = _call(server, 'module_outline',
                     path=form)['content'][0]['text']
        assert text == base, form
    # путь файла дампа
    text = _call(server, 'module_outline',
                 path='Catalog/Справочник1/Catalog.obj.bsl')['content'][0]['text']
    assert text == base
    # get_method с формой 'Объект.mgr'/'объект.obj.bsl'
    m = _call(server, 'get_method', path='Справочник.Справочник1.obj.bsl',
              code_name='obj', name='Тест')['content'][0]['text']
    assert 'Процедура Тест' in m


def test_optional_masks(tmp_path_factory):
    server = _server(tmp_path_factory)
    # просмотр объектов типа без маски
    text = _call(server, 'find_objects', type='Catalog')['content'][0]['text']
    assert 'Справочник.Справочник1' in text
    # список методов объекта без маски
    text = _call(server, 'find_methods',
                 path='Справочник.Справочник1')['content'][0]['text']
    assert 'Тест' in text


def test_http_client_reset_is_quiet(tmp_path_factory):
    """Обрыв соединения клиентом (RST на keep-alive) не должен печатать
    traceback и не должен ронять сервер."""
    import contextlib
    import http.client
    import io
    import socket
    import struct
    import time

    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {}}).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    try:
        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/mcp', body=body, headers=headers)
        assert conn.getresponse().status == 200
        conn.close()

        # клиент обрывает соединение с RST (SO_LINGER с нулевой задержкой)
        sock = socket.create_connection(('127.0.0.1', port))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack('ii', 1, 0))
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            sock.close()
            time.sleep(0.5)
        assert 'Traceback' not in err_buf.getvalue()

        # сервер жив и обслуживает следующие запросы
        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/mcp', body=body, headers=headers)
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_chunked_request(tmp_path_factory):
    """Claude Code шлёт POST с Transfer-Encoding: chunked — сервер обязан
    прочитать тело и ответить 200."""
    import http.client

    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {}}).encode('utf-8')
    try:
        conn = http.client.HTTPConnection('127.0.0.1', port)
        # iterable-тело — http.client сам включает chunked-кодировку
        conn.request('POST', '/mcp', body=iter([body]),
                     headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read().decode('utf-8'))
        assert data['result']['serverInfo']['name'] == '1confdb-knw'
        conn.close()

        # сервер продолжает работать и отвечать на обычные запросы
        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/mcp', body=body,
                     headers={'Content-Type': 'application/json'})
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_unknown_path_drains_body(tmp_path_factory):
    """404 на неизвестный путь обязан дочитать тело, иначе keep-alive
    соединение съезжает (следующий запрос ломается)."""
    import http.client

    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/register',
                     body=b'{"client_name": "test"}',
                     headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        assert resp.status == 201  # регистрация клиентов поддерживается
        resp.read()
        # то же соединение должно остаться рабочим
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                           'params': {}}).encode('utf-8')
        conn.request('POST', '/mcp', body=body,
                     headers={'Content-Type': 'application/json'})
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_oauth_flow(tmp_path_factory):
    """OAuth 2.1 для клиентов типа Claude Code: метаданные, регистрация,
    authorize с авто-редиректом, обмен кода с PKCE, работа MCP с токеном."""
    import base64
    import hashlib
    import http.client
    from urllib.parse import parse_qs, urlparse

    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection('127.0.0.1', port)

        conn.request('GET', '/.well-known/oauth-protected-resource')
        resp = conn.getresponse()
        assert resp.status == 200
        meta = json.loads(resp.read())
        assert meta['authorization_servers']

        conn.request('GET', '/.well-known/oauth-authorization-server')
        meta = json.loads(conn.getresponse().read())
        assert meta['registration_endpoint'].endswith('/oauth/register')
        assert 'S256' in meta['code_challenge_methods_supported']

        reg = json.dumps({'client_name': 'Claude Code',
                          'redirect_uris': ['http://localhost:9/cb'],
                          'grant_types': ['authorization_code'],
                          'token_endpoint_auth_method': 'none'}).encode()
        conn.request('POST', '/oauth/register', body=reg,
                     headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        assert resp.status == 201
        client_id = json.loads(resp.read())['client_id']
        assert client_id

        verifier = 'verifier-1234567890abcdef'
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        conn.request('GET', '/oauth/authorize?'
                     f'client_id={client_id}&redirect_uri=http://localhost:9/cb'
                     f'&code_challenge={challenge}&code_challenge_method=S256'
                     '&state=xyz')
        resp = conn.getresponse()
        assert resp.status == 302
        location = resp.getheader('Location')
        resp.read()
        qs = parse_qs(urlparse(location).query)
        assert qs['state'] == ['xyz']
        code = qs['code'][0]

        conn.request('POST', '/oauth/token',
                     body=(f'grant_type=authorization_code&code={code}'
                           f'&code_verifier={verifier}'
                           '&redirect_uri=http://localhost:9/cb'
                           f'&client_id={client_id}').encode(),
                     headers={'Content-Type':
                              'application/x-www-form-urlencoded'})
        resp = conn.getresponse()
        assert resp.status == 200
        token = json.loads(resp.read())
        assert token['access_token'] and token['token_type'] == 'Bearer'

        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                           'params': {}}).encode('utf-8')
        conn.request('POST', '/mcp', body=body,
                     headers={'Content-Type': 'application/json',
                              'Authorization':
                                  'Bearer ' + token['access_token']})
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
