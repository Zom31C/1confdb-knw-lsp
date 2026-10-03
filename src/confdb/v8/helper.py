"""Вспомогательные функции чтения/записи промежуточных файлов конвейера распаковки.

Порт read-части v8unpack.helper (MIT). Пул процессов заменён последовательным выполнением.
"""
import atexit
import json
import os
import shutil
import time
import uuid
from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE

from .ext_exception import ExtException
from .json_container_decoder import JsonContainerDecoder, BigBase64
from . import progress


def long_path(path):
    """Добавляет префикс \\\\?\\ для длинных путей на Windows (обход MAX_PATH 260).
    
    На Windows максимальная длина пути ограничена 260 символами (MAX_PATH).
    Префикс \\\\?\\ позволяет работать с путями до ~32,767 символов.
    Префикс работает только с абсолютными путями.
    """
    if os.name != 'nt':
        return path
    # Префикс работает только с абсолютными путями
    path = os.path.abspath(path)
    # Не добавляем префикс, если он уже есть
    if path.startswith('\\\\?\\'):
        return path
    return '\\\\?\\' + path


def brace_file_read(path, file_name):
    _path = os.path.normpath(os.path.join(path, file_name))
    progress.note_read(_path)
    try:
        for code_page in ['utf-8-sig', 'windows-1251']:
            try:
                with open(long_path(_path), 'r', encoding=code_page) as file:
                    decoder = JsonContainerDecoder(src_dir=path, file_name=file_name)
                    data = decoder.decode_file(file)
                    return data
            except UnicodeDecodeError:
                continue
        raise ExtException(message=f'Unknown code page in file {file_name}')
    except (BigBase64, FileNotFoundError) as err:
        raise err from err
    except Exception as err:
        raise ExtException(parent=err, message='Ошибка чтения', detail=f'{err} в файле ({_path})')


def json_read(path, file_name):
    _path = os.path.normpath(os.path.join(path, file_name))
    progress.note_read(_path)
    try:
        with open(long_path(_path), 'r', encoding='utf-8') as file:
            return json.load(file)
    except FileNotFoundError as err:
        raise err
    except Exception as err:
        raise ExtException(message='Ошибка чтения', detail=f'{err} в файле ({_path})')


def json_write(data, path, file_name, indent=None):
    """Запись JSON в дамп.

    По умолчанию вывод компактный: `json.dumps` без отступов включает C-энкодер `_json`,
    тогда как `json.dump` и любой `indent` всегда идут через рекурсивный Python-энкодер.
    Замер на 405 МиБ заголовков УНФ: 12.98 с и 366 МиБ против 2.19 с и 127 МиБ.
    `indent=2` возвращает побайтовое совпадение с дампом v8unpack (опция `--dump-indent`)
    — его и применяют для сверки эквивалентности декодера.
    """
    _path = os.path.normpath(os.path.join(path, file_name))
    makedirs(path, exist_ok=True)
    try:
        with open(long_path(_path), 'w', encoding='utf-8') as file:
            file.write(json.dumps(data, ensure_ascii=False, indent=indent))
    except Exception as err:
        raise ExtException(message='Ошибка записи', detail=f'{err} в файле ({_path})')


# --- поток заголовков: передача <Класс>.json записи БД мимо дампа ---
#
# Заголовок объекта и его uuid — единственное, что запись БД достаёт из 47 тысяч
# мелких файлов дампа (УНФ: 4.7 с на <Класс>.json плюс 1.5 с на <Класс>.id.json),
# и единственное, что она сериализует повторно в meta_object.header_json (1.8 с).
# Когда нужна база, а дерево дампа — нет, оба файла не пишутся: их содержимое
# уходит потоком по одному файлу на процесс, который запись читает целиком.
_SINKS = {}


