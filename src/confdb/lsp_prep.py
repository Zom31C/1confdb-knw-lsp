"""Подготовка дампа для BSL Language Server.

CLI-анализатор bsl-language-server корректно считает позиции только на
файлах с переводами строк LF и без BOM (на CRLF номера строк в
диагностиках «удваиваются», U+FEFF лексер не понимает). Дамп confdb
повторяет исходные модули 1С — UTF-8 с BOM и CRLF, — поэтому перед
использованием дампа как workspace для LSP его нужно привести к виду
UTF-8 без BOM + LF.

Модуль не трогает конвейер декодирования: это независимая постобработка
готового дампа (in-place или в отдельный каталог через --into).
"""
import os

__all__ = ['prep_dump']

_BOM = b'\xef\xbb\xbf'


def prep_dump(src_dir, dst_dir=None, on_file=None):
    """Переписывает *.bsl дампа: убирает BOM, приводит переводы строк к LF.

    :param src_dir: каталог дампа (результат ``confdb extract --dump``)
    :param dst_dir: куда писать; None — править файлы на месте.
        Если задан, дамп копируется целиком, .bsl — с конвертацией
    :param on_file: необязательный вызов ``on_file(rel_path)`` по каждому
        обрабатываемому файлу (для индикатора прогресса)
    :return: словарь со статистикой: ``files`` (всего .bsl),
        ``converted`` (переписано), ``skipped`` (уже в нужном формате
        или не читается как UTF-8 — скопирован как есть)
    """
    src = os.path.abspath(src_dir)
    if not os.path.isdir(src):
        raise FileNotFoundError(f'Каталог дампа не найден: {src_dir}')
    dst = os.path.abspath(dst_dir) if dst_dir else None
    if dst:
        if dst == src:
            raise ValueError('Каталог назначения совпадает с исходным')
        src_prefix = src + os.sep
        if dst.startswith(src_prefix):
            raise ValueError('Каталог назначения не может быть внутри исходного')
        os.makedirs(dst, exist_ok=True)

    stats = {'files': 0, 'converted': 0, 'skipped': 0}
    for dirpath, _dirnames, filenames in os.walk(src):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, src)
            if not fn.endswith('.bsl'):
                # в режим --into дамп копируется целиком
                if dst:
                    with open(full, 'rb') as f:
                        _write_bytes(os.path.join(dst, rel), f.read())
                continue
            stats['files'] += 1
            if on_file:
                on_file(rel)
            with open(full, 'rb') as f:
                data = f.read()
            has_bom = data.startswith(_BOM)
            if not has_bom and b'\r' not in data:
                # уже LF без BOM
                if dst:
                    _write_bytes(os.path.join(dst, rel), data)
                stats['skipped'] += 1
                continue
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                # не UTF-8 — не трогаем содержимое
                if dst:
                    _write_bytes(os.path.join(dst, rel), data)
                stats['skipped'] += 1
                continue
            text = text.replace('\r\n', '\n').replace('\r', '\n')
            target = os.path.join(dst, rel) if dst else full
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
            stats['converted'] += 1
    return stats


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
