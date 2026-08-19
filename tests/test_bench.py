"""Тесты бенчмарка: свойства нелинейного сэмпла объектов."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.bench import sample_tasks  # noqa: E402

TYPES = ['Catalog', 'Document', 'CatalogForm', 'Report', 'CommonModule']


def _tasks(count):
    return [(TYPES[i % len(TYPES)], (f'uuid{i}',)) for i in range(count)]


def test_small_sample_returns_all():
    tasks = _tasks(50)
    assert sample_tasks(tasks, 1000) == tasks


def test_size_cap_and_distribution():
    tasks = _tasks(10000)
    picked = sample_tasks(tasks, 1000)
    idx = {t: i for i, t in enumerate(tasks)}
    pos = sorted(idx[t] for t in picked)
    assert len(pos) == len(picked)
    # охват всего списка, а не головы
    assert pos[0] == 0 and pos[-1] > 9000
    gaps = [b - a for a, b in zip(pos, pos[1:])]
    assert max(gaps) <= 2 * (10000 // 1000) + 2


def test_type_coverage():
    tasks = _tasks(10000)
    picked = sample_tasks(tasks, 10)
    assert {t[0] for t in picked} == set(TYPES)
