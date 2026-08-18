"""Пользовательская конфигурация confdb (~/.confdb/config.json).

Хранит недавние пути и опции TUI, результат бенчмарка автонастройки.
"""
import json
import os

CONFIG_PATH = os.path.join(os.path.expanduser('~'), '.confdb', 'config.json')


def load_config():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as file:
        json.dump(config, file, ensure_ascii=False, indent=2)


def bench_workers():
    """Рекомендованное бенчмарком число процессов (или None)."""
    value = load_config().get('bench', {}).get('workers')
    if isinstance(value, int) and value >= 1:
        return value
    return None
