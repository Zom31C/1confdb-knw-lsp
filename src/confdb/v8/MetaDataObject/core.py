"""Базовые классы объектов метаданных (слитый порт v8unpack.MetaDataObject.core, MIT)."""
from . import MetaDataObject
from .. import helper
from ..ext_exception import ExtException
import os
import shutil


# --- MetaDataObject/core/Simple.py ---
class Simple(MetaDataObject):

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        super().decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)
        if self.help_file_number is not None:
            self._decode_html_data(src_dir, self.new_dest_dir, self.new_dest_file_name, header_field='help',
                                   file_number=self.help_file_number)
        self.decode_code(src_dir)

    def decode_includes(self, src_dir, dest_dir, dest_path, header):
        return []


class SimpleNameFolder(Simple):
    pass


# --- MetaDataObject/core/Container.py ---
class Container(MetaDataObject):
    predefined_file_number = None

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][1]

    @classmethod
    def get_decode_includes(cls, header_data):
        try:
            return [header_data[0]]
        except IndexError:
            raise ExtException(message='Include types not found', detail=cls.__name__)

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        super().decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)
        if self.help_file_number is not None:
            self._decode_html_data(src_dir, self.new_dest_dir, self.new_dest_file_name, header_field='help',
                                   file_number=self.help_file_number)
        if self.predefined_file_number is not None:
            self._decode_predefined(src_dir, self.new_dest_dir)
        self.decode_code(src_dir)

    def _decode_predefined(self, src_dir, dest_dir):
        try:
            data = helper.bin_read(src_dir, f'{self.header["uuid"]}.{self.predefined_file_number}')
            helper.bin_write(data, dest_dir, 'Предустановленные данные.bin')
        except FileNotFoundError:
            return


class FormContainer(Container):
    @classmethod
    def get_container_uuid(cls, header_data):
        return header_data[0][1][1]


# --- MetaDataObject/core/IncludeEmpty.py ---
class IncludeEmpty(MetaDataObject):

    @classmethod
    def decode(cls, src_dir: str, file_name: str, dest_dir: str, dest_path: str, options, *, parent_type=None,
               parent_container_uuid=None):
        raise Exception('Так быть не должно, этот класс обслуживает вложенные объекты')

    @classmethod
    def decode_internal_include(cls, parent, header_data, src_dir, dest_dir, dest_path, version):
        return


# --- MetaDataObject/core/IncludeSimple.py ---
class IncludeSimple(MetaDataObject):
    ext_code = {'obj': 2}

    def __init__(self, *, meta_obj_class=None, obj_version=None, options=None):
        super().__init__(meta_obj_class=meta_obj_class, obj_version=obj_version, options=options)
        self.new_dest_path = None
        self.new_dest_dir = None

    @classmethod
    def decode(cls, src_dir: str, file_name: str, dest_dir: str, dest_path: str, options, *, parent_type=None,
               parent_container_uuid=None):
        raise Exception('Так быть не должно, этот класс обслуживает вложенные объекты')

    @classmethod
    def decode_internal_include(cls, parent, header_data, src_dir, dest_dir, dest_path, options):
        try:
            self = cls(options=options)
            self.decode_header(header_data)
            self.set_write_decode_mode(dest_dir, dest_path)
            self.decode_code(src_dir)
            self.write_decode_object(dest_dir, self.new_dest_path, self.new_dest_file_name)
            return self.uuid
        except Exception as err:
            raise ExtException(
                parent=err,
                action=f'{cls.__name__}.decode_internal_include'
            )

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][3][2][9]

    def get_internal_data(self):
        return self.header['header']


# --- MetaDataObject/core/SimpleWithInfo.py ---
class SimpleWithInfo(Simple):
    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        super(Simple, self).decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)
        try:
            _src = os.path.join(src_dir, f'{self.header["uuid"]}.0')
            _dest = os.path.join(dest_dir, self.new_dest_path, f'{self.new_dest_file_name}.0.c1brace')
            shutil.copy2(_src, _dest)
        except FileNotFoundError:
            return
