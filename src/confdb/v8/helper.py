"""Вспомогательные функции чтения/записи промежуточных файлов конвейера распаковки.

Порт read-части v8unpack.helper (MIT). Пул процессов заменён последовательным выполнением.
"""
import json
import os
import shutil
import time
import uuid
from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE

from .ext_exception import ExtException
from .json_container_decoder import JsonContainerDecoder, BigBase64
from . import progress


def brace_file_read(path, file_name):
    _path = os.path.normpath(os.path.join(path, file_name))
    progress.note_read(_path)
    try:
        for code_page in ['utf-8-sig', 'windows-1251']:
            try:
                with open(_path, 'r', encoding=code_page) as file:
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
        with open(_path, 'r', encoding='utf-8') as file:
            return json.load(file)
    except FileNotFoundError as err:
        raise err
    except Exception as err:
        raise ExtException(message='Ошибка чтения', detail=f'{err} в файле ({_path})')


def json_write(data, path, file_name):
    _path = os.path.normpath(os.path.join(path, file_name))
    makedirs(path, exist_ok=True)
    try:
        with open(_path, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except Exception as err:
        raise ExtException(message='Ошибка записи', detail=f'{err} в файле ({_path})')


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
    with open(_path, 'r', encoding=encoding) as file:
        return file.read(), encoding


def txt_write(data, path, file_name, encoding='utf-8'):
    try:
        if data is None:
            return
        _path = os.path.normpath(os.path.join(path, file_name))
        makedirs(path, exist_ok=True)
        for i in range(3):
            try:
                with open(_path, 'w', encoding=encoding) as file:
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
    with open(_path, 'wb') as file:
        file.write(data)


def bin_read(path, file_name):
    _path = os.path.normpath(os.path.join(path, file_name))
    progress.note_read(_path)
    with open(_path, 'rb') as file:
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

    with open(path, 'rb') as f:
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
            os.makedirs(name, exist_ok=exist_ok)
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
