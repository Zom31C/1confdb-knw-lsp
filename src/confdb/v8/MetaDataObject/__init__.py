"""Базовый класс объектов метаданных (справочники, документы, общие модули и т.д.).

Порт decode-части v8unpack.MetaDataObject (MIT) — только разбор.
"""
import os

from .. import helper
from ..meta_object import MetaObject
from ..ext_exception import ExtException


class MetaDataObject(MetaObject):
    versions = None
    help_file_number = None

    def __init__(self, *, meta_obj_class=None, obj_version=None, options=None):
        super().__init__(meta_obj_class=meta_obj_class, obj_version=obj_version, options=options)
        self.path = ''
        self.new_dest_path = None
        self.new_dest_dir = None
        self.new_dest_file_name = None
        self.parent_id = None

    def get_obj_name(self):
        return self.meta_obj_class.__name__ if self.meta_obj_class else self.__class__.__name__

    @classmethod
    def get_version(cls, header_data, option):
        if cls.versions is None:
            return cls
        return header_data[0][1][0]

    @staticmethod
    def brace_file_read(src_dir, file_name):
        return helper.brace_file_read(src_dir, file_name)

    @classmethod
    def get_handler(cls, header_data, options):
        try:
            if cls.versions is None:
                return cls(options=options, obj_version=options['obj_version'])
            obj_version = cls.get_version(header_data, options)
            try:
                return cls.versions[obj_version](options=options, meta_obj_class=cls, obj_version=obj_version)
            except KeyError:
                raise Exception(f'Нет реализации {cls.__name__} для версии "{obj_version}"')
        except Exception as err:
            raise ExtException(message='Не смогли получить класс объекта', detail=f'{cls.__name__} {err}', action='get_handler')

    @classmethod
    def decode(cls, src_dir: str, file_name: str, dest_dir: str, dest_path: str, options, *, parent_type=None,
               parent_container_uuid=None):
        try:
            header_data = cls.brace_file_read(src_dir, file_name)
            self = cls.get_handler(header_data, options)
            self.parent_container_uuid = parent_container_uuid
            self.decode_header(header_data)
            self.decode_object(src_dir, file_name, dest_dir, dest_path, self.obj_version, header_data)
            tasks = self.decode_includes(src_dir, dest_dir, self.new_dest_path, header_data)
            self.write_decode_object(dest_dir, self.new_dest_path, self.new_dest_file_name)
            return tasks
        except Exception as err:
            problem_file = os.path.join(os.path.basename(src_dir), file_name)
            raise ExtException(
                parent=err,
                message="Ошибка декодирования",
                detail=f'"{cls.__name__}" файл "{problem_file}" ({dest_path})',
                action=f'{cls.__name__}.decode'
            )

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        self.set_write_decode_mode(dest_dir, dest_path)

    def decode_ids(self):
        return {
            'uuid': self.header.pop('uuid'),
            # 'name': self.header.pop('name')
        }

    def write_decode_object(self, dest_dir, dest_path, file_name):
        try:
            dest_full_path = os.path.join(dest_dir, dest_path)

            id_data = self.decode_ids()
            self.header['obj_version'] = self.obj_version
            helper.json_write(id_data, dest_full_path, f'{file_name}.id.json')
            helper.json_write(self.header, dest_full_path, f'{file_name}.json')
            self.write_decode_code(dest_full_path, file_name)
        except Exception as err:
            raise ExtException(parent=err)

    def get_internal_data(self):
        return self.uuid

    def set_write_decode_mode(self, dest_dir, dest_path):
        self.new_dest_path = os.path.join(dest_path, self.header['name'])
        self.new_dest_dir = os.path.join(dest_dir, self.new_dest_path)
        self.new_dest_file_name = self.get_obj_name()
        helper.makedirs(self.new_dest_dir)
