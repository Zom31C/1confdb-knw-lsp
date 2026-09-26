"""Консольный интерфейс confdb (только стандартная библиотека).

Меню с обновлением экрана: извлечение конфигурации, SQL-запросы к базе,
проверка запросов СКД, запуск MCP-сервера 1confdb-knw (одна база,
несколько баз или именованная группа баз).
Недавние пути .cf/.sqlite, опции и группы баз запоминаются
в ~/.confdb/config.json.
Запуск: confdb-ui.bat (или python -m confdb.tui)
"""
import glob
import json
import os
import sqlite3
import subprocess
import sys
import threading

from . import __version__
from .config import bench_workers
from .config import load_config as _load_config
from .config import save_config as _save_config
from .extract import extract

PRESETS = [
    ('Состав конфигурации',
     'SELECT type, COUNT(*) AS cnt FROM meta_object GROUP BY type ORDER BY cnt DESC'),
    ('Объекты по имени',
     "SELECT path, type, name, uuid FROM meta_object WHERE name LIKE '%{имя}%' LIMIT 50"),
    ('Модули объекта',
     "SELECT m.code_name, LENGTH(m.body) AS len FROM module m "
     "JOIN meta_object o ON o.id=m.object_id WHERE o.path='{путь}'"),
    ('Дети объекта',
     "SELECT path, type, name FROM meta_object "
     "WHERE parent_id=(SELECT id FROM meta_object WHERE path='{путь}')"),
    ('Иерархия до корня',
     "WITH RECURSIVE up(p) AS ("
     "SELECT parent_id FROM meta_object WHERE path='{путь}' "
     "UNION ALL SELECT m.parent_id FROM meta_object m JOIN up ON m.id=up.p WHERE up.p IS NOT NULL) "
     "SELECT COALESCE((SELECT path FROM meta_object WHERE id=up.p), '<корень>') AS parent FROM up"),
    ('Текст модуля',
     "SELECT m.body FROM module m JOIN meta_object o ON o.id=m.object_id "
     "WHERE o.path='{путь}' AND m.code_name='{модуль}'"),
    ('Методы объекта',
     "SELECT mt.kind, mt.name, mt.signature, mt.directives, mt.is_export, mt.description "
     "FROM method mt JOIN module m ON m.id=mt.module_id "
     "JOIN meta_object o ON o.id=m.object_id "
     "WHERE o.path='{путь}' ORDER BY m.id, mt.ord"),
    ('Тело метода',
     "SELECT mt.body FROM method mt JOIN module m ON m.id=mt.module_id "
     "JOIN meta_object o ON o.id=m.object_id "
     "WHERE o.path='{путь}' AND m.code_name='{модуль}' AND mt.name='{метод}'"),
    ('Дерево как в конфигураторе',
     "WITH RECURSIVE tree(id, path, type, lvl, sort) AS ("
     "SELECT id, path, type, 0, printf('%08d', COALESCE(ord, 0)) FROM meta_object "
     "WHERE parent_id IS NULL "
     "UNION ALL SELECT o.id, o.path, o.type, t.lvl+1, t.sort || printf('%08d', COALESCE(o.ord, 0)) "
     "FROM meta_object o JOIN tree t ON o.parent_id=t.id) "
     "SELECT substr('................', 1, lvl*2) || type || ' ' || name FROM tree "
     "ORDER BY sort LIMIT 200"),
    ('Запросы СКД макетов',
     "SELECT o.path, q.query FROM skd_query q "
     "JOIN meta_object o ON o.id=q.object_id "
     "WHERE instr(o.path, '{имя}') > 0 ORDER BY q.object_id, q.ord LIMIT 20"),
    ('Реквизиты объекта',
     "SELECT a.name, a.type_str FROM meta_attribute a "
     "JOIN meta_object o ON o.id=a.object_id "
     "WHERE o.path='{путь}' ORDER BY a.ord"),
    ('Связи реквизитов',
     "SELECT a.name, r.uuid, COALESCE(o.path, '<абстрактный>') FROM attribute_ref r "
     "JOIN meta_attribute a ON a.id=r.attribute_id "
     "JOIN meta_object v ON v.id=a.object_id LEFT JOIN meta_object o ON o.id=r.object_id "
     "WHERE v.path='{путь}' ORDER BY a.ord, r.ord"),
]

