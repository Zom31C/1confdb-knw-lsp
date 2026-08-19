"""Фейковый stdio-MCP-сервер для тестов прокси (запускается вместо Java).

Имитирует поведение BSL Language Server: отвечает на initialize, отдаёт
инструменты и — как McpRootsBootstrapper — сам запрашивает у клиента
roots/list перед ответом на tools/list.
"""
import json
import sys

TOOLS = [
    {'name': 'echo_tool', 'description': 'эхо-инструмент',
     'inputSchema': {'type': 'object',
                     'properties': {'text': {'type': 'string'}}}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def main():
    # реальный сервер (и Java, и confdb) пишет в stdio UTF-8
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get('method')
        mid = msg.get('id')
        if method == 'initialize':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'fake-bsl', 'version': '0'}}})
        elif method == 'tools/list':
            # сервер сначала сам запрашивает roots у клиента
            send({'jsonrpc': '2.0', 'id': 'srv-roots-1', 'method': 'roots/list'})
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': TOOLS}})
        elif method == 'tools/call':
            params = msg.get('params', {})
            if params.get('name') == 'echo_tool':
                text = 'эхо: ' + str(params.get('arguments', {}).get('text'))
                send({'jsonrpc': '2.0', 'id': mid, 'result': {
                    'content': [{'type': 'text', 'text': text}]}})
            else:
                send({'jsonrpc': '2.0', 'id': mid, 'result': {
                    'content': [{'type': 'text', 'text': 'неизвестный инструмент'}],
                    'isError': True}})
        # уведомления (initialized, roots/list_changed) и ответы на наши
        # запросы (id без method) — молча игнорируем


if __name__ == '__main__':
    main()
