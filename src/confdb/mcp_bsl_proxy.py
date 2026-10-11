"""MCP-шлюз к BSL Language Server (форк bsl-language-server-confdb).

Поднимает Java-сервер в режиме ``mcp`` (stdio) как дочерний процесс и
проксирует его инструменты через единое MCP-соединение 1confdb-knw:
LLM-клиент подключается к одному серверу и получает инструменты обоих.

Особенности протокола:
- транспорт stdio — по одному JSON-сообщению на строку;
- workspace Java-сервер получает через MCP roots — после handshake шлюз
  сам шлёт ``notifications/roots/list_changed`` и отвечает на запрос
  ``roots/list`` от сервера (McpRootsBootstrapper запрашивает его при
  первом tool-вызове);
- лог Java-процесса уходит в ``bsl-lsp-server.log`` в каталоге workspace,
  чтобы не смешиваться с JSON-RPC в stdout.
"""
import glob
import json
import os
import subprocess
import sys
import threading

__all__ = ['BslMcpProxy', 'find_jar', 'find_java']

_PROTOCOL_VERSION = '2024-11-05'
_TOOL_PREFIX = 'bsl_'
_START_TIMEOUT = 90
_CALL_TIMEOUT = 180


def find_jar(explicit=None):
    """Ищет jar BSL Language Server: аргумент → конфиг-переменная → bin рядом
    с дистрибутивом → текущий каталог."""
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)
    env = os.environ.get('CONFDB_BSL_JAR')
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    # .../dist/1confdb-knw-lsp/src/confdb → bin в корне дистрибутива
    dist_root = os.path.dirname(os.path.dirname(here))
    for root in (dist_root, os.getcwd()):
        jar = os.path.join(root, 'bin', 'bsl-language-server.jar')
        if os.path.isfile(jar):
            return jar
    return None


def find_java(explicit=None):
    """java: явный путь → JAVA_HOME → PATH → типовые каталоги установки JDK.

    JDK из winget (Temurin) не прописывает себя ни в JAVA_HOME, ни в PATH —
    поэтому последними проверяются стандартные каталоги установки.
    """
    if explicit:
        return explicit
    home = os.environ.get('JAVA_HOME')
    if home:
        exe = os.path.join(home, 'bin', 'java.exe' if os.name == 'nt' else 'java')
        if os.path.isfile(exe):
            return exe
    import shutil
    found = shutil.which('java')
    if found:
        return found
    if os.name == 'nt':
        patterns = (
            r'C:\Program Files\Eclipse Adoptium\jdk*\bin\java.exe',
            r'C:\Program Files\Java\jdk*\bin\java.exe',
            r'C:\Program Files\Microsoft\jdk*\bin\java.exe',
        )
        for pattern in patterns:
            matches = sorted(glob.glob(pattern), reverse=True)
            if matches:
                return matches[0]
    return None


