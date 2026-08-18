"""Конвейер: cf/cfe/epf → распакованные файлы → база данных SQLite.

Стадии повторяют поток v8unpack (только распаковка):
  0 — чтение внешних контейнеров (32/64-бит) в файлы как есть;
  1 — inflate (raw deflate) + рекурсивные вложенные контейнеры;
  3 — декодирование метаданных: скобкофайлы → JSON, тексты модулей → .bsl.
(стадия 2 оригинала — конвертация в json отдельным прогоном — отключена и там)
"""
import os
import shutil
import tempfile
from datetime import datetime

from .v8 import container_reader
from .v8 import decoder as v8_decoder


def extract(src_file, *, db_path=None, dump_dir=None, temp_dir=None, keep_temp=False, options=None,
            workers=1):
    """Распаковывает файл 1С и (опционально) загружает результат в SQLite.

    :param src_file: путь к .cf/.cfe/.epf
    :param db_path: путь к целевой базе SQLite (None — не писать БД)
    :param dump_dir: каталог для распакованного дерева (стадия 3); None — не сохранять
    :param temp_dir: рабочий каталог для стадий 0-1; None — временный каталог ОС
    :param keep_temp: не удалять рабочий каталог стадий 0-1
    :param options: словарь опций декодера (prefix, auto_include и т.п.)
    :param workers: число процессов стадии 3 (1 — последовательно)
    :return: словарь со статистикой
    """
    src_file = os.path.abspath(src_file)
    if not os.path.isfile(src_file):
        raise FileNotFoundError(src_file)

    if options is None:
        options = {}

    own_temp = temp_dir is None
    if own_temp:
        temp_dir = tempfile.mkdtemp(prefix='confdb_')
    stage0 = os.path.join(temp_dir, 'decode_stage_0')
    stage1 = os.path.join(temp_dir, 'decode_stage_1')

    stats = {'src': src_file, 'temp_dir': temp_dir, 'dump_dir': dump_dir, 'db': db_path}
    begin = datetime.now()
    try:
        print(f'Стадия 0: читаем контейнеры {src_file}')
        container_reader.extract(src_file, stage0, deflate=False, recursive=False)

        print('Стадия 1: разжимаем файлы контейнеров')
        container_reader.decompress_and_extract(stage0, stage1)

        if dump_dir:
            dump_dir = os.path.abspath(dump_dir)
            stage3 = dump_dir
        else:
            stage3 = os.path.join(temp_dir, 'decode_stage_3')
        print(f'Стадия 3: декодируем метаданные в {stage3}')
        v8_decoder.decode(stage1, stage3, options=options, workers=workers)

        if db_path:
            # импорт здесь, чтобы не тянуть sqlite при работе без БД
            from .db.writer import write_db
            db_path = os.path.abspath(db_path)
            print(f'Пишем базу данных {db_path}')
            stats['db_rows'] = write_db(stage3, db_path, source_file=src_file,
                                        store_blobs=options.get('store_blobs', False),
                                        workers=workers)

        stats['dump_dir'] = dump_dir if dump_dir else None
    finally:
        if own_temp and not keep_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)
            stats['temp_dir'] = None

    stats['elapsed'] = str(datetime.now() - begin)
    print(f'Готово за {stats["elapsed"]}')
    return stats