def _sink_handle(sink_dir):
    handle = _SINKS.get(sink_dir)
    if handle is None:
        makedirs(sink_dir, exist_ok=True)
        handle = open(long_path(os.path.join(sink_dir, f'headers-{os.getpid()}.pkl')), 'wb')
        _SINKS[sink_dir] = handle
    return handle


def sink_put(sink_dir, dest_path, file_name, obj_uuid, header):
    """Пишет в поток заголовок объекта и его uuid — то, что иначе ушло бы в дамп.

    :param dest_path: путь объекта от корня дампа (у корневого объекта пустой)
    :param header: заголовок ПОСЛЕ decode_ids() — без uuid, как в <Класс>.json
    """
    import pickle
    text = json.dumps(header, ensure_ascii=False)
    rel = dest_path.replace(os.sep, '/') if dest_path else ''
    pickle.dump((rel, file_name, obj_uuid, text), _sink_handle(sink_dir), protocol=4)


def sink_read(sink_dir):
    """{путь объекта от корня дампа через '/': (имя класса, uuid, текст заголовка)}."""
    import pickle
    result = {}
    for file_name in sorted(os.listdir(long_path(sink_dir))):
        if not file_name.endswith('.pkl'):
            continue
        with open(long_path(os.path.join(sink_dir, file_name)), 'rb') as file:
            while True:
                try:
                    rel, stem, obj_uuid, text = pickle.load(file)
                except EOFError:
                    break
                result[rel] = (stem, obj_uuid, text)
    return result


def sink_close():
    """Закрывает файлы потока; вызывается и по завершении процесса (atexit)."""
    for handle in _SINKS.values():
        try:
            handle.close()
        except OSError:
            pass
    _SINKS.clear()


atexit.register(sink_close)


def txt_read(path, file_name, encoding='utf-8-sig'):
    try:
        return txt_read_detect_encoding(path, file_name, encoding=encoding)[0]
    except (FileNotFoundError, UnicodeDecodeError) as err:
        raise err from err
    except Exception as err:
        raise ExtException(parent=err, message='Ошибка чтения', detail=f'{err} в файле ({file_name})')


def txt_read_detect_encoding(path, file_name, encoding=None):
    _path = os.path.normpath(os.path.join(path, file_name))
    progress.note_read(_path)
    if encoding is None:
        encoding = detect_by_bom(_path, 'utf-8')
    with open(long_path(_path), 'r', encoding=encoding) as file:
        return file.read(), encoding


def txt_write(data, path, file_name, encoding='utf-8'):
    try:
        if data is None:
            return
        _path = os.path.normpath(os.path.join(path, file_name))
        makedirs(path, exist_ok=True)
        for i in range(3):
            try:
                with open(long_path(_path), 'w', encoding=encoding) as file:
                    file.write(data)
                return
            except PermissionError:
                time.sleep(0.5)
        raise PermissionError(_path)
    except Exception as err:
        raise ExtException(message='Ошибка записи файла', detail=f'{err} в файле {path}')


def bin_write(data, path, file_name):
    _path = os.path.normpath(os.path.join(path, file_name))
    makedirs(path, exist_ok=True)
    with open(long_path(_path), 'wb') as file:
        file.write(data)


def bin_read(path, file_name):
    _path = os.path.normpath(os.path.join(path, file_name))
    progress.note_read(_path)
    with open(long_path(_path), 'rb') as file:
        return file.read()


def decode_header(meta_obj, header: list, *, id_in_separate_file=True):
    obj = meta_obj.header
    try:
        obj['uuid'] = header[1][2]
        uuid.UUID(obj['uuid'])
    except (ValueError, IndexError):
        raise ValueError('Заголовок определен не верно')

    prefix = meta_obj.options.get('prefix', '')
    obj['name'] = str_decode(header[2])
    if prefix and obj['name'].startswith(prefix):
        obj['name'] = obj['name'][len(prefix):]
        header[2] = str_encode(obj['name'])
    obj['name2'] = {}
    count_locale = int(header[3][0])
    for i in range(count_locale):
        obj['name2'][str_decode(header[3][i * 2 + 1])] = str_decode(header[3][i * 2 + 2])
    obj['comment'] = str_decode(header[4])
    if id_in_separate_file:
        header[1][2] = 'в отдельном файле'