class BslMcpProxy:
    """Дочерний MCP-сервер BSL Language Server за stdio-шлюзом."""

    def __init__(self, jar, workspace, db_path, java=None, command=None):
        self.jar = jar
        self.workspace = os.path.abspath(workspace)
        self.db_path = os.path.abspath(db_path)
        self.java = find_java(java)
        # command — переопределение командной строки (тесты, особые сборки)
        self.command = command
        self.proc = None
        self.tools = []
        self._next_id = 0
        self._pending = {}
        self._lock = threading.Lock()
        self._reader = None
        self._error = None

    # -- жизненный цикл -------------------------------------------------------

    def start(self):
        """Запускает процесс, делает handshake и запрашивает инструменты.

        Бросает RuntimeError с человекочитаемой причиной при неудаче.
        """
        if not self.command and not self.java:
            raise RuntimeError(
                'не найдена java: установите JDK 21 (setup.bat предложит winget) '
                'и перезапустите сервер, либо задайте JAVA_HOME/--java')
        self._ensure_workspace_config()
        log_path = os.path.join(self.workspace, 'bsl-lsp-server.log')
        try:
            log = open(log_path, 'wb')
        except OSError:
            log = subprocess.DEVNULL
        command = self.command or [self.java, '-jar', self.jar, 'mcp']
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            cwd=self.workspace)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._initialize()
        self._send({'method': 'notifications/initialized'})
        # сразу регистрируем workspace — сервер начнёт индексацию
        self._send({'method': 'notifications/roots/list_changed',
                    'params': {'roots': self._roots()}})
        result = self._request('tools/list', {}, timeout=_START_TIMEOUT)
        self.tools = result.get('tools', []) or []

    def stop(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.close()  # EOF — штатное завершение stdio-режима
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    # -- инструменты ----------------------------------------------------------

    def prefixed_tools(self):
        """Спецификации инструментов с префиксом ``bsl_``."""
        out = []
        for tool in self.tools:
            spec = dict(tool)
            spec['name'] = _TOOL_PREFIX + tool.get('name', '')
            out.append(spec)
        return out

    def call(self, name, arguments):
        """Вызывает инструмент Java-сервера; имя без префикса ``bsl_``.

        Возвращает текст для ответа LLM-клиенту.
        """
        result = self._request('tools/call',
                               {'name': name, 'arguments': arguments or {}},
                               timeout=_CALL_TIMEOUT)
        parts = []
        for item in result.get('content', []) or []:
            if item.get('type') == 'text':
                parts.append(item.get('text', ''))
        text = '\n'.join(parts)
        if result.get('isError'):
            raise RuntimeError(text or 'инструмент BSL LS вернул ошибку')
        return text

    # -- внутренности ----------------------------------------------------------

    def _ensure_workspace_config(self):
        """Подкладывает .bsl-language-server.json с confdbDatabase в workspace."""
        path = os.path.join(self.workspace, '.bsl-language-server.json')
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
        if data.get('confdbDatabase') == self.db_path:
            return
        data['confdbDatabase'] = self.db_path.replace('\\', '/')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _roots(self):
        uri = self.workspace.replace('\\', '/')
        if not uri.startswith('/'):
            uri = '/' + uri
        return [{'uri': 'file://' + uri, 'name': 'confdb-lsp-workspace'}]

    def _initialize(self):
        result = self._request('initialize', {
            'protocolVersion': _PROTOCOL_VERSION,
            'capabilities': {'roots': {}},
            'clientInfo': {'name': '1confdb-knw', 'version': '1.0'},
        }, timeout=_START_TIMEOUT)
        if not isinstance(result, dict):
            raise RuntimeError('BSL LS не ответил на initialize')

    def _send(self, msg):
        msg = dict(msg)
        msg.setdefault('jsonrpc', '2.0')
        line = json.dumps(msg, ensure_ascii=False) + '\n'
        with self._lock:
            if self.proc and self.proc.stdin:
                self.proc.stdin.write(line.encode('utf-8'))
                self.proc.stdin.flush()

    def _request(self, method, params, timeout):
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            event = threading.Event()
            box = {}
            self._pending[msg_id] = (event, box)
        self._send({'id': msg_id, 'method': method, 'params': params})
        if not event.wait(timeout):
            self._pending.pop(msg_id, None)
            raise RuntimeError(f'BSL LS не ответил за {timeout} с: {method}')
        self._pending.pop(msg_id, None)
        if 'error' in box:
            err = box['error'] or {}
            raise RuntimeError(f'BSL LS: {err.get("message", err)}')
        return box.get('result') or {}

    def _read_loop(self):
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode('utf-8', errors='replace').strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if 'method' in msg and 'id' in msg:
                self._answer_server_request(msg)
                continue
            msg_id = msg.get('id')
            entry = self._pending.get(msg_id)
            if entry:
                event, box = entry
                if 'error' in msg:
                    box['error'] = msg['error']
                else:
                    box['result'] = msg.get('result')
                event.set()
        # процесс завершился
        for event, box in self._pending.values():
            box.setdefault('error', {'message': 'процесс BSL LS завершился'})
            event.set()

    def _answer_server_request(self, msg):
        """Ответ на запрос сервера (roots/list и т.п.)."""
        method = msg.get('method')
        if method == 'roots/list':
            result = {'roots': self._roots()}
        elif method == 'ping':
            result = {}
        else:
            self._send({'id': msg.get('id'), 'error': {
                'code': -32601, 'message': f'method not found: {method}'}})
            return
        self._send({'id': msg.get('id'), 'result': result})