MAX_CELL = 60
MAX_ROWS = 100


def _cls():
    os.system('cls' if os.name == 'nt' else 'clear')


def _ask(prompt, default=''):
    text = input(f'{prompt} [{default}]: ').strip()
    return text or default


def _unquote(text):
    """Убирает окружающие двойные кавычки («копировать как путь» в Explorer)."""
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _ask_path(prompt, default=''):
    return _unquote(_ask(prompt, default))


def _yes_no(prompt, default=False):
    suffix = 'да/Нет' if not default else 'Да/нет'
    answer = input(f'{prompt} ({suffix}) [Enter]: ').strip().lower()
    if not answer:
        return default
    return answer in ('да', 'д', 'y', 'yes', '1')


def _out_defaults(src):
    base = os.path.splitext(os.path.basename(src))[0]
    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '_out'))
    return os.path.join(out_dir, base + '.sqlite'), os.path.join(out_dir, base)


def _remember(items, value, limit=6):
    return [value] + [x for x in items if x != value][:limit - 1]


def _cf_candidates():
    roots = [os.getcwd(),
             os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))]
    found = []
    for root in dict.fromkeys(roots):
        for pattern in ('*.cf', '*.cfe', '*.epf', os.path.join('cf', '*.cf'), os.path.join('cf', '*.cfe')):
            found += glob.glob(os.path.join(root, pattern))
    return sorted({os.path.abspath(p) for p in found})


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        return f'<BLOB {len(value)} байт>'
    text = str(value).replace('\n', ' ')
    if len(text) > MAX_CELL:
        return text[:MAX_CELL - 3] + '...'
    return text


def load_groups(config):
    """Группы баз из конфига; старый ключ 'packs' читается как группы."""
    raw = config.get('groups')
    if raw is None:
        raw = config.get('packs') or {}
    if not isinstance(raw, dict):
        return {}
    return {str(name): [str(p) for p in dbs]
            for name, dbs in raw.items()
            if isinstance(dbs, (list, tuple))}


def replace_server_dbs(server, paths):
    """Заменяет открытые базы запущенного сервера на указанный набор.

    Всё открытое закрывается и открывается заново — свежие соединения,
    в том числе после пересборки баз. Возвращает (алиасы, ошибки).
    """
    for alias in list(server.dbs):
        server.close_db(alias)
    opened, errors = [], []
    for path in paths:
        try:
            opened.append(server.open_db(path, activate=False))
        except ValueError as err:
            errors.append(str(err))
    return opened, errors


def _bsl_proxy_args():
    """Аргументы прокси BSL при запуске из TUI (пути берутся из конфига)."""
    from types import SimpleNamespace
    return SimpleNamespace(lsp_workspace=None, bsl_jar=None, java=None)