def clear_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    makedirs(path, exist_ok=True)


def str_encode(data: str) -> str:
    return f'"{data}"'


def str_decode(data: str) -> str:
    return data[1:-1]


def run_in_pool(method, list_args, pool=None, title=None, need_result=False):
    """Выполнение задач: последовательно или на пуле процессов (опция --workers).

    Сигнатура сохранена для совместимости с портированным кодом.
    """
    result = []
    if pool is not None:
        chunksize = max(1, len(list_args) // 64)
        for res in pool.map(method, list_args, chunksize=chunksize):
            if need_result and res:
                result.extend(res)
        return result
    for args in list_args:
        _res = method(args)
        if need_result and _res:
            result.extend(_res)
    return result


def list_merge(*args):
    result = []
    for lst in args:
        if lst:
            result.extend(lst)
    return result


def get_class_metadata_object(name):
    from .MetaDataObject import core, form, objects
    for mod in (objects, form, core):
        cls = getattr(mod, name, None)
        if isinstance(cls, type):
            return cls
    raise AttributeError(f'get_class_metadata_object: нет класса "{name}"')


def get_class(kls):
    try:
        parts = kls.split('.')
        module = ".".join(parts[:-1])
        m = __import__(module)
        for comp in parts[1:]:
            m = getattr(m, comp)
        return m
    except ImportError as e:
        # ошибки в классе  или нет файла
        raise ImportError(f'get_class({kls}: {str(e)}')
    except AttributeError as e:
        # Нет такого класса
        raise AttributeError(f'get_class({kls}: {str(e)}')
    except Exception as e:
        # ошибки в классе
        raise Exception(f'get_class({kls}: {str(e)}')


def detect_by_bom(path, default=None):
    boms = (
        ('utf-8-sig', BOM_UTF8),
        ('utf-32', BOM_UTF32_LE),
        ('utf-32', BOM_UTF32_BE),
        ('utf-16', BOM_UTF16_LE),
        ('utf-16', BOM_UTF16_BE),
    )

    with open(long_path(path), 'rb') as f:
        raw = f.read(4)  # will read less if the file is smaller
    for enc, bom in boms:
        if raw.startswith(bom):
            return enc
    return default


def str_time(value, _format='%H:%M:%S.%f'):
    return value.strftime(_format)


def get_extension_from_comment(comment: str) -> str:
    comment = comment.strip()
    res = 'bin'
    if comment:
        ext = comment.split(" ")[-1]
        if len(ext) < 6 and ext.isalnum():
            return ext
    return res


def makedirs(name, exist_ok=False):
    for i in range(3):
        try:
            os.makedirs(long_path(name), exist_ok=exist_ok)
            return
        except PermissionError:
            time.sleep(0.5)
    raise PermissionError(name)


class FuckingBrackets(ExtException):
    pass


def get_options_param(options, param_name, default=None):
    try:
        return options[param_name]
    except (KeyError, TypeError):
        return default


def set_options_param(options, param_name, param_value):
    if options is None:
        options = {}
    options[param_name] = param_value
    return options


def calc_offset(counters, raw_data):
    # counters - позиции указывающие на счетчики, если не 0 то за ним идет столько записей размера size
    #  [(3, 1), (1, 0)] (смещение относительно предыдущей записи, количество записей в единице)
    index = 0
    for counter_index, size in counters:
        index += counter_index
        if size:
            try:
                value = int(raw_data[index])
            except Exception as err:
                raise ExtException(
                    message='bad offset',
                    detail=f'{counter_index}={index}',
                    dump={'counters': counters, 'value': raw_data[index]}
                )
            index += value * size
    return index
