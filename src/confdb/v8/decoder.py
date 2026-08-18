"""Диспетчер типов/версий и рекурсивный разбор метаданных.

Порт decode-части v8unpack.decoder (MIT) — только разбор, без сборки.
"""
import os
import shutil
from datetime import datetime

from . import helper
from . import progress
from .meta_object import Configuration, ConfigurationExtension, ExternalDataProcessor
from .ext_exception import ExtException
from .metadata_types import MetaDataTypes

available_types = {
    'ExternalDataProcessor': ExternalDataProcessor,
    'ExternalReport': ExternalDataProcessor,  # .erf has the same binary format as .epf
    'Configuration': Configuration,
    'ConfigurationExtension': ConfigurationExtension
}


class Decoder:
    @staticmethod
    def get_handler_by_version_file(*, version=None, root=None, header=None, options=None, configinfo=None, **kwargs):
        if root:
            if int(version[0][0][0]) >= 216:
                _tmp = len(version[0][0])
                obj_type = MetaDataTypes(header[0][3][0])
                if _tmp == 2:
                    obj_version = '802'
                elif _tmp == 3:
                    obj_version = version[0][0][2][0][:3]
                    if len(obj_version) == 3:
                        pass
                    elif obj_version in ['1', '2']:
                        obj_version = '802'
                    else:
                        raise Exception(f'Not supported version {obj_version}')
                else:
                    raise Exception(f'Not supported version {_tmp}')
                try:
                    # if not options.get('version'):
                    options['obj_version'] = obj_version
                    return available_types[obj_type.name](options=options, obj_version=options['obj_version'])
                except KeyError:
                    raise Exception(f'Not supported type {obj_type.name}')
            elif version[0][0][0] == "106":
                options['obj_version'] = '801'
                return ExternalDataProcessor(options=options, obj_version=options['obj_version'])
        if configinfo:
            if int(configinfo[0][1][0]) >= 216:
                if len(configinfo[0][1]) == 3:
                    obj_version = configinfo[0][1][2][0]
                    if obj_version.startswith('803'):
                        options['obj_version'] = obj_version
                        return ConfigurationExtension(options=options, obj_version=options['obj_version'])
        raise Exception('Не удалось определить парсер')

    @classmethod
    def detect_version(cls, src_dir, options=None):
        version = None
        root = None
        header = None
        configinfo = None
        if os.path.isfile(os.path.join(src_dir, 'root')):
            version = helper.brace_file_read(src_dir, 'version')
            root = helper.brace_file_read(src_dir, 'root')
            header = helper.brace_file_read(src_dir, root[0][1])
        if os.path.isfile(os.path.join(src_dir, 'configinfo')):
            configinfo = helper.brace_file_read(src_dir, 'configinfo')

        return cls.get_handler_by_version_file(
            version=version,
            root=root,
            header=header,
            options=options,
            configinfo=configinfo
        )

    @classmethod
    def decode(cls, src_dir, dest_dir, *, pool=None, options=None, workers=1):
        begin = datetime.now()
        print(f'{"Разбираем объект":30}')
        total = 0
        for dirpath, _, filenames in os.walk(src_dir):
            for fn in filenames:
                total += os.path.getsize(os.path.join(dirpath, fn))

        own_pool = False
        if workers and workers > 1 and pool is None:
            import multiprocessing
            ctx = multiprocessing.get_context('spawn')
            shared = ctx.Value('Q', 0)
            progress.start('Декодируем метаданные', total, shared=shared)
            pool = ctx.Pool(workers, initializer=progress.attach_shared, initargs=(shared,))
            own_pool = True
        else:
            progress.start('Декодируем метаданные', total)
        try:
            decoder = cls.detect_version(src_dir, options=options)
            helper.clear_dir(dest_dir)
            tasks = decoder.decode(src_dir, dest_dir)  # возвращает список вложенных объектов MetaDataObject
            while tasks:  # рекурсивно декодируем вложенные объекты MetaDataObject
                tasks = helper.run_in_pool(cls.decode_include, tasks, pool, title=f'{"Разбираем вложенные объекты":30}',
                                           need_result=True)
        finally:
            if own_pool:
                pool.close()
                pool.join()
            progress.finish()
        print(f'{"Разбор объекта закончен":30}: {datetime.now() - begin}')

    @classmethod
    def decode_include(cls, params):
        include_type, (obj_uuid, src_dir, dest_dir, new_dest_path, parent_container_uuid, options) = params
        try:
            handler = helper.get_class_metadata_object(include_type)
            tasks = handler.decode(obj_uuid, src_dir, dest_dir, new_dest_path, options,
                                   parent_container_uuid=parent_container_uuid,
                                   parent_type=include_type)
            return tasks
        except ExtException as err:
            raise ExtException(
                parent=err,
                action=f'{cls.__name__}.decode_include {include_type}'
            )
        except Exception as err:
            raise ExtException(parent=err, action=f'{cls.__name__}.decode_include')


def decode(src_dir, dest_dir, *, pool=None, options=None, workers=1):
    containers = sorted(os.listdir(src_dir), key=lambda x: int(x) if x.isdigit() else x)
    containers_count = len(containers)
    if containers_count not in [1, 2]:
        raise NotImplementedError(f'Количество контейнеров {containers_count}')

    _src_dir = os.path.join(src_dir, containers[-1])
    Decoder.decode(_src_dir, dest_dir, pool=pool, options=options, workers=workers)
    if containers_count == 2:
        shutil.make_archive(os.path.join(dest_dir, 'dummy'), 'zip', os.path.join(src_dir, '0'))