class Tui:
    def __init__(self):
        config = _load_config()
        self.recent_src = config.get('recent_src', [])
        self.recent_db = config.get('recent_db', [])
        self.db_dirs = config.get('db_dirs', [])
        self.last_db = config.get('last_db', '')
        opts = config.get('options', {})
        self.src = opts.get('src', '')
        self.db = opts.get('db', '')
        self.dump = opts.get('dump', '')
        self.temp = opts.get('temp', '')
        self.prefix = opts.get('prefix', '')
        self.keep_temp = bool(opts.get('keep_temp'))
        self.store_blobs = bool(opts.get('store_blobs'))
        self.skip_errors = bool(opts.get('skip_errors'))
        self.workers = int(opts.get('workers') or bench_workers() or 1)
        self.groups = load_groups(config)

    def _save(self):
        # сливаем с текущим конфигом, чтобы не затереть чужие ключи
        # (bench, lsp_workspace и т.п.)
        config = _load_config()
        config.update({
            'recent_src': self.recent_src,
            'recent_db': self.recent_db,
            'db_dirs': self.db_dirs,
            'last_db': self.last_db,
            'groups': self.groups,
            'options': {
                'src': self.src, 'db': self.db, 'dump': self.dump, 'temp': self.temp,
                'prefix': self.prefix, 'keep_temp': self.keep_temp,
                'store_blobs': self.store_blobs, 'skip_errors': self.skip_errors,
                'workers': self.workers,
            },
        })
        _save_config(config)

    # ---------- главное меню ----------

    def run(self):
        while True:
            _cls()
            print(f'confdb {__version__} — экстрактор конфигурации 1С в SQLite')
            print('===========================================================')
            print(' 1. Запустить MCP-сервер (1confdb-knw)')
            print(' 2. Извлечь конфигурацию (.cf/.cfe/.epf -> SQLite)')
            print(' 3. Опции извлечения')
            print(' 4. Запросы к базе данных')
            print(' 5. Проверка запросов СКД')
            print(' 6. Группы баз: именованные наборы для MCP-сервера')
            print(' 0. Выход')
            if self.last_db:
                print(f'Последняя БД: {self.last_db}')
            choice = input('Выбор: ').strip()
            if choice == '1':
                self._run_mcp()
            elif choice == '2':
                self._extract_menu()
            elif choice == '3':
                self._options_menu()
            elif choice == '4':
                self._query_menu()
            elif choice == '5':
                self._check_queries()
            elif choice == '6':
                self._groups_menu()
            elif choice in ('0', 'q', 'exit', 'выход'):
                _cls()
                print('До свидания.')
                return
            else:
                print('Неизвестный пункт меню.')
                input('Нажмите Enter…')

    # ---------- выбор пути ----------

    def _pick_path(self, title, candidates, recent, allow_empty=False, empty_hint=''):
        entries = list(dict.fromkeys(list(candidates) + [r for r in recent if os.path.isfile(r)]))
        while True:
            _cls()
            print(title)
            for i, path in enumerate(entries, 1):
                print(f' {i}. {path}')
            print(' p. Указать путь вручную (или просто наберите путь)')
            if allow_empty:
                print(f' e. Пусто ({empty_hint})')
            print(' 0. Отмена')
            choice = input('Выбор: ').strip()
            if not choice:
                continue
            if choice == '0':
                return None
            if choice == 'e' and allow_empty:
                return ''
            if choice == 'p':
                return _ask_path('Путь')
            if choice.isdigit():
                if 1 <= int(choice) <= len(entries):
                    return entries[int(choice) - 1]
                print('Нет такого пункта.')
                continue
            # всё, что не номер и не команда, — путь, набранный вручную
            return _unquote(choice)

    def _db_candidates(self):
        roots = [os.getcwd(),
                 os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')),
                 os.path.abspath(os.path.join(os.path.dirname(__file__),
                                              '..', '..', '_out'))]
        roots += [d for d in self.db_dirs if os.path.isdir(d)]
        found = []
        for root in dict.fromkeys(roots):
            for pattern in ('*.db', '*.sqlite'):
                found += glob.glob(os.path.join(root, pattern))
        return sorted({os.path.abspath(p) for p in found if os.path.isfile(p)})

    def _pick_db(self, title, candidates=(), allow_empty=False, empty_hint=''):
        while True:
            recent = [r for r in ([self.last_db] + self.recent_db)
                      if r and os.path.isfile(r)]
            entries = list(dict.fromkeys(
                list(candidates) + self._db_candidates() + recent))
            _cls()
            print(title)
            for i, path in enumerate(entries, 1):
                print(f' {i}. {path}')
            print(' p. Указать путь вручную (или просто наберите путь)')
            print(' k. Добавить каталог с базами (его файлы появятся в списке)')
            if allow_empty:
                print(f' e. Пусто ({empty_hint})')
            print(' 0. Отмена')
            choice = input('Выбор: ').strip()
            if not choice:
                continue
            if choice == '0':
                return None
            if choice == 'e' and allow_empty:
                return ''
            if choice == 'p':
                return _ask_path('Путь к базе')
            if choice == 'k':
                directory = _ask_path('Каталог с базами')
                if directory and os.path.isdir(directory):
                    directory = os.path.abspath(directory)
                    if directory not in self.db_dirs:
                        self.db_dirs.append(directory)
                        self._save()
                else:
                    print('Нет такого каталога.')
                    input('Нажмите Enter…')
                continue
            if choice.isdigit():
                if 1 <= int(choice) <= len(entries):
                    return entries[int(choice) - 1]
                print('Нет такого пункта.')
                continue
            # всё, что не номер и не команда, — путь, набранный вручную;
            # файла может ещё не быть (сюда же задают базу для извлечения)
            return _unquote(choice)

    # ---------- извлечение ----------

    def _extract_menu(self):
        while True:
            _cls()
            print('--- Извлечение ---')
            print(f' 1. Файл 1С:      {self.src or "<не задан>"}')
            print(f' 2. База SQLite:  {self.db or "<не писать>"}')
            print(f' 3. Дамп дерева:  {self.dump or "<не сохранять>"}')
            print(' 4. Запустить извлечение')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                self._save()
                return
            if choice == '1':
                picked = self._pick_path('Файл конфигурации 1С (.cf/.cfe/.epf)',
                                         _cf_candidates(), self.recent_src)
                if picked:
                    self.src = picked
            elif choice == '2':
                candidates = [_out_defaults(self.src)[0]] if self.src else []
                picked = self._pick_db('База SQLite', candidates=candidates,
                                       allow_empty=True, empty_hint='не писать')
                if picked is not None:
                    self.db = picked
            elif choice == '3':
                candidates = [_out_defaults(self.src)[1]] if self.src else []
                picked = self._pick_path('Каталог дампа дерева', candidates, [],
                                         allow_empty=True, empty_hint='не сохранять')
                if picked is not None:
                    self.dump = picked
            elif choice == '4':
                self._run_extract()
            else:
                print('Неизвестный пункт меню.')
                input('Нажмите Enter…')

    def _run_extract(self):
        if not self.src or not os.path.isfile(self.src):
            print('Ошибка: укажите существующий файл 1С (пункт 1).')
            input('Нажмите Enter…')
            return
        if self.db and os.path.isdir(self.db):
            base = os.path.splitext(os.path.basename(self.src))[0]
            self.db = os.path.join(self.db, base + '.sqlite')
            print(f'Путь БД — каталог, файл будет создан как: {self.db}')
        if not self.db and not self.dump:
            db_def, dump_def = _out_defaults(self.src)
            print(f'Не заданы ни БД, ни дамп. По умолчанию: БД={db_def}')
            if _yes_no('Использовать значения по умолчанию?', True):
                self.db, self.dump = db_def, dump_def
            else:
                return
        for out in (self.db, self.dump):
            if out:
                os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

        options = {'store_blobs': self.store_blobs}
        if self.prefix:
            options['prefix'] = self.prefix
        if self.skip_errors:
            options['skip_errors'] = True
        _cls()
        try:
            stats = extract(
                self.src,
                db_path=self.db or None,
                dump_dir=self.dump or None,
                temp_dir=self.temp or None,
                keep_temp=self.keep_temp,
                options=options,
                workers=self.workers,
            )
        except Exception as err:
            print(f'Ошибка извлечения: {err}')
            input('Нажмите Enter…')
            return
        if stats.get('db'):
            self.last_db = stats['db']
        self.recent_src = _remember(self.recent_src, self.src)
        if self.db:
            self.recent_db = _remember(self.recent_db, self.db)
        self._save()
        print(f'Готово за {stats["elapsed"]}. '
              f'Объектов/модулей/файлов: {stats.get("db_rows", "дамп без БД")}')
        if self.last_db and _yes_no('Открыть запросы к полученной базе?', True):
            self._query_menu(self.last_db)
        input('Нажмите Enter…')

    # ---------- опции ----------

    def _options_menu(self):
        while True:
            _cls()
            print('--- Опции извлечения ---')
            print(f' 1. Число процессов стадии 3: {self.workers}')
            print(f' 2. Рабочий каталог стадий 0-1: {self.temp or "<temp ОС>"}')
            print(f' 3. Префикс имён для снятия:   {self.prefix or "<нет>"}')
            print(f' 4. Не удалять рабочий каталог: {"да" if self.keep_temp else "нет"}')
            print(f' 5. Хранить бинарники (BLOB):  {"да" if self.store_blobs else "нет"}')
            print(f' 6. Пропускать ошибки объектов (--skip-errors): '
                  f'{"да" if self.skip_errors else "нет"}')
            print(' 7. Бенчмарк: подобрать число процессов под железо')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                self._save()
                return
            if choice == '1':
                value = _ask('Число процессов (1 — последовательно)', str(self.workers))
                if value.isdigit() and int(value) >= 1:
                    self.workers = int(value)
                else:
                    print('Нужно целое число >= 1.')
                    input('Нажмите Enter…')
            elif choice == '2':
                self.temp = _ask_path('Рабочий каталог (пусто — temp ОС)', self.temp)
            elif choice == '3':
                self.prefix = _ask('Префикс (пусто — нет)', self.prefix)
            elif choice == '4':
                self.keep_temp = _yes_no('Не удалять рабочий каталог?', self.keep_temp)
            elif choice == '5':
                self.store_blobs = _yes_no('Хранить бинарники в БД (BLOB)?', self.store_blobs)
            elif choice == '6':
                self.skip_errors = _yes_no(
                    'Пропускать объекты с ошибками декодирования (дамп/база будут неполными)?',
                    self.skip_errors)
            elif choice == '7':
                self._run_bench()
            else:
                print('Неизвестный пункт меню.')
                input('Нажмите Enter…')

    # ---------- MCP-сервер ----------

    def _run_mcp(self):
        dbs, title = self._pick_launch_set()
        if not dbs:
            return
        self._launch_mcp(dbs, title)

    def _pick_launch_set(self):
        """Что открыть: группа баз либо одна/несколько баз вручную."""
        while True:
            _cls()
            print('--- MCP-сервер 1confdb-knw: что открыть ---')
            names = list(self.groups)
            if names:
                print('Группы баз (создать/изменить — пункт 6 главного меню):')
                for i, name in enumerate(names, 1):
                    shown = ', '.join(os.path.basename(d)
                                      for d in self.groups[name])
                    print(f' {i}. {name}: {shown}')
            else:
                print('Групп баз пока нет — создать можно в пункте 6.')
            print()
            print(' m. Выбрать базы вручную (одну или несколько)')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                return [], ''
            if choice == 'm':
                dbs = self._multi_select(
                    'Базы для MCP-сервера (первая — основная)',
                    self._db_pool(), ordered=True)
                return (dbs or []), 'выбранные базы'
            if choice.isdigit() and 1 <= int(choice) <= len(names):
                name = names[int(choice) - 1]
                raw = self.groups[name]
                dbs = [d for d in raw if os.path.isfile(d)]
                for d in raw:
                    if not os.path.isfile(d):
                        print(f'Не найдена база (пропущена): {d}')
                if not dbs:
                    print('В группе нет доступных баз.')
                    input('Нажмите Enter…')
                    continue
                return dbs, f'группа «{name}»'
            print('Неизвестный пункт.')
            input('Нажмите Enter…')

    # ---------- группы баз ----------

    def _db_pool(self):
        recent = [r for r in ([self.last_db] + self.recent_db)
                  if r and os.path.isfile(r)]
        return self._db_candidates() + recent

    def _groups_menu(self):
        while True:
            _cls()
            print('--- Группы баз для MCP-сервера ---')
            names = list(self.groups)
            if names:
                for i, name in enumerate(names, 1):
                    dbs = self.groups[name]
                    shown = ', '.join(os.path.basename(d) for d in dbs)
                    print(f' {i}. {name}: {shown}')
            else:
                print(' Групп пока нет.')
            print()
            print('База может входить в несколько групп; запуск — пункт 1.')
            print(' n. Создать/переписать группу')
            print(' d. Удалить группу')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                self._save()
                return
            if choice == 'n':
                self._edit_group()
            elif choice == 'd':
                self._delete_group()
            else:
                print('Неизвестный пункт.')
                input('Нажмите Enter…')

    def _edit_group(self):
        name = _ask('Имя группы')
        if not name:
            return
        dbs = self._multi_select(f'Базы группы «{name}» (первая — основная)',
                                 self._db_pool(),
                                 selected=self.groups.get(name, []), ordered=True)
        if dbs is None:
            return
        if dbs:
            self.groups[name] = dbs
        else:
            self.groups.pop(name, None)
        self._save()

    def _delete_group(self):
        names = list(self.groups)
        if not names:
            print('Групп нет.')
            input('Нажмите Enter…')
            return
        for i, n in enumerate(names, 1):
            print(f' {i}. {n}')
        pick = _ask('Номер группы для удаления')
        if pick.isdigit() and 1 <= int(pick) <= len(names):
            name = names[int(pick) - 1]
            if _yes_no(f'Удалить группу «{name}»?', False):
                self.groups.pop(name, None)
                self._save()

    # ---------- выбор нескольких файлов ----------

    def _multi_select(self, title, pool, selected=None, ordered=False):
        selected = list(selected or [])
        pool = list(dict.fromkeys(pool))
        while True:
            _cls()
            print(title)
            print()
            print('Выбрано' + (' (первый — основной)' if ordered else '') + ':')
            if selected:
                for i, p in enumerate(selected, 1):
                    print(f'  {i}. {p}')
            else:
                print('  <пусто>')
            print()
            avail = [p for p in pool if p not in selected]
            print('Доступно:')
            if avail:
                for i, p in enumerate(avail, 1):
                    print(f'  {i}. {p}')
            else:
                print('  <нет>')
            print()
            cmds = 'Команды: номер — добавить; -N — убрать N-й'
            if ordered:
                cmds += '; *N — сделать N-й первым'
            cmds += '; Enter — готово; 0 — отмена'
            print(cmds)
            choice = input('> ').strip()
            if choice in ('0', 'q'):
                return None
            if choice == '':
                return selected
            if choice.startswith('-') and choice[1:].isdigit():
                n = int(choice[1:])
                if 1 <= n <= len(selected):
                    selected.pop(n - 1)
                continue
            if ordered and choice.startswith('*') and choice[1:].isdigit():
                n = int(choice[1:])
                if 1 <= n <= len(selected):
                    selected.insert(0, selected.pop(n - 1))
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(avail):
                selected.append(avail[int(choice) - 1])
                continue
            print('Непонятная команда.')
            input('Нажмите Enter…')

    # ---------- MCP: запуск выбранного набора баз ----------

    def _launch_mcp(self, dbs, title):
        while True:
            _cls()
            print(f'--- MCP-сервер: {title} ---')
            for i, d in enumerate(dbs, 1):
                print(f' {i}. {d}' + (' (основная)' if i == 1 else ''))
            print()
            print(' 1. stdio — клиент сам запускает процесс')
            print(' 2. Сеть (HTTP) + управление базами без перезапуска')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                return
            if choice == '1':
                args = ['-m', 'confdb.mcp_server'] + dbs
                _cls()
                print('Сервер работает по stdio; остановка — Ctrl+C.')
                print(f'  {{"command": "{sys.executable}", '
                      f'"args": {json.dumps(args, ensure_ascii=False)}}}')
                print()
                try:
                    subprocess.run([sys.executable] + args)
                except KeyboardInterrupt:
                    pass
                print('Сервер остановлен.')
                input('Нажмите Enter…')
                return
            if choice == '2':
                self._serve_interactive(dbs)
                return
            print('Неизвестный пункт.')
            input('Нажмите Enter…')

    def _serve_interactive(self, dbs):
        port = _ask('Порт', '8765')
        if not port.isdigit():
            print('Нужно целое число.')
            input('Нажмите Enter…')
            return
        from .mcp_server import McpServer, start_http_server, create_bsl_proxy
        bsl = create_bsl_proxy(_bsl_proxy_args(), dbs[0])
        server = McpServer(dbs, bsl=bsl)
        try:
            httpd, real_port = start_http_server(server, '127.0.0.1', int(port))
        except OSError as err:
            print(f'Не удалось занять порт {port}: {err}')
            input('Нажмите Enter…')
            if bsl is not None:
                bsl.stop()
            return
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _cls()
        print(f'Сервер слушает http://127.0.0.1:{real_port}/mcp '
              '(legacy SSE: /sse).')
        print('С другой машины — через SSH-туннель: '
              f'ssh -L {real_port}:127.0.0.1:{real_port} user@host')
        print('Конфигурация клиента: '
              f'{{"mcpServers": {{"1confdb-knw": '
              f'{{"url": "http://127.0.0.1:{real_port}/mcp"}}}}}}')
        print()
        try:
            self._server_control(server)
        finally:
            httpd.shutdown()
            httpd.server_close()
            if bsl is not None:
                bsl.stop()
        print('Сервер остановлен.')
        input('Нажмите Enter…')

    def _rebind_bsl(self, server, alias):
        """Перепривязывает BSL-шлюз к новой первой базе после перезагрузки."""
        if getattr(server, 'bsl', None) is None:
            return
        from .mcp_server import create_bsl_proxy
        new_bsl = create_bsl_proxy(_bsl_proxy_args(),
                                   server.dbs[alias]['path'])
        if new_bsl is not None:
            server.bsl.stop()
            server.bsl = new_bsl

    def _server_control(self, server):
        while True:
            _cls()
            print('--- Управление базами запущенного MCP-сервера ---')
            print(server.db_list())
            print()
            print(' 1. Сменить активную базу')
            print(' 2. Открыть ещё базу')
            print(' 3. Закрыть базу')
            print(' 4. Применить группу (заменить открытые базы)')
            print(' 0. Остановить сервер и выйти')
            choice = input('Выбор: ').strip()
            if choice == '0':
                return
            aliases = list(server.dbs)
            if choice == '1':
                if not aliases:
                    print('Нет открытых баз.')
                    input('Нажмите Enter…')
                    continue
                for i, a in enumerate(aliases, 1):
                    print(f'  {i}. {a}' + (' *' if a == server.active else ''))
                pick = _ask('Номер базы, которую сделать активной')
                if pick.isdigit() and 1 <= int(pick) <= len(aliases):
                    server.active = aliases[int(pick) - 1]
            elif choice == '2':
                path = self._pick_db('Открыть базу в запущенном сервере')
                if path:
                    try:
                        alias = server.open_db(path)
                        print(f'Открыта база: {alias}')
                    except ValueError as err:
                        print(f'Ошибка: {err}')
                    input('Нажмите Enter…')
            elif choice == '3':
                if not aliases:
                    print('Нет открытых баз.')
                    input('Нажмите Enter…')
                    continue
                for i, a in enumerate(aliases, 1):
                    print(f'  {i}. {a}')
                pick = _ask('Номер базы, которую закрыть')
                if pick.isdigit() and 1 <= int(pick) <= len(aliases):
                    try:
                        server.close_db(aliases[int(pick) - 1])
                    except ValueError as err:
                        print(f'Ошибка: {err}')
                    input('Нажмите Enter…')
            elif choice == '4':
                names = list(self.groups)
                if not names:
                    print('Групп нет — создать можно в пункте 6 главного меню.')
                    input('Нажмите Enter…')
                    continue
                for i, name in enumerate(names, 1):
                    print(f'  {i}. {name}')
                pick = _ask('Номер группы (открытые базы будут заменены)')
                if pick.isdigit() and 1 <= int(pick) <= len(names):
                    group = self.groups[names[int(pick) - 1]]
                    opened, errors = replace_server_dbs(server, group)
                    for err in errors:
                        print(f'Ошибка: {err}')
                    if opened:
                        print('Открыты базы: ' + ', '.join(opened))
                        self._rebind_bsl(server, opened[0])
                    else:
                        print('Сервер остался без баз — откройте вручную (пункт 2).')
                    input('Нажмите Enter…')
            else:
                print('Неизвестный пункт.')
                input('Нажмите Enter…')

    # ---------- бенчмарк ----------

    def _run_bench(self):
        if not self.src or not os.path.isfile(self.src):
            picked = self._pick_path('Файл конфигурации 1С для бенчмарка',
                                     _cf_candidates(), self.recent_src)
            if not picked:
                return
            self.src = picked
        _cls()
        print('Бенчмарк: прогон сэмпла объектов при разном числе процессов.')
        print('Стадии 0/1 выполняются один раз, затем сэмпл стадии 3 + запись БД.')
        print()
        from .bench import bench
        try:
            best = bench(self.src)
        except Exception as err:
            print(f'Ошибка бенчмарка: {err}')
            input('Нажмите Enter…')
            return
        self.workers = best
        self.recent_src = _remember(self.recent_src, self.src)
        self._save()
        input('Нажмите Enter…')

    # ---------- проверка СКД ----------

    def _check_queries(self):
        db = self._pick_db('Проверка запросов СКД: файл базы')
        if not db:
            return
        _cls()
        from .__main__ import run_check
        run_check(db)
        input('Нажмите Enter…')

    # ---------- запросы ----------

    def _query_menu(self, db=None):
        if not db:
            db = self._pick_db('Запросы к базе: файл базы')
            if not db:
                return
        while True:
            _cls()
            print(f'--- Запросы к {os.path.basename(db)} ---')
            for i, (name, _) in enumerate(PRESETS, 1):
                print(f' {i}. {name}')
            print(' s. Свой запрос')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                return
            if choice == 's':
                print('Введите SQL (одна строка, без точки с запятой):')
                sql = input('> ').strip()
            elif choice.isdigit() and 1 <= int(choice) <= len(PRESETS):
                sql = self._build_preset(PRESETS[int(choice) - 1])
                if sql is None:
                    continue
            else:
                print('Неизвестный пункт.')
                input('Нажмите Enter…')
                continue
            if sql:
                self._exec(db, sql)
                input('Нажмите Enter…')

    def _build_preset(self, preset):
        name, sql = preset
        params = set()
        for token in ('имя', 'путь', 'модуль', 'метод'):
            if '{' + token + '}' in sql:
                params.add(token)
        values = {}
        for token in sorted(params):
            default = {'путь': 'Catalog/Номенклатура', 'модуль': 'obj',
                       'имя': '', 'метод': ''}[token]
            values[token] = _ask(f'Параметр "{token}"', default)
        try:
            return sql.format(**values)
        except KeyError as err:
            print(f'Ошибка шаблона: {err}')
            return None

    def _exec(self, db, sql):
        try:
            conn = sqlite3.connect(db)
            try:
                cur = conn.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(MAX_ROWS) if columns else []
                more = cur.fetchone() is not None if columns else False
            finally:
                conn.close()
        except Exception as err:
            print(f'Ошибка запроса: {err}')
            return
        if not columns:
            print('Запрос выполнен (без результата для вывода).')
            return
        self._print_table(columns, rows)
        total = f'{len(rows)}+' if more else str(len(rows))
        print(f'Показано строк: {total}' + (f' (лимит {MAX_ROWS})' if more else ''))

    @staticmethod
    def _print_table(columns, rows):
        cells = [[_cell(v) for v in row] for row in rows]
        widths = [min(MAX_CELL, len(c)) for c in columns]
        for row in cells:
            for i, val in enumerate(row):
                widths[i] = min(MAX_CELL, max(widths[i], len(val)))

        def line(row):
            return ' | '.join(val[:w].ljust(w) for w, val in zip(widths, row))

        print(line([str(c) for c in columns]))
        print('-+-'.join('-' * w for w in widths))
        for row in cells:
            print(line(row))


def main():
    try:
        Tui().run()
    except (EOFError, KeyboardInterrupt):
        print()
        print('Прервано. До свидания.')
        sys.exit(0)


if __name__ == '__main__':
    main()
