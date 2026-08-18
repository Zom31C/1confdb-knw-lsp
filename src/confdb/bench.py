"""Бенчмарк автонастройки числа процессов под железо пользователя.

Стадии 0/1 выполняются один раз, затем нелинейный сэмпл объектов верхнего
уровня (шаг по всему списку + покрытие всех типов объектов — чтобы задеть
и лёгкие справочники, и тяжёлые формы/отчёты) прогоняется через стадию 3
и запись БД при разных числах процессов. Лучший вариант сохраняется в
~/.confdb/config.json (ключ 'bench') и подставляется по умолчанию.
"""
import os
import shutil
import tempfile
import time
from datetime import datetime

from . import config as user_config
from .db.writer import write_db
from .v8 import container_reader
from .v8 import helper
from .v8.decoder import Decoder


def sample_tasks(tasks, size):
    """Нелинейный сэмпл: шаг по всему списку + первый представитель каждого типа."""
    tasks = list(tasks)
    if len(tasks) <= size:
        return tasks
    picked = []
    seen = set()
    step = len(tasks) / size
    pos = 0.0
    while len(picked) < size:
        idx = int(pos)
        if idx not in seen:
            seen.add(idx)
            picked.append(tasks[idx])
        pos += step
    have_types = {t[0] for t in picked}
    for idx, task in enumerate(tasks):
        if task[0] not in have_types:
            have_types.add(task[0])
            seen.add(idx)
            picked.append(task)
    return picked


def _run_stage3(decoder, tasks, workers):
    pool = None
    if workers > 1:
        import multiprocessing
        ctx = multiprocessing.get_context('spawn')
        pool = ctx.Pool(workers)
    try:
        while tasks:
            tasks = helper.run_in_pool(Decoder.decode_include, tasks, pool,
                                       need_result=True)
    finally:
        if pool is not None:
            pool.close()
            pool.join()


def bench(src_file, *, sample=1000, candidates=None, store=True):
    """Подбирает число процессов; возвращает лучшее значение."""
    src_file = os.path.abspath(src_file)
    if not os.path.isfile(src_file):
        raise FileNotFoundError(src_file)
    if candidates is None:
        candidates = sorted({1, 2, 4, 8, os.cpu_count() or 1})

    temp_dir = tempfile.mkdtemp(prefix='confdb_bench_')
    results = {}
    try:
        stage0 = os.path.join(temp_dir, 'decode_stage_0')
        stage1 = os.path.join(temp_dir, 'decode_stage_1')
        print(f'Бенчмарк: стадии 0/1 по {src_file}')
        container_reader.extract(src_file, stage0, deflate=False, recursive=False)
        container_reader.decompress_and_extract(stage0, stage1)

        # как в v8.decoder.decode: рабочий каталог — последний контейнер стадии 1
        containers = sorted(os.listdir(stage1),
                            key=lambda x: int(x) if x.isdigit() else x)
        stage1_inner = os.path.join(stage1, containers[-1])

        for workers in candidates:
            stage3 = os.path.join(temp_dir, f'decode3_w{workers}')
            db_path = os.path.join(temp_dir, f'bench_w{workers}.db')
            options = {}
            t0 = time.time()
            decoder = Decoder.detect_version(stage1_inner, options=options)
            tasks = sample_tasks(decoder.decode(stage1_inner, stage3), sample)
            _run_stage3(decoder, tasks, workers)
            t_decode = time.time() - t0
            t1 = time.time()
            write_db(stage3, db_path, source_file=src_file, workers=workers)
            t_write = time.time() - t1
            results[workers] = (round(t_decode, 1), round(t_write, 1))
            print(f' workers={workers:2}: стадия 3 {t_decode:6.1f} c, '
                  f'запись БД {t_write:6.1f} c, итого {t_decode + t_write:6.1f} c')
            shutil.rmtree(stage3, ignore_errors=True)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    best = min(results, key=lambda w: sum(results[w]))
    if store:
        cfg = user_config.load_config()
        cfg['bench'] = {
            'workers': best,
            'date': datetime.now().isoformat(timespec='seconds'),
            'sample': sample,
            'src': src_file,
            'times': {str(w): list(v) for w, v in sorted(results.items())},
        }
        cfg.setdefault('options', {})['workers'] = best
        user_config.save_config(cfg)
        print(f'Рекомендовано процессов: {best} — сохранено в {user_config.CONFIG_PATH}')
    return best
