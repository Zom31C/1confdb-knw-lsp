"""Индикатор прогресса: одна строка в консоли, обновление символом \\r.

Процент считается по обработанным байтам относительно известного объёма
(стадия 1 — размер файлов контейнеров, стадия 3 — размер файлов stage_1).
В нетерминальном выводе (пайпы, редиректы) индикатор молчит.

Многопроцессный режим: дочерние процессы увеличивают общий счётчик
(multiprocessing.Value), родительский поток-насос рисует строку.
"""
import os
import sys
import threading
import time

CURRENT = None


class Progress:
    def __init__(self, title, total=0, enabled=None, stream=None, shared=None):
        self.title = title
        self.total = total
        self.done = 0
        self.stream = stream if stream is not None else sys.stdout
        if enabled is None:
            enabled = bool(getattr(self.stream, 'isatty', None) and self.stream.isatty())
        self.enabled = enabled
        self._shared = shared
        self._last = 0.0
        self._width = 0
        self._lock = threading.Lock()
        self._pump = None
        if shared is not None and self.enabled:
            self._stop = threading.Event()
            self._pump = threading.Thread(target=self._pump_loop, daemon=True)
            self._pump.start()

    def _pump_loop(self):
        while not self._stop.wait(0.3):
            with self._lock:
                self._render('')

    def update(self, inc=0, detail=''):
        if self._shared is not None:
            with self._shared.get_lock():
                self._shared.value += inc
            return
        with self._lock:
            self.done += inc
            if not self.enabled:
                return
            now = time.monotonic()
            if self.total and self.done < self.total and now - self._last < 0.15:
                return
            self._last = now
            self._render(detail)

    def _render(self, detail):
        done = self._shared.value if self._shared is not None else self.done
        if self.total:
            percent = min(100, done * 100 // self.total)
            line = f'{self.title} {percent:3}% {detail}'
        else:
            line = f'{self.title} {detail}'
        if len(line) > 79:
            line = line[:79]
        if len(line) < self._width:
            line += ' ' * (self._width - len(line))
        self._width = len(line)
        self.stream.write('\r' + line)
        self.stream.flush()

    def finish(self, detail=''):
        if self._pump is not None:
            self._stop.set()
            self._pump.join()
            self._pump = None
        if not self.enabled:
            return
        with self._lock:
            if self.total:
                self.done = self.total
            self._last = 0.0
            self._render(detail)
            self.stream.write('\n')
            self.stream.flush()
            self._width = 0


def start(title, total=0, shared=None):
    """Создаёт и активирует индикатор; finish() деактивирует.

    :param shared: multiprocessing.Value для многопроцессного режима
    """
    global CURRENT
    CURRENT = Progress(title, total, shared=shared)
    return CURRENT


def attach_shared(shared):
    """Инициализатор дочерних процессов: счётчик без отрисовки."""
    global CURRENT
    CURRENT = Progress('', 0, enabled=False, shared=shared)


def note_read(path):
    """Учитывает прочитанный файл в активном индикаторе."""
    if CURRENT is not None:
        try:
            CURRENT.update(os.path.getsize(path))
        except OSError:
            pass


def finish():
    global CURRENT
    if CURRENT is not None:
        CURRENT.finish()
        CURRENT = None
