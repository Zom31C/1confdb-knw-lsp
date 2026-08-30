"""Тесты MCP-шлюза к BSL Language Server (mcp_bsl_proxy)."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.mcp_bsl_proxy import BslMcpProxy  # noqa: E402
from confdb.mcp_server import McpServer  # noqa: E402

FAKE_SERVER = os.path.join(os.path.dirname(__file__), 'fake_bsl_mcp.py')


def _make_db(path):
    """Минимальная валидная база confdb: мульти-БД open_db требует meta_object."""
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE meta_object (id INTEGER PRIMARY KEY)')
    conn.commit()
    conn.close()


def make_proxy(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    db = tmp_path / 'test.db'
    _make_db(db)
    return BslMcpProxy(jar='не-используется.jar', workspace=str(workspace),
                       db_path=str(db),
                       command=[sys.executable, FAKE_SERVER])


def test_proxy_handshake_and_call(tmp_path):
    proxy = make_proxy(tmp_path)
    proxy.start()
    try:
        assert [t['name'] for t in proxy.tools] == ['echo_tool']
        assert [t['name'] for t in proxy.prefixed_tools()] == ['bsl_echo_tool']
        assert proxy.call('echo_tool', {'text': 'привет'}) == 'эхо: привет'
        # в workspace подложен конфиг с confdbDatabase
        config = json.loads((tmp_path / 'ws' / '.bsl-language-server.json')
                            .read_text(encoding='utf-8'))
        assert config['confdbDatabase'].endswith('test.db')
    finally:
        proxy.stop()
    assert proxy.proc is None


def test_mcp_server_merges_bsl_tools(tmp_path):
    proxy = make_proxy(tmp_path)
    proxy.start()
    try:
        server = McpServer(proxy.db_path, bsl=proxy)
        tools = server.handle({'method': 'tools/list', 'id': 1})['result']['tools']
        names = [t['name'] for t in tools]
        assert 'find_objects' in names and 'bsl_echo_tool' in names
        resp = server.handle({'method': 'tools/call', 'id': 2, 'params': {
            'name': 'bsl_echo_tool', 'arguments': {'text': 'раз'}}})
        assert resp['result']['content'][0]['text'] == 'эхо: раз'
        instr = server.handle({'method': 'initialize', 'id': 3})
        assert 'bsl_' in instr['result']['instructions']
    finally:
        proxy.stop()


def test_mcp_server_without_bsl_rejects_bsl_tools(tmp_path):
    db = tmp_path / 'нет.db'
    _make_db(db)
    server = McpServer(str(db))
    resp = server.handle({'method': 'tools/call', 'id': 1, 'params': {
        'name': 'bsl_hover', 'arguments': {}}})
    assert resp['error']['code'] == -32602
    tools = server.handle({'method': 'tools/list', 'id': 2})['result']['tools']
    assert not any(t['name'].startswith('bsl_') for t in tools)
