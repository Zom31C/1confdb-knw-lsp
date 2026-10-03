"""Конвейер: cf/cfe/epf → распакованные файлы → база данных SQLite.

Стадии повторяют поток v8unpack (только распаковка):
  0 — чтение внешних контейнеров (32/64-бит) в файлы как есть;
  1 — inflate (raw deflate) + рекурсивные вложенные контейнеры;
  3 — декодирование метаданных: скобкофайлы → JSON, тексты модулей → .bsl.
(стадия 2 оригинала — конвертация в json отдельным прогоном — отключена и там)
"""
import concurrent.futures
import os
import shutil
import tempfile
from datetime import datetime

from .v8 import container_reader
from .v8 import decoder as v8_decoder


def remove_tree(root, workers=8):
    """Удаляет дерево параллельно, проглатывая ошибки, как shutil.rmtree(ignore_errors=True).

    Рабочий каталог стадий 0/1/3 — это ~125 тысяч файлов и 5.2 ГиБ, и однопоточный
    `shutil.rmtree` стоит на нём 13-24 с, которые целиком входят в `elapsed`.
    `os.unlink` освобождает GIL, поэтому пул потоков по каталогам даёт ~2.4x: замер
    `_tmp/prof_cleanup_ab.py` на реальном дереве — 23.8 с против 10.1 с при 8 потоках
    (16 потоков не быстрее). Стоимость удаления определяет число файлов, а не объём:
    4488 файлов на 979 МиБ удаляются за 0.32 с.

    :param root: каталог для удаления (несуществующий — не ошибка)
    :param workers: число потоков
    """
    if not os.path.isdir(root):
        return
    entries = [(dirpath, filenames) for dirpath, _dirnames, filenames in os.walk(root)]

    def clear(entry):
        dirpath, filenames = entry
        for name in filenames:
            try:
                os.unlink(os.path.join(dirpath, name))
            except OSError:
                pass

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(clear, entries))
    except Exception:  # noqa: BLE001 — удаление рабочего каталога не должно ронять извлечение
        shutil.rmtree(root, ignore_errors=True)
        return
    # каталоги снизу вверх: к этому моменту они пусты
    for dirpath, _filenames in sorted(entries, key=lambda e: e[0].count(os.sep), reverse=True):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)


def make_temp_dir(target=None):
    """Создаёт рабочий каталог стадий 0-1 на томе результата, а не в %TEMP%.

    Удаление ~125 тысяч файлов рабочего каталога на системном томе стоит 29.9 с и
    почти не распараллеливается (`remove_tree` даёт 1.04x), на несистемном — 23.8 с
    однопоточно и 10.1 с пулом потоков; прогон УНФ целиком 93 с против 67.5 с
    (замер `_tmp/prof_cleanup_ab.py`). Если каталог результата недоступен,
    остаётся временный каталог ОС.

    :param target: путь к базе или дампу, чей том предпочтителен (None — %TEMP%)
    :return: путь к созданному каталогу
    """
    base = os.path.dirname(os.path.abspath(target)) if target else None
    try:
        return tempfile.mkdtemp(prefix='confdb_', dir=base)
    except OSError:
        return tempfile.mkdtemp(prefix='confdb_')


def extract(src_file, *, db_path=None, dump_dir=None, temp_dir=None, keep_temp=False, options=None,
            workers=1, build_fts=True):
    """Распаковывает файл 1С и (опционально) загружает результат в SQLite.

    :param src_file: путь к .cf/.cfe/.epf
    :param db_path: путь к целевой базе SQLite (None — не писать БД)
    :param dump_dir: каталог для распакованного дерева (стадия 3); None — не сохранять
    :param temp_dir: рабочий каталог для стадий 0-1; None — на томе базы или дампа
        (`make_temp_dir`), а при его недоступности — временный каталог ОС
    :param keep_temp: не удалять рабочий каталог стадий 0-1
    :param options: словарь опций декодера (prefix, auto_include и т.п.)
    :param workers: число процессов стадии 3 (1 — последовательно)
    :param build_fts: строить FTS5-индекс по телам методов (половина времени
        записи БД; без него база рабочая, индекс собирается позже — `confdb fts`)
    :return: словарь со статистикой
    """
    src_file = os.path.abspath(src_file)
    if not os.path.isfile(src_file):
        raise FileNotFoundError(src_file)

    if options is None:
        options = {}

    own_temp = temp_dir is None
    if own_temp:
        temp_dir = make_temp_dir(db_path or dump_dir)
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

        headers_dir = None
        decode_options = options
        if db_path:
            # заголовки объектов уходят записи БД потоком (helper.sink_put), минуя
            # 47 тысяч мелких файлов дампа; само дерево остаётся полным, только
            # если его просили сохранить
            headers_dir = os.path.join(temp_dir, 'headers')
            decode_options = dict(options)
            decode_options['header_sink'] = headers_dir
            decode_options['dump_headers'] = bool(dump_dir)
        print(f'Стадия 3: декодируем метаданные в {stage3}')
        v8_decoder.decode(stage1, stage3, options=decode_options, workers=workers)

        if db_path:
            # импорт здесь, чтобы не тянуть sqlite при работе без БД
            from .db.writer import write_db
            db_path = os.path.abspath(db_path)
            print(f'Пишем базу данных {db_path}')
            stats['db_rows'] = write_db(stage3, db_path, source_file=src_file,
                                        store_blobs=options.get('store_blobs', False),
                                        workers=workers, build_fts=build_fts,
                                        headers_dir=headers_dir)

        stats['dump_dir'] = dump_dir if dump_dir else None
    finally:
        if own_temp and not keep_temp:
            remove_tree(temp_dir, workers=max(workers, 4))
            stats['temp_dir'] = None

    stats['elapsed'] = str(datetime.now() - begin)
    print(f'Готово за {stats["elapsed"]}')
    return stats
